# NTE Piano - 自動演奏與自動化工具
# Copyright (C) 2026  Yulun
# Licensed under GPL-3.0-or-later. See LICENSE.
"""nte_piano_roll — 主編輯區的橫向卷簾 widget.

對外提供:
    PianoRollView  時間從左流向右,遊標固定在 LOOK_BEHIND 比例位置;支援拖動編輯、
                   滾輪縮放、區間 loop、ghost 預覽、tempo 變速標記等。
                   1800 行的大 widget,功能涵蓋整個編輯體驗。

依賴:
    PySide6.QtCore/QtGui/QtWidgets
    nte_dsl  (TRACK_INDEX / TRACK_ORDER)
    nte_theme (THEME + _blend_color + _ease_out_quad)

sheet / events 由 caller 透過 set_sheet() 等 method 餵入, 用 duck typing 取 attribute.
"""
from __future__ import annotations

import time

from PySide6.QtCore import (
    QEasingCurve,
    QPointF,
    QRectF,
    Qt,
    QTimer,
    QVariantAnimation,
    Signal,
    Slot,
)
from PySide6.QtGui import (
    QColor,
    QContextMenuEvent,
    QFont,
    QFontMetrics,
    QGradient,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import QInputDialog, QMenu, QSizePolicy, QWidget

from nte_dsl import TRACK_INDEX, TRACK_ORDER
from nte_perf import perf
from nte_theme import THEME, _blend_color, _ease_out_quad


# Piano roll 縮放係數範圍。給滾輪縮放 / set_zoom_factor / smooth zoom 動畫用,
# 0.4 是「全曲剛好塞滿視野」的最小;3.0 是「單拍佔滿視野」的最大。
ZOOM_MIN = 0.4
ZOOM_MAX = 3.0


class PianoRollView(QWidget):
    """橫向卷簾:時間從左流向右,遊標固定在 LOOK_BEHIND/(LOOK_BEHIND+LOOK_AHEAD) 比例位置。

    停止時可用滾輪左右瀏覽;Ctrl+滾輪縮放可視範圍;
    左鍵拖動改音符的時間(橫向)/音高(縱向,僅單音事件);
    雙擊音符會發出信號讓主視窗開啟調色盤自訂該音符顏色。
    """

    BASE_LOOK_BEHIND_SECONDS = 1.5
    BASE_LOOK_AHEAD_SECONDS = 4.5
    SCROLL_STEP_SECONDS = 0.4
    SNAP_DIVISOR = 4
    RESIZE_HANDLE_PIXELS = 7
    PULSE_SECONDS = 0.18
    PAST_FADE_SECONDS = 0.55

    # pitch sort 模式的雙向 mapping:把各 prefix(H/M/L)段內的 12 個半音反轉,
    # 跨段順序(H 在頂、L 在底)不變。公式:f(idx) = prefix_idx*12 + (11 - within),
    # 為 involution (f(f(x)) == x),所以 logical→visual 與 visual→logical 用同一張表。
    # 結果:visual 0..35 對應 MIDI 95..60 嚴格遞減(最頂 H7=95、最底 L1=60)。
    _PITCH_SORT_VISUAL: tuple[int, ...] = tuple(
        (i // 12) * 12 + (11 - (i % 12)) for i in range(36)
    )
    _PITCH_SORT_LOGICAL: tuple[int, ...] = _PITCH_SORT_VISUAL

    event_moved = Signal(int, float, float, int, int)  # idx, start, dur, new_track_idx_or_-1, track_offset
    # 批次版本:一次拖完(可能多選),payload = list of (idx, start, dur, new_track_idx, track_offset)。
    # 主視窗端在這個 handler 內只 push 一次 undo snapshot,讓「多選移動 → Ctrl+Z 一次回到原樣」。
    # event_moved 保留作為單一事件 API(目前內部 emit 已改走 events_moved)。
    events_moved = Signal(object)
    event_double_clicked = Signal(int)
    event_delete_requested = Signal(int)
    events_delete_requested = Signal(object)
    note_insert_requested = Signal(float, str)  # (start_beats, stroke_label)
    copy_requested = Signal()
    cut_requested = Signal()
    paste_requested = Signal()
    timeline_changed = Signal(float)
    seek_requested = Signal(float)
    zoom_changed = Signal(float)
    tempo_change_set = Signal(float, float)  # (beats, new_bpm)
    tempo_change_remove = Signal(float)  # (beats)
    # 拖出和弦中某音為獨立事件:(orig_idx, label, new_start_beats, new_dur_beats, new_track_idx)
    chord_stroke_extracted = Signal(int, str, float, float, int)
    # 主視覺上拖動播放區間 markers 後 emit;與 OverviewBar.loop_range_changed 同構,
    # 主視窗端兩條路徑接到同一個 _on_loop_range_changed,雙向同步狀態。
    loop_range_changed = Signal(object, object)

    LOOP_MARKER_HIT_RADIUS = 6  # 拖 marker 用的容忍半徑(像素)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._sheet = None
        self._playing = False
        self._started_at = 0.0
        self._paused_at = None
        self._active_labels = set()
        self._label_pulse_starts: dict[str, float] = {}
        self._animations_enabled = True
        self._browse_offset = 0.0
        # 平滑滾輪捲動:wheelEvent 啟用時把 _browse_offset 用動畫推到目標值,
        # 而不是離散跳一段 SCROLL_STEP_SECONDS。連續滾時把 delta 累加在
        # _smooth_browse_target 上,動畫重啟從目前值出發,看起來會連續變速。
        # 預設啟用;主視窗依「動畫效果」總開關(animations_enabled)連帶切換。
        self._smooth_browse_enabled: bool = True
        self._smooth_browse_target: float = 0.0
        self._smooth_browse_anim = QVariantAnimation(self)
        self._smooth_browse_anim.setDuration(220)
        self._smooth_browse_anim.setEasingCurve(QEasingCurve.OutCubic)
        self._smooth_browse_anim.valueChanged.connect(self._on_smooth_browse_tick)
        # Ctrl+滾輪縮放的平滑過渡。140ms 比 browse 的 220ms 短,因為縮放是精確操作,
        # 動畫太長會卡住下一次滾輪輸入。動畫過程不 emit zoom_changed,只在 finished
        # 落盤一次,避免 settings 被狂寫。
        self._smooth_zoom_enabled: bool = True
        self._zoom_target: float = 1.0
        self._zoom_anim = QVariantAnimation(self)
        self._zoom_anim.setDuration(140)
        self._zoom_anim.setEasingCurve(QEasingCurve.OutCubic)
        self._zoom_anim.valueChanged.connect(self._on_zoom_anim_tick)
        self._zoom_anim.finished.connect(self._on_zoom_anim_finished)
        self._event_colors: dict = {}
        # 事件層級的選取 (給刪除/複製貼上等操作使用)
        self._selected_indices: set[int] = set()
        # 視覺層級的精細選取 (idx, stroke_label) — paint 只亮這集合裡的 stroke
        # _selected_indices 永遠 = {idx for (idx,_) in _selected_strokes}
        self._selected_strokes: set[tuple[int, str]] = set()
        self._drag_active = False
        self._drag_event_index = None
        self._drag_start_pos = None
        self._drag_states: dict[int, dict] = {}
        self._drag_can_move_pitch = False
        self._drag_mode = "move"
        self._drag_button = Qt.NoButton
        # 點下時是否點到「已選中組的成員」(用來判斷沒移動就 collapse 成單選)
        self._press_was_in_group = False
        self._press_clicked_label: str | None = None
        # 拖出和弦音的暫存:None 表示不在拆出流程中
        self._drag_split: dict | None = None
        self._marquee_active = False
        self._marquee_start_pos = None
        self._marquee_current_pos = None
        # 框選的起點以「秒」為單位釘住譜面,讓滾輪平移後仍能延續選擇
        self._marquee_start_seconds: float | None = None
        self._marquee_start_y: float | None = None
        self._zoom_factor = 1.0
        self._playback_speed = 1.0
        self._pitch_sort_mode = False
        self._last_hover_seconds: float | None = None
        # 右鍵空白/休止符時 mousePress 設 True,mouseRelease 直接彈選單。
        # 不靠平台的 contextMenuEvent(實測在本機環境不一定觸發),改用一定會
        # 送達的 mouseRelease 當選單入口;contextMenuEvent 只留給鍵盤選單鍵。
        self._pending_context_menu = False
        # 播放區間 markers — 與 OverviewBar 同步,顯示在 piano roll 上層 + ruler 標籤。
        # 左鍵點擊 marker handle 可拖動,拖完 emit loop_range_changed。
        self._loop_start_seconds: float | None = None
        self._loop_end_seconds: float | None = None
        self._loop_drag_target: str | None = None  # "start" / "end" 或 None
        self._loop_drag_active: bool = False
        # 預設 60fps;主視窗載入 settings 後會呼叫 set_fps() 套用實際值。
        self._fps = 60
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.PreciseTimer)
        self._timer.setInterval(max(1, round(1000 / self._fps)))
        self._timer.timeout.connect(self._on_tick)
        # paintEvent 內常用的字型/筆刷預先建立避免每幀重建。
        self._track_label_font = QFont("Cascadia Mono", 8)
        self._bar_number_font = QFont("Cascadia Mono", 7)
        # note 上緣玻璃高光漸層(固定色)。ObjectBoundingMode 讓同一物件套到每個
        # note rect,免去每音符每幀重新配置 QLinearGradient(捲動時的高光成本)。
        self._note_highlight_grad = QLinearGradient(0.0, 0.0, 0.0, 1.0)
        self._note_highlight_grad.setCoordinateMode(QGradient.ObjectBoundingMode)
        self._note_highlight_grad.setColorAt(0.0, QColor(255, 255, 255, 80))
        self._note_highlight_grad.setColorAt(1.0, QColor(255, 255, 255, 0))
        # 靜態層快取:背景漸層 + 列底色 + 軌道分隔線 + 軌道標籤 + ruler 底色帶。
        # 這些只依(視窗尺寸 / pitch_sort / 主題色),與時間軸捲動/縮放無關,
        # 每幀直接 blit 省下 36 軌 tint/標籤文字重畫 + 漸層重配置。key 變才重建。
        self._static_pixmap: QPixmap | None = None
        self._static_key: tuple | None = None
        self.setMinimumHeight(320)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.ClickFocus)

    def set_fps(self, fps: int) -> None:
        fps = max(15, min(240, int(fps)))
        if fps == self._fps:
            return
        self._fps = fps
        self._timer.setInterval(max(1, round(1000 / fps)))

    @property
    def look_behind_seconds(self) -> float:
        return self.BASE_LOOK_BEHIND_SECONDS / max(0.01, self._zoom_factor)

    @property
    def look_ahead_seconds(self) -> float:
        return self.BASE_LOOK_AHEAD_SECONDS / max(0.01, self._zoom_factor)

    def set_zoom_factor(self, factor: float) -> None:
        new_factor = max(ZOOM_MIN, min(ZOOM_MAX, float(factor)))
        if abs(new_factor - self._zoom_factor) < 1e-4:
            return
        self._zoom_factor = new_factor
        self.update()
        self.zoom_changed.emit(new_factor)

    def zoom_factor(self) -> float:
        return self._zoom_factor

    @Slot(float)
    def set_playback_speed(self, speed: float) -> None:
        new_speed = max(0.1, float(speed))
        if abs(new_speed - self._playback_speed) < 1e-6:
            return
        if self._playing:
            ref = self._paused_at if self._paused_at is not None else time.perf_counter()
            current_pos = max(0.0, (ref - self._started_at) * self._playback_speed)
            self._started_at = ref - current_pos / new_speed
        self._playback_speed = new_speed
        self.update()

    @Slot(object)
    def set_sheet(self, sheet) -> None:
        self._sheet = sheet
        self._event_colors = {}
        self._selected_indices = set()
        self._selected_strokes = set()
        self._drag_active = False
        self._drag_event_index = None
        self._drag_states = {}
        self._marquee_active = False
        self._marquee_start_pos = None
        self._marquee_current_pos = None
        self._marquee_start_seconds = None
        self._marquee_start_y = None
        self._browse_offset = 0.0
        self.update()

    @property
    def _selected_index(self):
        return next(iter(self._selected_indices)) if self._selected_indices else None

    def clear_selection(self) -> None:
        self._selected_indices = set()
        self._selected_strokes = set()
        self.update()

    def _set_selected_strokes(self, strokes) -> None:
        """同步設定精細與事件層級選取。strokes = iterable of (idx, label)。"""
        self._selected_strokes = {(int(i), str(l)) for i, l in strokes}
        self._selected_indices = {i for i, _ in self._selected_strokes}

    def _add_selected_stroke(self, idx: int, label: str) -> None:
        self._selected_strokes.add((int(idx), str(label)))
        self._selected_indices.add(int(idx))

    def _remove_selected_stroke(self, idx: int, label: str) -> None:
        self._selected_strokes.discard((int(idx), str(label)))
        if not any(i == idx for i, _ in self._selected_strokes):
            self._selected_indices.discard(int(idx))

    def _all_strokes_of(self, idx: int):
        if not (0 <= idx < len(self._sheet.events)):
            return ()
        ev = self._sheet.events[idx]
        return tuple(s.label for s in ev.strokes)

    @Slot(object)
    def set_active_labels(self, labels) -> None:
        new_set = set(labels) if labels else set()
        if self._animations_enabled and new_set != self._active_labels:
            now = time.perf_counter()
            for lbl in new_set - self._active_labels:
                self._label_pulse_starts[lbl] = now
        self._active_labels = new_set
        self.update()

    @Slot(object)
    def apply_active_delta(self, delta) -> None:
        """高頻路徑:接 worker.active_delta(adds, removes),只動差集 + update。"""
        adds, removes = delta
        if not adds and not removes:
            return
        if perf.enabled:
            perf.log("gui", "roll_delta", adds=len(adds), removes=len(removes))
        if self._animations_enabled and adds:
            now = time.perf_counter()
            for lbl in adds:
                self._label_pulse_starts[lbl] = now
        self._active_labels.update(adds)
        self._active_labels.difference_update(removes)
        self.update()

    def set_animations_enabled(self, enabled: bool) -> None:
        self._animations_enabled = bool(enabled)
        if not enabled:
            self._label_pulse_starts.clear()
        self.update()

    def _pulse_strength(self, label: str, now: float) -> float:
        if not self._animations_enabled:
            return 0.0
        start = self._label_pulse_starts.get(label)
        if start is None:
            return 0.0
        t = now - start
        if t > self.PULSE_SECONDS:
            return 0.0
        return 1.0 - t / self.PULSE_SECONDS

    def _visual_index(self, logical_idx: int) -> int:
        """logical_idx → visual y 軸 index(0=最頂、35=最底)。

        預設模式:直接回傳 logical(H 在頂、各段內按簡譜 1..7)。
        pitch sort 模式:透過 _PITCH_SORT_VISUAL 查表,各段內 12 半音反轉,
        讓 H7(MIDI 95) 在最頂、L1(MIDI 60) 在最底。
        """
        if self._pitch_sort_mode:
            return self._PITCH_SORT_VISUAL[logical_idx]
        return logical_idx

    def _logical_for_visual(self, visual_idx: int) -> int:
        """visual y index → logical_idx 反向查表。pitch sort 公式為 involution,
        所以反查與正查用同一張表。"""
        if self._pitch_sort_mode:
            return self._PITCH_SORT_LOGICAL[visual_idx]
        return visual_idx

    def set_pitch_sort_mode(self, enabled: bool) -> None:
        """切換是否按 MIDI 嚴格遞減排列軌道。"""
        new_mode = bool(enabled)
        if new_mode == self._pitch_sort_mode:
            return
        self._pitch_sort_mode = new_mode
        self.update()

    def is_pitch_sort_mode(self) -> bool:
        return self._pitch_sort_mode

    @Slot(float)
    def start_playing(self, started_at: float = 0.0) -> None:
        self._playing = True
        self._started_at = float(started_at) if started_at else time.perf_counter()
        self._paused_at = None
        self._browse_offset = 0.0
        self._drag_active = False
        self._drag_states = {}
        if not self._timer.isActive():
            self._timer.start()
        self.update()

    @Slot()
    def pause_playing(self) -> None:
        if not self._playing or self._paused_at is not None:
            return
        self._paused_at = time.perf_counter()
        self._timer.stop()
        self.update()

    @Slot()
    def resume_playing(self) -> None:
        if self._paused_at is None:
            return
        self._started_at += time.perf_counter() - self._paused_at
        self._paused_at = None
        if not self._timer.isActive():
            self._timer.start()
        self.update()

    @Slot(float)
    def seek_to(self, position_seconds: float) -> None:
        position_seconds = max(0.0, float(position_seconds))
        if self._playing:
            ref = self._paused_at if self._paused_at is not None else time.perf_counter()
            self._started_at = ref - position_seconds / max(0.1, self._playback_speed)
        else:
            self._browse_offset = position_seconds
        self.update()

    @Slot()
    def stop_playing(self, position_seconds: float | None = None) -> None:
        # 停止時把瀏覽位置定在 worker 真正停下來的時間點(由外部傳入),
        # 而不是直接歸零。這樣下一次按播放才會從目前位置接續。
        if position_seconds is None:
            position_seconds = self.current_seconds()
        self._playing = False
        self._timer.stop()
        self._active_labels = set()
        self._browse_offset = max(0.0, float(position_seconds))
        self._paused_at = None
        self.update()

    def set_event_color(self, index: int, color) -> None:
        if self._sheet is None:
            return
        if not (0 <= index < len(self._sheet.events)):
            return
        if color is None:
            self._event_colors.pop(index, None)
        else:
            self._event_colors[index] = QColor(color)
        self.update()

    def selected_event_index(self):
        return self._selected_index

    def _on_tick(self) -> None:
        self.update()
        self.timeline_changed.emit(self.current_seconds())

    def set_browse_offset(self, seconds: float) -> None:
        self._browse_offset = max(0.0, float(seconds))
        self.update()

    def set_smooth_browse_enabled(self, enabled: bool) -> None:
        """切換滾輪平滑捲動。關閉時把進行中的動畫立即停掉,後續 wheel 走離散。"""
        new_state = bool(enabled)
        if new_state == self._smooth_browse_enabled:
            return
        self._smooth_browse_enabled = new_state
        if not new_state:
            self._smooth_browse_anim.stop()

    def _on_smooth_browse_tick(self, value) -> None:
        """平滑捲動動畫每幀。

        未播放:把 float 推回 _browse_offset 並重繪(維持原本不 emit timeline_changed
        的同步範圍)。播放中:把動畫值當 seek 目標 emit,讓主視窗調 worker 時鐘,
        畫面隨之平滑 scrub(否則播放中滾輪只會離散跳)。
        """
        try:
            v = max(0.0, float(value))
        except (TypeError, ValueError):
            return
        if self._playing:
            self.seek_requested.emit(v)
        else:
            self._browse_offset = v
            self.update()

    def set_smooth_zoom_enabled(self, enabled: bool) -> None:
        """切換 Ctrl+滾輪縮放平滑過渡。關閉時若動畫正在跑,先收斂到當前終點再停。"""
        new_state = bool(enabled)
        if new_state == self._smooth_zoom_enabled:
            return
        self._smooth_zoom_enabled = new_state
        if not new_state and self._zoom_anim.state() == QVariantAnimation.Running:
            # 停動畫前先把 zoom_factor 落到當前 target 並通知設定,避免狀態半吊。
            self._zoom_anim.stop()
            self._zoom_factor = max(ZOOM_MIN, min(ZOOM_MAX, float(self._zoom_target)))
            self.update()
            self.zoom_changed.emit(self._zoom_factor)

    def _on_zoom_anim_tick(self, value) -> None:
        """動畫每幀:推 _zoom_factor 並重繪;刻意不 emit zoom_changed,避免狂寫設定。"""
        try:
            self._zoom_factor = max(ZOOM_MIN, min(ZOOM_MAX, float(value)))
        except (TypeError, ValueError):
            return
        self.update()

    def _on_zoom_anim_finished(self) -> None:
        """動畫結束:emit 一次 zoom_changed,讓主視窗把終點值落盤到 settings。"""
        self.zoom_changed.emit(self._zoom_factor)

    def current_seconds(self) -> float:
        if self._playing:
            ref = self._paused_at if self._paused_at is not None else time.perf_counter()
            return max(0.0, (ref - self._started_at) * self._playback_speed)
        return max(0.0, self._browse_offset)

    def _view_metrics(self):
        margin_left = 56
        margin_right = 16
        margin_top = 26
        margin_bottom = 10
        usable_w = max(0, self.width() - margin_left - margin_right)
        usable_h = max(0, self.height() - margin_top - margin_bottom)
        if usable_w <= 0 or usable_h <= 0:
            return None
        current = self.current_seconds()
        view_start = current - self.look_behind_seconds
        view_end = current + self.look_ahead_seconds
        view_span = self.look_behind_seconds + self.look_ahead_seconds
        return {
            "margin_left": margin_left,
            "margin_top": margin_top,
            "usable_w": usable_w,
            "usable_h": usable_h,
            "current": current,
            "view_start": view_start,
            "view_end": view_end,
            "view_span": view_span,
            "track_h": usable_h / len(TRACK_ORDER),
        }

    def set_loop_range(self, start_seconds: float | None, end_seconds: float | None) -> None:
        """外部 setter:更新 loop range 但不 emit signal(避免 echo)。"""
        self._loop_start_seconds = (
            max(0.0, float(start_seconds)) if start_seconds is not None else None
        )
        self._loop_end_seconds = (
            max(0.0, float(end_seconds)) if end_seconds is not None else None
        )
        if (
            self._loop_start_seconds is not None
            and self._loop_end_seconds is not None
            and self._loop_end_seconds < self._loop_start_seconds
        ):
            self._loop_start_seconds, self._loop_end_seconds = (
                self._loop_end_seconds,
                self._loop_start_seconds,
            )
        self.update()

    def loop_range(self) -> tuple[float | None, float | None]:
        return (self._loop_start_seconds, self._loop_end_seconds)

    def _loop_marker_x(self, which: str) -> float | None:
        """回傳 marker 在 widget 座標的 x;不可視範圍或 marker 未設則回 None。"""
        metrics = self._view_metrics()
        if metrics is None or metrics["view_span"] <= 0:
            return None
        t = self._loop_start_seconds if which == "start" else self._loop_end_seconds
        if t is None:
            return None
        if t < metrics["view_start"] - 1e-6 or t > metrics["view_end"] + 1e-6:
            return None
        return (
            metrics["margin_left"]
            + (t - metrics["view_start"]) / metrics["view_span"] * metrics["usable_w"]
        )

    def _is_near_loop_marker(self, x: float, which: str) -> bool:
        mx = self._loop_marker_x(which)
        if mx is None:
            return False
        return abs(x - mx) <= self.LOOP_MARKER_HIT_RADIUS

    def _hit_event(self, x: float, y: float):
        """回傳被點到的 (event_index, stroke_label) tuple,沒命中則 None。
        一個和弦事件包多個 stroke,各佔不同 row,精確回傳被點的那一個。
        """
        if self._sheet is None:
            return None
        metrics = self._view_metrics()
        if metrics is None:
            return None
        margin_left = metrics["margin_left"]
        margin_top = metrics["margin_top"]
        usable_w = metrics["usable_w"]
        view_start = metrics["view_start"]
        view_span = metrics["view_span"]
        track_h = metrics["track_h"]

        def t_to_x(t: float) -> float:
            return margin_left + (t - view_start) / view_span * usable_w

        # 反向遍歷讓「最上層」(後繪製) 的事件優先命中,避免重疊時永遠選到下層
        for i in range(len(self._sheet.events) - 1, -1, -1):
            ev = self._sheet.events[i]
            if ev.is_rest:
                continue
            start_s = self._sheet.beats_to_seconds(ev.start_beats)
            end_s = self._sheet.beats_to_seconds(ev.start_beats + ev.duration_beats)
            if end_s < view_start or start_s > metrics["view_end"]:
                continue
            x1 = t_to_x(start_s)
            x2 = t_to_x(end_s)
            if x2 < margin_left or x1 > margin_left + usable_w:
                continue
            for stroke in ev.strokes:
                track_idx = TRACK_INDEX.get(stroke.label)
                if track_idx is None:
                    continue
                ty = margin_top + self._visual_index(track_idx) * track_h
                if x1 - 3 <= x <= x2 + 3 and ty <= y <= ty + track_h:
                    return (i, stroke.label)
        return None

    def wheelEvent(self, event) -> None:  # noqa: N802 - Qt override.
        if self._sheet is None:
            return
        delta = event.angleDelta().y()
        if delta == 0:
            return
        notches = delta / 120.0
        modifiers = event.modifiers()
        if modifiers & Qt.ControlModifier:
            factor = (1.1 if notches > 0 else 1.0 / 1.1) ** abs(notches)
            if self._smooth_zoom_enabled:
                # 連續滾時把目標累加在「上一次動畫終點」上,動畫從目前值重啟到新目標,
                # 連續滾就會像連續變焦;停滾 140ms 後自然停下。動畫過程不 emit
                # zoom_changed,終點才 emit 一次落盤,避免 settings 被狂寫。
                if self._zoom_anim.state() == QVariantAnimation.Running:
                    base = self._zoom_target
                else:
                    base = self._zoom_factor
                target = max(ZOOM_MIN, min(ZOOM_MAX, base * factor))
                if abs(target - self._zoom_factor) < 1e-4:
                    event.accept()
                    return
                self._zoom_target = target
                self._zoom_anim.stop()
                self._zoom_anim.setStartValue(float(self._zoom_factor))
                self._zoom_anim.setEndValue(float(target))
                self._zoom_anim.start()
            else:
                self.set_zoom_factor(self._zoom_factor * factor)
            event.accept()
            return
        if modifiers & Qt.AltModifier:
            step = self.SCROLL_STEP_SECONDS * 10
        elif modifiers & Qt.ShiftModifier:
            step = self.SCROLL_STEP_SECONDS * 4
        else:
            step = self.SCROLL_STEP_SECONDS
        total = self._sheet.beats_to_seconds(self._sheet.total_beats) if self._sheet else 0.0
        if self._smooth_browse_enabled:
            # 平滑分支(播放中 / 未播放皆走這條):把 delta 累加在「上一次動畫終點」上,
            # 動畫從目前值重啟到新目標,連續滾就像連續變速;停滾 220ms 後自然停下。
            # 播放中時 _on_smooth_browse_tick 會把動畫值當 seek 目標 emit。
            cur = self.current_seconds() if self._playing else self._browse_offset
            if self._smooth_browse_anim.state() == QVariantAnimation.Running:
                base = self._smooth_browse_target
            else:
                base = cur
            target = base - notches * step
            target = max(0.0, target)
            if total > 0:
                target = min(target, total)
            self._smooth_browse_target = target
            self._smooth_browse_anim.stop()
            self._smooth_browse_anim.setStartValue(float(cur))
            self._smooth_browse_anim.setEndValue(float(target))
            self._smooth_browse_anim.start()
        elif self._playing:
            new_pos = max(0.0, self.current_seconds() - notches * step)
            if total > 0:
                new_pos = min(new_pos, total)
            self.seek_requested.emit(new_pos)
        else:
            self._browse_offset = max(0.0, self._browse_offset - notches * step)
            if total > 0:
                self._browse_offset = min(self._browse_offset, total)
            self.update()
        event.accept()

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt override.
        button = event.button()
        if button not in (Qt.LeftButton, Qt.RightButton) or self._sheet is None:
            return super().mousePressEvent(event)
        pos = event.position()

        # 播放區間 markers 優先處理:左鍵命中 → 進入單 marker drag;
        # 右鍵命中 → 直接清除該 marker。命中後不要落進音符 / marquee 判斷。
        for which in ("start", "end"):
            if not self._is_near_loop_marker(pos.x(), which):
                continue
            if button == Qt.LeftButton:
                self._loop_drag_active = True
                self._loop_drag_target = which
                self.setCursor(Qt.SizeHorCursor)
                self.setFocus(Qt.MouseFocusReason)
                self.update()
                return
            if button == Qt.RightButton:
                if which == "start":
                    self._loop_start_seconds = None
                else:
                    self._loop_end_seconds = None
                self.loop_range_changed.emit(
                    self._loop_start_seconds, self._loop_end_seconds
                )
                self.update()
                return

        hit = self._hit_event(pos.x(), pos.y())
        idx = hit[0] if hit is not None else None
        clicked_label = hit[1] if hit is not None else None

        # 右鍵分支:命中音符 → resize(右拉長短);沒命中 → 等 release 時彈 tempo 選單。
        # 原本「右鍵 = contextMenu」改成「右鍵在空白處 = contextMenu」,音符上 = resize。
        if button == Qt.RightButton:
            # 右鍵空白 / 休止符 → 選單交給 mouseReleaseEvent(最可靠的右鍵入口)。
            # 右鍵命中實音符 → 進入 resize。
            if idx is None or not (0 <= idx < len(self._sheet.events)):
                self._pending_context_menu = True
                return
            ev = self._sheet.events[idx]
            if ev.is_rest:
                self._pending_context_menu = True
                return
            stroke_tracks = tuple(TRACK_INDEX.get(s.label) for s in ev.strokes)
            track_idx = (
                TRACK_INDEX.get(ev.strokes[0].label, 0)
                if len(ev.strokes) == 1 else None
            )
            # 右鍵 resize:保留現有 selection (多選後想拉一票同步變長度的場景)
            # 若點到的本就在多選裡 → 整批 resize;否則只 resize 自己。
            already_selected = (idx, clicked_label) in self._selected_strokes
            target_indices = (
                list(self._selected_indices)
                if already_selected and len(self._selected_indices) > 1
                else [idx]
            )
            self._drag_states = {}
            for di in target_indices:
                if not (0 <= di < len(self._sheet.events)):
                    continue
                dev = self._sheet.events[di]
                if dev.is_rest:
                    continue
                dev_tracks = tuple(TRACK_INDEX.get(s.label) for s in dev.strokes)
                dev_track_idx = (
                    TRACK_INDEX.get(dev.strokes[0].label, 0)
                    if len(dev.strokes) == 1 else None
                )
                self._drag_states[di] = {
                    "initial_start": dev.start_beats,
                    "initial_duration": dev.duration_beats,
                    "initial_track_idx": dev_track_idx,
                    "stroke_initial_tracks": dev_tracks,
                    "can_pitch": False,
                    "current_start": dev.start_beats,
                    "current_duration": dev.duration_beats,
                    "current_track_idx": dev_track_idx,
                    "track_offset": 0,
                }
            if not self._drag_states:
                return
            self._drag_split = None
            self._drag_active = True
            self._drag_event_index = idx
            self._drag_start_pos = pos
            self._drag_mode = "resize"
            self._drag_can_move_pitch = False
            self._drag_button = Qt.RightButton
            self._press_clicked_label = clicked_label
            self._press_was_in_group = False
            self.setCursor(Qt.SizeHorCursor)
            self.setFocus(Qt.MouseFocusReason)
            self.update()
            return

        # 以下為左鍵分支(原本邏輯,但拿掉「右緣 = resize」的特殊區)。播放中也允許,
        # 不過要記得 piano_roll 仍在滾動,拖曳會相對於當下 view 的位置。
        shift_held = bool(event.modifiers() & Qt.ShiftModifier)
        if idx is None:
            if not shift_held:
                self._selected_indices = set()
                self._selected_strokes = set()
            self._marquee_active = True
            self._marquee_start_pos = pos
            self._marquee_current_pos = pos
            self._marquee_start_seconds = self._seconds_at_x(pos.x())
            self._marquee_start_y = pos.y()
            self.setFocus(Qt.MouseFocusReason)
            self.update()
            return
        if shift_held:
            # shift+click: 切換 (idx, label) 在精細選取裡的存在
            if (idx, clicked_label) in self._selected_strokes:
                self._remove_selected_stroke(idx, clicked_label)
            else:
                self._add_selected_stroke(idx, clicked_label)
            self.setFocus(Qt.MouseFocusReason)
            self.update()
            return

        # 點到和弦中的某個音 → 進入「拆出該音獨立拖」模式 (延遲到實際移動才拆)
        # 但若該 stroke 已被選 (例如剛框選了一票),改走整批拖,不拆!
        clicked_event = self._sheet.events[idx] if 0 <= idx < len(self._sheet.events) else None
        already_selected = (idx, clicked_label) in self._selected_strokes
        is_chord_stroke = (
            clicked_event is not None
            and not clicked_event.is_rest
            and len(clicked_event.strokes) > 1
            and clicked_label is not None
        )
        if is_chord_stroke and not already_selected:
            ev = clicked_event
            track_idx = TRACK_INDEX.get(clicked_label, 0)
            self._drag_split = {
                "orig_idx": idx,
                "label": clicked_label,
                "initial_start": ev.start_beats,
                "initial_duration": ev.duration_beats,
                "initial_track_idx": track_idx,
                "current_start": ev.start_beats,
                "current_duration": ev.duration_beats,
                "current_track_idx": track_idx,
                "extracted": False,
                "can_pitch": True,
            }
            # 視覺上只亮被點的那個 stroke (不要整個 chord 都亮)
            self._set_selected_strokes({(idx, clicked_label)})
            self._press_was_in_group = False
            self._drag_states = {}
            self._drag_active = True
            self._drag_event_index = idx
            self._drag_start_pos = pos
            self._drag_mode = "move"
            self._drag_can_move_pitch = True
            self.setCursor(Qt.ClosedHandCursor)
            self.setFocus(Qt.MouseFocusReason)
            self.update()
            return
        # 一般單音事件 / 已被選的 chord stroke (整批拖)
        self._drag_split = None
        self._press_clicked_label = clicked_label
        if not already_selected:
            self._set_selected_strokes({(idx, clicked_label)})
            self._press_was_in_group = False
        else:
            # 點到已選中成員:多選時保留整組以便拖整批,若沒實際移動則 release 時 collapse
            self._press_was_in_group = len(self._selected_indices) > 1
        drag_indices = (
            list(self._selected_indices)
            if len(self._selected_indices) > 1
            else [idx]
        )
        self._drag_states = {}
        for di in drag_indices:
            if not (0 <= di < len(self._sheet.events)):
                continue
            dev = self._sheet.events[di]
            if dev.is_rest:
                continue
            # 該事件內哪些 stroke 是被選中要一起拖的(可能是子集 = 和弦中部分音被選)。
            # 子集時 partial=True,release 時主視窗會把這些 stroke 從原 chord 拆出,
            # 形成新事件;未選的 stroke 留在原事件不動 — 對齊「多選什麼動什麼」訴求。
            all_labels = tuple(s.label for s in dev.strokes)
            if len(drag_indices) > 1 or idx != di:
                # 多選或這個 di 不是被點擊的事件:嚴格按 _selected_strokes 投影出 labels
                selected_labels = tuple(
                    s.label for s in dev.strokes
                    if (di, s.label) in self._selected_strokes
                )
            else:
                # 單選且 di == idx:已被選 → 用既有 _selected_strokes 集合;沒被選代表
                # 點了空白處後拖(理論上走不到這分支,因為前面 not already_selected 已 set);
                # 保底用 dev.strokes 整個視為被選。
                selected_labels = tuple(
                    s.label for s in dev.strokes
                    if (di, s.label) in self._selected_strokes
                )
                if not selected_labels:
                    selected_labels = all_labels
            if not selected_labels:
                # 該事件沒任何 stroke 被選但 idx 還是進來 (理論不該發生),跳過
                continue
            partial = len(selected_labels) < len(all_labels)
            stroke_tracks = tuple(
                TRACK_INDEX.get(s.label) for s in dev.strokes
            )
            # 計算「被拖 stroke」的 initial tracks,給垂直 clamp / track_offset 用
            selected_initial_tracks = tuple(
                TRACK_INDEX.get(s.label)
                for s in dev.strokes
                if s.label in selected_labels
            )
            valid_tracks = tuple(t for t in selected_initial_tracks if t is not None)
            # 所有事件 (含 chord) 都可以垂直整體 shift;chord 用 track_offset 對每個 stroke 同步偏移
            can_pitch = bool(valid_tracks)
            # 單音事件或部分 chord 只選 1 個 stroke 才能直接換 pitch(current_track_idx);
            # 其餘都用 track_offset 同步偏移。
            track_idx = (
                TRACK_INDEX.get(selected_labels[0], 0)
                if len(selected_labels) == 1 else None
            )
            self._drag_states[di] = {
                "initial_start": dev.start_beats,
                "initial_duration": dev.duration_beats,
                "initial_track_idx": track_idx,
                "stroke_initial_tracks": stroke_tracks,
                "selected_labels": selected_labels,
                "selected_initial_tracks": selected_initial_tracks,
                "partial": partial,
                "can_pitch": can_pitch,
                "current_start": dev.start_beats,
                "current_duration": dev.duration_beats,
                "current_track_idx": track_idx,
                "track_offset": 0,
            }
        main_state = self._drag_states.get(idx)
        if main_state is None:
            self._drag_states = {}
            self.setFocus(Qt.MouseFocusReason)
            self.update()
            return
        self._drag_active = True
        self._drag_event_index = idx
        self._drag_start_pos = pos
        # 左鍵永遠 move (resize 改走右鍵)。pitch 允許整批上下:chord 用 track_offset
        # 同步偏移每個 stroke,單音直接用 current_track_idx。
        self._drag_mode = "move"
        self._drag_can_move_pitch = any(
            st.get("can_pitch") for st in self._drag_states.values()
        )
        self._drag_button = Qt.LeftButton
        self.setCursor(Qt.ClosedHandCursor)
        self.setFocus(Qt.MouseFocusReason)
        self.update()

    def _seconds_at_x(self, x: float) -> float:
        metrics = self._view_metrics()
        if metrics is None or metrics["usable_w"] <= 0:
            return 0.0
        rel = (x - metrics["margin_left"]) / metrics["usable_w"]
        rel = max(0.0, min(1.0, rel))
        return metrics["view_start"] + rel * metrics["view_span"]

    def hover_seconds(self) -> float | None:
        return self._last_hover_seconds

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt override.
        pos = event.position()
        self._last_hover_seconds = self._seconds_at_x(pos.x())
        # 拖播放區間 marker:把該端鎖到滑鼠 x 對應的秒數,clamp 不跨過另一端。
        if self._loop_drag_active and self._loop_drag_target is not None:
            new_s = max(0.0, self._seconds_at_x(pos.x()))
            if self._loop_drag_target == "start":
                if (
                    self._loop_end_seconds is not None
                    and new_s > self._loop_end_seconds
                ):
                    new_s = self._loop_end_seconds
                self._loop_start_seconds = new_s
            else:
                if (
                    self._loop_start_seconds is not None
                    and new_s < self._loop_start_seconds
                ):
                    new_s = self._loop_start_seconds
                self._loop_end_seconds = new_s
            self.update()
            return
        # 滑鼠靠近 marker 時改成水平拖動 cursor,提示可以抓;離開時還原。
        if (
            not self._drag_active
            and not self._marquee_active
        ):
            if (
                self._is_near_loop_marker(pos.x(), "start")
                or self._is_near_loop_marker(pos.x(), "end")
            ):
                self.setCursor(Qt.SizeHorCursor)
            elif self.cursor().shape() == Qt.SizeHorCursor:
                self.setCursor(Qt.ArrowCursor)
        if self._marquee_active:
            self._marquee_current_pos = pos
            self.update()
            return
        # 拆出和弦音的拖曳分支
        if self._drag_active and self._drag_split is not None and self._sheet is not None:
            metrics = self._view_metrics()
            if metrics is None:
                return
            split = self._drag_split
            dx = pos.x() - self._drag_start_pos.x()
            dy = pos.y() - self._drag_start_pos.y()
            sec_per_px = (
                metrics["view_span"] / metrics["usable_w"]
                if metrics["usable_w"] > 0 else 0.0
            )
            beat_seconds = self._sheet.beats_to_seconds(1.0)
            delta_beats = (dx * sec_per_px / beat_seconds) if beat_seconds > 0 else 0.0
            snap = self._sheet.beat / self.SNAP_DIVISOR if self._sheet.beat > 0 else 0.0
            target_start = split["initial_start"] + delta_beats
            if snap > 0:
                target_start = round(target_start / snap) * snap
            split["current_start"] = max(0.0, target_start)
            split["current_duration"] = split["initial_duration"]
            if metrics["track_h"] > 0 and split["initial_track_idx"] is not None:
                delta_tracks = round(dy / metrics["track_h"])
                if self._pitch_sort_mode:
                    # pitch sort 模式下 delta_tracks 為 visual delta(往下 = MIDI -1),
                    # 從 visual 換回 logical 才是拖出後該停的軌道。
                    initial_v = self._visual_index(split["initial_track_idx"])
                    new_v = max(0, min(35, initial_v + delta_tracks))
                    new_idx = self._logical_for_visual(new_v)
                else:
                    new_idx = split["initial_track_idx"] + delta_tracks
                    new_idx = max(0, min(len(TRACK_ORDER) - 1, new_idx))
                split["current_track_idx"] = new_idx
            # 標記為「已實際移動」(release 時用來判斷要不要拆出)
            moved = (
                abs(split["current_start"] - split["initial_start"]) > 1e-6
                or split["current_track_idx"] != split["initial_track_idx"]
            )
            split["extracted"] = split["extracted"] or moved
            self.update()
            return
        if not self._drag_active or self._sheet is None or not self._drag_states:
            return super().mouseMoveEvent(event)
        metrics = self._view_metrics()
        if metrics is None:
            return
        main_state = self._drag_states.get(self._drag_event_index)
        if main_state is None:
            return
        pos = event.position()
        dx = pos.x() - self._drag_start_pos.x()
        dy = pos.y() - self._drag_start_pos.y()
        sec_per_px = metrics["view_span"] / metrics["usable_w"] if metrics["usable_w"] > 0 else 0.0
        delta_sec = dx * sec_per_px
        beat_seconds = self._sheet.beats_to_seconds(1.0)
        if beat_seconds > 0:
            delta_beats = delta_sec / beat_seconds
        else:
            delta_beats = 0.0
        snap = self._sheet.beat / self.SNAP_DIVISOR if self._sheet.beat > 0 else 0.0
        min_dur = snap if snap > 0 else self._sheet.beat * 0.0625
        if self._drag_mode == "resize":
            target_main_dur = main_state["initial_duration"] + delta_beats
            if snap > 0:
                target_main_dur = round(target_main_dur / snap) * snap
            target_main_dur = max(min_dur, target_main_dur)
            snapped_delta_dur = target_main_dur - main_state["initial_duration"]
            for st in self._drag_states.values():
                st["current_duration"] = max(
                    min_dur, st["initial_duration"] + snapped_delta_dur
                )
                st["current_start"] = st["initial_start"]
                st["current_track_idx"] = st["initial_track_idx"]
        else:
            target_main_start = main_state["initial_start"] + delta_beats
            if snap > 0:
                target_main_start = round(target_main_start / snap) * snap
            target_main_start = max(0.0, target_main_start)
            snapped_delta_start = target_main_start - main_state["initial_start"]
            min_initial_start = min(
                st["initial_start"] for st in self._drag_states.values()
            )
            if snapped_delta_start < -min_initial_start:
                snapped_delta_start = -min_initial_start
            for st in self._drag_states.values():
                st["current_start"] = max(
                    0.0, st["initial_start"] + snapped_delta_start
                )
                st["current_duration"] = st["initial_duration"]
            if self._drag_can_move_pitch and metrics["track_h"] > 0:
                delta_tracks = round(dy / metrics["track_h"])
                # 用「被選中要拖的 stroke 初始 track」當邊界。未被選的 stroke 不會動,
                # 不應該參與 clamp(否則和弦中有 stroke 在邊緣會把整批拖卡住)。
                all_initial_tracks: list[int] = []
                for st in self._drag_states.values():
                    if not st.get("can_pitch"):
                        continue
                    for t in st.get("selected_initial_tracks", ()):
                        if t is not None:
                            all_initial_tracks.append(t)
                if all_initial_tracks:
                    if self._pitch_sort_mode:
                        # pitch sort 模式:delta_tracks 解釋為 visual delta(y 一格 = MIDI ±1)。
                        # 邊界用 visual,clamp 讓 chord 中任一 stroke 不超出 0..35。每個 stroke
                        # 各自從新 visual 換回 logical,保證 chord 內音程一致 ±1 半音。
                        initial_visuals = [self._visual_index(t) for t in all_initial_tracks]
                        min_v = min(initial_visuals)
                        max_v = max(initial_visuals)
                        delta_tracks = max(
                            -min_v,
                            min(35 - max_v, delta_tracks),
                        )
                        for st in self._drag_states.values():
                            if not st.get("can_pitch"):
                                continue
                            st["track_offset"] = delta_tracks
                            if st["initial_track_idx"] is not None:
                                initial_v = self._visual_index(st["initial_track_idx"])
                                new_v = max(0, min(35, initial_v + delta_tracks))
                                st["current_track_idx"] = self._logical_for_visual(new_v)
                    else:
                        min_t = min(all_initial_tracks)
                        max_t = max(all_initial_tracks)
                        delta_tracks = max(
                            -min_t,
                            min(len(TRACK_ORDER) - 1 - max_t, delta_tracks),
                        )
                        for st in self._drag_states.values():
                            if not st.get("can_pitch"):
                                continue
                            st["track_offset"] = delta_tracks
                            if st["initial_track_idx"] is not None:
                                st["current_track_idx"] = (
                                    st["initial_track_idx"] + delta_tracks
                                )
        self.update()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt override.
        button = event.button()
        # 右鍵空白/休止符:press 設了 pending → 在這裡彈合併選單(編輯 + 變速)。
        # 用 mouseRelease 當入口最可靠(滑鼠事件一定送達);contextMenuEvent 只留鍵盤。
        if button == Qt.RightButton and self._pending_context_menu:
            self._pending_context_menu = False
            self._show_blank_context_menu(
                event.position(), event.globalPosition().toPoint()
            )
            return
        # 播放區間 marker 拖完:emit signal,主視窗端會同步給 OverviewBar / sheet / spinbox。
        if self._loop_drag_active and button == Qt.LeftButton:
            self._loop_drag_active = False
            self._loop_drag_target = None
            self.setCursor(Qt.ArrowCursor)
            self.loop_range_changed.emit(
                self._loop_start_seconds, self._loop_end_seconds
            )
            self.update()
            return
        if self._marquee_active:
            self._marquee_active = False
            rect = self._marquee_rect()
            shift_held = bool(event.modifiers() & Qt.ShiftModifier)
            if rect is not None and rect.width() > 2 and rect.height() > 2:
                hit = self._strokes_in_rect(rect)
                if shift_held:
                    self._set_selected_strokes(self._selected_strokes | hit)
                else:
                    self._set_selected_strokes(hit)
            self._marquee_start_pos = None
            self._marquee_current_pos = None
            self._marquee_start_seconds = None
            self._marquee_start_y = None
            self.update()
            return
        if not self._drag_active:
            return super().mouseReleaseEvent(event)
        self._drag_active = False
        self.setCursor(Qt.ArrowCursor)

        # 拆出和弦音的釋放分支
        if self._drag_split is not None:
            split = self._drag_split
            self._drag_split = None
            self._drag_event_index = None
            if split["extracted"]:
                self.chord_stroke_extracted.emit(
                    int(split["orig_idx"]),
                    str(split["label"]),
                    float(split["current_start"]),
                    float(split["current_duration"]),
                    int(split["current_track_idx"]),
                )
            self.update()
            return

        states = self._drag_states
        self._drag_states = {}
        clicked_idx = self._drag_event_index
        self._drag_event_index = None

        # 判斷是否實際移動 (任一 state 的 start/duration/track 與 initial 不同)
        any_moved = any(
            abs(st["current_start"] - st["initial_start"]) > 1e-6
            or abs(st["current_duration"] - st["initial_duration"]) > 1e-6
            or st["current_track_idx"] != st["initial_track_idx"]
            or int(st.get("track_offset") or 0) != 0
            for st in states.values()
        )

        # 沒移動 + 點到的本來就在多選組裡 → collapse 成單選 (對齊主流 DAW 行為)
        if not any_moved and self._press_was_in_group and clicked_idx is not None:
            # 保留實際被點的那一個 stroke (而非整個事件)
            label = self._press_clicked_label
            if label is not None:
                self._set_selected_strokes({(clicked_idx, label)})
            else:
                # fallback: 保留事件所有 stroke
                labels = self._all_strokes_of(clicked_idx)
                self._set_selected_strokes({(clicked_idx, lbl) for lbl in labels})
            self.update()
        self._press_was_in_group = False
        self._press_clicked_label = None

        # 只在實際移動時才 emit,避免 push undo / 觸發無效改動。
        # 多選一次拖完一次 emit:主視窗端只 push 一次 undo snapshot,
        # 一次 Ctrl+Z 即可整批還原(避免 N 個音符 → undo stack 多 N 個 snapshot)。
        # payload 多帶 selected_labels:partial drag(和弦只拖部分音)時主視窗端
        # 走「拆出新事件 + 留原 chord」路徑,對齊「多選什麼動什麼」訴求。
        if any_moved:
            payload: list[tuple] = []
            for di, st in states.items():
                # 單音事件:用 current_track_idx 換新音高;chord/多 stroke:用 track_offset 同步偏移
                track_offset = int(st.get("track_offset") or 0)
                if st.get("current_track_idx") is not None and track_offset != 0:
                    # 單音的 current_track_idx 已包含 offset → 傳 new_track_idx
                    new_track = st["current_track_idx"]
                    track_offset = 0  # 已用 new_track 表達,不重複
                elif st.get("current_track_idx") is not None:
                    # 沒移動 pitch 也傳 current 給單音事件 (向下相容)
                    new_track = st["current_track_idx"]
                else:
                    new_track = -1
                payload.append((
                    int(di),
                    float(st["current_start"]),
                    float(st["current_duration"]),
                    int(new_track) if new_track is not None else -1,
                    int(track_offset),
                    tuple(st.get("selected_labels") or ()),
                ))
            self.events_moved.emit(payload)

    def _marquee_rect(self):
        if self._marquee_current_pos is None:
            return None
        # 起點優先用「秒」回算 x,讓滾輪平移後框選範圍仍維持譜面上的同一點
        if self._marquee_start_seconds is not None and self._marquee_start_y is not None:
            metrics = self._view_metrics()
            if metrics is None:
                return None
            usable_w = metrics["usable_w"]
            margin_left = metrics["margin_left"]
            view_start = metrics["view_start"]
            view_span = metrics["view_span"]
            if view_span <= 0 or usable_w <= 0:
                return None
            sx = margin_left + (self._marquee_start_seconds - view_start) / view_span * usable_w
            sy = self._marquee_start_y
        elif self._marquee_start_pos is not None:
            sx = self._marquee_start_pos.x()
            sy = self._marquee_start_pos.y()
        else:
            return None
        e = self._marquee_current_pos
        x = min(sx, e.x())
        y = min(sy, e.y())
        w = abs(e.x() - sx)
        h = abs(e.y() - sy)
        return QRectF(x, y, w, h)

    def _strokes_in_rect(self, rect: QRectF) -> set:
        """收集 rect 範圍內的個別 stroke,回傳 set of (event_idx, stroke_label)。
        水平範圍以「秒」為主 (滾輪平移後仍維持譜面同一點),垂直方向用像素比對 row。
        """
        if self._sheet is None:
            return set()
        metrics = self._view_metrics()
        if metrics is None:
            return set()
        margin_top = metrics["margin_top"]
        track_h = metrics["track_h"]

        if (
            self._marquee_start_seconds is not None
            and self._marquee_current_pos is not None
        ):
            end_seconds = self._seconds_at_x(self._marquee_current_pos.x())
            sec_lo = min(self._marquee_start_seconds, end_seconds)
            sec_hi = max(self._marquee_start_seconds, end_seconds)
        else:
            view_start = metrics["view_start"]
            view_span = metrics["view_span"]
            usable_w = metrics["usable_w"]
            margin_left = metrics["margin_left"]
            if usable_w <= 0 or view_span <= 0:
                return set()
            sec_lo = view_start + max(0.0, rect.left() - margin_left) / usable_w * view_span
            sec_hi = view_start + max(0.0, rect.right() - margin_left) / usable_w * view_span

        result: set[tuple[int, str]] = set()
        for i, ev in enumerate(self._sheet.events):
            if ev.is_rest:
                continue
            start_s = self._sheet.beats_to_seconds(ev.start_beats)
            end_s = self._sheet.beats_to_seconds(ev.start_beats + ev.duration_beats)
            if end_s < sec_lo or start_s > sec_hi:
                continue
            for stroke in ev.strokes:
                track_idx = TRACK_INDEX.get(stroke.label)
                if track_idx is None:
                    continue
                ty = margin_top + self._visual_index(track_idx) * track_h
                if ty + track_h >= rect.top() and ty <= rect.bottom():
                    result.add((i, stroke.label))
        return result

    def _events_in_rect(self, rect: QRectF) -> set:
        """舊 API:從 _strokes_in_rect 衍生 event index 集合 (給其他需要 idx 的地方)。"""
        return {idx for idx, _label in self._strokes_in_rect(rect)}

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt override.
        if (
            event.key() in (Qt.Key_Delete, Qt.Key_Backspace)
            and not self._playing
            and self._selected_indices
        ):
            indices = sorted(self._selected_indices, reverse=True)
            self.events_delete_requested.emit(indices)
            event.accept()
            return
        super().keyPressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802 - Qt override.
        if event.button() != Qt.LeftButton or self._playing or self._sheet is None:
            return super().mouseDoubleClickEvent(event)
        pos = event.position()
        hit = self._hit_event(pos.x(), pos.y())
        if hit is None:
            return
        idx = hit[0]
        self._selected_indices = {idx}
        self.update()
        self.event_double_clicked.emit(idx)

    def _show_blank_context_menu(self, local_pos, global_pos) -> None:
        """空白 / 休止符處右鍵選單:編輯項(新增音符/剪下/複製/貼上/刪除) + 變速項。

        新增音符落在右鍵點擊的時間位置(非譜面開頭),音高用視覺最頂那一行
        (一般模式 H1、音高排序模式 H7)。編輯動作 emit signal 交主視窗(掌管
        sheet / undo / clipboard)處理。
        """
        if self._sheet is None:
            return
        seconds = self._seconds_at_x(local_pos.x())
        beats = self._sheet.seconds_to_beats(seconds)
        # 貼上要落在右鍵位置 → 先把 hover 設成這裡(主視窗貼上讀 hover_seconds)。
        self._last_hover_seconds = seconds
        top_label = TRACK_ORDER[self._logical_for_visual(0)][1]
        has_sel = bool(self._selected_strokes)

        menu = QMenu(self)
        add_note_act = menu.addAction(f"在此處新增音符（{top_label}）")
        add_note_act.triggered.connect(
            lambda _=False, b=beats, lb=top_label: self.note_insert_requested.emit(b, lb)
        )
        menu.addSeparator()
        cut_act = menu.addAction("剪下")
        cut_act.setEnabled(has_sel)
        cut_act.triggered.connect(lambda _=False: self.cut_requested.emit())
        copy_act = menu.addAction("複製")
        copy_act.setEnabled(has_sel)
        copy_act.triggered.connect(lambda _=False: self.copy_requested.emit())
        paste_act = menu.addAction("貼上")
        paste_act.triggered.connect(lambda _=False: self.paste_requested.emit())
        del_act = menu.addAction("刪除")
        del_act.setEnabled(has_sel)
        del_act.triggered.connect(
            lambda _=False: self.events_delete_requested.emit(sorted(self._selected_indices))
        )
        menu.addSeparator()

        # ── 變速項(沿用原 tempo 選單邏輯) ──
        near = None
        if self._sheet.tempo_changes:
            nearest = min(
                self._sheet.tempo_changes,
                key=lambda c: abs(c[0] - beats),
            )
            if abs(nearest[0] - beats) <= 0.4:
                near = nearest
        if near is not None:
            change_beat, change_tempo = near
            edit_act = menu.addAction(
                f"修改變速 ({change_tempo:g} BPM @ 第 {change_beat:g} 拍)…"
            )
            edit_act.triggered.connect(
                lambda _=False, b=change_beat, t=change_tempo:
                    self._prompt_edit_tempo_change(b, t)
            )
            remove_act = menu.addAction(f"移除此變速 ({change_tempo:g} BPM)")
            remove_act.triggered.connect(
                lambda _=False, b=change_beat: self.tempo_change_remove.emit(b)
            )
        else:
            cur_tempo = self._sheet.tempo_at_beat(beats)
            add_tempo_act = menu.addAction(
                f"在第 {beats:.2f} 拍加入變速 (目前 {cur_tempo:g} BPM)…"
            )
            add_tempo_act.triggered.connect(
                lambda _=False, b=beats, t=cur_tempo:
                    self._prompt_add_tempo_change(b, t)
            )
        menu.exec(global_pos)

    def contextMenuEvent(self, event) -> None:  # noqa: N802 - Qt override.
        # 滑鼠右鍵的選單改由 mouseReleaseEvent 觸發(平台 contextMenuEvent 在本機
        # 環境實測不一定送達);這裡只接「鍵盤選單鍵」,並避免與滑鼠路徑雙重彈出。
        if event.reason() != QContextMenuEvent.Keyboard or self._sheet is None:
            event.accept()
            return
        pos = event.pos()
        hit = self._hit_event(float(pos.x()), float(pos.y()))
        idx = hit[0] if hit is not None else None
        if (
            idx is not None
            and 0 <= idx < len(self._sheet.events)
            and not self._sheet.events[idx].is_rest
        ):
            event.accept()
            return
        self._show_blank_context_menu(pos, event.globalPos())
        event.accept()

    def _prompt_add_tempo_change(self, beats: float, current_bpm: float) -> None:
        bpm, ok = QInputDialog.getDouble(
            self, "加入變速",
            f"在第 {beats:.2f} 拍將速度改為 (BPM):",
            current_bpm, 20.0, 400.0, 1,
        )
        if ok:
            self.tempo_change_set.emit(float(beats), float(bpm))

    def _prompt_edit_tempo_change(self, beats: float, current_bpm: float) -> None:
        bpm, ok = QInputDialog.getDouble(
            self, "修改變速",
            f"第 {beats:g} 拍的變速 (BPM):",
            current_bpm, 20.0, 400.0, 1,
        )
        if ok:
            self.tempo_change_set.emit(float(beats), float(bpm))

    def _static_layer(self, metrics) -> QPixmap:
        """背景漸層 + 列底色 + 段落分隔線 + 軌道標籤 + ruler 底色帶的離屏快取。

        只依(視窗尺寸 / pitch_sort / 主題色),與時間軸捲動、縮放無關,因此能跨幀
        沿用;key 任一項變動(resize / 切換音高排序 / 換配色)才重建。每幀只需一次
        drawPixmap,省下 36 軌 tint + 標籤文字重畫與漸層重配置。
        """
        w = self.width()
        h = self.height()
        dpr = self.devicePixelRatioF()
        key = (
            w, h, round(dpr, 3), bool(self._pitch_sort_mode),
            THEME["bg"], THEME["panel"], THEME["panel_alt"],
            THEME["H"], THEME["M"], THEME["L"],
            THEME["grid_strong"], THEME["grid"],
        )
        if self._static_pixmap is not None and self._static_key == key:
            return self._static_pixmap

        pm = QPixmap(max(1, int(round(w * dpr))), max(1, int(round(h * dpr))))
        pm.setDevicePixelRatio(dpr)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)

        margin_left = metrics["margin_left"]
        margin_top = metrics["margin_top"]
        usable_w = metrics["usable_w"]
        track_h = metrics["track_h"]

        bg_color = QColor(THEME["bg"])
        bg_dark = QColor(
            max(0, bg_color.red() - 6),
            max(0, bg_color.green() - 6),
            max(0, bg_color.blue() - 6),
        )
        bg_grad = QLinearGradient(QPointF(0.0, 0.0), QPointF(w, 0.0))
        bg_grad.setColorAt(0.0, bg_dark)
        bg_grad.setColorAt(0.35, bg_color)
        bg_grad.setColorAt(1.0, bg_color)
        p.fillRect(QRectF(0, 0, w, h), bg_grad)

        for i, (prefix, _, _, accidental, _) in enumerate(TRACK_ORDER):
            y = margin_top + self._visual_index(i) * track_h
            base_panel = QColor(THEME["panel"]) if accidental else QColor(THEME["panel_alt"])
            tint = _blend_color(base_panel, QColor(THEME[prefix]), 0.05)
            p.fillRect(QRectF(margin_left, y, usable_w, track_h), tint)

        p.setPen(QPen(QColor(THEME["grid_strong"]), 1.0))
        for i in range(1, len(TRACK_ORDER)):
            if TRACK_ORDER[i][0] != TRACK_ORDER[i - 1][0]:
                y = margin_top + self._visual_index(i) * track_h
                p.drawLine(int(margin_left), int(y), int(margin_left + usable_w), int(y))

        p.setFont(self._track_label_font)
        for i, (prefix, _, _, accidental, degree) in enumerate(TRACK_ORDER):
            y = margin_top + self._visual_index(i) * track_h
            p.setPen(QColor(THEME[prefix]))
            p.drawText(
                QRectF(0, y, margin_left - 6, track_h),
                Qt.AlignVCenter | Qt.AlignRight,
                f"{prefix}{accidental}{degree}",
            )

        p.fillRect(QRectF(margin_left, 0, usable_w, margin_top), QColor(THEME["panel"]))
        p.setPen(QPen(QColor(THEME["grid"]), 1.0))
        p.drawLine(
            int(margin_left), int(margin_top - 0.5),
            int(margin_left + usable_w), int(margin_top - 0.5),
        )

        p.end()
        self._static_pixmap = pm
        self._static_key = key
        return pm

    def paintEvent(self, _event) -> None:
        _t0 = time.perf_counter() if perf.enabled else 0.0
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        metrics = self._view_metrics()
        if metrics is None:
            painter.fillRect(self.rect(), QColor(THEME["bg"]))
            if perf.enabled:
                perf.log(
                    "gui", "roll_paint",
                    ms=round((time.perf_counter() - _t0) * 1000.0, 3),
                )
            return
        margin_left = metrics["margin_left"]
        margin_top = metrics["margin_top"]
        usable_w = metrics["usable_w"]
        usable_h = metrics["usable_h"]
        track_h = metrics["track_h"]
        current = metrics["current"]
        view_start = metrics["view_start"]
        view_end = metrics["view_end"]
        view_span = metrics["view_span"]

        # 靜態層(背景/列底色/分隔線/標籤/ruler 底色)一次 blit,取代每幀重畫。
        painter.drawPixmap(0, 0, self._static_layer(metrics))

        def t_to_x(t: float) -> float:
            return margin_left + (t - view_start) / view_span * usable_w

        cursor_x = t_to_x(current)

        if self._sheet is not None and self._sheet.tempo > 0:
            beat_seconds = self._sheet.beats_to_seconds(1.0)
            if beat_seconds > 0:
                k_start = int(view_start / beat_seconds) - 1
                k_end = int(view_end / beat_seconds) + 2
                strong_pen = QPen(QColor(THEME["grid_strong"]), 0.9)
                weak_color = QColor(THEME["grid"])
                weak_color.setAlpha(150)
                weak_pen = QPen(weak_color, 0.6)
                for k in range(k_start, k_end):
                    t = k * beat_seconds
                    x = t_to_x(t)
                    if x < margin_left or x > margin_left + usable_w:
                        continue
                    if k % 4 == 0:
                        painter.setPen(strong_pen)
                    else:
                        painter.setPen(weak_pen)
                    painter.drawLine(int(x), int(margin_top), int(x), int(margin_top + usable_h))

        if self._sheet is not None and self._sheet.tempo > 0:
            beat_seconds = self._sheet.beats_to_seconds(1.0)
            if beat_seconds > 0:
                bar_font = self._bar_number_font
                painter.setFont(bar_font)
                bar_metrics = QFontMetrics(bar_font)
                k_start = max(0, int(view_start / beat_seconds) - 1)
                k_end = int(view_end / beat_seconds) + 2
                for k in range(k_start, k_end):
                    t = k * beat_seconds
                    x = t_to_x(t)
                    if x < margin_left or x > margin_left + usable_w:
                        continue
                    is_bar = k % 4 == 0
                    tick_color = QColor(THEME["fg_dim"])
                    tick_color.setAlpha(160 if is_bar else 80)
                    painter.setPen(QPen(tick_color, 1.0))
                    tick_h = 7.0 if is_bar else 3.0
                    painter.drawLine(
                        int(x), int(margin_top - tick_h),
                        int(x), int(margin_top - 1),
                    )
                    if is_bar:
                        bar_index = k // 4 + 1
                        text = str(bar_index)
                        text_w = bar_metrics.horizontalAdvance(text)
                        text_color = QColor(THEME["fg_dim"])
                        text_color.setAlpha(80 if x < cursor_x - 1 else 200)
                        painter.setPen(text_color)
                        painter.drawText(
                            QPointF(x + 3, margin_top - 8),
                            text,
                        )

        content_clip = QRectF(margin_left, margin_top, usable_w, usable_h)
        painter.save()
        painter.setClipRect(content_clip)

        if self._sheet is not None:
            now_perf = time.perf_counter()
            for i, ev in enumerate(self._sheet.events):
                if ev.is_rest:
                    continue
                drag_state = self._drag_states.get(i) if self._drag_active else None
                # partial drag:該事件只有部分 stroke 被選中拖,未選的 stroke 留在原位畫。
                # 整事件被拖(partial=False)或沒拖時,所有 stroke 共用同一組座標(原本邏輯)。
                partial_drag = bool(drag_state and drag_state.get("partial"))
                dragged_labels = (
                    set(drag_state.get("selected_labels", ()))
                    if drag_state is not None else set()
                )
                # 算「整體事件」的 effective 範圍(view cull 用)。partial 時也要把
                # 「拖出的位置」納入 cull,否則 ghost 拖到 view 內但 cull 用原位置會被剔掉。
                if drag_state is not None:
                    eff_start = min(drag_state["current_start"], ev.start_beats)
                    eff_end_beats = max(
                        drag_state["current_start"] + drag_state["current_duration"],
                        ev.start_beats + ev.duration_beats,
                    )
                    eff_duration = eff_end_beats - eff_start
                else:
                    eff_start = ev.start_beats
                    eff_duration = ev.duration_beats
                start_s = self._sheet.beats_to_seconds(eff_start)
                end_s = self._sheet.beats_to_seconds(eff_start + eff_duration)
                if end_s < view_start or start_s > view_end:
                    continue
                custom = self._event_colors.get(i)
                # 拖出和弦音時,該音原位上的方塊用半透明顯示
                split_dim_label = None
                if (
                    self._drag_split is not None
                    and self._drag_split.get("orig_idx") == i
                    and self._drag_split.get("extracted")
                ):
                    split_dim_label = self._drag_split.get("label")
                for stroke in ev.strokes:
                    base_track_idx = TRACK_INDEX.get(stroke.label)
                    # stroke 是否被當前拖曳帶走;partial drag 時未選 stroke 走「原位」路徑。
                    stroke_is_dragged = (
                        drag_state is not None
                        and (not partial_drag or stroke.label in dragged_labels)
                    )
                    if stroke_is_dragged:
                        s_start_beats = drag_state["current_start"]
                        s_dur_beats = drag_state["current_duration"]
                    else:
                        s_start_beats = ev.start_beats
                        s_dur_beats = ev.duration_beats
                    s_start_s = self._sheet.beats_to_seconds(s_start_beats)
                    s_end_s = self._sheet.beats_to_seconds(s_start_beats + s_dur_beats)
                    x1 = t_to_x(s_start_s)
                    x2 = t_to_x(s_end_s)
                    w = max(3.0, x2 - x1 - 1.5)
                    if (
                        stroke_is_dragged
                        and drag_state.get("can_pitch")
                        and drag_state.get("current_track_idx") is not None
                        and len(dragged_labels) == 1
                    ):
                        # 單音(或 partial 中只 1 個 stroke 被選):用 current_track_idx 直接覆寫
                        track_idx = drag_state["current_track_idx"]
                    elif (
                        stroke_is_dragged
                        and drag_state.get("can_pitch")
                        and drag_state.get("track_offset")
                        and base_track_idx is not None
                    ):
                        # 多 stroke 同步偏移 track_offset 個格子。
                        # pitch sort mode 下 track_offset 是 visual delta,先換成 visual
                        # 再 clamp + 回 logical;default mode 直接用 logical 相加。
                        if self._pitch_sort_mode:
                            initial_v = self._visual_index(base_track_idx)
                            new_v = max(0, min(35, initial_v + drag_state["track_offset"]))
                            track_idx = self._logical_for_visual(new_v)
                        else:
                            target = base_track_idx + drag_state["track_offset"]
                            track_idx = max(0, min(len(TRACK_ORDER) - 1, target))
                    else:
                        track_idx = base_track_idx
                    if track_idx is None:
                        continue
                    y = margin_top + self._visual_index(track_idx) * track_h + 1.0
                    h = max(2.5, track_h - 2.0)
                    prefix = TRACK_ORDER[track_idx][0]
                    is_now = s_start_s <= current <= s_end_s and stroke.label in self._active_labels
                    pulse = self._pulse_strength(stroke.label, now_perf) if is_now else 0.0
                    is_past = s_end_s < current and not stroke_is_dragged
                    if custom is not None:
                        color = QColor(custom)
                        if is_now:
                            color = color.lighter(122 + int(28 * pulse))
                        elif is_past:
                            past_dt = current - s_end_s
                            faded = QColor(custom)
                            faded.setAlpha(140)
                            if (
                                self._animations_enabled
                                and self._playing
                                and past_dt < self.PAST_FADE_SECONDS
                            ):
                                fade_t = _ease_out_quad(past_dt / self.PAST_FADE_SECONDS)
                                color = _blend_color(QColor(custom), faded, fade_t)
                            else:
                                color = faded
                    elif is_now:
                        base = QColor(THEME[f"{prefix}_active"])
                        color = base.lighter(100 + int(22 * pulse))
                    elif is_past:
                        past_dt = current - s_end_s
                        past_color = QColor(THEME[prefix])
                        past_color.setAlpha(110)
                        if (
                            self._animations_enabled
                            and self._playing
                            and past_dt < self.PAST_FADE_SECONDS
                        ):
                            fade_t = _ease_out_quad(past_dt / self.PAST_FADE_SECONDS)
                            active = QColor(THEME[f"{prefix}_active"])
                            color = _blend_color(active, past_color, fade_t)
                        else:
                            color = past_color
                    else:
                        color = QColor(THEME[prefix])
                    if split_dim_label is not None and stroke.label == split_dim_label:
                        # 該 stroke 正被拖出 → 原位上的方塊半透明顯示
                        color = QColor(color)
                        color.setAlpha(70)
                    note_rect = QRectF(x1 + 1, y, w, h)
                    radius = min(4.0, h * 0.45)
                    painter.setBrush(color)
                    edge = QColor(color).darker(140)
                    if (i, stroke.label) in self._selected_strokes:
                        painter.setPen(QPen(QColor("#ffd166"), 2.0))
                    else:
                        painter.setPen(QPen(edge, 0.8))
                    painter.drawRoundedRect(note_rect, radius, radius)

                    if h >= 6.0:
                        highlight_h = max(2.0, h * 0.4)
                        highlight_rect = QRectF(
                            note_rect.left(),
                            note_rect.top(),
                            note_rect.width(),
                            highlight_h,
                        )
                        painter.setBrush(self._note_highlight_grad)
                        painter.setPen(Qt.NoPen)
                        painter.drawRoundedRect(highlight_rect, radius, radius)

                    if pulse > 0.05:
                        glow_color = QColor(THEME[prefix])
                        rings = (
                            (5.5, 0.10),
                            (4.5, 0.18),
                            (3.5, 0.30),
                            (2.5, 0.45),
                            (1.5, 0.65),
                            (0.8, 0.85),
                        )
                        painter.setBrush(Qt.NoBrush)
                        for ex_factor, alpha_factor in rings:
                            ex = ex_factor * pulse
                            halo = QColor(glow_color)
                            halo.setAlpha(int(220 * pulse * alpha_factor))
                            painter.setPen(QPen(halo, 1.5))
                            painter.drawRoundedRect(
                                note_rect.adjusted(-ex, -ex, ex, ex),
                                radius + ex,
                                radius + ex,
                            )

        # 拖出和弦音的「飄移中」方塊 (畫在所有事件之上,不受 _drag_states 影響)
        if (
            self._drag_split is not None
            and self._drag_split.get("extracted")
            and self._sheet is not None
        ):
            split = self._drag_split
            track_idx = split.get("current_track_idx")
            if track_idx is not None and 0 <= track_idx < len(TRACK_ORDER):
                eff_start = float(split["current_start"])
                eff_dur = float(split["current_duration"])
                ss = self._sheet.beats_to_seconds(eff_start)
                se = self._sheet.beats_to_seconds(eff_start + eff_dur)
                gx1 = t_to_x(ss)
                gx2 = t_to_x(se)
                gy = margin_top + self._visual_index(track_idx) * track_h + 1.0
                gh = max(2.5, track_h - 2.0)
                gw = max(3.0, gx2 - gx1 - 1.5)
                ghost_rect = QRectF(gx1 + 1, gy, gw, gh)
                prefix = TRACK_ORDER[track_idx][0]
                ghost_color = QColor(THEME[f"{prefix}_active"])
                painter.setBrush(ghost_color)
                painter.setPen(QPen(QColor("#ffd166"), 2.0))
                painter.drawRoundedRect(ghost_rect, min(4.0, gh * 0.45), min(4.0, gh * 0.45))

        # 播放區間半透明填色(在 cursor 線之下、事件之上)
        if (
            self._loop_start_seconds is not None
            and self._loop_end_seconds is not None
            and self._loop_end_seconds > self._loop_start_seconds
        ):
            lx1 = t_to_x(max(view_start, self._loop_start_seconds))
            lx2 = t_to_x(min(view_end, self._loop_end_seconds))
            if lx2 > lx1:
                loop_fill = QColor(THEME["accent"])
                loop_fill.setAlpha(40)
                painter.fillRect(
                    QRectF(lx1, margin_top, lx2 - lx1, usable_h),
                    loop_fill,
                )

        if margin_left <= cursor_x <= margin_left + usable_w:
            cursor_color = QColor(THEME["cursor"])
            painter.setPen(QPen(cursor_color, 2.0))
            painter.drawLine(
                int(cursor_x), int(margin_top),
                int(cursor_x), int(margin_top + usable_h),
            )

        # 變速點:垂直黃色虛線 + 上方標籤
        if self._sheet is not None and self._sheet.tempo_changes:
            tempo_pen = QPen(QColor("#fbbf24"), 1.5, Qt.DashLine)
            painter.setPen(tempo_pen)
            tempo_font = QFont("Cascadia Mono", 8)
            tempo_font.setBold(True)
            for change_beat, change_tempo in self._sheet.tempo_changes:
                t = self._sheet.beats_to_seconds(change_beat)
                if t < view_start - 0.5 or t > view_end + 0.5:
                    continue
                tx = margin_left + (t - view_start) / view_span * usable_w
                if tx < margin_left or tx > margin_left + usable_w:
                    continue
                painter.drawLine(int(tx), int(margin_top), int(tx), int(margin_top + usable_h))

        if self._marquee_active:
            mrect = self._marquee_rect()
            if mrect is not None and mrect.width() > 0 and mrect.height() > 0:
                fill = QColor(255, 209, 102, 50)
                painter.fillRect(mrect, fill)
                painter.setPen(QPen(QColor(255, 209, 102), 1.0, Qt.DashLine))
                painter.setBrush(Qt.NoBrush)
                painter.drawRect(mrect)

        painter.restore()

        # 變速點標籤 (在 ruler 上方,clip 之外才畫得到)
        if self._sheet is not None and self._sheet.tempo_changes:
            label_font = QFont("Cascadia Mono", 8)
            label_font.setBold(True)
            painter.setFont(label_font)
            label_metrics = QFontMetrics(label_font)
            label_color = QColor("#fbbf24")
            for change_beat, change_tempo in self._sheet.tempo_changes:
                t = self._sheet.beats_to_seconds(change_beat)
                if t < view_start - 0.5 or t > view_end + 0.5:
                    continue
                tx = margin_left + (t - view_start) / view_span * usable_w
                if tx < margin_left or tx > margin_left + usable_w:
                    continue
                text = f"♩={change_tempo:g}"
                text_w = label_metrics.horizontalAdvance(text)
                bg_rect = QRectF(tx + 2, 2, text_w + 8, margin_top - 6)
                bg = QColor("#fbbf24")
                bg.setAlpha(40)
                painter.fillRect(bg_rect, bg)
                painter.setPen(label_color)
                painter.drawText(
                    QPointF(tx + 6, margin_top - 8),
                    text,
                )

        # 播放區間 markers:垂直線(穿過 ruler + 內容區)+ ruler 上方標籤 A/B。
        # 畫在 clip 之外,讓 marker 線延伸到 ruler 區,並可以蓋上文字標籤。
        if self._loop_start_seconds is not None or self._loop_end_seconds is not None:
            marker_font = QFont("Cascadia Mono", 8)
            marker_font.setBold(True)
            painter.setFont(marker_font)
            marker_metrics = QFontMetrics(marker_font)
            accent = QColor(THEME["accent"])
            for which, value, text in (
                ("start", self._loop_start_seconds, "A"),
                ("end", self._loop_end_seconds, "B"),
            ):
                if value is None:
                    continue
                if value < view_start - 0.5 or value > view_end + 0.5:
                    continue
                mx = margin_left + (value - view_start) / view_span * usable_w
                if mx < margin_left or mx > margin_left + usable_w:
                    continue
                # 高亮拖動中的那一端,標示現在抓的是哪根。
                is_dragging = (
                    self._loop_drag_active and self._loop_drag_target == which
                )
                line_color = QColor(accent)
                if is_dragging:
                    line_color = line_color.lighter(130)
                painter.setPen(QPen(line_color, 2.0))
                painter.drawLine(int(mx), 0, int(mx), int(margin_top + usable_h))
                # ruler 上的標籤背板
                text_w = marker_metrics.horizontalAdvance(text)
                bg_rect = QRectF(mx - text_w - 8, 2, text_w + 12, margin_top - 6)
                bg = QColor(accent)
                bg.setAlpha(180 if is_dragging else 120)
                painter.fillRect(bg_rect, bg)
                painter.setPen(QColor(THEME["bg"]))
                painter.drawText(
                    QPointF(mx - text_w - 3, margin_top - 8),
                    text,
                )

        if margin_left <= cursor_x <= margin_left + usable_w:
            cursor_color = QColor(THEME["cursor"])
            tri = QPainterPath()
            tri.moveTo(cursor_x - 5.0, 0.0)
            tri.lineTo(cursor_x + 5.0, 0.0)
            tri.lineTo(cursor_x, 6.0)
            tri.closeSubpath()
            painter.setBrush(cursor_color)
            painter.setPen(Qt.NoPen)
            painter.drawPath(tri)

        if self._sheet is not None:
            total = self._sheet.beats_to_seconds(self._sheet.total_beats)
            painter.setPen(QColor(THEME["fg_dim"]))
            painter.setFont(QFont("Cascadia Mono", 9))
            mode = "瀏覽" if not self._playing and self._browse_offset > 0 else ""
            label = f"{max(0.0, current):05.2f} / {total:05.2f} s"
            if mode:
                label = f"{mode}  {label}"
            painter.drawText(
                QRectF(margin_left, 0, usable_w, margin_top),
                Qt.AlignRight | Qt.AlignVCenter,
                label,
            )

        if perf.enabled:
            perf.log(
                "gui", "roll_paint",
                ms=round((time.perf_counter() - _t0) * 1000.0, 3),
            )
