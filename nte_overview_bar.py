# NTE Piano - 自動演奏與自動化工具
# Copyright (C) 2026  Yulun
# Licensed under GPL-3.0-or-later. See LICENSE.
"""nte_overview_bar — 全譜縮圖預覽條 widget.

對外提供:
    OverviewBar  全譜縮圖 + 可視範圍指示 + 拖動 seek + 右鍵拉 loop region.

依賴:
    PySide6.QtCore/QtGui/QtWidgets
    nte_theme (THEME)

不直接 import nte_dsl;sheet 由 set_sheet() 餵入,只使用 sheet.events / sheet.total_beats / sheet.beats_to_seconds() 等 duck-typed 介面。
"""
from __future__ import annotations

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from nte_theme import THEME


class OverviewBar(QWidget):
    """整曲鳥瞰列:細條表示事件,紅線表示 cursor,灰色矩形表示可視範圍。

    左鍵:點擊跳轉(seek_to)、拖曳灰色矩形平移瀏覽位置、拖曳區間 marker 移動該端點。
    右鍵:在空白處 press+drag 拉出一個播放區間(loop range);
         在 marker handle 上點擊清除該 marker。
    區間在 worker 啟動時生效:from loop_start 開始播,到 loop_end 自然結束;
    對已執行中的 worker 不會即時生效(維持當前播放,下次重啟才套用)。
    """

    seek_to = Signal(float)
    view_drag = Signal(float)
    # (start_seconds | None, end_seconds | None);None 表示該端未設,
    # 兩個都 None = 完全清除播放區間。
    loop_range_changed = Signal(object, object)

    MARKER_HIT_RADIUS = 6  # 點擊 marker handle 的容忍半徑(像素)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._sheet = None
        self._cursor_seconds = 0.0
        self._view_start = 0.0
        self._view_end = 0.0
        self._cached_rects: dict[str, list] | None = None
        self._cached_for_size = (0, 0)
        self._dragging_view = False
        self._drag_offset = 0.0
        # 播放區間 markers — 起終點各自可獨立設定/拖曳;兩者都設才會「區間生效」。
        self._loop_start_seconds: float | None = None
        self._loop_end_seconds: float | None = None
        # 拖曳狀態:右鍵拖曳期間用 _loop_anchor 鎖住 anchor 端;
        # 左鍵單 marker 拖曳用 _loop_drag_target 標示移動哪一端("start"/"end")。
        self._loop_dragging = False
        self._loop_anchor_seconds = 0.0
        self._loop_drag_target: str | None = None
        self.setFixedHeight(36)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMouseTracking(True)
        self.setAttribute(Qt.WA_StyledBackground, True)

    def set_sheet(self, sheet) -> None:
        self._sheet = sheet
        self._cached_rects = None
        self.update()

    def set_cursor(self, seconds: float) -> None:
        if abs(seconds - self._cursor_seconds) < 1e-3:
            return
        self._cursor_seconds = max(0.0, float(seconds))
        self.update()

    def set_view_window(self, start_seconds: float, end_seconds: float) -> None:
        if (abs(start_seconds - self._view_start) < 1e-3
                and abs(end_seconds - self._view_end) < 1e-3):
            return
        self._view_start = max(0.0, float(start_seconds))
        self._view_end = max(self._view_start, float(end_seconds))
        self.update()

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

    def _total_seconds(self) -> float:
        if self._sheet is None:
            return 0.0
        return self._sheet.beats_to_seconds(self._sheet.total_beats)

    def _seconds_at_x(self, x: float) -> float:
        total = self._total_seconds()
        if total <= 0 or self.width() <= 0:
            return 0.0
        rel = max(0.0, min(1.0, x / self.width()))
        return rel * total

    def _x_for_seconds(self, seconds: float) -> float:
        total = self._total_seconds()
        if total <= 0 or self.width() <= 0:
            return 0.0
        return max(0.0, min(self.width(), seconds / total * self.width()))

    def _is_near_marker(self, x: float, which: str) -> bool:
        target = self._loop_start_seconds if which == "start" else self._loop_end_seconds
        if target is None:
            return False
        return abs(x - self._x_for_seconds(target)) <= self.MARKER_HIT_RADIUS

    def _build_rects(self) -> dict[str, list]:
        """依 H/M/L 分桶,每桶內為 (x, y, w, h) 方形;回傳實心矩形列表。"""
        rects: dict[str, list] = {"H": [], "M": [], "L": []}
        if self._sheet is None:
            return rects
        total = self._total_seconds()
        if total <= 0 or self.width() <= 0:
            return rects
        h = self.height()
        # 三排上下對應:H(高)在最上,M 中間,L(低)最下;每排佔 1/3 高度
        row_h = max(2.0, (h - 6) / 3.0)
        row_y_for: dict[str, float] = {
            "H": 3.0,
            "M": 3.0 + row_h,
            "L": 3.0 + row_h * 2,
        }
        for ev in self._sheet.events:
            if ev.is_rest:
                continue
            start_s = self._sheet.beats_to_seconds(ev.start_beats)
            end_s = self._sheet.beats_to_seconds(ev.start_beats + ev.duration_beats)
            x1 = start_s / total * self.width()
            x2 = end_s / total * self.width()
            w = max(2.0, x2 - x1)
            for stroke in ev.strokes:
                prefix = stroke.label[0] if stroke.label else "M"
                if prefix not in rects:
                    continue
                rects[prefix].append((x1, row_y_for[prefix], w, row_h - 1))
        return rects

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, False)
        painter.fillRect(self.rect(), QColor(THEME["panel"]))

        total = self._total_seconds()
        if total <= 0:
            painter.setPen(QColor(THEME["fg_subtle"]))
            painter.drawText(self.rect(), Qt.AlignCenter, "(無譜面)")
            return

        size_key = (self.width(), self.height())
        if self._cached_rects is None or self._cached_for_size != size_key:
            self._cached_rects = self._build_rects()
            self._cached_for_size = size_key

        # H/M/L 各自填色,用半透明 alpha 讓重疊看得到密度
        painter.setPen(Qt.NoPen)
        for prefix in ("L", "M", "H"):
            base = QColor(THEME[prefix])
            base.setAlpha(200)
            painter.setBrush(base)
            for (x, y, w, h) in self._cached_rects.get(prefix, ()):
                painter.fillRect(QRectF(x, y, w, h), base)

        # 可視範圍灰色矩形(畫在事件上層)
        if self._view_end > self._view_start:
            x1 = self._x_for_seconds(self._view_start)
            x2 = self._x_for_seconds(self._view_end)
            view_color = QColor(THEME["fg"])
            view_color.setAlpha(45)
            painter.fillRect(QRectF(x1, 0, max(2.0, x2 - x1), self.height()), view_color)
            painter.setPen(QPen(QColor(THEME["fg_dim"]), 1.0))
            painter.setBrush(Qt.NoBrush)
            painter.drawRect(QRectF(x1 + 0.5, 0.5, max(2.0, x2 - x1) - 1.0, self.height() - 1))

        # 播放區間高亮(畫在 cursor 線之下)
        if (
            self._loop_start_seconds is not None
            and self._loop_end_seconds is not None
            and self._loop_end_seconds > self._loop_start_seconds
        ):
            xa = self._x_for_seconds(self._loop_start_seconds)
            xb = self._x_for_seconds(self._loop_end_seconds)
            loop_fill = QColor(THEME["accent"])
            loop_fill.setAlpha(55)
            painter.fillRect(QRectF(xa, 0, max(1.0, xb - xa), self.height()), loop_fill)
        # 兩個 marker handle:不論成對與否都各自畫,讓使用者看到只設一邊的狀態
        for s in (self._loop_start_seconds, self._loop_end_seconds):
            if s is None:
                continue
            xm = self._x_for_seconds(s)
            painter.setPen(QPen(QColor(THEME["accent"]), 2.0))
            painter.drawLine(int(xm), 0, int(xm), self.height())

        # cursor 紅線(最上層)
        cx = self._x_for_seconds(self._cursor_seconds)
        painter.setPen(QPen(QColor(THEME["cursor"]), 2.0))
        painter.drawLine(int(cx), 0, int(cx), self.height())

    def mousePressEvent(self, event) -> None:  # noqa: N802
        x = event.position().x()
        seconds = self._seconds_at_x(x)

        if event.button() == Qt.RightButton:
            # 右鍵點 marker handle → 清除該 marker
            if self._is_near_marker(x, "start"):
                self._loop_start_seconds = None
                self.loop_range_changed.emit(self._loop_start_seconds, self._loop_end_seconds)
                self.update()
                return
            if self._is_near_marker(x, "end"):
                self._loop_end_seconds = None
                self.loop_range_changed.emit(self._loop_start_seconds, self._loop_end_seconds)
                self.update()
                return
            # 右鍵 press + drag 拉出一段 region。anchor = press 處;
            # 拖到放開時:若 release 距離 anchor < 0.1s 視為單擊(不留 region)。
            self._loop_dragging = True
            self._loop_anchor_seconds = seconds
            self._loop_start_seconds = seconds
            self._loop_end_seconds = seconds
            self.update()
            return

        if event.button() != Qt.LeftButton:
            return super().mousePressEvent(event)

        # 左鍵 hit-test marker handle → 單 marker drag
        if self._is_near_marker(x, "start"):
            self._loop_drag_target = "start"
            return
        if self._is_near_marker(x, "end"):
            self._loop_drag_target = "end"
            return

        # 左鍵 viewport drag(現有行為)
        if self._view_end > self._view_start:
            x1 = self._x_for_seconds(self._view_start)
            x2 = self._x_for_seconds(self._view_end)
            if x1 <= x <= x2:
                self._dragging_view = True
                center = (x1 + x2) / 2.0
                self._drag_offset = x - center
                return
        self.seek_to.emit(seconds)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        x = event.position().x()
        if self._loop_dragging:
            cur = self._seconds_at_x(x)
            if cur >= self._loop_anchor_seconds:
                self._loop_start_seconds = self._loop_anchor_seconds
                self._loop_end_seconds = cur
            else:
                self._loop_start_seconds = cur
                self._loop_end_seconds = self._loop_anchor_seconds
            self.update()
            return
        if self._loop_drag_target == "start":
            new_s = max(0.0, self._seconds_at_x(x))
            if self._loop_end_seconds is not None and new_s > self._loop_end_seconds:
                new_s = self._loop_end_seconds
            self._loop_start_seconds = new_s
            self.update()
            return
        if self._loop_drag_target == "end":
            new_s = max(0.0, self._seconds_at_x(x))
            if self._loop_start_seconds is not None and new_s < self._loop_start_seconds:
                new_s = self._loop_start_seconds
            self._loop_end_seconds = new_s
            self.update()
            return
        if self._dragging_view:
            view_span = self._view_end - self._view_start
            target_center = self._seconds_at_x(x - self._drag_offset)
            new_start = max(0.0, target_center - view_span / 2.0)
            self.view_drag.emit(new_start)
            return
        # hover hint
        if self._is_near_marker(x, "start") or self._is_near_marker(x, "end"):
            self.setCursor(Qt.SizeHorCursor)
            return
        if self._view_end > self._view_start:
            x1 = self._x_for_seconds(self._view_start)
            x2 = self._x_for_seconds(self._view_end)
            if x1 <= x <= x2:
                self.setCursor(Qt.SizeHorCursor)
                return
        self.setCursor(Qt.PointingHandCursor)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        was_loop_drag = self._loop_dragging or self._loop_drag_target is not None
        self._dragging_view = False
        self._loop_dragging = False
        self._loop_drag_target = None
        self._loop_anchor_seconds = 0.0
        if was_loop_drag:
            # 右鍵 press 後沒拖動(< 0.1s)→ 視為單擊,不留 region
            if (
                self._loop_start_seconds is not None
                and self._loop_end_seconds is not None
                and (self._loop_end_seconds - self._loop_start_seconds) < 0.1
            ):
                self._loop_start_seconds = None
                self._loop_end_seconds = None
            self.loop_range_changed.emit(self._loop_start_seconds, self._loop_end_seconds)
            self.update()
        super().mouseReleaseEvent(event)
