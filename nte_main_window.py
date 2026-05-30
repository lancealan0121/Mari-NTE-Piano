# NTE Piano - 自動演奏與自動化工具
# Copyright (C) 2026  Yulun
# Licensed under GPL-3.0-or-later. See LICENSE.
"""nte_main_window — 主視窗 PianoPlayerWindow 與 GUI 行為類常數.

對外提供:
    PianoPlayerWindow  整合工具列 / Piano roll / 鍵盤 / 設定 / 自動化的 QMainWindow.
    APP_TITLE / START_DELAY_SECONDS / PLAYBACK_SPEEDS
    AUTOSAVE_DEBOUNCE_MS / FOCUS_CHECK_INTERVAL_MS / FOCUS_CHECK_GRACE_MS

主檔 piano_player.py 只負責 main() / parse_args() 等 CLI 入口,
GUI 邏輯全部在這個模組.
"""
from __future__ import annotations

import argparse
import bisect
import ctypes
import datetime as _dt
import json
import math
import os
import random
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
import zipfile
from collections import defaultdict
from ctypes import wintypes
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Iterable

from PySide6.QtCore import (
    QElapsedTimer,
    QEasingCurve,
    QEvent,
    QMetaObject,
    QObject,
    QPointF,
    QPropertyAnimation,
    QRectF,
    QSize,
    Qt,
    QThread,
    QTimer,
    QVariantAnimation,
    Q_ARG,
    Signal,
    Slot,
)
from PySide6.QtGui import (
    QAction,
    QActionGroup,
    QColor,
    QCloseEvent,
    QFont,
    QFontMetrics,
    QIcon,
    QKeyEvent,
    QKeySequence,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QRadialGradient,
    QShortcut,
    QTextCursor,
    QWheelEvent,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QAbstractSpinBox,
    QApplication,
    QColorDialog,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDockWidget,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QGraphicsDropShadowEffect,
    QGraphicsOpacityEffect,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListView,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressDialog,
    QPushButton,
    QScrollBar,
    QSizePolicy,
    QSpinBox,
    QStatusBar,
    QStyle,
    QVBoxLayout,
    QWidget,
)

from nte_dsl import (
    BASE_KEYS,
    CHROMATIC_LAYOUT,
    DEFAULT_BEATS_PER_BAR,
    KeyStroke,
    NoteEvent,
    REST_TOKENS,
    ROW_LABELS,
    Sheet,
    SheetParseError,
    SheetParser,
    TRACK_INDEX,
    TRACK_ORDER,
    _fmt_num,
    make_stroke,
)
from nte_playback import (
    GAME_PROCESS_NAME,
    GAME_TITLE_HINT,
    GlobalHotkeys,
    HotkeyBridge,
    KeyBackend,
    PlaybackWorker,
    ScheduledAction,
    WindowInfo,
    create_backend,
    create_backend_with_fallback,
    find_game_window,
    focus_window,
    foreground_hwnd,
    is_game_window,
    is_running_as_admin,
    is_target_foreground,
    is_window_alive,
)
from nte_importers import MidiImporter, MsczImporter, MusicXMLImporter
from nte_import_dialogs import ImportOptionsDialog
from nte_batch_import import (
    BatchImportOptionsDialog,
    BatchImportResult,
    BatchImportResultsDialog,
    SUPPORTED_EXTENSIONS as BATCH_SUPPORTED_EXTENSIONS,
    run_batch_import,
)
from nte_automation import (
    BackgroundAudioMuter,
    HeistController,
)
from nte_checker import NTECheckerProbe, NTECheckerState, NTECheckerWidget
from nte_settings_panel import SettingsPanel, build_panel_qss
from nte_audio import PianoSoundPlayer
from nte_perf import perf, init_perf_from_env
from nte_updater import (
    CheckUpdateTask,
    DownloadUpdateTask,
    UpdateInfo,
    UpdaterProxy,
)
from nte_version import APP_VERSION, GITHUB_REPO_URL

from nte_paths import (
    DSL_EXTENSIONS,
    MIDI_EXTENSIONS,
    MSCZ_EXTENSIONS,
    MUSICXML_EXTENSIONS,
    SETTINGS_DIR,
    SETTINGS_PATH,
    _is_frozen,
    _resource_path,
    _seed_default_songs,
    _user_data_dir,
)
from nte_theme import (
    NOTE_COLOR_STYLES,
    THEME,
    _blend_color,
    _derive_active_color,
    _ease_in_out_sine,
    _ease_out_back,
    _ease_out_quad,
    _radial_alpha_gradient,
    apply_note_color_style,
)
from nte_settings import SettingsManager
from nte_transport_button import _TransportButton
from nte_piano_keyboard import PianoKeyboardWidget
from nte_overview_bar import OverviewBar
from nte_piano_roll import PianoRollView


APP_TITLE = "NTE Piano Auto Player"
START_DELAY_SECONDS = 1.0
PLAYBACK_SPEEDS = (0.5, 0.75, 1.0, 1.25, 1.5)
AUTOSAVE_DEBOUNCE_MS = 5000
FOCUS_CHECK_INTERVAL_MS = 200
FOCUS_CHECK_GRACE_MS = 1500
# 自然播完後游標停點往後多留的秒數,避免剛好壓在最後一個音符上不好看。
# 只在「非主動停止」時套用;手動 stop 仍忠實停在當下位置。
PLAYBACK_FINISH_TAIL_SECONDS = 0.4


class _SmoothWheelComboView(QListView):
    """song_combo popup view:滾輪以 QPropertyAnimation 平滑捲動。

    只覆寫 wheelEvent(沿用 SmoothScrollArea 的累加目標 + OutCubic 模式),
    不碰 palette / 不透明,外觀維持 Qt 預設 combo popup。
    """

    _DURATION_MS = 200
    _STEP_PIXELS = 90

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # QComboBox 預設 view 走 ScrollPerItem:scrollbar value 單位是 item index
        # 而非 pixel。用 pixel 量去推會一次衝到底,且 item 離散插值看不出平滑。改
        # per-pixel 讓 scrollbar 連續,QPropertyAnimation 才能做出平滑捲動。
        self.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self._target_value = 0
        self._anim = QPropertyAnimation(self.verticalScrollBar(), b"value", self)
        self._anim.setDuration(self._DURATION_MS)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)

    def wheelEvent(self, event: QWheelEvent) -> None:
        bar = self.verticalScrollBar()
        # 水平滾動或 modifier(Ctrl 等)交回父類,不攔。
        if event.angleDelta().y() == 0 or event.modifiers() != Qt.NoModifier:
            super().wheelEvent(event)
            return
        # 動畫進行中從當前終點累加,連滾才會疊加而不被舊目標卡住。
        if self._anim.state() == QPropertyAnimation.Running:
            start_value = self._target_value
        else:
            start_value = bar.value()
        pixels = int(round(event.angleDelta().y() / 120.0 * self._STEP_PIXELS))
        new_target = max(bar.minimum(), min(bar.maximum(), start_value - pixels))
        self._target_value = new_target
        self._anim.stop()
        self._anim.setStartValue(bar.value())
        self._anim.setEndValue(new_target)
        self._anim.start()
        event.accept()


class PianoPlayerWindow(QMainWindow):
    AUTOSAVE_PATH = _user_data_dir("autosave.txt")
    IMPORT_LOG_PATH = _user_data_dir("logs/import_error.log")

    # 高頻 slider 類 setting key:_on_panel_setting_changed 走 defer_set + 300ms flush。
    # 其他 toggle/combo key 立刻寫盤,維持原本的「按一下就落地」語意。
    _SETTINGS_DEBOUNCE_KEYS = frozenset({
        "playback_speed", "zoom_factor", "countdown_seconds",
        "piano_sound_volume",
    })

    def __init__(self, initial_file=None):
        super().__init__()
        self._current_file = None
        self._dirty = False
        self._thread = None
        self._worker = None
        self._sheet = None
        self._last_playback_stopped = False
        # 匯入 (MXL/MIDI/MSCZ) 後記錄原始檔名,讓「另存新檔」對話框自動帶入。
        # 一旦使用者實際儲存或載入了既有檔,就清掉,避免下次又拿到舊的名字。
        self._imported_source_name: str | None = None
        # 匯入但尚未存檔時,在曲目下拉顯示「(未存檔) 標題」的臨時項。
        # None = 沒有未存檔的匯入;字串 = 顯示用標題(不含前綴)。
        # 真正落盤(_save_file) / 切到其他歌 / 刪除 / 重新匯入都會清掉這個 state。
        self._unsaved_combo_title: str | None = None
        self._paused = False
        self._auto_paused = False
        self._suppress_sheet_field_signals = False
        # _suppress_text_signal / _loading_text 隨譜面文字編輯器移除
        self._is_admin = is_running_as_admin()
        self._undo_stack: list[str] = []
        self._redo_stack: list[str] = []
        self._undo_limit = 100
        self._game_hwnd: int | None = None
        self._self_hwnd_cache: int | None = None

        self._settings = SettingsManager()
        self._playback_speed = float(self._settings.get("playback_speed", 1.0))
        self._auto_pause_on_focus_loss = bool(self._settings.get("auto_pause_on_focus_loss", True))
        self._focus_game_on_play = bool(self._settings.get("focus_game_on_play", True))
        # 播放區間(由 OverviewBar marker 設定);worker 啟動時生效,running 中不變。
        # 換譜面時清空(_refresh_sheet_from_text 跟 _load_file 結尾會 reset)。
        self._loop_start_seconds: float | None = None
        self._loop_end_seconds: float | None = None
        # _confirm_discard_unsaved / _autosave_restore_prompt 已隨譜面文字編輯器移除
        zoom_factor = float(self._settings.get("zoom_factor", 1.0))
        self._note_color_style = apply_note_color_style(
            str(self._settings.get("note_color_style", "default")),
            self._read_custom_note_colors(),
        )
        self._import_tempo_changes = bool(self._settings.get("import_tempo_changes", True))
        self._pitch_sort_mode = bool(self._settings.get("pitch_sort_mode", False))
        self._roll_fps = int(self._settings.get("roll_fps", 60))
        if self._roll_fps not in (30, 60, 120):
            self._roll_fps = 60
        self._show_piano_keyboard = bool(self._settings.get("show_piano_keyboard", True))
        # 本機鋼琴音色 player:跟著 PlaybackWorker.note_pressed signal 出聲。
        # 預先聆聽模式(preview_mode)時 worker 走 silent_mode,不送鍵但仍 emit
        # note_pressed,因此這個 player 是「聽得到自己排的譜」唯一通道。
        self._preview_mode = bool(self._settings.get("preview_mode", False))
        self._sound_player = PianoSoundPlayer(_resource_path("assets/sounds/piano"))
        self._sound_player.set_enabled(bool(self._settings.get("piano_sound_enabled", True)))
        self._sound_player.set_volume(float(self._settings.get("piano_sound_volume", 0.7)))

        # 高頻 slider 類設定的寫盤 debounce(300ms);拖 slider 連續變動時暫存,
        # 停手後一次 atomic-write settings.json。closeEvent / _play_sheet 前都會 flush。
        self._settings_flush_timer = QTimer(self)
        self._settings_flush_timer.setSingleShot(True)
        self._settings_flush_timer.setInterval(300)
        self._settings_flush_timer.timeout.connect(self._on_settings_flush_timer)
        # 自動更新 worker:threading.Thread + main-thread QObject proxy,跨執行緒
        # signal 走 QueuedConnection。
        self._updater_proxy = UpdaterProxy()
        self._update_check_task: CheckUpdateTask | None = None
        self._update_download_task: DownloadUpdateTask | None = None
        self._update_progress_dialog: QProgressDialog | None = None
        self._pending_update_info: UpdateInfo | None = None
        self._manual_update_check_pending: bool = False
        # 失焦自動靜音 muter:獨立於 task,跟 task 並行運作,使用者切到別的視窗時
        # 自動把 HTGame.exe 設 mute。pycaw 缺套件時 BackgroundAudioMuter
        # is_available() 為 False,GUI 端會 disable 對應選項。
        self._bg_audio_muter = BackgroundAudioMuter(
            log_callback=lambda msg: self._append_automation_log(msg),
        )

        # 粉爪大劫案常駐 helper:跟失焦自動靜音不互斥,各自獨立 daemon thread。
        # status 訊息走自動化 log dock。滾輪固定啟用,不再拆獨立開關(整個
        # 功能就叫「快速拾取」,滾輪是其中一部分)。
        # 全自動子設定由設定面板維護。
        # auto_mode_changed_callback 由 controller 線程觸發 toggle 後呼叫,
        # GUI 端落盤 + 顯示狀態列訊息(Qt slot 內呼叫,要透過 QMetaObject 跨執行緒)。
        self._heist_controller = HeistController(
            pickup_enabled=bool(self._settings.get("heist_enabled", False)),
            trigger_key=str(self._settings.get("heist_trigger_key", "f")),
            use_scroll=True,
            auto_mode=bool(self._settings.get("heist_auto_mode", False)),
            auto_mode_hotkey=str(self._settings.get("heist_auto_mode_hotkey", "f8")),
            status_callback=lambda msg: self._append_automation_log(msg),
            auto_mode_changed_callback=lambda v: QMetaObject.invokeMethod(
                self,
                "_on_heist_auto_mode_changed_from_thread",
                Qt.QueuedConnection,
                Q_ARG(bool, bool(v)),
            ),
        )

        self._bridge = HotkeyBridge()
        self._hotkeys = GlobalHotkeys(self._bridge)

        self._build_ui()
        self.piano_roll.set_zoom_factor(zoom_factor)
        self.piano_roll.set_playback_speed(self._playback_speed)
        self.piano_roll.set_pitch_sort_mode(self._pitch_sort_mode)
        self.piano_roll.set_fps(self._roll_fps)
        # 平滑捲動/縮放已併入「動畫效果」總開關,初始狀態跟 _animations_enabled 一致。
        self.piano_roll.set_smooth_browse_enabled(self._animations_enabled)
        self.piano_roll.set_smooth_zoom_enabled(self._animations_enabled)
        # 動畫總開關初值(可能來自 settings)套用到 widget。
        self.piano_keyboard.set_animations_enabled(self._animations_enabled)
        self.piano_roll.set_animations_enabled(self._animations_enabled)
        self.piano_keyboard.setVisible(self._show_piano_keyboard)

        # NTE Checker probe:1Hz 主執行緒 tick,推狀態給底部 dock 的 widget。
        self._nte_probe = NTECheckerProbe(self, interval_ms=1000)
        self._nte_probe.state_changed.connect(
            self.nte_checker_widget.apply_state, Qt.QueuedConnection
        )
        self._nte_probe.start()
        self._wire_actions()
        self._populate_song_combo()
        self.setAcceptDrops(True)

        # autosave_timer 已隨譜面文字編輯器一起移除。

        self._focus_check_timer = QTimer(self)
        self._focus_check_timer.setInterval(FOCUS_CHECK_INTERVAL_MS)
        self._focus_check_timer.timeout.connect(self._on_focus_check_tick)

        self._load_startup_score(initial_file)

        self._bridge.play_requested.connect(self.start_playback, Qt.QueuedConnection)
        self._bridge.stop_requested.connect(self.stop_playback, Qt.QueuedConnection)
        self._bridge.pause_requested.connect(self.toggle_pause, Qt.QueuedConnection)

        ok, message = self._hotkeys.start(
            hotkey_map=self._current_hotkey_map(),
            automation_enabled=True,
        )
        admin_note = "管理員" if self._is_admin else "非管理員"
        suffix = "" if self._is_admin else "．若遊戲不接受輸入請改用 run.bat"
        self.statusBar().showMessage(f"{message}（{admin_note}{suffix}）", 6000)

        # 依設定啟動失焦自動靜音(pycaw 缺套件時 start() 內部已處理回 False)。
        if bool(self._settings.get("mute_on_focus_loss", False)) and BackgroundAudioMuter.is_available():
            self._bg_audio_muter.start()

        # 依設定啟動粉爪大劫案 controller。失敗時 rollback settings + 同步面板。
        if bool(self._settings.get("heist_enabled", False)):
            self._sync_heist_controller_config()
            if not self._heist_controller.start():
                self._settings.set("heist_enabled", False)
                if hasattr(self, "_settings_panel"):
                    self._settings_panel.refresh_from_settings()

        # 啟動 5 秒後背景查 GitHub Releases,UI 此時已經顯示完;6 小時節流寫在
        # _maybe_auto_check_update 內,跳過時不會打 API。
        QTimer.singleShot(5000, self._maybe_auto_check_update)

    def _build_ui(self) -> None:
        self.setWindowTitle(f"{APP_TITLE} v{APP_VERSION}")
        self.resize(1280, 760)
        self.setStyleSheet(self._stylesheet())
        self._animations_enabled = bool(self._settings.get("animations_enabled", True))
        self._dock_animation = None
        self._dock_natural_width = 420

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(12, 10, 12, 8)
        layout.setSpacing(8)

        layout.addLayout(self._build_toolbar())

        self.piano_roll = PianoRollView()
        self.overview_bar = OverviewBar()
        self.scrollbar = QScrollBar(Qt.Horizontal)
        self.scrollbar.setObjectName("rollScrollbar")
        roll_container = QWidget()
        roll_box = QVBoxLayout(roll_container)
        roll_box.setContentsMargins(0, 0, 0, 0)
        roll_box.setSpacing(2)
        roll_box.addWidget(self.overview_bar)
        roll_box.addWidget(self.piano_roll, 1)
        roll_box.addWidget(self.scrollbar)
        layout.addWidget(roll_container, 1)

        self.piano_keyboard = PianoKeyboardWidget()
        layout.addWidget(self.piano_keyboard)

        self.setCentralWidget(central)
        self.setStatusBar(QStatusBar())
        self._playback_status = QLabel("")
        self._playback_status.setObjectName("playbackStatus")
        self._playback_status.setMinimumWidth(120)
        self.statusBar().addPermanentWidget(self._playback_status)

        self.editor = None  # 譜面文字編輯器已移除;留 attribute 防舊程式碼引用 NameError
        editor_container = QWidget()
        editor_layout = QVBoxLayout(editor_container)
        editor_layout.setContentsMargins(0, 0, 0, 0)
        editor_layout.setSpacing(0)
        editor_layout.addWidget(self._build_settings_panel())
        editor_layout.addWidget(self._build_settings_dock_panel(), 1)

        self._editor_dock = QDockWidget("設定", self)
        self._editor_dock.setObjectName("settingsDock")
        self._editor_dock.setWidget(editor_container)
        self._editor_dock.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)
        self._editor_dock.setFeatures(QDockWidget.DockWidgetClosable)
        self._editor_dock.setFixedWidth(self._dock_natural_width)
        self.addDockWidget(Qt.RightDockWidgetArea, self._editor_dock)
        self._editor_dock.hide()
        self._editor_dock.visibilityChanged.connect(self._on_dock_visibility)

        self._build_automation_dock()

    def _build_automation_dock(self) -> None:
        """底部 dock:NTE Checker 燈號條 + 自動化即時 log。"""
        container = QWidget()
        container.setObjectName("automationDockContent")
        vbox = QVBoxLayout(container)
        vbox.setContentsMargins(0, 0, 0, 0)
        vbox.setSpacing(0)

        self.nte_checker_widget = NTECheckerWidget(container)
        vbox.addWidget(self.nte_checker_widget)

        self.automation_log = QPlainTextEdit(container)
        self.automation_log.setReadOnly(True)
        self.automation_log.setMaximumBlockCount(1000)
        self.automation_log.setObjectName("automationLog")
        log_font = QFont("Cascadia Mono", 10)
        log_font.setStyleHint(QFont.Monospace)
        self.automation_log.setFont(log_font)
        self.automation_log.setPlaceholderText(
            "自動化 log 會顯示在這裡(可按 Ctrl+L 顯示/隱藏此區)"
        )
        vbox.addWidget(self.automation_log, 1)

        self._automation_dock = QDockWidget("自動化監控", self)
        self._automation_dock.setObjectName("automationDock")
        self._automation_dock.setWidget(container)
        self._automation_dock.setAllowedAreas(
            Qt.BottomDockWidgetArea | Qt.TopDockWidgetArea
        )
        self._automation_dock.setFeatures(
            QDockWidget.DockWidgetClosable
            | QDockWidget.DockWidgetMovable
            | QDockWidget.DockWidgetFloatable
        )
        self._automation_dock.setMinimumHeight(120)
        self.addDockWidget(Qt.BottomDockWidgetArea, self._automation_dock)
        if not bool(self._settings.get("automation_dock_visible", True)):
            self._automation_dock.hide()

    def _build_toolbar(self) -> QHBoxLayout:
        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)

        self.file_button = QPushButton("檔案")
        self.file_button.setObjectName("fileButton")
        self.file_button.setMinimumWidth(96)
        self.file_button.setToolTip("檔案操作（開啟 / 儲存 / 匯入 / 自動化）")
        self.file_menu = QMenu(self.file_button)
        self.act_new = self.file_menu.addAction("新增空樂譜\tCtrl+N")
        self.act_open = self.file_menu.addAction("開啟…\tCtrl+O")
        self.file_menu.addSeparator()
        self.act_save = self.file_menu.addAction("儲存\tCtrl+S")
        self.act_save_as = self.file_menu.addAction("另存新檔…\tCtrl+Shift+S")
        self.file_menu.addSeparator()
        self.act_import = self.file_menu.addAction("匯入…\tCtrl+I")
        self.act_import_batch = self.file_menu.addAction("批量匯入…\tCtrl+Shift+I")
        self.file_menu.addSeparator()
        self.act_delete_song = self.file_menu.addAction("刪除目前歌曲…")
        self.file_menu.addSeparator()
        # 大多數一般設定(動畫/失焦/聚焦/音高排序/顯示鍵盤/FPS/音色)都搬進
        # 右側設定面板(Ctrl+E)。這裡只留無法以面板替代的 runtime 操作。
        self.act_auto_dock = self.file_menu.addAction("顯示自動化監控\tCtrl+L")
        self.act_auto_dock.setCheckable(True)
        self.act_auto_dock.setChecked(
            bool(self._settings.get("automation_dock_visible", True))
        )
        # 說明區塊:檢查更新、自動檢查 toggle、關於對話框。
        # 放在 file_menu 內(不另開頂層按鈕)以維持工具列既有視覺。
        self.file_menu.addSeparator()
        self.act_check_update = self.file_menu.addAction("檢查更新…")
        self.act_auto_update_check = self.file_menu.addAction("啟動時自動檢查更新")
        self.act_auto_update_check.setCheckable(True)
        self.act_auto_update_check.setChecked(
            bool(self._settings.get("auto_update_check", True))
        )
        self.act_about = self.file_menu.addAction("關於 NTE Piano…")
        self.file_button.setMenu(self.file_menu)
        toolbar.addWidget(self.file_button)

        # Transport 區:純文字按鈕,無框無背景。
        # 播放/暫停整合到同一個按鈕(self.play_button),依播放狀態切換顯示
        # 「播放」/「暫停」/「繼續」三種文字。
        transport_frame = QFrame()
        transport_frame.setObjectName("transportGroup")
        transport_row = QHBoxLayout(transport_frame)
        transport_row.setContentsMargins(0, 0, 0, 0)
        transport_row.setSpacing(2)
        transport_font = QFont()
        transport_font.setPointSize(11)
        transport_font.setBold(True)

        self.prev_button = _TransportButton("prev", "prevButton", "跳到開頭", transport_font)
        self.play_button = _TransportButton("play", "playButton", "播放 (F6)", transport_font)
        self.stop_button = _TransportButton("stop", "stopButton", "停止 (F7)", transport_font)
        self.next_button = _TransportButton("next", "nextButton", "跳到結尾", transport_font)
        self.stop_button.setEnabled(False)
        transport_row.addWidget(self.prev_button)
        transport_row.addWidget(self.play_button)
        transport_row.addWidget(self.stop_button)
        transport_row.addWidget(self.next_button)
        toolbar.addWidget(transport_frame)

        toolbar.addSpacing(10)

        speed_label = QLabel("速度")
        speed_label.setObjectName("toolLabel")
        toolbar.addWidget(speed_label)
        self.speed_combo = QComboBox()
        for s in PLAYBACK_SPEEDS:
            self.speed_combo.addItem(f"{s:g}×", s)
        idx = next(
            (i for i, s in enumerate(PLAYBACK_SPEEDS) if abs(s - self._playback_speed) < 1e-3),
            PLAYBACK_SPEEDS.index(1.0),
        )
        self.speed_combo.setCurrentIndex(idx)
        self.speed_combo.setToolTip("播放速度（下次播放生效）")
        toolbar.addWidget(self.speed_combo)

        toolbar.addSpacing(14)

        song_label = QLabel("曲目")
        song_label.setObjectName("toolLabel")
        toolbar.addWidget(song_label)
        self.song_combo = QComboBox()
        self.song_combo.setMinimumWidth(240)
        self.song_combo.setView(_SmoothWheelComboView(self.song_combo))
        toolbar.addWidget(self.song_combo)
        self.song_refresh_button = QPushButton("↻")
        self.song_refresh_button.setObjectName("songRefreshButton")
        self.song_refresh_button.setToolTip("刷新曲目清單（重新掃描 songs/ 資料夾）")
        self.song_refresh_button.setFixedWidth(32)
        toolbar.addWidget(self.song_refresh_button)

        toolbar.addStretch(1)

        self.now_label = QLabel("")
        self.now_label.setObjectName("nowLabel")
        toolbar.addWidget(self.now_label)

        toolbar.addSpacing(8)
        self.edit_button = QPushButton("設定  Ctrl+E")
        self.edit_button.setObjectName("editButton")
        self.edit_button.setCheckable(True)
        self.edit_button.setToolTip("開啟右側設定面板(自動化開關 / 視覺 / 焦點 / 匯入等)")
        toolbar.addWidget(self.edit_button)
        return toolbar

    def _stylesheet(self) -> str:
        t = THEME
        return f"""
QMainWindow, QWidget {{ background-color: {t['bg']}; color: {t['fg']}; font-family: "Microsoft JhengHei UI", "Segoe UI", sans-serif; }}
QWidget#settingsPanel {{ background-color: {t['panel']}; border-bottom: 1px solid {t['grid']}; }}
QLabel {{ color: {t['fg']}; }}
QLabel#toolLabel {{ color: {t['fg_dim']}; padding-right: 4px; background: transparent; }}
QLabel#nowLabel {{ color: {t['fg_dim']}; font-family: "Cascadia Mono", "Consolas", monospace; }}
QLabel#playbackStatus {{ color: {t['fg_dim']}; font-family: "Cascadia Mono", "Consolas", monospace; padding-right: 8px; }}
QPushButton {{ background-color: {t['panel_alt']}; color: {t['fg']}; border: 1px solid {t['grid_strong']}; padding: 7px 14px; border-radius: 6px; }}
QPushButton:hover {{ background-color: #2a2e36; border: 1px solid {t['accent']}; }}
QPushButton:pressed {{ background-color: #16191f; border: 1px solid {t['accent']}; }}
QPushButton:disabled {{ color: {t['fg_subtle']}; border: 1px solid {t['grid']}; background-color: #1d2027; }}
QPushButton:checked {{ background-color: {t['M']}; color: #16181d; border: 1px solid {t['M_active']}; font-weight: 600; }}
QPushButton:checked:hover {{ background-color: {t['M_active']}; }}
QPushButton#songRefreshButton {{ padding: 7px 0px; font-size: 14px; font-weight: 700; }}
QPushButton#playButton, QPushButton#stopButton, QPushButton#prevButton, QPushButton#nextButton {{
    background-color: transparent; border: none; padding: 6px 14px; color: {t['fg']}; font-weight: 700;
}}
QPushButton#playButton:hover, QPushButton#stopButton:hover, QPushButton#prevButton:hover, QPushButton#nextButton:hover {{
    background-color: transparent; border: none; color: {t['accent']};
}}
QPushButton#playButton:pressed, QPushButton#stopButton:pressed, QPushButton#prevButton:pressed, QPushButton#nextButton:pressed {{
    background-color: transparent; border: none; color: {t['accent']};
}}
QPushButton#playButton:disabled, QPushButton#stopButton:disabled, QPushButton#prevButton:disabled, QPushButton#nextButton:disabled {{
    background-color: transparent; border: none; color: {t['fg_subtle']};
}}
QPushButton#stopButton {{ color: {t['stop']}; }}
QFrame#transportGroup {{ background-color: transparent; border: none; }}
QPushButton#fileButton::menu-indicator {{ image: none; width: 0; }}
QComboBox {{ background-color: {t['panel_alt']}; color: {t['fg']}; border: 1px solid {t['grid_strong']}; padding: 5px 10px; border-radius: 6px; }}
QComboBox:hover {{ border: 1px solid {t['accent']}; }}
QComboBox:focus {{ border: 1px solid {t['grid_strong']}; }}
QComboBox::drop-down {{ border: none; width: 18px; }}
QComboBox QAbstractItemView {{ background-color: {t['panel']}; color: {t['fg']}; selection-background-color: {t['accent']}; selection-color: {t['bg']}; border: 1px solid {t['grid_strong']}; padding: 4px; }}
QStatusBar {{ background-color: {t['panel']}; color: {t['fg_dim']}; border-top: 1px solid {t['grid']}; }}
QPlainTextEdit {{ background-color: {t['panel']}; color: {t['fg']}; border: none; padding: 8px; selection-background-color: #3a4250; selection-color: {t['fg']}; }}
QDockWidget {{ color: {t['fg']}; }}
QDockWidget::title {{ background-color: {t['panel_alt']}; padding: 6px 10px; border: none; border-bottom: 1px solid {t['grid']}; }}
QDoubleSpinBox, QSpinBox {{ background-color: {t['panel_alt']}; color: {t['fg']}; border: 1px solid {t['grid_strong']}; border-radius: 4px; padding: 3px 6px; }}
QDoubleSpinBox:hover, QSpinBox:hover {{ border: 1px solid {t['accent']}; }}
QDoubleSpinBox:focus, QSpinBox:focus {{ border: 1px solid {t['accent']}; }}

QScrollBar:horizontal {{
    background: {t['panel']};
    height: 12px;
    border: none;
    margin: 0 14px 0 14px;
    border-radius: 6px;
}}
QScrollBar::handle:horizontal {{
    background: {t['panel_alt']};
    min-width: 32px;
    border-radius: 5px;
    margin: 2px 0 2px 0;
    border: 1px solid {t['grid_strong']};
}}
QScrollBar::handle:horizontal:hover {{ background: #353a44; border-color: {t['accent']}; }}
QScrollBar::handle:horizontal:pressed {{ background: {t['accent']}; border-color: {t['accent']}; }}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
    width: 12px; background: transparent; border: none; subcontrol-origin: margin;
}}
QScrollBar::add-line:horizontal {{ subcontrol-position: right; }}
QScrollBar::sub-line:horizontal {{ subcontrol-position: left; }}
QScrollBar::up-arrow:horizontal, QScrollBar::down-arrow:horizontal,
QScrollBar::left-arrow:horizontal, QScrollBar::right-arrow:horizontal {{
    background: none; border: none; width: 0; height: 0;
}}
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{ background: transparent; }}

QScrollBar:vertical {{
    background: {t['panel']};
    width: 12px;
    border: none;
    margin: 14px 0 14px 0;
    border-radius: 6px;
}}
QScrollBar::handle:vertical {{
    background: {t['panel_alt']};
    min-height: 32px;
    border-radius: 5px;
    margin: 0 2px 0 2px;
    border: 1px solid {t['grid_strong']};
}}
QScrollBar::handle:vertical:hover {{ background: #353a44; border-color: {t['accent']}; }}
QScrollBar::handle:vertical:pressed {{ background: {t['accent']}; border-color: {t['accent']}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 12px; background: transparent; border: none; subcontrol-origin: margin;
}}
QScrollBar::add-line:vertical {{ subcontrol-position: bottom; }}
QScrollBar::sub-line:vertical {{ subcontrol-position: top; }}
QScrollBar::up-arrow:vertical, QScrollBar::down-arrow:vertical {{
    background: none; border: none; width: 0; height: 0;
}}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
"""

    def _wire_actions(self) -> None:
        # play_button 一鍵切換:未播放 → 開始播放;播放中 → 暫停;暫停中 → 繼續。
        # 文字標籤由 _refresh_play_button 依狀態同步,維持「同一個區域」變化的視覺。
        self.play_button.clicked.connect(self._on_play_button_clicked)
        self.stop_button.clicked.connect(self.stop_playback)
        self.prev_button.clicked.connect(self.seek_to_start)
        self.next_button.clicked.connect(self.seek_to_end)
        self.act_open.triggered.connect(self.open_score)
        self.act_new.triggered.connect(self.new_score)
        self.act_save.triggered.connect(self.save_score)
        self.act_save_as.triggered.connect(self.save_score_as)
        self.act_import.triggered.connect(self.import_score)
        self.act_import_batch.triggered.connect(self.import_score_batch)
        self.act_delete_song.triggered.connect(self.delete_current_song)
        self.act_auto_dock.toggled.connect(self._on_automation_dock_toggled)
        self._automation_dock.visibilityChanged.connect(
            self._on_automation_dock_visibility
        )
        # 自動更新:CheckUpdateTask / DownloadUpdateTask 在 worker thread,
        # signal 走 QueuedConnection 回 main thread。
        self._updater_proxy.check_finished.connect(
            self._on_update_check_finished, Qt.QueuedConnection
        )
        self._updater_proxy.download_progress.connect(
            self._on_update_download_progress, Qt.QueuedConnection
        )
        self._updater_proxy.download_finished.connect(
            self._on_update_download_finished, Qt.QueuedConnection
        )
        self._updater_proxy.failed.connect(
            self._on_update_failed, Qt.QueuedConnection
        )
        self.act_check_update.triggered.connect(self._on_check_update_clicked)
        self.act_about.triggered.connect(self._show_about_dialog)
        self.act_auto_update_check.toggled.connect(
            self._on_auto_update_check_toggled
        )
        QShortcut(QKeySequence("Ctrl+L"), self, activated=self._toggle_automation_dock)
        self.speed_combo.activated.connect(self._on_speed_selected)
        self.edit_button.toggled.connect(self._set_dock_visible)
        self.song_combo.activated.connect(self._on_song_selected)
        self.song_refresh_button.clicked.connect(self._on_refresh_songs)
        # 譜面文字編輯器已移除;原本 editor.textChanged → _on_text_changed 也跟著拿掉。
        self.piano_keyboard.note_clicked.connect(self._on_note_clicked)
        self.piano_roll.event_moved.connect(self._on_event_moved)
        self.piano_roll.events_moved.connect(self._on_events_moved)
        self.piano_roll.event_double_clicked.connect(self._on_event_double_clicked)
        self.piano_roll.event_delete_requested.connect(self._on_event_delete_requested)
        self.piano_roll.events_delete_requested.connect(self._on_events_delete_requested)
        self.piano_roll.note_insert_requested.connect(self._on_note_insert_requested)
        self.piano_roll.copy_requested.connect(self._on_copy_shortcut)
        self.piano_roll.cut_requested.connect(self._on_cut_requested)
        self.piano_roll.paste_requested.connect(self._on_paste_shortcut)
        self.piano_roll.timeline_changed.connect(self._on_timeline_changed)
        self.piano_roll.seek_requested.connect(self._on_seek_requested)
        self.piano_roll.zoom_changed.connect(self._on_zoom_changed)
        self.piano_roll.tempo_change_set.connect(self._on_tempo_change_set)
        self.piano_roll.tempo_change_remove.connect(self._on_tempo_change_remove)
        self.piano_roll.chord_stroke_extracted.connect(self._on_chord_stroke_extracted)
        self.scrollbar.valueChanged.connect(self._on_scrollbar_changed)
        self.overview_bar.seek_to.connect(self._on_overview_seek)
        self.overview_bar.view_drag.connect(self._on_overview_view_drag)
        self.overview_bar.loop_range_changed.connect(self._on_loop_range_changed)
        self.piano_roll.loop_range_changed.connect(self._on_loop_range_changed)

        for shortcut, slot in (
            ("F6", self.start_playback),
            ("F7", self.stop_playback),
            ("F8", self.toggle_pause),
            ("Ctrl+E", self._toggle_dock),
            ("Ctrl+S", self.save_score),
            ("Ctrl+Shift+S", self.save_score_as),
            ("Ctrl+O", self.open_score),
            ("Ctrl+N", self.new_score),
            ("Ctrl+I", self.import_musicxml),
            ("Ctrl+Shift+I", self.import_score_batch),
            ("Esc", self._close_dock),
        ):
            action = QAction(self)
            action.setShortcut(QKeySequence(shortcut))
            action.triggered.connect(slot)
            self.addAction(action)

        # 在 piano_roll 上綁定 Ctrl+C/V/Z/Y(WidgetShortcut 不影響編輯器內建)
        for keyseq, slot in (
            ("Ctrl+C", self._on_copy_shortcut),
            ("Ctrl+V", self._on_paste_shortcut),
            ("Ctrl+Z", self._undo),
            ("Ctrl+Shift+Z", self._redo),
            ("Ctrl+Y", self._redo),
            ("Ctrl+A", self._select_all_visible),
        ):
            sc = QShortcut(QKeySequence(keyseq), self.piano_roll)
            sc.setContext(Qt.WidgetShortcut)
            sc.activated.connect(slot)

    @Slot(bool)
    def _set_dock_visible(self, visible: bool) -> None:
        # 編輯隨時可開,worker 跑的是已 build 的 schedule,改 sheet 不影響當下這次播放
        dock = self._editor_dock
        target_w = self._dock_natural_width
        prev_running = (
            self._dock_animation is not None
            and self._dock_animation.state() == QVariantAnimation.Running
        )
        if prev_running:
            self._dock_animation.stop()
        if not self._animations_enabled:
            dock.setGraphicsEffect(None)
            dock.setFixedWidth(target_w)
            if visible:
                dock.show()
                dock.setFocus()
            else:
                dock.hide()
            return

        opacity = QGraphicsOpacityEffect(dock)
        dock.setGraphicsEffect(opacity)

        if visible:
            dock.setFixedWidth(0)
            opacity.setOpacity(0.0)
            dock.show()
            dock.setFocus()

            anim = QVariantAnimation(self)
            anim.setDuration(260)
            anim.setStartValue(0.0)
            anim.setEndValue(1.0)
            anim.setEasingCurve(QEasingCurve.OutCubic)

            def _on_value(v):
                f = float(v)
                w = max(1, int(target_w * f))
                dock.setFixedWidth(w)
                opacity.setOpacity(min(1.0, f * 1.4))

            def _finalize():
                dock.setFixedWidth(target_w)
                dock.setGraphicsEffect(None)

            anim.valueChanged.connect(_on_value)
            anim.finished.connect(_finalize)
            self._dock_animation = anim
            anim.start()
        else:
            current_w = dock.width() if dock.width() > 0 else target_w
            opacity.setOpacity(1.0)

            anim = QVariantAnimation(self)
            anim.setDuration(200)
            anim.setStartValue(1.0)
            anim.setEndValue(0.0)
            anim.setEasingCurve(QEasingCurve.InCubic)

            def _on_value(v):
                f = float(v)
                w = max(1, int(current_w * f))
                dock.setFixedWidth(w)
                opacity.setOpacity(f)

            def _finalize():
                dock.hide()
                dock.setFixedWidth(target_w)
                dock.setGraphicsEffect(None)

            anim.valueChanged.connect(_on_value)
            anim.finished.connect(_finalize)
            self._dock_animation = anim
            anim.start()

    @Slot()
    def _toggle_dock(self) -> None:
        new_state = not self._editor_dock.isVisible()
        self._set_dock_visible(new_state)

    @Slot()
    def _close_dock(self) -> None:
        if self._editor_dock.isVisible():
            self._set_dock_visible(False)

    @Slot(bool)
    def _on_dock_visibility(self, visible: bool) -> None:
        if self.edit_button.isChecked() != visible:
            self.edit_button.blockSignals(True)
            self.edit_button.setChecked(visible)
            self.edit_button.blockSignals(False)

    def _build_settings_panel(self) -> QWidget:
        panel = QWidget()
        panel.setObjectName("settingsPanel")
        outer = QVBoxLayout(panel)
        outer.setContentsMargins(8, 8, 8, 6)
        outer.setSpacing(4)

        title = QLabel("曲目設定")
        title.setObjectName("toolLabel")
        outer.addWidget(title)

        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(4)

        self.tempo_spin = QDoubleSpinBox()
        self.tempo_spin.setRange(20.0, 400.0)
        self.tempo_spin.setSingleStep(1.0)
        self.tempo_spin.setDecimals(0)
        self.tempo_spin.setSuffix(" BPM")
        self.tempo_spin.valueChanged.connect(lambda v: self._set_sheet_field("tempo", v))

        self.beat_spin = QDoubleSpinBox()
        self.beat_spin.setRange(0.0625, 4.0)
        self.beat_spin.setSingleStep(0.125)
        self.beat_spin.setDecimals(3)
        self.beat_spin.valueChanged.connect(lambda v: self._set_sheet_field("beat", v))

        self.gap_spin = QDoubleSpinBox()
        self.gap_spin.setRange(0.0, 1.0)
        self.gap_spin.setSingleStep(0.005)
        self.gap_spin.setDecimals(3)
        self.gap_spin.setSuffix(" 秒")
        self.gap_spin.valueChanged.connect(lambda v: self._set_sheet_field("gap", v))

        self.hold_spin = QDoubleSpinBox()
        self.hold_spin.setRange(0.05, 1.0)
        self.hold_spin.setSingleStep(0.05)
        self.hold_spin.setDecimals(2)
        self.hold_spin.valueChanged.connect(lambda v: self._set_sheet_field("hold", v))

        self.mod_spin = QDoubleSpinBox()
        self.mod_spin.setRange(0.0, 0.2)
        self.mod_spin.setSingleStep(0.005)
        self.mod_spin.setDecimals(3)
        self.mod_spin.setSuffix(" 秒")
        self.mod_spin.valueChanged.connect(lambda v: self._set_sheet_field("modifier_delay", v))

        # A/B 點(播放起點/終點,秒)。min = -1 視為「未設定」(用 setSpecialValueText
        # 顯示「未設定」),正值 = 該秒;更新時 _set_sheet_field("play_range_*_seconds")
        # 把 -1 還原成 None 寫進 sheet。
        self.play_a_spin = QDoubleSpinBox()
        self.play_a_spin.setRange(-1.0, 99999.0)
        self.play_a_spin.setSingleStep(0.1)
        self.play_a_spin.setDecimals(2)
        self.play_a_spin.setSuffix(" 秒")
        self.play_a_spin.setSpecialValueText("未設定")
        self.play_a_spin.valueChanged.connect(
            lambda v: self._set_sheet_field("play_range_start_seconds", v)
        )

        self.play_b_spin = QDoubleSpinBox()
        self.play_b_spin.setRange(-1.0, 99999.0)
        self.play_b_spin.setSingleStep(0.1)
        self.play_b_spin.setDecimals(2)
        self.play_b_spin.setSuffix(" 秒")
        self.play_b_spin.setSpecialValueText("未設定")
        self.play_b_spin.valueChanged.connect(
            lambda v: self._set_sheet_field("play_range_end_seconds", v)
        )

        for row, (text, widget, tip) in enumerate(
            (
                ("BPM", self.tempo_spin, "速度(每分鐘拍數)"),
                ("拍長", self.beat_spin, "每個音符的預設長度(以拍為單位)"),
                ("間隙", self.gap_spin, "音符間最小間隙(秒)"),
                ("持續", self.hold_spin, "按住比例(佔該音符長度)"),
                ("修飾延遲", self.mod_spin, "Shift/Ctrl 修飾鍵延遲(秒)"),
                ("起點A", self.play_a_spin, "播放起點(秒);未設定 = 從頭播"),
                ("終點B", self.play_b_spin, "播放終點(秒);未設定 = 播到尾"),
            )
        ):
            label = QLabel(text)
            label.setObjectName("toolLabel")
            label.setToolTip(tip)
            widget.setToolTip(tip)
            grid.addWidget(label, row, 0)
            grid.addWidget(widget, row, 1)
        grid.setColumnStretch(1, 1)
        outer.addLayout(grid)

        hint = QLabel(
            "拖動音符=改位置;拖右側邊緣=改長度;雙擊音符=改顏色;"
            "選中後按 Delete=刪除;點底下鍵盤=加到 main 軌末端;"
            "縮略圖上左鍵拖曳=設 A/B 區間,右鍵點 marker=清除該端"
        )
        hint.setObjectName("toolLabel")
        hint.setWordWrap(True)
        outer.addWidget(hint)
        return panel

    def _build_settings_dock_panel(self) -> QWidget:
        """設定 dock 主體:含 automation + 一般偏好 toggle/slider。"""
        # 把 NOTE_COLOR_STYLES 傳進去,避免 panel 模組反向 import piano_player。
        self._settings_panel = SettingsPanel(
            get_setting=self._settings.get,
            note_color_styles=NOTE_COLOR_STYLES,
            parent=self,
        )
        self._settings_panel.setStyleSheet(build_panel_qss(THEME))
        self._settings_panel.setting_changed.connect(self._on_panel_setting_changed)
        return self._settings_panel

    def _set_sheet_field(self, name: str, value) -> None:
        if self._sheet is None or self._suppress_sheet_field_signals:
            return
        # play_range_*_seconds 採 -1=None 語意(配合 spinbox SpecialValueText)。
        if name in ("play_range_start_seconds", "play_range_end_seconds"):
            v = float(value)
            normalized: float | None = None if v < 0 else v
            setattr(self._sheet, name, normalized)
            # 同步給 main window 屬性 + overview_bar(這條路徑反向同步,不會 echo
            # 因為 overview_bar.set_loop_range 不 emit signal)。
            if name == "play_range_start_seconds":
                self._loop_start_seconds = normalized
            else:
                self._loop_end_seconds = normalized
            if hasattr(self, "overview_bar"):
                self.overview_bar.set_loop_range(
                    self._loop_start_seconds, self._loop_end_seconds
                )
            if hasattr(self, "piano_roll"):
                self.piano_roll.set_loop_range(
                    self._loop_start_seconds, self._loop_end_seconds
                )
        else:
            setattr(self._sheet, name, float(value))
        self._dirty = True
        self._update_title()
        self._refresh_now_label()
        self._update_scrollbar_range()
        self.piano_roll.update()

    def _sync_settings_from_sheet(self) -> None:
        if self._sheet is None:
            return
        self._suppress_sheet_field_signals = True
        try:
            self.tempo_spin.setValue(self._sheet.tempo)
            self.beat_spin.setValue(self._sheet.beat)
            self.gap_spin.setValue(self._sheet.gap)
            self.hold_spin.setValue(self._sheet.hold)
            self.mod_spin.setValue(self._sheet.modifier_delay)
            # AB:None → -1(SpecialValueText 顯示「未設定」)。
            a = self._sheet.play_range_start_seconds
            b = self._sheet.play_range_end_seconds
            self.play_a_spin.setValue(-1.0 if a is None else float(a))
            self.play_b_spin.setValue(-1.0 if b is None else float(b))
        finally:
            self._suppress_sheet_field_signals = False

    def _insert_token(self, text: str) -> None:
        # 譜面文字編輯器已移除;此方法保留 stub 避免舊 caller 出錯。
        # 真正的「點鍵盤加音」走 _on_note_clicked(直接修改 self._sheet)。
        return

    @Slot(str)
    def _on_note_clicked(self, label: str) -> None:
        if self._worker is not None or self._sheet is None:
            return
        if perf.enabled:
            perf.log("gui", "note_click", label=label)
        stroke = self._stroke_from_label(label)
        if stroke is None:
            return
        self._push_undo()

        cursor_seconds = self.piano_roll.current_seconds()
        start_beats = max(0.0, self._sheet.seconds_to_beats(cursor_seconds))

        snap = self._sheet.beat if self._sheet.beat > 0 else 0.25
        start_beats = round(start_beats / snap) * snap

        default_duration = 1.0

        while True:
            push_to = None
            for ev in self._sheet.events:
                if ev.is_rest:
                    continue
                if not any(s.key == stroke.key for s in ev.strokes):
                    continue
                ev_end = ev.start_beats + ev.duration_beats
                if ev.start_beats - 1e-6 <= start_beats < ev_end - 1e-6:
                    push_to = ev_end
                    break
            if push_to is None:
                break
            start_beats = push_to

        new_event = NoteEvent(
            start_beats=start_beats,
            duration_beats=default_duration,
            strokes=(stroke,),
            source=label,
            line=0,
            track="main",
        )
        self._sheet.events.append(new_event)
        self._dirty = True
        self._update_title()
        self._refresh_now_label()
        self.piano_roll.update()
        self._update_scrollbar_range()
        if hasattr(self, "overview_bar"):
            self.overview_bar.set_sheet(self._sheet)
        # 編輯試聽:點 piano_keyboard 加音時就讓本機鋼琴出聲一下。
        # sound_player.set_enabled(False) 時 play() 是 no-op,自動跟隨總開關。
        self._sound_player.play(label)
        self.statusBar().showMessage(
            f"加入 {label}（main 軌，起始 {start_beats:g} beat，長度 {default_duration:g} beat）",
            2000,
        )

    @staticmethod
    def _stroke_from_label(label: str):
        match = SheetParser.NOTE_RE.match(label)
        if not match:
            return None
        octave = match.group("oct").upper()
        degree = int(match.group("num"))
        accidental = (
            SheetParser._normalize_accidental(match.group("acc1"))
            or SheetParser._normalize_accidental(match.group("acc2"))
        )
        return make_stroke(octave, degree, accidental)

    def _compute_move_change(
        self,
        event,
        new_start_beats: float,
        new_duration_beats: float,
        new_track_idx: int,
        track_offset: int,
    ) -> tuple[tuple, bool]:
        """從拖曳結果計算 new_strokes,並判斷實際是否有變動。

        回 (new_strokes, unchanged)。unchanged=True 表示這筆 move 等同 no-op,
        caller 不該為它 apply。pitch sort mode 下 track_offset 是 visual delta。
        """
        new_strokes = event.strokes
        if new_track_idx >= 0 and len(event.strokes) == 1:
            prefix, _label, _display, accidental, degree = TRACK_ORDER[new_track_idx]
            new_strokes = (make_stroke(prefix, degree, accidental),)
        elif track_offset != 0 and event.strokes:
            shifted: list = []
            for s in event.strokes:
                base = TRACK_INDEX.get(s.label)
                if base is None:
                    shifted.append(s)
                    continue
                if self._pitch_sort_mode:
                    initial_v = self.piano_roll._visual_index(base)
                    new_v = max(0, min(35, initial_v + track_offset))
                    target = self.piano_roll._logical_for_visual(new_v)
                else:
                    target = max(0, min(len(TRACK_ORDER) - 1, base + track_offset))
                prefix, _label, _display, accidental, degree = TRACK_ORDER[target]
                shifted.append(make_stroke(prefix, degree, accidental))
            new_strokes = tuple(shifted)
        unchanged = (
            abs(event.start_beats - new_start_beats) < 1e-6
            and abs(event.duration_beats - new_duration_beats) < 1e-6
            and tuple(s.label for s in new_strokes) == tuple(s.label for s in event.strokes)
        )
        return new_strokes, unchanged

    @Slot(int, float, float, int, int)
    def _on_event_moved(
        self,
        index: int,
        new_start_beats: float,
        new_duration_beats: float,
        new_track_idx: int,
        track_offset: int = 0,
    ) -> None:
        # 播放中也接受編輯:worker 跑的是已 build 的 schedule,改 sheet 不影響當下這次播放
        if self._sheet is None:
            return
        if not (0 <= index < len(self._sheet.events)):
            return
        event = self._sheet.events[index]
        new_strokes, unchanged = self._compute_move_change(
            event, new_start_beats, new_duration_beats, new_track_idx, track_offset
        )
        if unchanged:
            return
        self._push_undo()
        self._apply_event_change(index, new_start_beats, new_duration_beats, new_strokes)
        if hasattr(self, "overview_bar"):
            self.overview_bar.set_sheet(self._sheet)
        moved_label = (
            "+".join(s.label for s in new_strokes) if new_strokes else "-"
        )
        self.statusBar().showMessage(
            f"已更新:{moved_label} 起始 {new_start_beats:g} beat / 長度 {new_duration_beats:g} beat",
            2500,
        )

    @Slot(object)
    def _on_events_moved(self, payload) -> None:
        """批次版 _on_event_moved:多選一次拖完只 push 一次 undo,
        讓 Ctrl+Z 一次回到拖曳前的整體狀態。

        payload: list of (idx, start, dur, new_track_idx_or_-1, track_offset, selected_labels)
        - selected_labels:該事件中「實際被選中拖走」的 stroke labels。
          - 空 tuple 或等於事件全部 stroke labels:整事件被拖,走原 _apply_event_change。
          - 真子集(partial):把這些 stroke 從原 chord 「拆出」成新事件,未選的 stroke
            留在原事件不動。對齊「多選什麼動什麼,別管和弦」的訴求。
        """
        if self._sheet is None or not payload:
            return
        sheet = self._sheet
        # 兩種分支收集:
        # - full_changes: (idx, start, dur, new_strokes) → 整事件修改 in place
        # - partial_changes: (idx, labels_to_remove, new_event) → 從原事件 strokes 拿掉
        #   labels_to_remove + 新增 new_event;若 labels_to_remove 等於原事件 strokes 整個就刪除原事件
        full_changes: list[tuple[int, float, float, tuple]] = []
        partial_changes: list[tuple[int, set, object]] = []  # set[str], NoteEvent
        for entry in payload:
            try:
                idx, start_b, dur_b, new_track, off, selected_labels = entry
            except (TypeError, ValueError):
                # 向後相容:沒帶 selected_labels 的舊 payload 走整事件路徑
                try:
                    idx, start_b, dur_b, new_track, off = entry
                    selected_labels = ()
                except (TypeError, ValueError):
                    continue
            idx = int(idx)
            if not (0 <= idx < len(sheet.events)):
                continue
            ev = sheet.events[idx]
            all_labels = tuple(s.label for s in ev.strokes)
            sel_set = set(selected_labels) if selected_labels else set()
            # 判斷:沒帶 selected_labels(空)→ 視為整事件;sel_set 等於全部 → 整事件;
            # 真子集且 ≥1 個 stroke 沒被選中 → partial。
            is_partial = bool(sel_set) and sel_set != set(all_labels)
            if not is_partial:
                # 整事件路徑 — 跟舊行為一致
                new_strokes, unchanged = self._compute_move_change(
                    ev, float(start_b), float(dur_b), int(new_track), int(off)
                )
                if unchanged:
                    continue
                full_changes.append((idx, float(start_b), float(dur_b), new_strokes))
            else:
                # Partial 路徑:把 sel_set 中的 stroke 從原事件拆出,組成新事件。
                # 對「被拖的 stroke 子集」單獨算 new_strokes(沿用既有 helper),
                # 但要先做出一個「虛擬事件」只含被拖的 strokes,讓 helper 看到正確的
                # initial 集合。
                dragged_strokes = tuple(s for s in ev.strokes if s.label in sel_set)
                if not dragged_strokes:
                    continue
                virtual_event = replace(ev, strokes=dragged_strokes)
                new_strokes, unchanged = self._compute_move_change(
                    virtual_event, float(start_b), float(dur_b),
                    int(new_track), int(off)
                )
                if unchanged:
                    continue
                # 新事件 = 拖到位的 strokes;line/track/source 沿用原事件,避免 sheet
                # to_text 排序時跨軌道亂跳。
                new_event = NoteEvent(
                    start_beats=max(0.0, float(start_b)),
                    duration_beats=max(sheet.beat * 0.0625, float(dur_b)),
                    strokes=new_strokes,
                    source="+".join(s.label for s in new_strokes),
                    line=ev.line,
                    track=ev.track,
                )
                partial_changes.append((idx, sel_set, new_event))

        if not full_changes and not partial_changes:
            return
        self._push_undo()

        # 先處理 full_changes(in place,不影響 idx)
        for idx, start_b, dur_b, new_strokes in full_changes:
            self._apply_event_change(idx, start_b, dur_b, new_strokes)

        # 再處理 partial_changes:
        # 1) 把原事件 strokes 中被拆出的 labels 移除,若剩 0 個 → 標記要刪;
        #    要刪的 idx 從大到小排序避免錯位。
        # 2) append 全部新事件 + sort。
        to_delete_indices: list[int] = []
        new_events: list = []
        for idx, labels_to_remove, new_event in partial_changes:
            if not (0 <= idx < len(sheet.events)):
                continue
            ev = sheet.events[idx]
            keep_strokes = tuple(s for s in ev.strokes if s.label not in labels_to_remove)
            if keep_strokes:
                sheet.events[idx] = replace(ev, strokes=keep_strokes)
            else:
                to_delete_indices.append(idx)
            new_events.append(new_event)
        # 從大到小刪原事件,並同步 _event_colors 重新編號
        if to_delete_indices:
            to_delete_indices.sort(reverse=True)
            for di in to_delete_indices:
                if 0 <= di < len(sheet.events):
                    sheet.events.pop(di)
                    new_colors = {}
                    for k, v in self.piano_roll._event_colors.items():
                        if k == di:
                            continue
                        new_colors[k - 1 if k > di else k] = v
                    self.piano_roll._event_colors = new_colors
        # 新事件加進去並排序
        if new_events:
            sheet.events.extend(new_events)
            sheet.events.sort(key=lambda e: (e.start_beats, e.line, e.track))

        if partial_changes or to_delete_indices:
            # partial / 刪除路徑會動到 sheet.events 結構,要重新刷 widget
            self._dirty = True
            self._update_title()
            self._refresh_now_label()
            self.piano_roll.update()
            self._update_scrollbar_range()
            # 拖完做健全性檢查(沿用 _apply_event_change 內的延後 audit 機制)
            if not getattr(self, "_audit_pending", False):
                self._audit_pending = True
                QTimer.singleShot(0, self._run_post_edit_audit)

        if hasattr(self, "overview_bar"):
            self.overview_bar.set_sheet(self._sheet)

        # 狀態列
        total = len(full_changes) + len(partial_changes)
        if total == 1 and len(full_changes) == 1:
            _idx, start_b, dur_b, new_strokes = full_changes[0]
            moved_label = (
                "+".join(s.label for s in new_strokes) if new_strokes else "-"
            )
            self.statusBar().showMessage(
                f"已更新:{moved_label} 起始 {start_b:g} beat / 長度 {dur_b:g} beat",
                2500,
            )
        elif partial_changes:
            self.statusBar().showMessage(
                f"已更新 {total} 個目標(其中 {len(partial_changes)} 個從和弦拆出)"
                "(Ctrl+Z 可整批還原)", 3000
            )
        else:
            self.statusBar().showMessage(
                f"已更新 {total} 個音符(Ctrl+Z 可整批還原)", 2500
            )

    @Slot(int)
    def _on_event_delete_requested(self, index: int) -> None:
        if self._sheet is None:
            return
        if not (0 <= index < len(self._sheet.events)):
            return
        self._push_undo()
        deleted = self._sheet.events.pop(index)
        new_colors = {}
        for k, v in self.piano_roll._event_colors.items():
            if k == index:
                continue
            new_colors[k - 1 if k > index else k] = v
        self.piano_roll._event_colors = new_colors
        self.piano_roll.clear_selection()
        self._dirty = True
        self._update_title()
        self._refresh_now_label()
        self.piano_roll.update()
        self._update_scrollbar_range()
        if hasattr(self, "overview_bar"):
            self.overview_bar.set_sheet(self._sheet)
        # 譜面文字編輯器已移除,不再需要 sheet → editor 反向同步
        label = "-" if deleted.is_rest else "+".join(s.label for s in deleted.strokes)
        self.statusBar().showMessage(f"已刪除 {label}", 2000)

    @Slot(object)
    def _on_events_delete_requested(self, indices) -> None:
        if self._sheet is None:
            return
        sorted_idx = sorted({int(i) for i in indices}, reverse=True)
        if not sorted_idx:
            return
        self._push_undo()
        colors = dict(self.piano_roll._event_colors)
        deleted_count = 0
        for index in sorted_idx:
            if not (0 <= index < len(self._sheet.events)):
                continue
            self._sheet.events.pop(index)
            new_colors = {}
            for k, v in colors.items():
                if k == index:
                    continue
                new_colors[k - 1 if k > index else k] = v
            colors = new_colors
            deleted_count += 1
        self.piano_roll._event_colors = colors
        self.piano_roll.clear_selection()
        if deleted_count:
            self._dirty = True
            self._update_title()
            self._refresh_now_label()
            self.piano_roll.update()
            self._update_scrollbar_range()
            if hasattr(self, "overview_bar"):
                self.overview_bar.set_sheet(self._sheet)
            # 譜面文字編輯器已移除,不再需要 sheet → editor 反向同步
            self.statusBar().showMessage(f"已刪除 {deleted_count} 個音符", 2000)

    @Slot(int)
    def _on_event_double_clicked(self, index: int) -> None:
        if self._sheet is None:
            return
        if not (0 <= index < len(self._sheet.events)):
            return
        event = self._sheet.events[index]
        if event.is_rest:
            return
        prefix = event.strokes[0].label[0] if event.strokes else "M"
        initial = QColor(THEME.get(prefix, THEME["M"]))
        existing = self.piano_roll._event_colors.get(index)
        if existing is not None:
            initial = QColor(existing)
        chosen = QColorDialog.getColor(initial, self, "選擇音符顏色")
        if not chosen.isValid():
            return
        self.piano_roll.set_event_color(index, chosen)
        self.statusBar().showMessage(
            f"已套用顏色 {chosen.name()}（顏色不會存進譜面，重新載入會還原）", 4000
        )

    def _apply_event_change(
        self,
        index: int,
        new_start_beats: float,
        new_duration_beats: float,
        new_strokes: tuple,
    ) -> None:
        if self._sheet is None:
            return
        sheet = self._sheet
        event = sheet.events[index]
        new_event = replace(
            event,
            start_beats=max(0.0, float(new_start_beats)),
            duration_beats=max(sheet.beat * 0.0625, float(new_duration_beats)),
            strokes=new_strokes,
        )
        sheet.events[index] = new_event
        self._dirty = True
        self._update_title()
        self._refresh_now_label()
        self.piano_roll.update()
        self._update_scrollbar_range()
        # 拖完做健全性檢查 (用 QTimer 延後到 event loop 結束,合併批次拖)
        if not getattr(self, "_audit_pending", False):
            self._audit_pending = True
            QTimer.singleShot(0, self._run_post_edit_audit)

    def _run_post_edit_audit(self) -> None:
        self._audit_pending = False
        if self._sheet is None:
            return
        repairs, warnings = self._validate_and_repair()
        if repairs:
            self._dirty = True
            self.piano_roll.update()
            if hasattr(self, "overview_bar"):
                self.overview_bar.set_sheet(self._sheet)
        # audit 完一次性 sync editor 文字,避免 piano_roll 拖曳後 editor 仍是舊內容,
        # 這也是 Ctrl+Z/Y 還原能拿到正確 redo 基準的前置條件。
        # 譜面文字編輯器已移除,不再需要 sheet → editor 反向同步
        msg_parts: list[str] = []
        if repairs:
            msg_parts.append(f"自動清理 {repairs} 個重複/無效 stroke")
        msg_parts.extend(warnings)
        if msg_parts:
            self.statusBar().showMessage(" / ".join(msg_parts), 6000)

    RETRIGGER_MIN_SECONDS = 0.05

    def _validate_and_repair(self) -> tuple[int, list[str]]:
        """掃描 sheet.events 修復可逆問題,回傳 (修復數, 警告訊息)。
        修可逆問題:
          - 同 event 內 strokes 重複 label → 去重
          - duration < 最小限度 → 提升至最小
          - start_beats < 0 → clamp 到 0
        只警告 (不破壞使用者意圖):
          - 同 onset 同 key 跨 event 衝突 → 第二個按下會被吃掉
          - 同 key 在 RETRIGGER_MIN_SECONDS 內連觸 → 遊戲可能吃不到
        """
        sheet = self._sheet
        if sheet is None or not sheet.events:
            return 0, []

        repairs = 0
        min_dur = sheet.beat * 0.0625

        # 1. 修可逆問題 — 在同一 pass 處理重複/負起點/過短
        for i, ev in enumerate(sheet.events):
            new_start = max(0.0, float(ev.start_beats))
            new_dur = max(min_dur, float(ev.duration_beats))
            if ev.is_rest:
                deduped = ev.strokes
            else:
                seen_labels: set[str] = set()
                deduped_list = []
                for s in ev.strokes:
                    if s.label in seen_labels:
                        continue
                    seen_labels.add(s.label)
                    deduped_list.append(s)
                deduped = tuple(deduped_list)
            changed = (
                abs(new_start - ev.start_beats) > 1e-9
                or abs(new_dur - ev.duration_beats) > 1e-9
                or len(deduped) != len(ev.strokes)
            )
            if changed:
                if not ev.is_rest:
                    repairs += max(0, len(ev.strokes) - len(deduped))
                sheet.events[i] = replace(
                    ev,
                    start_beats=new_start,
                    duration_beats=new_dur,
                    strokes=deduped,
                )

        warnings: list[str] = []

        # 2. 同 onset 同 key 衝突 (只警告)
        eps = 1e-4
        occupancy: dict[tuple[float, str], set[int]] = {}
        for i, ev in enumerate(sheet.events):
            if ev.is_rest:
                continue
            bucket = round(ev.start_beats / eps) * eps
            for stroke in ev.strokes:
                occupancy.setdefault((bucket, stroke.key), set()).add(i)
        collisions = [
            (k, v) for k, v in occupancy.items() if len(v) > 1
        ]
        if collisions:
            sample = "; ".join(
                f"{start:g}拍 {key.upper()}×{len(idxs)}"
                for (start, key), idxs in collisions[:3]
            )
            more = "…" if len(collisions) > 3 else ""
            warnings.append(
                f"⚠ {len(collisions)} 處同時刻同鍵衝突 ({sample}{more})"
            )

        # 3. 同 key 過近連觸 (只警告)
        retrigger_min = self.RETRIGGER_MIN_SECONDS
        by_key: dict[str, list[float]] = {}
        for ev in sheet.events:
            if ev.is_rest:
                continue
            seconds = sheet.beats_to_seconds(ev.start_beats)
            for stroke in ev.strokes:
                by_key.setdefault(stroke.key, []).append(seconds)
        too_close = 0
        for k, lst in by_key.items():
            lst.sort()
            for j in range(1, len(lst)):
                if lst[j] - lst[j - 1] < retrigger_min:
                    too_close += 1
        if too_close:
            warnings.append(
                f"⚠ {too_close} 處鍵在 {retrigger_min*1000:g}ms 內連觸 (遊戲可能吃不到)"
            )

        return repairs, warnings
        self._update_scrollbar_range()

    @Slot(float)
    def _on_timeline_changed(self, seconds: float) -> None:
        if not self.piano_roll._playing:
            return
        self.scrollbar.blockSignals(True)
        try:
            self.scrollbar.setValue(int(max(0.0, seconds) * 100))
        finally:
            self.scrollbar.blockSignals(False)
        self._sync_overview_view_window()
        self._refresh_now_label()

    @Slot(int)
    def _on_scrollbar_changed(self, value: int) -> None:
        seconds = value / 100.0
        if self._worker is not None and self.piano_roll._playing:
            self.piano_roll.seek_requested.emit(seconds)
            return
        self.piano_roll.set_browse_offset(seconds)
        self._sync_overview_view_window()
        self._refresh_now_label()

    def _refresh_now_label(self) -> None:
        if self._sheet is None:
            return
        sheet = self._sheet
        total_seconds = sheet.beats_to_seconds(sheet.total_beats)
        cursor_seconds = self.piano_roll.current_seconds() if hasattr(self, "piano_roll") else 0.0
        cursor_beats = sheet.seconds_to_beats(cursor_seconds)
        bar_no = sheet.bar_of_beat(cursor_beats)
        total_bars = sheet.total_bars
        if total_bars > 0:
            bar_no = max(1, min(bar_no, total_bars))
            bars_text = f"．{bar_no}/{total_bars} 小節"
        else:
            bars_text = ""
        speed_text = ""
        if abs(self._playback_speed - 1.0) > 1e-3:
            speed_text = f" × {self._playback_speed:g}"
        current_tempo = sheet.tempo_at_beat(cursor_beats)
        if abs(current_tempo - sheet.tempo) > 1e-3:
            tempo_text = f"{current_tempo:g} BPM (起 {sheet.tempo:g})"
        else:
            tempo_text = f"{sheet.tempo:g} BPM"
        self.now_label.setText(
            f"{tempo_text}{speed_text}．{sheet.playable_events} 音．{total_seconds:.1f}s{bars_text}"
        )

    def _update_scrollbar_range(self) -> None:
        if not hasattr(self, "scrollbar"):
            return
        total_centi = 0
        if self._sheet is not None:
            total_centi = int(self._sheet.beats_to_seconds(self._sheet.total_beats) * 100)
        self.scrollbar.blockSignals(True)
        try:
            self.scrollbar.setRange(0, max(0, total_centi))
        finally:
            self.scrollbar.blockSignals(False)

    def _load_startup_score(self, initial_file) -> None:
        if initial_file is not None:
            self._load_file(initial_file)
            return

        # 從使用者 songs/ 隨機挑一首;沒有任何 .txt 才退回 bundled examples。
        candidates: list[Path] = []
        songs_dir = _user_data_dir("songs")
        if songs_dir.exists():
            candidates = list(songs_dir.glob("*.txt"))
        if not candidates:
            bundled = _resource_path("examples")
            if bundled.exists():
                candidates = list(bundled.glob("*.txt"))
        if candidates:
            self._load_file(random.choice(candidates))
        else:
            # 沒有任何譜面可用;主視窗呈現空白狀態,使用者可從「檔案 → 開啟/匯入」載入。
            self._sheet = None
            self._current_file = None
            self._dirty = False
            self._update_title()

    def _populate_song_combo(self) -> None:
        self.song_combo.blockSignals(True)
        try:
            self.song_combo.clear()
            # 使用者譜面庫:exe 同目錄(frozen)或 piano_player.py 旁(dev)的 songs/。
            # 可以直接丟新 .txt 進去,重啟後出現在下拉選單。
            songs_dir = _user_data_dir("songs")
            if songs_dir.exists():
                for path in sorted(songs_dir.glob("*.txt")):
                    self.song_combo.addItem(self._score_title_from_file(path), path)
            # 匯入但尚未存檔的臨時項:擺在第一個,data=None 與磁碟項區分。
            # 切到其他項或實際存檔後會被 _clear_unsaved_combo 拿掉。
            if self._unsaved_combo_title:
                self.song_combo.insertItem(
                    0, f"(未存檔) {self._unsaved_combo_title}", None
                )
                self.song_combo.setCurrentIndex(0)
        finally:
            self.song_combo.blockSignals(False)

    def _clear_unsaved_combo(self) -> None:
        """清掉「(未存檔)」臨時項與其 state,通常在實際存檔/換歌/刪除時呼叫。"""
        if self._unsaved_combo_title is None:
            return
        self._unsaved_combo_title = None
        # 找出 data is None 的那一項(若有)並移除;refresh 後新 combo 不再帶它。
        for index in range(self.song_combo.count()):
            if self.song_combo.itemData(index) is None:
                self.song_combo.blockSignals(True)
                try:
                    self.song_combo.removeItem(index)
                finally:
                    self.song_combo.blockSignals(False)
                break

    @staticmethod
    def _extract_title_from_dsl(text: str) -> str:
        """從 DSL 文字第一行 `# title` 註解抽標題,沒有就回空字串。

        importer 在每首 DSL 開頭都會寫 `# {meta_title or path.stem}`,
        所以這個函式幾乎一定取得到非空字串。
        """
        for raw in text.splitlines():
            line = raw.strip()
            if not line:
                continue
            if line.startswith("#"):
                return line.lstrip("#").strip()
            break
        return ""

    @Slot()
    def _on_refresh_songs(self) -> None:
        """重新掃描 songs/ 並更新下拉清單,保留目前選中的歌曲。"""
        before = self.song_combo.count()
        self._populate_song_combo()
        self._set_song_combo_path(self._current_file)
        after = self.song_combo.count()
        delta = after - before
        if delta > 0:
            msg = f"已刷新曲目清單(+{delta} 首,共 {after})"
        elif delta < 0:
            msg = f"已刷新曲目清單({delta} 首,共 {after})"
        else:
            msg = f"已刷新曲目清單(共 {after} 首,無變動)"
        self.statusBar().showMessage(msg, 3000)
        self.song_refresh_button.clearFocus()

    @Slot(int)
    def _on_song_selected(self, index: int) -> None:
        path = self.song_combo.itemData(index)
        if not isinstance(path, Path) or self._current_file == path:
            self.song_combo.clearFocus()
            return
        if not self._confirm_discard_changes():
            self._set_song_combo_path(self._current_file)
            self.song_combo.clearFocus()
            return
        # 播放中切歌前先讓 worker 收尾,避免舊 schedule 的鍵繼續送、跟新譜面 UI 錯位。
        self.stop_playback(wait_until_finished=True)
        # _load_file 內部會清 (未存檔) 臨時項,這裡不重複處理。
        self._load_file(path)
        self.song_combo.clearFocus()

    @staticmethod
    def _score_title_from_file(path: Path) -> str:
        try:
            for raw_line in path.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if line.startswith("#"):
                    title = line.lstrip("#").strip()
                    if title:
                        return title
                if line:
                    break
        except OSError:
            pass
        return path.stem.replace("_", " ").title()

    def _set_song_combo_path(self, path) -> None:
        self.song_combo.blockSignals(True)
        try:
            for index in range(self.song_combo.count()):
                if self.song_combo.itemData(index) == path:
                    self.song_combo.setCurrentIndex(index)
                    return
            self.song_combo.setCurrentIndex(-1)
        finally:
            self.song_combo.blockSignals(False)

    def _load_file(self, path: Path) -> None:
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            try:
                text = path.read_text(encoding="utf-8-sig")
            except UnicodeDecodeError as exc:
                self._show_error(
                    "載入失敗",
                    f"無法以 UTF-8 解碼:{path.name}\n請確認檔案是文字譜面而非二進位檔。\n\n{exc}",
                )
                return
        except OSError as exc:
            self._show_error("載入失敗", str(exc))
            return

        try:
            sheet = SheetParser.parse(text)
        except Exception as exc:  # noqa: BLE001
            self._show_error(
                "譜面解析失敗",
                f"{path.name} 不是有效的譜面文字檔。\n\n{exc}",
            )
            return

        self._current_file = path
        self._dirty = False
        # 載入既有檔不需要再帶匯入名了
        self._imported_source_name = None
        # 載入到任何真實檔就把 (未存檔) 臨時項清掉,避免顯示錯亂。
        self._clear_unsaved_combo()
        self._clear_loop_range()
        self._apply_sheet(sheet)
        self._update_title()
        self._set_song_combo_path(path)
        self.statusBar().showMessage(f"已載入 {path.name}", 3000)

    def _save_file(self, path: Path) -> None:
        if self._sheet is None:
            self._show_error("儲存失敗", "目前沒有譜面可以儲存")
            return
        text = self._sheet.to_text()
        try:
            path.write_text(text, encoding="utf-8")
        except OSError as exc:
            self._show_error("儲存失敗", str(exc))
            return
        self._current_file = path
        self._dirty = False
        # 已實際存檔,清掉匯入記號避免下次又帶舊名
        self._imported_source_name = None
        # 已落盤,(未存檔) 臨時項使命結束(不能在 _populate_song_combo 前留著,
        # 否則 refresh 又會把它插回來)。
        self._unsaved_combo_title = None
        self._update_title()
        self._populate_song_combo()
        self._set_song_combo_path(path)
        self.statusBar().showMessage(f"已儲存 {path.name}", 4000)

    def _apply_sheet(self, sheet) -> None:
        """把解析好的 Sheet 套到主視窗各 widget,並更新譜面屬性面板。"""
        self._sheet = sheet
        if hasattr(self, "piano_roll"):
            self.piano_roll.set_sheet(sheet)
        if hasattr(self, "overview_bar"):
            self.overview_bar.set_sheet(sheet)
        # 把 sheet 內的 AB 點同步到 main window 屬性 + overview_bar markers。
        # 順序:屬性 → overview(set_loop_range 不 emit,不會 echo) → spinbox(在
        # _sync_settings_from_sheet 內,已有 _suppress 保護)。
        self._loop_start_seconds = sheet.play_range_start_seconds
        self._loop_end_seconds = sheet.play_range_end_seconds
        if hasattr(self, "overview_bar"):
            self.overview_bar.set_loop_range(
                self._loop_start_seconds, self._loop_end_seconds
            )
        if hasattr(self, "piano_roll"):
            self.piano_roll.set_loop_range(
                self._loop_start_seconds, self._loop_end_seconds
            )
        self._sync_settings_from_sheet()
        self._refresh_now_label()
        self._update_scrollbar_range()

    def _clear_loop_range(self) -> None:
        """換譜面時清掉播放區間 markers,避免舊曲的時間軸套到新曲上。"""
        self._loop_start_seconds = None
        self._loop_end_seconds = None
        if hasattr(self, "overview_bar"):
            self.overview_bar.set_loop_range(None, None)
        if hasattr(self, "piano_roll"):
            self.piano_roll.set_loop_range(None, None)

    @Slot()
    def import_score(self) -> None:
        """統一匯入入口:按副檔名自動分派到 MusicXML / MIDI / MSCZ 對應 importer。"""
        if not self._confirm_discard_changes():
            return
        filename, _ = QFileDialog.getOpenFileName(
            self,
            "匯入譜面",
            str(Path.cwd()),
            (
                "所有支援格式 (*.mxl *.xml *.musicxml *.mid *.midi *.mscz *.mscx *.txt *.score);;"
                "MusicXML (*.mxl *.xml *.musicxml);;"
                "MIDI (*.mid *.midi);;"
                "MuseScore (*.mscz *.mscx);;"
                "Text score (*.txt *.score);;"
                "All files (*.*)"
            ),
        )
        if not filename:
            return
        path = Path(filename)
        suffix = path.suffix.lower()
        if suffix in MUSICXML_EXTENSIONS:
            self._import_musicxml_path(path)
        elif suffix in MIDI_EXTENSIONS:
            self._import_midi_path(path)
        elif suffix in MSCZ_EXTENSIONS:
            self._import_mscz_path(path)
        elif suffix in DSL_EXTENSIONS:
            self._load_file(path)
        else:
            self.statusBar().showMessage(
                f"不支援的檔案格式:{suffix}(僅支援 mxl / xml / musicxml / mid / midi / mscz / mscx / txt / score)",
                4000,
            )

    @Slot()
    def import_score_batch(self) -> None:
        """批量匯入入口:多選檔案 → 統一設定 → 暫存資料夾 → 結果挑選對話框。"""
        if not self._confirm_discard_changes():
            return
        filenames, _ = QFileDialog.getOpenFileNames(
            self,
            "批量匯入譜面",
            str(Path.cwd()),
            (
                "所有支援格式 (*.mxl *.xml *.musicxml *.mid *.midi *.mscz *.mscx);;"
                "MusicXML (*.mxl *.xml *.musicxml);;"
                "MIDI (*.mid *.midi);;"
                "MuseScore (*.mscz *.mscx);;"
                "All files (*.*)"
            ),
        )
        if not filenames:
            return
        all_paths = [Path(f) for f in filenames]
        supported = [p for p in all_paths if p.suffix.lower() in BATCH_SUPPORTED_EXTENSIONS]
        ignored = [p for p in all_paths if p not in supported]
        if not supported:
            self._show_error("批量匯入", "選取的檔案沒有可匯入的格式 (僅支援 mxl/xml/musicxml/mid/midi/mscz/mscx)。")
            return
        if ignored:
            self.statusBar().showMessage(
                f"批量匯入:已忽略 {len(ignored)} 個不支援的檔案", 5000
            )

        dialog = BatchImportOptionsDialog(self, supported)
        if dialog.exec() != QDialog.Accepted:
            return
        opts = dialog.values()

        timestamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        staging_dir = _user_data_dir("batch_imports") / timestamp
        try:
            staging_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._show_error("批量匯入", f"無法建立暫存資料夾:{exc}")
            return

        # 切譜前 worker 必須先收尾,避免舊 schedule 的按鍵跟新匯入流程搶 UI。
        self.stop_playback(wait_until_finished=True)

        progress = QProgressDialog("正在批量匯入…", "取消", 0, len(supported), self)
        progress.setWindowTitle("批量匯入")
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)
        # 固定寬度,避免 label 因檔名長短變化讓 dialog 自動 resize 跳動。
        progress.setFixedWidth(480)
        progress.setValue(0)

        try:
            results = run_batch_import(supported, opts, staging_dir, progress=progress)
        finally:
            progress.close()

        if not results:
            self.statusBar().showMessage("批量匯入已取消", 4000)
            return

        # Non-modal 結果視窗:show() 後立刻 return,讓主視窗能繼續操作 (試聽 / 滾捲等)。
        # 後處理走 finished signal,closure 捕獲 results / staging_dir。
        # 用 deleteLater 而不是 WA_DeleteOnClose,因為 accept()/reject() 不會走 closeEvent,
        # 改在 finished slot 結尾顯式 deleteLater() 才能讓 non-modal dialog 真的被 free。
        result_dialog = BatchImportResultsDialog(self, results)
        result_dialog.preview_requested.connect(self._on_batch_preview)
        result_dialog.finished.connect(
            lambda _code, d=result_dialog, r=results, s=staging_dir:
            self._on_batch_results_finished(d, r, s)
        )
        result_dialog.show()
        result_dialog.raise_()
        result_dialog.activateWindow()

    def _on_batch_results_finished(
        self,
        dialog: BatchImportResultsDialog,
        results: list[BatchImportResult],
        staging_dir: Path,
    ) -> None:
        """結果視窗 finished 後的後處理:依使用者意圖把勾選/全部成功的存到 songs/。"""
        action = dialog.action()
        success_count = sum(1 for r in results if r.success)
        fail_count = len(results) - success_count
        try:
            if action in ("save_checked", "save_all"):
                to_save = (
                    dialog.checked_results() if action == "save_checked"
                    else [r for r in results if r.success]
                )
                saved_count = 0
                for r in to_save:
                    if r.staged_path is None:
                        continue
                    try:
                        text = r.staged_path.read_text(encoding="utf-8")
                    except OSError:
                        continue
                    target = self._save_imported_to_songs(r.source_path.stem, text)
                    if target is not None:
                        saved_count += 1
                self._populate_song_combo()
                self.statusBar().showMessage(
                    f"批量匯入完成:成功 {success_count} / 失敗 {fail_count} / 已存 {saved_count}  暫存於 {staging_dir}",
                    10000,
                )
            else:
                self.statusBar().showMessage(
                    f"批量匯入完成:成功 {success_count} / 失敗 {fail_count}  暫存於 {staging_dir}",
                    8000,
                )
        finally:
            dialog.deleteLater()

    @Slot(object)
    def _on_batch_preview(self, result: BatchImportResult) -> None:
        """從結果對話框雙擊載入暫存譜面到編輯器試聽 (標記未存檔)。"""
        if result is None or not result.success or result.staged_path is None:
            return
        try:
            text = result.staged_path.read_text(encoding="utf-8")
        except OSError as exc:
            self._show_error("載入暫存譜面", f"讀取暫存檔失敗:{exc}")
            return
        self._apply_imported_text(
            text,
            result.source_path,
            save_to_songs=False,
            status_message=f"已載入 {result.source_path.name} (暫存試聽)",
        )

    @Slot()
    def import_musicxml(self) -> None:
        if not self._confirm_discard_changes():
            return
        filename, _ = QFileDialog.getOpenFileName(
            self,
            "匯入 MusicXML",
            str(Path.cwd()),
            "MusicXML (*.mxl *.xml *.musicxml);;All files (*.*)",
        )
        if not filename:
            return
        self._import_musicxml_path(Path(filename))

    @Slot()
    def new_score(self) -> None:
        """建立一份空樂譜。不落盤,在歌曲下拉顯示 (未存檔) 新樂譜,Ctrl+S 可另存。"""
        if not self._confirm_discard_changes():
            return
        try:
            sheet = SheetParser.parse("tempo 120\nbeat 0.25\n")
        except Exception as exc:  # noqa: BLE001
            self._show_error("新增失敗", str(exc))
            return
        self._current_file = None
        self._dirty = True
        self._imported_source_name = None
        self._clear_loop_range()
        self._apply_sheet(sheet)
        self._unsaved_combo_title = "新樂譜"
        self._update_title()
        self._populate_song_combo()
        self.statusBar().showMessage("已建立空樂譜(未存檔),按 Ctrl+S 存入 songs/", 5000)

    @Slot()
    def open_score(self) -> None:
        if not self._confirm_discard_changes():
            return
        filename, _ = QFileDialog.getOpenFileName(
            self,
            "載入譜面",
            str(Path.cwd()),
            "Text score (*.txt *.score);;All files (*.*)",
        )
        if filename:
            self._load_file(Path(filename))

    @Slot()
    def save_score(self) -> None:
        if self._current_file is None:
            self.save_score_as()
            return
        self._save_file(self._current_file)

    @Slot()
    def save_score_as(self) -> None:
        # 預設檔名優先順序:已開啟的檔 → 匯入時的原始檔名 → score.txt
        # 從 MXL/MIDI/MSCZ 匯入後直接另存,自動帶入原始檔名(放在 songs 目錄)。
        if self._current_file is not None:
            default_path = str(self._current_file)
        elif self._imported_source_name:
            songs_dir = _user_data_dir("songs")
            try:
                songs_dir.mkdir(parents=True, exist_ok=True)
            except OSError:
                songs_dir = Path.cwd()
            default_path = str(songs_dir / f"{self._imported_source_name}.txt")
        else:
            default_path = str(Path.cwd() / "score.txt")
        filename, _ = QFileDialog.getSaveFileName(
            self,
            "儲存譜面",
            default_path,
            "Text score (*.txt);;All files (*.*)",
        )
        if filename:
            self._save_file(Path(filename))

    @Slot()
    def delete_current_song(self) -> None:
        """從磁碟刪除目前載入的歌曲檔,刪完自動切到清單下一首或清空編輯狀態。

        只允許刪 songs/ 內的使用者譜面;bundled examples 是只讀路徑,刪了下次啟動
        又會被資源解包覆蓋。播放中先 stop 再刪,避免 worker 還在送鍵就被換譜。
        """
        if self._current_file is None:
            QMessageBox.information(
                self, "刪除歌曲", "目前沒有載入的歌曲檔可以刪除喔。"
            )
            return

        current = Path(self._current_file)
        songs_dir = _user_data_dir("songs").resolve()
        try:
            current_resolved = current.resolve()
        except OSError:
            current_resolved = current
        try:
            current_resolved.relative_to(songs_dir)
        except ValueError:
            QMessageBox.warning(
                self,
                "刪除歌曲",
                f"這個檔不在 songs 目錄內,無法刪除:\n{current}",
            )
            return

        # 設定面板可關掉「刪除歌曲確認」,關掉時點到「刪除目前歌曲」就立刻刪。
        if bool(self._settings.get("confirm_delete_song", True)):
            confirm = QMessageBox.question(
                self,
                "刪除歌曲",
                f"確定要永久刪除這首歌嗎?\n\n{current.name}\n\n此動作無法復原。",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if confirm != QMessageBox.Yes:
                return

        # 播放中先停掉,避免 worker 還在跑時 _load_file 換譜出現殘留按鍵或時序錯位。
        self.stop_playback(wait_until_finished=True)

        try:
            current.unlink()
        except OSError as exc:
            QMessageBox.critical(
                self, "刪除歌曲", f"刪除失敗:\n{exc}"
            )
            return

        # 從目前下拉清單裡找下一個可載入的歌(略過剛被刪掉的)。
        next_path: Path | None = None
        for i in range(self.song_combo.count()):
            data = self.song_combo.itemData(i)
            if isinstance(data, Path) and data != current:
                next_path = data
                break

        self._current_file = None
        self._dirty = False

        if next_path is not None:
            self._load_file(next_path)
        else:
            # 沒有其他歌了,清空 sheet 與 piano roll,等待重新匯入或開啟。
            self._sheet = None
            self.piano_roll.set_sheet(None)
            self.overview_bar.set_sheet(None)
            self._update_title()
            self._refresh_now_label()

        self._populate_song_combo()
        self._set_song_combo_path(self._current_file)
        self.statusBar().showMessage(f"已刪除 {current.name}", 3000)

    # _on_text_changed / _refresh_sheet_from_text 已隨譜面文字編輯器移除。
    # 譜面現在只在 _load_file / _apply_sheet / import 流程中被建立或更新。

    @Slot()
    def seek_to_start(self) -> None:
        """跳到譜面開頭。播放中走 worker seek 路徑;停止時更新 piano_roll 瀏覽位置。"""
        if self._worker is not None:
            self._on_seek_requested(0.0)
        else:
            self.piano_roll.seek_to(0.0)
            self.piano_roll.set_browse_offset(0.0)
            self.scrollbar.blockSignals(True)
            try:
                self.scrollbar.setValue(0)
            finally:
                self.scrollbar.blockSignals(False)
            self._refresh_now_label()
            self._sync_overview_view_window()
            self.statusBar().showMessage("跳到開頭", 1500)

    @Slot()
    def seek_to_end(self) -> None:
        """跳到譜面結尾 (留一拍緩衝避免越界)。"""
        if self._sheet is None or self._sheet.total_beats <= 0:
            return
        total = self._sheet.beats_to_seconds(self._sheet.total_beats)
        target = max(0.0, total - 0.05)
        if self._worker is not None:
            self._on_seek_requested(target)
        else:
            self.piano_roll.seek_to(target)
            self.piano_roll.set_browse_offset(target)
            self.scrollbar.blockSignals(True)
            try:
                self.scrollbar.setValue(int(target * 100))
            finally:
                self.scrollbar.blockSignals(False)
            self._refresh_now_label()
            self._sync_overview_view_window()
            self.statusBar().showMessage(f"跳到結尾 {target:.2f}s", 1500)

    @Slot()
    def start_playback(self) -> None:
        if self._worker is not None and (self._thread is None or not self._thread.isRunning()):
            self._on_thread_finished()
        if self._worker is not None:
            self.statusBar().showMessage("已經在播放中", 3000)
            return
        if self._sheet is None or self._sheet.playable_events == 0:
            self.statusBar().showMessage("譜面沒有可播放的音符", 3000)
            return
        self._play_sheet(self._sheet)

    def _play_sheet(self, sheet: Sheet) -> None:
        if perf.enabled:
            perf.log(
                "gui",
                "play_sheet",
                events=len(sheet.events),
                preview=self._preview_mode,
                speed=self._playback_speed,
            )
        self._sheet = sheet
        self.piano_roll.set_sheet(sheet)
        self.piano_roll.set_playback_speed(self._playback_speed)
        if hasattr(self, "overview_bar"):
            self.overview_bar.set_sheet(sheet)

        target_window = find_game_window()
        target_hwnd = None
        focus_before_play = False
        # 編輯模式:不碰遊戲視窗,worker 走 silent_mode 不送任何鍵,只發本機音。
        if self._preview_mode:
            self.statusBar().showMessage("編輯模式:不送鍵,只發本機鋼琴音色", 4000)
        elif self._focus_game_on_play and target_window is not None:
            target_hwnd = target_window.hwnd
            focus_before_play = True
            self.statusBar().showMessage(
                f"自動聚焦遊戲視窗：{target_window.display}", 4000
            )
        else:
            self.statusBar().showMessage(
                f"找不到 {GAME_PROCESS_NAME} 或標題含 {GAME_TITLE_HINT} 的視窗，仍會送鍵到目前焦點視窗",
                5000,
            )
        self._game_hwnd = target_hwnd

        self._thread = QThread()
        initial_offset = self.piano_roll.current_seconds()
        total = sheet.beats_to_seconds(sheet.total_beats) if sheet.total_beats else 0.0
        if total > 0 and initial_offset >= max(0.0, total - 0.05):
            initial_offset = 0.0
        # 套用 loop range:cursor 若在 loop 區間之外,把 initial_offset 拉到 loop_start。
        # loop_end 直接傳給 worker,run loop 看到超過就自然結束。
        loop_end_for_worker: float | None = None
        if self._loop_start_seconds is not None or self._loop_end_seconds is not None:
            ls = self._loop_start_seconds
            le = self._loop_end_seconds
            if ls is not None and initial_offset < ls:
                initial_offset = ls
            if le is not None and initial_offset >= le:
                # cursor 已經過了 loop end → 從 loop_start(或 0)重新開始
                initial_offset = ls if ls is not None else 0.0
            loop_end_for_worker = le
        self._worker = PlaybackWorker(
            sheet,
            START_DELAY_SECONDS,
            target_hwnd=target_hwnd,
            focus_before_play=focus_before_play,
            initial_offset_seconds=initial_offset,
            speed=self._playback_speed,
            loop_end_seconds=loop_end_for_worker,
            silent_mode=self._preview_mode,
        )
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.started.connect(self.piano_roll.start_playing, Qt.QueuedConnection)
        self._worker.progress.connect(self._on_playback_progress, Qt.QueuedConnection)
        self._worker.active_notes.connect(self.piano_keyboard.set_active_labels, Qt.QueuedConnection)
        self._worker.active_notes.connect(self.piano_roll.set_active_labels, Qt.QueuedConnection)
        # delta 路徑:down/up 高頻 emit (adds, removes),widget 只動差集 + update。
        # set_active_labels 留給「全清/seek 重設」場景,行為不變。
        self._worker.active_delta.connect(self.piano_keyboard.apply_active_delta, Qt.QueuedConnection)
        self._worker.active_delta.connect(self.piano_roll.apply_active_delta, Qt.QueuedConnection)
        # 編輯模式 on 時 GUI 才出聲;off 時遊戲負責出聲,避免遊戲 + GUI 雙重聲。
        # piano_sound_enabled 為總開關,off 時連線了也不會出聲(sound_player.set_enabled)。
        if self._preview_mode:
            self._worker.note_pressed.connect(self._sound_player.play_chord, Qt.QueuedConnection)
        self._worker.failed.connect(self._on_playback_failed, Qt.QueuedConnection)
        self._worker.finished.connect(self._on_playback_finished, Qt.QueuedConnection)
        self._worker.finished.connect(self._thread.quit, Qt.QueuedConnection)
        self._thread.finished.connect(self._on_thread_finished)
        self._thread.start()

        self.play_button.setEnabled(True)
        self.stop_button.setEnabled(True)
        # 播放中也允許編輯:worker 跑的是已 build 的 schedule,改 sheet 不影響當下這次播放。
        # 編輯抽屜照舊保持可開,editor 不再 setReadOnly。
        self._paused = False
        self._auto_paused = False
        self._refresh_play_button()
        self._set_playback_status("播放中")
        if (
            self._auto_pause_on_focus_loss
            and self._game_hwnd
            and not self._focus_check_timer.isActive()
        ):
            QTimer.singleShot(FOCUS_CHECK_GRACE_MS, self._focus_check_timer.start)

    @Slot()
    def _on_play_button_clicked(self) -> None:
        # 同一個按鈕串連三種行為:未播放→開始播放;播放中→暫停;暫停中→繼續。
        # 「同一個區域」變化邏輯集中在這裡,點擊後再交給 _refresh_play_button 同步文字。
        if self._worker is None:
            self.start_playback()
            return
        self.toggle_pause()

    def _refresh_play_button(self) -> None:
        if self._worker is None:
            self.play_button._icon_key = "play"
            self.play_button._refresh_icon()
            self.play_button.setToolTip("播放 (F6)")
        elif self._paused:
            self.play_button._icon_key = "play"
            self.play_button._refresh_icon()
            self.play_button.setToolTip("繼續 (F8)")
        else:
            self.play_button._icon_key = "pause"
            self.play_button._refresh_icon()
            self.play_button.setToolTip("暫停 (F8)")

    @Slot()
    def toggle_pause(self) -> None:
        if self._worker is None:
            return
        if self._paused:
            self._worker.request_resume()
            self.piano_roll.resume_playing()
            self._paused = False
            self._set_playback_status("播放中")
            self.statusBar().showMessage("繼續播放", 2000)
        else:
            self._worker.request_pause()
            self.piano_roll.pause_playing()
            self.piano_keyboard.set_active_labels(set())
            self.piano_roll.set_active_labels(set())
            self._paused = True
            self._set_playback_status("已暫停")
        self._refresh_play_button()

    @Slot()
    def stop_playback(self, wait_until_finished: bool = False) -> None:
        if self._worker is None:
            return
        self._worker.request_stop()
        self.stop_button.setEnabled(False)
        self._set_playback_status("停止中…")
        if wait_until_finished and self._thread is not None and self._thread.isRunning():
            # 阻塞 main thread 直到 worker run() 跑完 finally(_release_all + emit finished)。
            # 給匯入流程用 — 必須在 _set_editor_text 之前確定 worker 已完全停止,
            # 否則 worker 仍在按舊 sheet 的 schedule 送鍵會跟新譜面 UI 錯位。
            # 1000ms 對齊 closeEvent 內的 timeout(_release_all 最壞 ~100ms)。
            if not self._thread.wait(1000):
                self._thread.quit()
                self._thread.wait(500)

    @Slot()
    def _on_settings_flush_timer(self) -> None:
        if perf.enabled:
            perf.log("gui", "settings_flush")
        self._settings.flush()

    @Slot(int, str, object)
    def _on_playback_progress(self, _index: int, token: str, _labels) -> None:
        self.statusBar().showMessage(f"播放：{token}", 800)

    def _current_hotkey_map(self) -> dict[str, str]:
        """從 settings 組出 GlobalHotkeys.start 用的 hotkey_map。

        值是 KeybindCaptureWidget 拿到的鍵名字串(lowercased),空字串視為停用。
        """
        return {
            "play": str(self._settings.get("hotkey_play", "f6") or ""),
            "stop": str(self._settings.get("hotkey_stop", "f7") or ""),
            "pause": str(self._settings.get("hotkey_pause", "f8") or ""),
        }

    def _reapply_global_hotkeys(self) -> None:
        """settings 中任一 hotkey_* 變動時,把 GlobalHotkeys 重新註冊一次。"""
        if not hasattr(self, "_hotkeys") or self._hotkeys is None:
            return
        ok, message = self._hotkeys.restart(hotkey_map=self._current_hotkey_map())
        self.statusBar().showMessage(message, 3000)

    @Slot(float)
    def _on_seek_requested(self, position_seconds: float) -> None:
        if self._worker is None:
            return
        self._worker.request_seek(position_seconds)
        self.piano_roll.seek_to(position_seconds)
        self.piano_keyboard.set_active_labels(set())
        self.piano_roll.set_active_labels(set())
        self.statusBar().showMessage(f"跳到 {position_seconds:.2f} 秒", 1500)

    @Slot(str, object)
    def _on_panel_setting_changed(self, key: str, value) -> None:
        """設定面板任何 widget 變動的統一入口。

        負責:
          1. 寫入 settings(原 dialog 每個 handler 都會做這件事,集中起來)。
          2. 視 key 觸發 side-effect:
             - heist_*:走 sync_heist_controller_config + ensure_running
             - 視覺(zoom/fps/pitch_sort/note_color_style/show_piano_keyboard/animations):
               同步給 piano_roll / overview_bar / 主視窗 flag
             - automation_hotkeys_enabled:重新註冊全域熱鍵
             - auto_pause_on_focus_loss / mute_on_focus_loss / focus_game_on_play:更新對應 flag
        """
        # 高頻 slider 類 key 走 defer_set + QTimer flush(300ms debounce),
        # 拖動時不會每像素 atomic-write settings.json。
        if perf.enabled:
            debounced = key in self._SETTINGS_DEBOUNCE_KEYS
            perf.log("gui", "setting_change", key=key, value=value, debounced=debounced)
        if key in self._SETTINGS_DEBOUNCE_KEYS:
            self._settings.defer_set(key, value)
            self._settings_flush_timer.start()
        else:
            self._settings.set(key, value)

        # heist 設定變動:把整份 config push 到 controller,並依 pickup_enabled
        # 決定 controller 啟停。
        if key in (
            "heist_enabled", "heist_trigger_key", "heist_auto_mode",
            "heist_auto_mode_hotkey",
        ):
            self._sync_heist_controller_config()
            ok = self._ensure_heist_controller_running()
            if not ok and key == "heist_enabled":
                # controller 起不來時 rollback toggle 到關閉
                self._settings.set(key, False)
                if hasattr(self, "_settings_panel"):
                    self._settings_panel.refresh_from_settings()
            return

        # 視覺 / 一般 flag
        if key == "playback_speed":
            self._playback_speed = float(value)
            self.piano_roll.set_playback_speed(self._playback_speed)
            return
        if key == "zoom_factor":
            self.piano_roll.set_zoom_factor(float(value))
            return
        if key == "countdown_seconds":
            # 純 setting,不需要 side-effect(下次播放 worker 讀 settings.get 即可)
            return
        if key == "roll_fps":
            self._roll_fps = int(value)
            self.piano_roll.set_fps(self._roll_fps)
            return
        if key == "note_color_style":
            self._note_color_style = apply_note_color_style(
                str(value), self._read_custom_note_colors()
            )
            self.piano_roll.update()
            if hasattr(self, "overview_bar"):
                self.overview_bar.update()
            self.piano_keyboard.update()
            return
        if key in ("custom_note_h_color", "custom_note_m_color", "custom_note_l_color"):
            # 自訂三色變動;只在當前 style == "custom" 時才重套 THEME 並重繪。
            # 不論啟用與否都先把值存進 settings(主視窗 _on_panel_setting_changed
            # 路徑統一在外面 settings.set,所以此處只負責 side-effect)。
            if self._note_color_style == "custom":
                self._note_color_style = apply_note_color_style(
                    "custom", self._read_custom_note_colors()
                )
                self.setStyleSheet(self._stylesheet())
                self.piano_roll.update()
                if hasattr(self, "overview_bar"):
                    self.overview_bar.update()
                self.piano_keyboard.update()
            return
        if key == "pitch_sort_mode":
            self._pitch_sort_mode = bool(value)
            self.piano_roll.set_pitch_sort_mode(self._pitch_sort_mode)
            return
        if key == "show_piano_keyboard":
            self._show_piano_keyboard = bool(value)
            self.piano_keyboard.setVisible(self._show_piano_keyboard)
            return
        if key == "animations_enabled":
            self._on_animations_toggled(bool(value))
            return
        if key == "focus_game_on_play":
            self._focus_game_on_play = bool(value)
            return
        if key == "piano_sound_enabled":
            self._sound_player.set_enabled(bool(value))
            return
        if key == "piano_sound_volume":
            self._sound_player.set_volume(float(value))
            return
        if key == "preview_mode":
            self._preview_mode = bool(value)
            if self._worker is not None:
                self.statusBar().showMessage(
                    "編輯模式變更會在下一次播放生效", 3000
                )
            return
        if key == "auto_pause_on_focus_loss":
            self._on_focus_loss_pause_changed(bool(value))
            return
        if key == "mute_on_focus_loss":
            self._on_mute_on_focus_loss_toggled(bool(value))
            return
        if key in ("confirm_discard_unsaved", "confirm_delete_song"):
            # 純設定;_confirm_discard_changes / delete_current_song 下一次呼叫時
            # 自己讀 settings,不需要立即推 side-effect。
            return
        if key in ("hotkey_play", "hotkey_stop", "hotkey_pause"):
            self._reapply_global_hotkeys()
            return
        if key == "automation_hotkeys_enabled":
            # 已移除 toggle,留分支吞掉舊 setting 的可能 emit,維持 forward-compat。
            return

    @Slot(bool)
    def _on_animations_toggled(self, enabled: bool) -> None:
        self._animations_enabled = enabled
        self.piano_keyboard.set_animations_enabled(enabled)
        self.piano_roll.set_animations_enabled(enabled)
        # 平滑捲動/縮放已併入此總開關,連帶開關 piano roll 的平滑行為。
        self.piano_roll.set_smooth_browse_enabled(enabled)
        self.piano_roll.set_smooth_zoom_enabled(enabled)
        if not enabled and self._dock_animation is not None and self._dock_animation.state() == QVariantAnimation.Running:
            self._dock_animation.stop()
            self._editor_dock.setFixedWidth(self._dock_natural_width)
            self._editor_dock.setGraphicsEffect(None)
        self.statusBar().showMessage(
            "動畫與平滑效果已開啟" if enabled else "動畫與平滑效果已關閉", 2000
        )

    def _start_play_glow(self) -> None:
        """播放中讓播放按鈕散發呼吸光暈（已停用）。"""
        return

    def _stop_play_glow(self) -> None:
        """停止播放按鈕的呼吸光暈（已停用）。"""
        return

    def _set_playback_status(self, text: str) -> None:
        self._playback_status.setText(text)

    @Slot(str)
    def _on_playback_failed(self, message: str) -> None:
        self.piano_keyboard.set_active_labels(set())
        self.piano_roll.set_active_labels(set())
        self._show_error("播放失敗", message)

    @Slot(bool)
    def _on_playback_finished(self, stopped: bool) -> None:
        self._last_playback_stopped = stopped
        # 從 worker 取最後位置(以「主動 stop」場景為主;自然播完則為譜面尾)。
        # piano_roll、scrollbar、overview 都對齊到這個位置,下次按播放從這裡接續。
        last_pos = 0.0
        if self._worker is not None:
            try:
                last_pos = float(self._worker.current_position())
            except Exception:  # noqa: BLE001
                last_pos = self.piano_roll.current_seconds()
        # 播完整曲時(沒被主動停下)位置會落在譜面尾,留給 _play_sheet 內的尾端偵測
        # 自動把 initial_offset 重設為 0;這裡單純把位置忠實寫回。
        if self._sheet is not None and self._sheet.total_beats > 0:
            total = self._sheet.beats_to_seconds(self._sheet.total_beats)
            if not stopped:
                # 自然播完:停點往後挪一些,讓游標停在最後一個音符之後而非壓在其上。
                # 仍 >= total-0.05,下次按播放的尾端偵測會照常從 0 起播。
                last_pos = max(0.0, total) + PLAYBACK_FINISH_TAIL_SECONDS
            else:
                last_pos = min(last_pos, max(0.0, total))
        self.piano_roll.stop_playing(last_pos)
        self.piano_keyboard.set_active_labels(set())
        # 同步 scrollbar / overview / now_label 到停止位置。
        self.scrollbar.blockSignals(True)
        try:
            self.scrollbar.setValue(int(max(0.0, last_pos) * 100))
        finally:
            self.scrollbar.blockSignals(False)
        self._sync_overview_view_window()
        self._refresh_now_label()

    @Slot()
    def _on_thread_finished(self) -> None:
        worker = self._worker
        thread = self._thread
        self._worker = None
        self._thread = None
        if self._focus_check_timer.isActive():
            self._focus_check_timer.stop()
        if worker is not None:
            worker.deleteLater()
        if thread is not None:
            thread.deleteLater()
        self.play_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self._paused = False
        self._auto_paused = False
        self._refresh_play_button()
        self._set_playback_status("")
        self.statusBar().showMessage(
            "已停止" if self._last_playback_stopped else "播放完成", 3000
        )

    def _update_title(self) -> None:
        name = self._current_file.name if self._current_file else "未命名"
        dirty = "*" if self._dirty else ""
        self.setWindowTitle(f"{dirty}{name} — {APP_TITLE} v{APP_VERSION}")

    def _confirm_discard_changes(self) -> bool:
        if not self._dirty:
            return True
        # 設定面板可關掉「未存檔提示」,關掉時直接放行 = 視為同意覆蓋。
        if not bool(self._settings.get("confirm_discard_unsaved", True)):
            return True
        result = QMessageBox.question(
            self,
            "尚未儲存",
            "目前譜面尚未儲存，確定要繼續？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        return result == QMessageBox.Yes

    # ---------- 設定切換 ----------

    @Slot(bool)
    def _on_show_keyboard_toggled(self, checked: bool) -> None:
        self._show_piano_keyboard = bool(checked)
        self._settings.set("show_piano_keyboard", self._show_piano_keyboard)
        self.piano_keyboard.setVisible(self._show_piano_keyboard)
        self.statusBar().showMessage(
            "已顯示遊戲鋼琴鍵盤" if checked else "已隱藏遊戲鋼琴鍵盤", 2000
        )

    def _on_roll_fps_selected(self, fps: int) -> None:
        fps = int(fps)
        if fps not in (30, 60, 120):
            return
        self._roll_fps = fps
        self._settings.set("roll_fps", fps)
        self.piano_roll.set_fps(fps)
        # 同步 menu 勾選狀態(防止點同一個再次觸發)
        for f, act in self._roll_fps_actions.items():
            if act.isChecked() != (f == fps):
                act.blockSignals(True)
                try:
                    act.setChecked(f == fps)
                finally:
                    act.blockSignals(False)
        self.statusBar().showMessage(f"卷簾更新率已設定為 {fps} FPS", 2000)

    @Slot(bool)
    def _on_pitch_sort_toggled(self, checked: bool) -> None:
        self._pitch_sort_mode = bool(checked)
        self._settings.set("pitch_sort_mode", self._pitch_sort_mode)
        self.piano_roll.set_pitch_sort_mode(self._pitch_sort_mode)
        self.statusBar().showMessage(
            "Piano Roll 已切換為依音高排序(H7 在頂、L1 在底)"
            if checked else
            "Piano Roll 已切換為依區段排序(H/M/L 各段內 1..7)",
            2500,
        )

    def _read_custom_note_colors(self) -> dict[str, str]:
        """從 settings 撈三色,給 apply_note_color_style 用。"""
        return {
            "H": str(self._settings.get("custom_note_h_color", "#ff7a59")),
            "M": str(self._settings.get("custom_note_m_color", "#4dd0c2")),
            "L": str(self._settings.get("custom_note_l_color", "#8a7cff")),
        }

    def _on_color_style_selected(self, key: str) -> None:
        actual = apply_note_color_style(key, self._read_custom_note_colors())
        self._note_color_style = actual
        self._settings.set("note_color_style", actual)
        for k, act in self._color_style_actions.items():
            act.blockSignals(True)
            act.setChecked(k == actual)
            act.blockSignals(False)
        # 樣式表內已混入了 H/M/L 顏色 (例如 piano_roll 元素) → 重套
        self.setStyleSheet(self._stylesheet())
        self.piano_roll.update()
        self.piano_keyboard.update()
        if hasattr(self, "overview_bar"):
            self.overview_bar.update()
        label = NOTE_COLOR_STYLES.get(actual, NOTE_COLOR_STYLES["default"])["label"]
        self.statusBar().showMessage(f"音符配色已切換:{label}", 2500)

    @Slot(bool)
    def _on_focus_loss_pause_changed(self, checked: bool) -> None:
        self._auto_pause_on_focus_loss = bool(checked)
        self._settings.set("auto_pause_on_focus_loss", self._auto_pause_on_focus_loss)
        if not checked and self._focus_check_timer.isActive():
            self._focus_check_timer.stop()
        elif checked and self._worker is not None and self._game_hwnd:
            self._focus_check_timer.start()
        self.statusBar().showMessage(
            "失焦自動暫停已開啟" if checked else "失焦自動暫停已關閉", 2000
        )

    @Slot(bool)
    def _on_focus_game_toggled(self, checked: bool) -> None:
        self._focus_game_on_play = bool(checked)
        self._settings.set("focus_game_on_play", self._focus_game_on_play)
        self.statusBar().showMessage(
            "播放時自動聚焦遊戲視窗已開啟" if checked else "播放時自動聚焦遊戲視窗已關閉", 2000
        )

    @Slot(int)
    def _on_speed_selected(self, index: int) -> None:
        speed = self.speed_combo.itemData(index)
        try:
            speed = float(speed)
        except (TypeError, ValueError):
            speed = 1.0
        self._playback_speed = speed
        self._settings.set("playback_speed", speed)
        self.piano_roll.set_playback_speed(speed)
        if self._worker is not None:
            self._worker.set_speed(speed)
            self.statusBar().showMessage(
                f"播放速度即時切換為 {speed:g}×", 2000
            )
        else:
            self.statusBar().showMessage(f"播放速度設為 {speed:g}×", 2000)

    @Slot(float)
    def _on_zoom_changed(self, factor: float) -> None:
        self._settings.set("zoom_factor", float(factor))
        total = (self.piano_roll.look_behind_seconds + self.piano_roll.look_ahead_seconds)
        self.statusBar().showMessage(f"視野 {total:.1f}s", 2000)

    @Slot(float, float)
    def _on_tempo_change_set(self, beats: float, bpm: float) -> None:
        if self._sheet is None or bpm <= 0:
            return
        self._push_undo()
        # 移除同位置舊值,加入新值
        self._sheet.tempo_changes = [
            (b, t) for b, t in self._sheet.tempo_changes if abs(b - beats) > 1e-6
        ]
        if beats <= 1e-6:
            self._sheet.tempo = float(bpm)
        else:
            self._sheet.tempo_changes.append((float(beats), float(bpm)))
            self._sheet.tempo_changes.sort()
        # 譜面文字編輯器已移除,不再需要 sheet → editor 反向同步
        self._dirty = True
        self._update_title()
        self._refresh_now_label()
        self.piano_roll.update()
        if hasattr(self, "overview_bar"):
            self.overview_bar.set_sheet(self._sheet)
        self.statusBar().showMessage(
            f"已設定變速:{bpm:g} BPM @ 第 {beats:g} 拍", 3000
        )

    @Slot(float)
    def _on_tempo_change_remove(self, beats: float) -> None:
        if self._sheet is None:
            return
        before = len(self._sheet.tempo_changes)
        self._push_undo()
        self._sheet.tempo_changes = [
            (b, t) for b, t in self._sheet.tempo_changes if abs(b - beats) > 1e-6
        ]
        if len(self._sheet.tempo_changes) == before:
            return
        # 譜面文字編輯器已移除,不再需要 sheet → editor 反向同步
        self._dirty = True
        self._update_title()
        self._refresh_now_label()
        self.piano_roll.update()
        if hasattr(self, "overview_bar"):
            self.overview_bar.set_sheet(self._sheet)
        self.statusBar().showMessage(f"已移除第 {beats:g} 拍的變速", 3000)

    @Slot(int, str, float, float, int)
    def _on_chord_stroke_extracted(
        self,
        orig_idx: int,
        label: str,
        new_start_beats: float,
        new_dur_beats: float,
        new_track_idx: int,
    ) -> None:
        """把和弦中的某個 stroke 拆出成獨立 event,放到指定位置/音高。"""
        if self._sheet is None:
            return
        if not (0 <= orig_idx < len(self._sheet.events)):
            return
        ev = self._sheet.events[orig_idx]
        if ev.is_rest or len(ev.strokes) <= 1:
            return
        keep_strokes = tuple(s for s in ev.strokes if s.label != label)
        extracted = next((s for s in ev.strokes if s.label == label), None)
        if extracted is None:
            return
        # 決定新事件的 stroke (依拖曳目的 track 換 pitch)
        new_stroke = extracted
        if 0 <= new_track_idx < len(TRACK_ORDER):
            prefix, _label, _disp, accidental, degree = TRACK_ORDER[new_track_idx]
            new_stroke = make_stroke(prefix, degree, accidental)

        self._push_undo()
        sheet = self._sheet
        # 更新原 chord (移除被拆的 stroke);若剩 0 個 → 整個事件刪除
        if keep_strokes:
            sheet.events[orig_idx] = replace(ev, strokes=keep_strokes)
        else:
            sheet.events.pop(orig_idx)
            new_colors = {}
            for k, v in self.piano_roll._event_colors.items():
                if k == orig_idx:
                    continue
                new_colors[k - 1 if k > orig_idx else k] = v
            self.piano_roll._event_colors = new_colors

        new_event = NoteEvent(
            start_beats=max(0.0, float(new_start_beats)),
            duration_beats=max(sheet.beat * 0.0625, float(new_dur_beats)),
            strokes=(new_stroke,),
            source=new_stroke.label,
            line=ev.line,
            track=ev.track,
        )
        sheet.events.append(new_event)
        sheet.events.sort(key=lambda e: (e.start_beats, e.line, e.track))
        # 找出新 event 在排序後的 index 並選中
        new_idx = next(
            (i for i, e in enumerate(sheet.events) if e is new_event),
            None,
        )
        if new_idx is not None:
            self.piano_roll._set_selected_strokes({(new_idx, new_stroke.label)})

        # 譜面文字編輯器已移除,不再需要 sheet → editor 反向同步
        self._dirty = True
        self._update_title()
        self._refresh_now_label()
        self.piano_roll.update()
        self._update_scrollbar_range()
        if hasattr(self, "overview_bar"):
            self.overview_bar.set_sheet(self._sheet)
        self.statusBar().showMessage(
            f"已從和弦拆出 {new_stroke.label} 為獨立音符 (=^･ω･^=)",
            3000,
        )

    # ---------- Undo / Redo ----------
    # 譜面文字編輯器移除後,改用 Sheet.to_text() snapshot 推進 undo/redo,
    # 仍能保留 piano_roll 拖曳/插入/刪除等動作的可回復性。

    def _push_undo(self) -> None:
        if self._sheet is None:
            return
        snapshot = self._sheet.to_text()
        if self._undo_stack and self._undo_stack[-1] == snapshot:
            return
        self._undo_stack.append(snapshot)
        if len(self._undo_stack) > self._undo_limit:
            self._undo_stack = self._undo_stack[-self._undo_limit:]
        self._redo_stack.clear()

    @Slot()
    def _undo(self) -> None:
        if not self._undo_stack:
            self.statusBar().showMessage("沒有可復原的動作", 1500)
            return
        current = self._sheet.to_text() if self._sheet is not None else ""
        target = self._undo_stack.pop()
        if target == current and self._undo_stack:
            target = self._undo_stack.pop()
        self._redo_stack.append(current)
        self._apply_text_snapshot(target)
        self.statusBar().showMessage("已復原", 1500)

    @Slot()
    def _redo(self) -> None:
        if not self._redo_stack:
            self.statusBar().showMessage("沒有可重做的動作", 1500)
            return
        current = self._sheet.to_text() if self._sheet is not None else ""
        target = self._redo_stack.pop()
        self._undo_stack.append(current)
        self._apply_text_snapshot(target)
        self.statusBar().showMessage("已重做", 1500)

    def _apply_text_snapshot(self, text: str) -> None:
        try:
            sheet = SheetParser.parse(text)
        except Exception as exc:  # noqa: BLE001
            self.statusBar().showMessage(f"還原失敗:{exc}", 4000)
            return
        # 保留 undo/redo 前的瀏覽位置(set_sheet 內會把 _browse_offset 歸零,
        # 那是為了「載入全新譜面」設計;undo/redo 不該把已捲到中段的視窗打回 0)。
        # 播放中時 current_seconds 包含 worker 推進,不能直接拿來還原,改保留 browse_offset。
        prev_browse = self.piano_roll._browse_offset
        # 進行中的平滑捲動動畫也停掉,避免動畫終點蓋過剛還原的位置。
        if hasattr(self.piano_roll, "_smooth_browse_anim"):
            self.piano_roll._smooth_browse_anim.stop()
        self._apply_sheet(sheet)
        # 把瀏覽位置 clamp 在新譜面總長內(snapshot 若把譜面變短,原位置可能超出),
        # 用 set_browse_offset 同步觸發 update。
        total = sheet.beats_to_seconds(sheet.total_beats)
        restored = max(0.0, min(prev_browse, total)) if total > 0 else 0.0
        self.piano_roll.set_browse_offset(restored)
        # scrollbar / overview 也跟著對齊到還原後的瀏覽位置
        if hasattr(self, "scrollbar"):
            self.scrollbar.blockSignals(True)
            try:
                self.scrollbar.setValue(int(restored * 100))
            finally:
                self.scrollbar.blockSignals(False)
        self._sync_overview_view_window()
        self._refresh_now_label()
        self._dirty = True
        self._update_title()

    @Slot()
    def _select_all_visible(self) -> None:
        if self._sheet is None:
            return
        all_strokes: set[tuple[int, str]] = set()
        for i, ev in enumerate(self._sheet.events):
            if ev.is_rest:
                continue
            for s in ev.strokes:
                all_strokes.add((i, s.label))
        self.piano_roll._set_selected_strokes(all_strokes)
        self.piano_roll.update()
        self.statusBar().showMessage(
            f"已全選 {len(self.piano_roll._selected_indices)} 個音符", 1500
        )

    # ---------- 複製 / 貼上 ----------

    @Slot()
    def _on_copy_shortcut(self) -> None:
        if self._sheet is None or not self.piano_roll._selected_indices:
            return
        indices = sorted(self.piano_roll._selected_indices)
        events = [self._sheet.events[i] for i in indices if 0 <= i < len(self._sheet.events)]
        if not events:
            return
        dsl = self._serialize_events_to_dsl(events)
        QApplication.clipboard().setText(dsl)
        self.statusBar().showMessage(f"已複製 {len(events)} 個音符到剪貼簿", 2000)

    def _serialize_events_to_dsl(self, events) -> str:
        if not events or self._sheet is None:
            return ""
        sheet = self._sheet
        beat = sheet.beat if sheet.beat > 0 else 0.5
        min_start = min(ev.start_beats for ev in events)
        by_track: dict[str, list] = {}
        for ev in events:
            by_track.setdefault(ev.track, []).append(ev)
        lines = [
            f"# nte-piano clip: {len(events)} events",
            f"tempo {_fmt_num(sheet.tempo)}",
            f"beat {_fmt_num(beat)}",
        ]
        for track in sorted(by_track):
            track_events = sorted(by_track[track], key=lambda e: e.start_beats - min_start)
            lines.append("")
            lines.append(f"track {track}")
            cursor = 0.0
            tokens: list[str] = []
            for ev in track_events:
                rel_start = ev.start_beats - min_start
                if rel_start > cursor + 1e-6:
                    gap_beats = rel_start - cursor
                    gap_mult = gap_beats / beat
                    tokens.append("-" if abs(gap_mult - 1.0) < 1e-6 else f"-*{_fmt_num(gap_mult)}")
                    cursor += gap_beats
                mult = ev.duration_beats / beat
                suffix = "" if abs(mult - 1.0) < 1e-6 else f"*{_fmt_num(mult)}"
                if not ev.strokes:
                    tokens.append(f"-{suffix}" if suffix else "-")
                elif len(ev.strokes) == 1:
                    tokens.append(f"{ev.strokes[0].label}{suffix}")
                else:
                    inner = "+".join(s.label for s in ev.strokes)
                    tokens.append(f"[{inner}]{suffix}")
                cursor = max(cursor, rel_start + ev.duration_beats)
            if tokens:
                lines.append("  " + " ".join(tokens))
        return "\n".join(lines) + "\n"

    @Slot()
    def _on_paste_shortcut(self) -> None:
        if self._sheet is None:
            return
        text = QApplication.clipboard().text()
        if not text or not text.strip():
            self.statusBar().showMessage("剪貼簿沒有內容", 2000)
            return
        try:
            clip_sheet = SheetParser.parse(text)
        except SheetParseError as exc:
            self.statusBar().showMessage(f"剪貼簿格式錯誤:{exc}", 4000)
            return
        if not clip_sheet.events:
            self.statusBar().showMessage("剪貼簿沒有可貼上的音符", 2000)
            return

        # 平移目標決策
        hover = self.piano_roll.hover_seconds()
        if hover is not None and hover > 0:
            target_seconds = hover
        elif self._worker is not None:
            target_seconds = self.piano_roll.current_seconds()
        else:
            target_seconds = self.piano_roll._browse_offset

        offset_beats = max(0.0, self._sheet.seconds_to_beats(target_seconds))

        self._push_undo()
        new_indices: set[int] = set()
        original_count = len(self._sheet.events)
        for ev in clip_sheet.events:
            new_event = NoteEvent(
                start_beats=ev.start_beats + offset_beats,
                duration_beats=ev.duration_beats,
                strokes=ev.strokes,
                source=ev.source,
                line=ev.line,
                track=ev.track,
            )
            self._sheet.events.append(new_event)
        self._sheet.events.sort(key=lambda e: (e.start_beats, e.line, e.track))
        for i, ev in enumerate(self._sheet.events):
            if not ev.is_rest and ev.start_beats >= offset_beats - 1e-6 and i >= original_count - len(clip_sheet.events):
                # 重新比對:用 source+start 配對
                pass
        # 簡化:把所有「位於 offset 之後且 source 在貼上的事件」都選起來
        clip_keys = {(ev.source, ev.line) for ev in clip_sheet.events}
        new_strokes: set[tuple[int, str]] = set()
        for i, ev in enumerate(self._sheet.events):
            if (ev.source, ev.line) in clip_keys and ev.start_beats >= offset_beats - 1e-6:
                new_indices.add(i)
                if not ev.is_rest:
                    for s in ev.strokes:
                        new_strokes.add((i, s.label))
        self.piano_roll._set_selected_strokes(new_strokes)
        self._dirty = True
        self._update_title()
        self._refresh_now_label()
        self._update_scrollbar_range()
        self.piano_roll.update()
        self.overview_bar.set_sheet(self._sheet)
        self.statusBar().showMessage(
            f"已貼上 {len(clip_sheet.events)} 個音符（從 {target_seconds:.2f}s）", 3000
        )

    @Slot()
    def _on_cut_requested(self) -> None:
        # 剪下 = 複製選取到剪貼簿 + 刪除選取(undo 由刪除那步 push 一次)。
        if self._sheet is None or not self.piano_roll._selected_indices:
            return
        indices = sorted(self.piano_roll._selected_indices)
        self._on_copy_shortcut()
        self._on_events_delete_requested(indices)

    @Slot(float, str)
    def _on_note_insert_requested(self, start_beats: float, label: str) -> None:
        # 右鍵選單「新增音符」:在指定拍數插入單音(音高 = label,視覺最頂那行)。
        if self._sheet is None:
            return
        idx = TRACK_INDEX.get(label)
        if idx is None:
            return
        prefix, _lbl, _disp, accidental, degree = TRACK_ORDER[idx]
        stroke = make_stroke(prefix, degree, accidental)
        self._push_undo()
        beat = self._sheet.beat if self._sheet.beat > 0 else 0.5
        new_event = NoteEvent(
            start_beats=max(0.0, float(start_beats)),
            duration_beats=beat,
            strokes=(stroke,),
            source="piano_roll",
            line=0,
            track="main",
        )
        self._sheet.events.append(new_event)
        self._sheet.events.sort(key=lambda e: (e.start_beats, e.line, e.track))
        self._dirty = True
        self._update_title()
        self._refresh_now_label()
        self._update_scrollbar_range()
        self.piano_roll.update()
        if hasattr(self, "overview_bar"):
            self.overview_bar.set_sheet(self._sheet)
        self.statusBar().showMessage(
            f"已新增音符 {label}（第 {float(start_beats):.2f} 拍）", 2000
        )

    # _replace_editor_text / _sync_editor_from_sheet / _write_autosave /
    # _clear_autosave / _maybe_offer_autosave_restore 都隨譜面文字編輯器一起移除。

    # ---------- 失焦自動暫停 ----------

    def _self_hwnd(self) -> int:
        if self._self_hwnd_cache is None:
            try:
                self._self_hwnd_cache = int(self.winId())
            except Exception:  # noqa: BLE001
                self._self_hwnd_cache = 0
        return self._self_hwnd_cache or 0

    @Slot()
    def _on_focus_check_tick(self) -> None:
        if self._worker is None or not self._auto_pause_on_focus_loss:
            return
        if not self._game_hwnd:
            return
        fg = foreground_hwnd()
        if fg == 0:
            return
        if fg == self._game_hwnd or fg == self._self_hwnd():
            if self._auto_paused and self._paused and fg == self._game_hwnd:
                self._auto_paused = False
                self.toggle_pause()
            return
        if not self._paused:
            self._auto_paused = True
            self.toggle_pause()

    # ---------- OverviewBar 同步 ----------

    @Slot(float)
    def _on_overview_seek(self, seconds: float) -> None:
        if self._worker is not None:
            self._on_seek_requested(seconds)
        else:
            self.piano_roll.set_browse_offset(seconds)
            self.scrollbar.blockSignals(True)
            try:
                self.scrollbar.setValue(int(seconds * 100))
            finally:
                self.scrollbar.blockSignals(False)
            self._sync_overview_view_window()

    @Slot(float)
    def _on_overview_view_drag(self, start_seconds: float) -> None:
        if self._worker is not None:
            return
        target = max(0.0, start_seconds + self.piano_roll.look_behind_seconds)
        self.piano_roll.set_browse_offset(target)
        self.scrollbar.blockSignals(True)
        try:
            self.scrollbar.setValue(int(target * 100))
        finally:
            self.scrollbar.blockSignals(False)
        self._sync_overview_view_window()

    @Slot(object, object)
    def _on_loop_range_changed(self, start_seconds, end_seconds) -> None:
        """OverviewBar 或 PianoRollView 改 marker 後同步到 main window 屬性 + sheet + spinbox,
        並把對方那一個 widget 的 marker 也帶到一致狀態。

        worker 啟動時讀這兩個值決定播放範圍;running 中改不會即時生效
        (避免 schedule rebuild 跟 cursor 不連續)— 需停掉再播。

        AB 點也寫進 sheet.play_range_*_seconds 並反映到 BPM 區的 spinbox,
        讓拖曳/spinbox/sheet 三向同步。寫入時用 _suppress_sheet_field_signals
        防止 spinbox setValue 又 echo 回 _set_sheet_field。
        """
        self._loop_start_seconds = (
            float(start_seconds) if start_seconds is not None else None
        )
        self._loop_end_seconds = (
            float(end_seconds) if end_seconds is not None else None
        )
        # 把兩個 widget 的 markers 都同步成相同狀態(set_loop_range 不 emit signal,
        # 不會 echo 回這個 slot)。其中一個是源頭、值已對齊,另一個需要被推進。
        if hasattr(self, "overview_bar"):
            self.overview_bar.set_loop_range(
                self._loop_start_seconds, self._loop_end_seconds
            )
        if hasattr(self, "piano_roll"):
            self.piano_roll.set_loop_range(
                self._loop_start_seconds, self._loop_end_seconds
            )
        if self._sheet is not None:
            self._sheet.play_range_start_seconds = self._loop_start_seconds
            self._sheet.play_range_end_seconds = self._loop_end_seconds
            self._dirty = True
            self._update_title()
        # 同步 spinbox(suppress 防回授)
        if hasattr(self, "play_a_spin") and hasattr(self, "play_b_spin"):
            self._suppress_sheet_field_signals = True
            try:
                self.play_a_spin.setValue(
                    -1.0 if self._loop_start_seconds is None else float(self._loop_start_seconds)
                )
                self.play_b_spin.setValue(
                    -1.0 if self._loop_end_seconds is None else float(self._loop_end_seconds)
                )
            finally:
                self._suppress_sheet_field_signals = False
        if self._loop_start_seconds is None and self._loop_end_seconds is None:
            self.statusBar().showMessage("已清除播放區間", 2000)
        elif self._loop_start_seconds is not None and self._loop_end_seconds is not None:
            self.statusBar().showMessage(
                f"播放區間 = {self._loop_start_seconds:.1f}s ~ {self._loop_end_seconds:.1f}s"
                + ("(將於下次按播放生效)" if self._worker is not None else ""),
                3000,
            )
        else:
            which = "起點" if self._loop_start_seconds is not None else "終點"
            self.statusBar().showMessage(f"已設區間{which}(另一端未設前不限制範圍)", 2000)

    def _sync_overview_view_window(self) -> None:
        current = self.piano_roll.current_seconds()
        view_start = max(0.0, current - self.piano_roll.look_behind_seconds)
        view_end = current + self.piano_roll.look_ahead_seconds
        self.overview_bar.set_cursor(current)
        self.overview_bar.set_view_window(view_start, view_end)

    # ---------- 拖放 ----------

    def dragEnterEvent(self, event):  # noqa: N802
        if not event.mimeData().hasUrls():
            return
        urls = event.mimeData().urls()
        if not urls:
            return
        path = Path(urls[0].toLocalFile())
        suffix = path.suffix.lower()
        if suffix in DSL_EXTENSIONS or suffix in MUSICXML_EXTENSIONS or suffix in MIDI_EXTENSIONS or suffix in MSCZ_EXTENSIONS:
            event.acceptProposedAction()

    def dropEvent(self, event):  # noqa: N802
        urls = event.mimeData().urls()
        if not urls:
            return
        path = Path(urls[0].toLocalFile())
        suffix = path.suffix.lower()
        if not self._confirm_discard_changes():
            return
        if suffix in DSL_EXTENSIONS:
            self._load_file(path)
        elif suffix in MUSICXML_EXTENSIONS:
            self._import_musicxml_path(path)
        elif suffix in MIDI_EXTENSIONS:
            self._import_midi_path(path)
        elif suffix in MSCZ_EXTENSIONS:
            self._import_mscz_path(path)
        else:
            self.statusBar().showMessage(f"不支援的檔案格式:{suffix}", 4000)
        event.acceptProposedAction()

    # ---------- MusicXML / MIDI 匯入(共用路徑) ----------

    def _log_import_error(self, path: Path, exc: Exception) -> None:
        try:
            self.IMPORT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            stamp = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with self.IMPORT_LOG_PATH.open("a", encoding="utf-8") as f:
                import traceback
                f.write(f"[{stamp}] {path}\n")
                f.write(traceback.format_exc())
                f.write("\n")
        except OSError:
            pass

    def _save_imported_to_songs(self, stem: str, text: str) -> Path | None:
        """把匯入的 DSL 文字寫入 songs/{stem}.txt。同名衝突自動加 (2)/(3) 後綴避免覆蓋。"""
        songs_dir = _user_data_dir("songs")
        try:
            songs_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            return None
        target = songs_dir / f"{stem}.txt"
        if target.exists():
            idx = 2
            while True:
                candidate = songs_dir / f"{stem} ({idx}).txt"
                if not candidate.exists():
                    target = candidate
                    break
                idx += 1
        try:
            target.write_text(text, encoding="utf-8")
        except OSError:
            return None
        return target

    def _apply_imported_text(
        self,
        text: str,
        source_path: Path,
        save_to_songs: bool,
        status_message: str,
    ) -> None:
        """匯入結尾共用流程:解析 DSL → 套到 sheet → 可選另存到 songs/。

        save_to_songs=False(預設)時,曲目下拉會顯示「(未存檔) 標題」臨時項;
        實際存檔(_save_file)/切換歌曲/載入別檔時這個臨時項就會被清掉。
        """
        self._push_undo()
        try:
            sheet = SheetParser.parse(text)
        except Exception as exc:  # noqa: BLE001
            self._show_error("匯入後解析失敗", str(exc))
            return
        self._clear_loop_range()
        self._imported_source_name = source_path.stem
        target: Path | None = None
        if save_to_songs:
            target = self._save_imported_to_songs(source_path.stem, text)
        self._apply_sheet(sheet)
        if target is not None:
            self._current_file = target
            self._dirty = False
            # 已落盤,清掉可能殘留的臨時項 state(避免 refresh 又插回來)。
            self._unsaved_combo_title = None
            self._update_title()
            self._populate_song_combo()
            self._set_song_combo_path(target)
            self.statusBar().showMessage(
                f"{status_message}  →  已另存 {target.name}", 8000
            )
        else:
            self._current_file = None
            self._dirty = True
            # 標題優先取 DSL 開頭 `# title`(importer 都會寫),沒有才用來源 stem。
            title = self._extract_title_from_dsl(text) or source_path.stem
            self._unsaved_combo_title = title
            self._update_title()
            # refresh combo 讓 (未存檔) 臨時項出現並 currentIndex 設到它。
            self._populate_song_combo()
            self.statusBar().showMessage(
                f"{status_message}  →  (未存檔)按 Ctrl+S 存入 songs/", 8000
            )

    def _import_musicxml_path(self, path: Path) -> None:
        # 播放中時先同步停止 — 切譜前 worker 必須先收尾,否則舊 schedule 的鍵
        # 會繼續按下/釋放,跟新 sheet UI 對不上,造成「狀態奇怪」。
        self.stop_playback(wait_until_finished=True)
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            try:
                root = MusicXMLImporter.load_score(path)
                suggested = MusicXMLImporter.suggest_transpose(root)
                suggested_range = MusicXMLImporter.suggest_transpose_for_range(root)
            except Exception as exc:  # noqa: BLE001
                self._log_import_error(path, exc)
                self.statusBar().showMessage(
                    f"匯入失敗:{path.name}({exc.__class__.__name__})", 6000
                )
                return
        finally:
            QApplication.restoreOverrideCursor()
        dialog = ImportOptionsDialog(self, suggested, suggested_range)
        if dialog.exec() != QDialog.Accepted:
            return
        opts = dialog.values()
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            try:
                text, stats = MusicXMLImporter.to_dsl(
                    path,
                    transpose=opts["transpose"],
                    right_prefer=opts["right_prefer"],
                    left_prefer=opts["left_prefer"],
                    melody_mode=opts.get("melody_mode", "dense"),
                    import_tempo_changes=opts.get("import_tempo_changes", True),
                )
            except Exception as exc:  # noqa: BLE001
                self._log_import_error(path, exc)
                self.statusBar().showMessage(
                    f"轉換 {path.name} 時發生錯誤:{exc}", 8000
                )
                return
        finally:
            QApplication.restoreOverrideCursor()
        self._apply_imported_text(
            text,
            path,
            save_to_songs=bool(opts.get("save_to_songs", True)),
            status_message=(
                f"已匯入 {path.name}：右手 {stats['right_count']} / 左手 {stats['left_count']} 個音，"
                f"tempo {stats['tempo']:g}，移調 {stats['transpose']:+d}"
            ),
        )

    def _import_midi_path(self, path: Path) -> None:
        self.stop_playback(wait_until_finished=True)
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            try:
                suggested = MidiImporter.suggest_transpose(path)
                suggested_range = MidiImporter.suggest_transpose_for_range(path)
            except Exception as exc:  # noqa: BLE001
                self._log_import_error(path, exc)
                self.statusBar().showMessage(
                    f"MIDI 匯入失敗:{path.name}({exc.__class__.__name__})", 6000
                )
                return
        finally:
            QApplication.restoreOverrideCursor()
        dialog = ImportOptionsDialog(self, suggested, suggested_range)
        if dialog.exec() != QDialog.Accepted:
            return
        opts = dialog.values()
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            try:
                text, stats = MidiImporter.to_dsl(
                    path,
                    transpose=opts["transpose"],
                    right_prefer=opts["right_prefer"],
                    left_prefer=opts["left_prefer"],
                    melody_mode=opts.get("melody_mode", "dense"),
                    import_tempo_changes=opts.get("import_tempo_changes", True),
                )
            except Exception as exc:  # noqa: BLE001
                self._log_import_error(path, exc)
                self.statusBar().showMessage(
                    f"轉換 MIDI {path.name} 時發生錯誤:{exc}", 8000
                )
                return
        finally:
            QApplication.restoreOverrideCursor()
        self._apply_imported_text(
            text,
            path,
            save_to_songs=bool(opts.get("save_to_songs", True)),
            status_message=(
                f"已匯入 MIDI {path.name}：右手 {stats['right_count']} / 左手 {stats['left_count']} 個音，"
                f"tempo {stats['tempo']:g}，移調 {stats['transpose']:+d}"
            ),
        )

    @Slot()
    def import_midi(self) -> None:
        if not self._confirm_discard_changes():
            return
        filename, _ = QFileDialog.getOpenFileName(
            self,
            "匯入 MIDI",
            str(Path.cwd()),
            "MIDI (*.mid *.midi);;All files (*.*)",
        )
        if not filename:
            return
        self._import_midi_path(Path(filename))

    @Slot()
    def import_mscz(self) -> None:
        if not self._confirm_discard_changes():
            return
        filename, _ = QFileDialog.getOpenFileName(
            self,
            "匯入 MuseScore",
            str(Path.cwd()),
            "MuseScore (*.mscz *.mscx);;All files (*.*)",
        )
        if not filename:
            return
        self._import_mscz_path(Path(filename))

    def _import_mscz_path(self, path: Path) -> None:
        self.stop_playback(wait_until_finished=True)
        QApplication.setOverrideCursor(Qt.WaitCursor)
        tmp_xml: Path | None = None
        try:
            try:
                tmp_xml = MsczImporter.prepare_musicxml(path)
                root = MusicXMLImporter.load_score(tmp_xml)
                suggested = MusicXMLImporter.suggest_transpose(root)
                suggested_range = MusicXMLImporter.suggest_transpose_for_range(root)
            except Exception as exc:  # noqa: BLE001
                self._log_import_error(path, exc)
                self.statusBar().showMessage(
                    f"MSCZ 匯入失敗:{path.name}({exc.__class__.__name__})", 6000
                )
                if tmp_xml is not None:
                    try:
                        tmp_xml.unlink()
                    except OSError:
                        pass
                return
        finally:
            QApplication.restoreOverrideCursor()
        dialog = ImportOptionsDialog(self, suggested, suggested_range, is_mscz=True)
        if dialog.exec() != QDialog.Accepted:
            try:
                tmp_xml.unlink()
            except OSError:
                pass
            return
        opts = dialog.values()
        fmt = opts.get("mscz_format", "musicxml")
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            try:
                if fmt == "midi":
                    # 走 MIDI 中介:不用 tmp_xml,再呼叫一次 MuseScore CLI 拿 tmp_mid。
                    tmp_mid = MsczImporter.prepare_midi(path)
                    try:
                        text, stats = MidiImporter.to_dsl(
                            tmp_mid,
                            transpose=opts["transpose"],
                            right_prefer=opts["right_prefer"],
                            left_prefer=opts["left_prefer"],
                            melody_mode=opts.get("melody_mode", "dense"),
                            import_tempo_changes=opts.get("import_tempo_changes", True),
                        )
                    finally:
                        try:
                            tmp_mid.unlink()
                        except OSError:
                            pass
                else:
                    text, stats = MusicXMLImporter.to_dsl(
                        tmp_xml,
                        transpose=opts["transpose"],
                        right_prefer=opts["right_prefer"],
                        left_prefer=opts["left_prefer"],
                        melody_mode=opts.get("melody_mode", "dense"),
                        import_tempo_changes=opts.get("import_tempo_changes", True),
                    )
            except Exception as exc:  # noqa: BLE001
                self._log_import_error(path, exc)
                self.statusBar().showMessage(
                    f"轉換 MSCZ {path.name} 時發生錯誤:{exc}", 8000
                )
                return
            finally:
                try:
                    tmp_xml.unlink()
                except OSError:
                    pass
        finally:
            QApplication.restoreOverrideCursor()
        fmt_label = "MIDI" if fmt == "midi" else "MusicXML"
        self._apply_imported_text(
            text,
            path,
            save_to_songs=bool(opts.get("save_to_songs", True)),
            status_message=(
                f"已匯入 MSCZ {path.name}({fmt_label})：右手 {stats['right_count']} / 左手 {stats['left_count']} 個音，"
                f"tempo {stats['tempo']:g}，移調 {stats['transpose']:+d}"
            ),
        )

    def _show_error(self, title: str, message: str) -> None:
        QMessageBox.critical(self, title, message)
        self.statusBar().showMessage(message, 6000)

    # ──────────── 自動化任務(釣魚 / 閃避 / 音游) ────────────

    @Slot(bool)
    # _on_heist_toggled 已合併進 _on_panel_setting_changed,
    # 不再走 modal dialog。

    def _ensure_heist_controller_running(self) -> bool:
        """根據 pickup_enabled 狀態決定 controller 啟停。

        enabled 就 start(若已 running 則 no-op);disabled 才 stop。
        回 True 表示「期望 running 且實際 running」或「期望 stopped 且已 stopped」,
        False 表示 start 失敗(GUI 端應 rollback)。
        """
        pickup_on = bool(self._settings.get("heist_enabled", False))
        if pickup_on:
            if self._heist_controller.is_running():
                return True
            return self._heist_controller.start()
        else:
            if self._heist_controller.is_running():
                self._heist_controller.stop()
            return True

    def _sync_heist_controller_config(self) -> None:
        """把目前所有 heist_* 設定 push 到 controller(start/設定變動共用)。"""
        self._heist_controller.update_config(
            pickup_enabled=bool(self._settings.get("heist_enabled", False)),
            trigger_key=str(self._settings.get("heist_trigger_key", "f")),
            use_scroll=True,
            auto_mode=bool(self._settings.get("heist_auto_mode", False)),
            auto_mode_hotkey=str(self._settings.get("heist_auto_mode_hotkey", "f8")),
        )

    @Slot(bool)
    def _on_heist_auto_mode_changed_from_thread(self, new_value: bool) -> None:
        """controller polling thread 偵測到 toggle 熱鍵後 invoke 回主執行緒。

        負責落盤 heist_auto_mode 並在狀態列提示。不更新 menu(粉爪是單一 toggle
        action,不顯示子狀態);使用者下次開設定對話框時 checkbox 會反映新值。
        """
        self._settings.set("heist_auto_mode", bool(new_value))
        self.statusBar().showMessage(
            f"粉爪全自動拾取:{'已啟用' if new_value else '已關閉'}",
            3000,
        )

    @Slot(bool)
    def _on_mute_on_focus_loss_toggled(self, checked: bool) -> None:
        # 切換選單後立刻啟動或停止 muter,並落盤 setting。
        # 停止時 BackgroundAudioMuter.stop() 內部會強制 SetMute(False),
        # 確保遊戲不會殘留被靜音的狀態。
        self._settings.set("mute_on_focus_loss", bool(checked))
        if checked:
            if not BackgroundAudioMuter.is_available():
                err = BackgroundAudioMuter.availability_error()
                self.statusBar().showMessage(
                    f"失焦自動靜音不可用:{err}(裝完 pycaw 後請重啟)",
                    8000,
                )
                self._append_automation_log(
                    f"[錯誤] 失焦自動靜音不可用:{err}\n"
                    f"請執行: py -3.14 -m pip install pycaw comtypes\n"
                    f"裝完後關掉 Piano Player 重開"
                )
                self.act_auto_mute.blockSignals(True)
                self.act_auto_mute.setChecked(False)
                self.act_auto_mute.blockSignals(False)
                self._settings.set("mute_on_focus_loss", False)
                return
            self._bg_audio_muter.start()
        else:
            self._bg_audio_muter.stop()

    # ----- 自動更新 -----
    # 流程:_maybe_auto_check_update (啟動 5 秒後)/_on_check_update_clicked
    # (手動) → _start_update_check → CheckUpdateTask (worker thread)
    # → _on_update_check_finished → 用 QMessageBox 提示三個選項
    # → _start_update_download → DownloadUpdateTask → 進度條
    # → _on_update_download_finished → 啟 installer + quit

    def _maybe_auto_check_update(self) -> None:
        if not bool(self._settings.get("auto_update_check", True)):
            return
        try:
            last_ts = int(self._settings.get("last_update_check_ts", 0) or 0)
        except (TypeError, ValueError):
            last_ts = 0
        # 6 小時 = 21600 秒。GitHub 未認證 60 req/h/IP,綽綽有餘。
        if time.time() - last_ts < 21600:
            return
        self._start_update_check(manual=False)

    @Slot()
    def _on_check_update_clicked(self) -> None:
        self._start_update_check(manual=True)

    def _start_update_check(self, manual: bool) -> None:
        if self._update_check_task is not None and self._update_check_task.is_alive():
            if manual:
                self.statusBar().showMessage("正在檢查更新…", 3000)
            return
        self._manual_update_check_pending = bool(manual)
        self._settings.set("last_update_check_ts", int(time.time()))
        task = CheckUpdateTask(self._updater_proxy)
        self._update_check_task = task
        task.start()
        if manual:
            self.statusBar().showMessage("正在檢查更新…", 3000)

    @Slot(object)
    def _on_update_check_finished(self, info) -> None:
        manual = self._manual_update_check_pending
        self._manual_update_check_pending = False
        self._update_check_task = None
        if info is None:
            if manual:
                QMessageBox.information(
                    self, "檢查更新", "已是最新版本"
                )
            return
        # 自動檢查時,曾按過「略過此版本」就跳過。手動點擊一律顯示。
        skip = str(self._settings.get("update_skip_version", "") or "")
        if not manual and skip and info.latest_version == skip:
            return
        self._pending_update_info = info
        self._show_update_prompt(info)

    def _show_update_prompt(self, info: UpdateInfo) -> None:
        size_mb = info.asset_size / (1024 * 1024) if info.asset_size else 0.0
        size_str = f"{size_mb:.1f} MB" if size_mb else "未知大小"
        digest_note = "" if info.digest else "\n\n注意:此版本未提供 SHA256 校驗碼。"
        text = (
            f"發現新版本 v{info.latest_version}\n"
            f"目前版本 v{info.current_version}\n\n"
            f"檔案:{info.asset_name} ({size_str}){digest_note}"
        )
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Information)
        box.setWindowTitle("有可用的更新")
        box.setText(text)
        btn_download = box.addButton("立即下載", QMessageBox.AcceptRole)
        btn_skip = box.addButton("略過此版本", QMessageBox.DestructiveRole)
        btn_later = box.addButton("稍後提醒", QMessageBox.RejectRole)
        box.setDefaultButton(btn_download)
        box.exec()
        clicked = box.clickedButton()
        if clicked is btn_download:
            self._start_update_download(info)
        elif clicked is btn_skip:
            self._settings.set("update_skip_version", info.latest_version)
        # btn_later 或關閉:不動 settings,下次啟動再提示。

    def _start_update_download(self, info: UpdateInfo) -> None:
        if self._update_download_task is not None and self._update_download_task.is_alive():
            return
        import tempfile
        dest_dir = Path(tempfile.gettempdir())
        task = DownloadUpdateTask(self._updater_proxy, info, dest_dir)
        self._update_download_task = task

        dialog = QProgressDialog(
            f"正在下載 v{info.latest_version}…", "取消", 0, max(info.asset_size, 1), self
        )
        dialog.setWindowTitle("下載更新")
        dialog.setWindowModality(Qt.WindowModal)
        dialog.setMinimumDuration(0)
        dialog.setAutoClose(False)
        dialog.setAutoReset(False)
        dialog.canceled.connect(self._cancel_update_download)
        self._update_progress_dialog = dialog
        dialog.show()

        task.start()

    @Slot()
    def _cancel_update_download(self) -> None:
        task = self._update_download_task
        if task is not None:
            try:
                task.request_stop()
            except Exception:
                pass

    @Slot(int, int)
    def _on_update_download_progress(self, done: int, total: int) -> None:
        dialog = self._update_progress_dialog
        if dialog is None:
            return
        if total > 0:
            dialog.setMaximum(total)
            dialog.setValue(done)
        else:
            dialog.setMaximum(0)
        done_mb = done / (1024 * 1024)
        total_mb = total / (1024 * 1024)
        if total > 0:
            dialog.setLabelText(f"已下載 {done_mb:.1f} / {total_mb:.1f} MB")
        else:
            dialog.setLabelText(f"已下載 {done_mb:.1f} MB")

    @Slot(str)
    def _on_update_download_finished(self, path: str) -> None:
        self._update_download_task = None
        if self._update_progress_dialog is not None:
            self._update_progress_dialog.close()
            self._update_progress_dialog = None
        info = self._pending_update_info
        # 若 release 沒帶 digest,先給警告讓使用者決定是否仍要安裝。
        if info is not None and info.digest is None:
            warn = QMessageBox.warning(
                self,
                "未提供校驗碼",
                "此版本未提供 SHA256 校驗碼,無法核對檔案完整性。\n"
                "建議到 GitHub 比對檔案 hash 後再安裝。\n\n是否仍要繼續安裝?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if warn != QMessageBox.Yes:
                return
        confirm = QMessageBox.question(
            self,
            "下載完成",
            "更新安裝檔已下載完成,是否關閉 NTE Piano 並啟動安裝?\n"
            "(系統會跳出 UAC 確認視窗)",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if confirm != QMessageBox.Yes:
            return
        try:
            # DETACHED_PROCESS:讓 installer 脫離本程式生命週期,quit() 之後仍能跑。
            subprocess.Popen(
                [path],
                creationflags=subprocess.DETACHED_PROCESS
                | subprocess.CREATE_NEW_PROCESS_GROUP,
                close_fds=True,
            )
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "啟動安裝失敗", str(e))
            return
        QApplication.instance().quit()

    @Slot(str)
    def _on_update_failed(self, message: str) -> None:
        self._update_download_task = None
        if self._update_progress_dialog is not None:
            self._update_progress_dialog.close()
            self._update_progress_dialog = None
        QMessageBox.critical(self, "更新失敗", message)

    @Slot(bool)
    def _on_auto_update_check_toggled(self, checked: bool) -> None:
        self._settings.set("auto_update_check", bool(checked))

    @Slot()
    def _show_about_dialog(self) -> None:
        QMessageBox.about(
            self,
            "關於 NTE Piano",
            f"<b>{APP_TITLE}</b><br>"
            f"版本 v{APP_VERSION}<br><br>"
            f"NTE 遊戲鋼琴介面自動演奏工具<br>"
            f'<a href="{GITHUB_REPO_URL}">{GITHUB_REPO_URL}</a>',
        )

    def _append_automation_log(self, message: str) -> None:
        """把訊息以 [HH:MM:SS] 字首寫進底部 log。

        worker 從 worker thread 透過 QueuedConnection 進來,所以這個 method
        會在 main thread 跑。
        """
        if not hasattr(self, "automation_log") or self.automation_log is None:
            return
        stamp = _dt.datetime.now().strftime("%H:%M:%S")
        # appendPlainText 會自動加換行;multi-line message 也保留格式
        self.automation_log.appendPlainText(f"[{stamp}] {message}")

    @Slot(bool)
    def _on_automation_dock_visibility(self, visible: bool) -> None:
        self._settings.set("automation_dock_visible", bool(visible))
        if hasattr(self, "act_auto_dock") and self.act_auto_dock is not None:
            self.act_auto_dock.blockSignals(True)
            self.act_auto_dock.setChecked(bool(visible))
            self.act_auto_dock.blockSignals(False)

    @Slot(bool)
    def _on_automation_dock_toggled(self, checked: bool) -> None:
        if not hasattr(self, "_automation_dock"):
            return
        if checked and not self._automation_dock.isVisible():
            self._automation_dock.show()
        elif not checked and self._automation_dock.isVisible():
            self._automation_dock.hide()

    @Slot()
    def _toggle_automation_dock(self) -> None:
        if not hasattr(self, "_automation_dock"):
            return
        self._automation_dock.setVisible(not self._automation_dock.isVisible())

    def closeEvent(self, event):  # noqa: N802 - Qt override name.
        if not self._confirm_discard_changes():
            event.ignore()
            return
        # debounce 中的 slider 寫盤強制落地,免得 300ms 視窗內關掉就丟資料。
        if hasattr(self, "_settings_flush_timer"):
            self._settings_flush_timer.stop()
        self._settings.flush()
        # 視窗關閉時 dock 會自動 hide,不應視為使用者主動隱藏 → 把 visibilityChanged
        # 訊號斷開,免得把 settings 的 automation_dock_visible 寫成 False。
        if hasattr(self, "_automation_dock") and self._automation_dock is not None:
            try:
                self._automation_dock.visibilityChanged.disconnect(
                    self._on_automation_dock_visibility
                )
            except (TypeError, RuntimeError):
                pass
        if self._focus_check_timer.isActive():
            self._focus_check_timer.stop()
        # _autosave_timer 已隨譜面文字編輯器一起移除
        if hasattr(self, "_nte_probe") and self._nte_probe is not None:
            self._nte_probe.stop()
        # 失焦自動靜音 muter 一定要停,內部會強制 SetMute(False)
        # 避免遊戲關掉後留下被靜音的 audio session(下次再開沒聲音很慘)。
        if hasattr(self, "_bg_audio_muter") and self._bg_audio_muter is not None:
            try:
                self._bg_audio_muter.stop()
            except Exception:  # noqa: BLE001
                pass
        # 粉爪大劫案 controller 也要清理,別讓 daemon thread 在背景繼續送 key。
        if hasattr(self, "_heist_controller") and self._heist_controller is not None:
            try:
                self._heist_controller.stop()
            except Exception:  # noqa: BLE001
                pass
        # autosave 機制已移除,_dirty 只用於關閉時提示(`_confirm_discard_changes`)
        self._hotkeys.stop()
        # 自動更新 worker 跟 download worker:跟其他 daemon thread 一樣請它停。
        if self._update_check_task is not None:
            try:
                self._update_check_task.request_stop()
            except Exception:
                pass
            self._update_check_task = None
        if self._update_download_task is not None:
            try:
                self._update_download_task.request_stop()
            except Exception:
                pass
            self._update_download_task = None
        if self._worker is not None:
            self._worker.request_stop()
        if self._thread is not None and self._thread.isRunning():
            # worker._wait_until_action 每 50ms 看一次 stop_event,
            # finally 內 _release_all + emit finished 約 100ms;1000ms 足夠
            # 給最壞情況(backend.key_up 30 鍵 + Qt queued signal)收尾。
            if not self._thread.wait(1000):
                self._thread.quit()
                self._thread.wait(500)
        event.accept()
