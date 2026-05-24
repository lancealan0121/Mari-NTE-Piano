# NTE Piano - 自動演奏與自動化工具
# Copyright (C) 2026  Yulun
# Licensed under GPL-3.0-or-later. See LICENSE.
"""NTE Piano Auto Player.

極簡 GUI：頂部薄工具列 + Piano Roll 卷簾 + 仿遊戲鋼琴鍵盤；
譜面編輯器收進右側抽屜（Ctrl+E 切換）。
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
)
from PySide6.QtWidgets import (
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
from nte_automation import (
    AutomationProxy,
    AutomationTask,
    BackgroundAudioMuter,
    HeistController,
    RhythmTask,
    SoundCombatTask,
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


APP_TITLE = "NTE Piano Auto Player"
START_DELAY_SECONDS = 1.0


# ============================================================================
# 路徑 helpers — 處理 PyInstaller 打包後 (frozen) 與 dev 環境的差異。
# 兩種環境:
#   - dev:Path(__file__).resolve().parent 同時當 bundled 資源根 + 使用者資料根
#   - frozen onedir / onefile:
#       * bundled 資源在 sys._MEIPASS(onedir 6.x 指向 _internal/、onefile 指向
#         解壓 temp);PyInstaller 5.x 老 onedir 沒設 _MEIPASS → fallback exe 旁
#       * 使用者資料(歌曲、autosave、log、npy cache)放 exe 同目錄,使用者能直接
#         看到並丟譜面、看 log
# ============================================================================
def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _resource_path(rel: str = "") -> Path:
    """唯讀 bundled 資源(assets、預設 examples)根目錄。"""
    if _is_frozen():
        base = Path(getattr(sys, "_MEIPASS", "") or Path(sys.executable).resolve().parent)
    else:
        base = Path(__file__).resolve().parent
    return base / rel if rel else base


def _user_data_dir(rel: str = "") -> Path:
    """使用者可寫資料(歌曲、autosave、logs)根目錄。frozen 時 = exe 同目錄。"""
    if _is_frozen():
        base = Path(sys.executable).resolve().parent
    else:
        base = Path(__file__).resolve().parent
    return base / rel if rel else base


def _seed_default_songs() -> None:
    """首次啟動把 bundled 預設譜面 copy 一份到使用者 songs/。

    songs/ 已存在就不動 — 若使用者刪掉某首,下次啟動不會重新塞回來。
    若要還原預設,直接砍掉整個 songs/ 重啟即可。
    """
    target = _user_data_dir("songs")
    if target.exists():
        return
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    source = _resource_path("examples")
    if not source.exists():
        return
    for src in source.glob("*.txt"):
        dst = target / src.name
        try:
            shutil.copyfile(str(src), str(dst))
        except OSError:
            pass


SETTINGS_DIR = Path.home() / ".nte_piano"
SETTINGS_PATH = SETTINGS_DIR / "settings.json"
SETTINGS_VERSION = 21
PLAYBACK_SPEEDS = (0.5, 0.75, 1.0, 1.25, 1.5)
ZOOM_MIN = 0.4
ZOOM_MAX = 3.0
AUTOSAVE_DEBOUNCE_MS = 5000
FOCUS_CHECK_INTERVAL_MS = 200
FOCUS_CHECK_GRACE_MS = 1500
MIDI_EXTENSIONS = {".mid", ".midi"}
MUSICXML_EXTENSIONS = {".mxl", ".xml", ".musicxml"}
MSCZ_EXTENSIONS = {".mscz", ".mscx"}
DSL_EXTENSIONS = {".txt", ".score"}

THEME = {
    "bg": "#16181d",
    "panel": "#1a1d23",
    "panel_alt": "#1f232a",
    "fg": "#e6e8ec",
    "fg_dim": "#9aa1ad",
    "fg_subtle": "#6b7280",
    "accent": "#ff7a59",
    "stop": "#ff4d6d",
    "play": "#4d8cff",
    "grid": "#262a33",
    "grid_strong": "#3a414d",
    "cursor": "#ff4d6d",
    "key_face": "#f4f5f7",
    "key_stroke": "#9ea4b0",
    "key_text": "#1f232a",
    "key_letter_bg": "#1f232a",
    "key_letter_fg": "#e6e8ec",
    "H": "#ff7a59",
    "M": "#4dd0c2",
    "L": "#8a7cff",
    "H_active": "#ffd166",
    "M_active": "#a4f7c3",
    "L_active": "#c5b6ff",
}


# 音符配色預設;每個 style 提供 (H, M, L, H_active, M_active, L_active)
NOTE_COLOR_STYLES: dict[str, dict[str, str]] = {
    "default": {
        "label": "預設(橘綠紫)",
        "H": "#ff7a59", "M": "#4dd0c2", "L": "#8a7cff",
        "H_active": "#ffd166", "M_active": "#a4f7c3", "L_active": "#c5b6ff",
    },
    "ocean": {
        "label": "海洋(冷色)",
        "H": "#7dd3fc", "M": "#60a5fa", "L": "#a78bfa",
        "H_active": "#bae6fd", "M_active": "#bfdbfe", "L_active": "#ddd6fe",
    },
    "sunset": {
        "label": "日落(暖色)",
        "H": "#fbbf24", "M": "#fb7185", "L": "#f472b6",
        "H_active": "#fde68a", "M_active": "#fecdd3", "L_active": "#fbcfe8",
    },
    "forest": {
        "label": "森林(綠系)",
        "H": "#a3e635", "M": "#22c55e", "L": "#14b8a6",
        "H_active": "#d9f99d", "M_active": "#86efac", "L_active": "#99f6e4",
    },
    "mono": {
        "label": "單色(灰白)",
        "H": "#e5e7eb", "M": "#9ca3af", "L": "#6b7280",
        "H_active": "#f9fafb", "M_active": "#d1d5db", "L_active": "#9ca3af",
    },
    "neon": {
        "label": "霓虹(高對比)",
        "H": "#ff00aa", "M": "#00f5d4", "L": "#9b5de5",
        "H_active": "#ff70c8", "M_active": "#7df9e6", "L_active": "#c89bee",
    },
    "candy": {
        "label": "糖果(粉嫩)",
        "H": "#fda4af", "M": "#a5f3fc", "L": "#c4b5fd",
        "H_active": "#fecdd3", "M_active": "#cffafe", "L_active": "#ddd6fe",
    },
    # custom 的 H/M/L 由主視窗從 settings 動態餵入 apply_note_color_style;
    # 這裡只給一份 fallback 預設(同 default),避免 callsite 沒帶 custom_colors
    # 卻又選了 custom 時崩潰。_active 變體在 apply 內由基色 lighter 派生。
    "custom": {
        "label": "自訂",
        "H": "#ff7a59", "M": "#4dd0c2", "L": "#8a7cff",
        "H_active": "#ffd166", "M_active": "#a4f7c3", "L_active": "#c5b6ff",
    },
}


def _derive_active_color(base_hex: str) -> str:
    """從基色派生 active 變體(亮度 +35%)。custom style 用。"""
    c = QColor(base_hex)
    if not c.isValid():
        return base_hex
    return c.lighter(135).name()


def apply_note_color_style(name: str, custom_colors: dict | None = None) -> str:
    """套用配色到 THEME 並回傳實際使用的 style key (找不到時退回 default)。

    name="custom" 時優先從 custom_colors 取 H/M/L 三色,_active 變體由基色派生
    (lighter 135%);custom_colors 為 None 時退回 custom entry 內的 fallback。
    """
    style = NOTE_COLOR_STYLES.get(name) or NOTE_COLOR_STYLES["default"]
    actual = name if name in NOTE_COLOR_STYLES else "default"
    if actual == "custom" and custom_colors:
        base = {
            "H": str(custom_colors.get("H", style["H"])),
            "M": str(custom_colors.get("M", style["M"])),
            "L": str(custom_colors.get("L", style["L"])),
        }
        for k in ("H", "M", "L"):
            THEME[k] = base[k]
            THEME[f"{k}_active"] = _derive_active_color(base[k])
        return actual
    for key in ("H", "M", "L", "H_active", "M_active", "L_active"):
        THEME[key] = style[key]
    return actual


def _blend_color(c0: QColor, c1: QColor, t: float) -> QColor:
    """線性混合兩個 QColor，t=0 回傳 c0、t=1 回傳 c1。"""
    t = max(0.0, min(1.0, t))
    return QColor(
        int(c0.red() + (c1.red() - c0.red()) * t),
        int(c0.green() + (c1.green() - c0.green()) * t),
        int(c0.blue() + (c1.blue() - c0.blue()) * t),
        int(c0.alpha() + (c1.alpha() - c0.alpha()) * t),
    )


def _ease_out_quad(t: float) -> float:
    """OutQuad 緩動，給光環/光暈衰減用。"""
    t = max(0.0, min(1.0, t))
    return 1.0 - (1.0 - t) * (1.0 - t)


def _ease_in_out_sine(t: float) -> float:
    """SineInOut 緩動，給呼吸動畫用，輸出 0..1。"""
    t = max(0.0, min(1.0, t))
    return 0.5 - 0.5 * math.cos(math.pi * t)


class _TransportButton(QPushButton):
    """播放控制按鈕：用 QStyle 標準圖示代替 emoji 文字。"""

    _ICON_MAP: dict[str, QStyle.StandardPixmap] = {
        "prev": QStyle.StandardPixmap.SP_MediaSkipBackward,
        "play": QStyle.StandardPixmap.SP_MediaPlay,
        "pause": QStyle.StandardPixmap.SP_MediaPause,
        "stop": QStyle.StandardPixmap.SP_MediaStop,
        "next": QStyle.StandardPixmap.SP_MediaSkipForward,
    }

    def __init__(
        self,
        icon_key: str,
        name: str,
        tip: str,
        font: QFont,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._icon_key = icon_key
        self.setObjectName(name)
        self.setFlat(True)
        self.setFont(font)
        self.setToolTip(tip)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._refresh_icon()

    def _refresh_icon(self) -> None:
        icon = self.style().standardIcon(
            self._ICON_MAP.get(self._icon_key, QStyle.StandardPixmap.SP_MediaPlay)
        )
        pixmap = icon.pixmap(QSize(22, 22))
        # 依 icon_key 選顏色:play / pause 走藍,stop 走紅,其他用前景色。
        if self._icon_key in ("play", "pause"):
            tint = THEME["play"]
        elif self._icon_key == "stop":
            tint = THEME["stop"]
        else:
            tint = THEME["fg"]
        painter = QPainter(pixmap)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
        painter.fillRect(pixmap.rect(), QColor(tint))
        painter.end()
        self.setIcon(QIcon(pixmap))
        self.setIconSize(QSize(22, 22))

    def changeEvent(self, event: QEvent | None) -> None:
        if event is not None and event.type() == QEvent.Type.StyleChange:
            self._refresh_icon()
        super().changeEvent(event)


def _ease_out_back(t: float) -> float:
    """OutBack 緩動，回彈到 1.0 前略超過再退回，給按鍵彈性釋放用。"""
    t = max(0.0, min(1.0, t))
    c1 = 3.0
    c3 = c1 + 1.0
    return 1.0 + c3 * (t - 1.0) ** 3 + c1 * (t - 1.0) ** 2


def _radial_alpha_gradient(
    cx: float,
    cy: float,
    radius: float,
    inner_color: QColor,
    inner_alpha: int = 200,
    outer_alpha: int = 0,
) -> QRadialGradient:
    """建立中心 alpha 高、邊緣透明（或反之）的徑向漸層。"""
    g = QRadialGradient(QPointF(cx, cy), max(0.5, radius))
    inner = QColor(inner_color)
    inner.setAlpha(max(0, min(255, inner_alpha)))
    outer = QColor(inner_color)
    outer.setAlpha(max(0, min(255, outer_alpha)))
    g.setColorAt(0.0, inner)
    g.setColorAt(1.0, outer)
    return g


class SettingsManager:
    """JSON-backed key/value store at ~/.nte_piano/settings.json.

    壞檔自動備份回退預設;atomic 寫入避免半寫檔。
    """

    _DEFAULTS = {
        "version": SETTINGS_VERSION,
        "playback_speed": 1.0,
        "auto_pause_on_focus_loss": False,
        "zoom_factor": 1.0,
        "countdown_seconds": 0,
        "note_color_style": "default",
        # 「自訂」配色三色;只在 note_color_style == "custom" 時生效。預設同
        # default style 的 H/M/L,_active 由 apply_note_color_style 派生。
        "custom_note_h_color": "#ff7a59",
        "custom_note_m_color": "#4dd0c2",
        "custom_note_l_color": "#8a7cff",
        "import_tempo_changes": False,
        # v3:自動化任務(閃避/音游)
        "automation_hotkeys_enabled": False,
        "dodge_threshold": 0.13,
        "dodge_counter_threshold": 0.12,
        "dodge_key": "shift",
        "dodge_counter_use_mouse": True,
        "rhythm_loop_count": 0,  # 0 = 無限
        "rhythm_timeout_seconds": 180,
        "rhythm_track_keys": "d,f,j,k",
        "rhythm_delay_ms": 0,  # 正值:偵測到後延後送 key;負值:亮度閾值放寬提早觸發
        # v4:自動化監控視窗(NTE Checker + log dock)
        "automation_dock_visible": False,
        # v5:遊戲失焦時自動把 HTGame.exe 靜音(pycaw 控制 audio session)
        "mute_on_focus_loss": False,
        # v7:Piano Roll 依音高排序顯示。預設 False = H 在頂、各段內按簡譜 1..7;
        # True = 反轉各段內 12 半音,讓 H7(MIDI 95) 在最頂、L1(MIDI 60) 在最底,
        # 整個 y 軸嚴格按 MIDI 由高到低排列。
        "pitch_sort_mode": False,
        # v8:粉爪大劫案便利功能。常駐 helper,跟 dodge/rhythm 不互斥,不占用
        # _automation_task 槽位。透過 GetAsyncKeyState polling 不註冊全域 hotkey
        # (否則會搶 F 鍵)。只在 NTE 視窗為前景時生效,編輯器打字不會誤觸發。
        # 滾輪固定啟用(整個功能叫「快速拾取」就是 F 連點 + 滾輪交替)。
        # v9:加全自動模式。
        "heist_enabled": False,
        "heist_trigger_key": "f",
        "heist_auto_mode": False,
        # 全自動 toggle 熱鍵 — 遊戲為前景時按一下即切換 _auto_mode。
        # 空字串視為停用此熱鍵。預設 F8(避開 F6/F7/F10/F11 已用)。
        "heist_auto_mode_hotkey": "f8",
        # v11:Piano Roll 重繪 FPS;30/60/120 三檔。
        # 預設 60 兼顧流暢與功耗;120 對應高更新率螢幕或對延遲敏感的使用者。
        "roll_fps": 60,
        # v11:底部遊戲鋼琴鍵盤是否顯示。預設 True(維持原本看到的版面)。
        "show_piano_keyboard": True,
        # v12:播放時自動聚焦遊戲視窗。預設 True(維持原本行為)。
        "focus_game_on_play": True,
        # v13:匯入的譜面開頭如果有空白拍(第一個音很晚才出現),自動快轉跳過。
        # 預設 True 解決 MIDI/MXL 匯入後「按 F6 卻等了好幾拍才響」的體驗。
        # 關掉後完全照譜面 beat 0 起算,適合譜面前奏本來就該等的情境。
        "auto_trim_leading_silence": True,
        # v15:設定面板平滑捲動。預設 True;滑鼠/觸控板滾輪以 QPropertyAnimation
        # 推進 scroll value,Ctrl+滾輪或 modifier 按住時仍走 Qt 預設(避免攔
        # 縮放這類專用手勢)。關閉後完全回到瞬間跳的預設行為。
        "smooth_scroll_enabled": True,
        # v16:確認對話框開關。預設都 True 維持原本「保護性提示」行為。
        # confirm_discard_unsaved=False:切歌/開檔/匯入/關閉視窗時不再詢問,直接覆蓋。
        # confirm_delete_song=False:刪歌不再詢問,點到就立刻刪(此檔離開磁碟)。
        "confirm_discard_unsaved": True,
        "confirm_delete_song": True,
        # v17:Piano Roll 滾輪平滑捲動(獨立於 smooth_scroll_enabled 設定面板那個)。
        # 預設 True:未播放時滑鼠滾輪動 piano roll,_browse_offset 用 220ms 動畫
        # 推到目標位置,連續滾會累加目標、看起來連續變速。
        # False:回到一格一格離散跳的原本行為。
        # 播放中(走 seek_requested)恆走原離散路徑,避免動畫打亂 worker 時序。
        "smooth_scroll_pianoroll": True,
        # Ctrl+滾輪縮放音樂編輯區的平滑過渡動畫。140ms OutCubic,連續滾時累加目標。
        # False:回到舊行為 — set_zoom_factor 直接生效、無過渡。
        "smooth_zoom_pianoroll": True,
        # 啟動 5 秒後背景查 GitHub Releases latest tag,有新版會跳提示對話框。
        # 6 小時節流(last_update_check_ts)避免頻繁打 API;按「略過此版本」
        # 會寫入 update_skip_version,該版本以前的自動提示會被吃掉(手動檢查不受影響)。
        "auto_update_check": True,
        "last_update_check_ts": 0,
        "update_skip_version": "",
        # v21:本機鋼琴音色播放(assets/sounds/piano/*.ogg)。
        # piano_sound_enabled:播放譜面時是否同步用 QSoundEffect 出聲;關掉就只送鍵不發聲。
        # piano_sound_volume:0.0~1.0,UI 用 0~100 slider 內部 /100 落盤。
        # preview_mode:預先聆聽模式 — worker 走完整 schedule 但不送任何鍵、不聚焦遊戲視窗,
        # 只發本機音。屬於「執行中功能」,_RESET_ON_LOAD 強制每次啟動歸 False。
        "piano_sound_enabled": True,
        "piano_sound_volume": 0.7,
        "preview_mode": False,
    }

    # 「執行中功能」開關 — 每次啟動強制歸 False,不論 settings.json 上次存什麼。
    # 使用者在 menu 勾選會即時落盤(維持當下 session 行為),但下次重開又是關的。
    # 數值/按鍵字串/靜音之類的純設定不在此列。
    _RESET_ON_LOAD = (
        "import_tempo_changes",
        "automation_hotkeys_enabled",
        "automation_dock_visible",
        "heist_enabled",
        "heist_auto_mode",
        "preview_mode",
    )

    def __init__(self, path: Path = SETTINGS_PATH) -> None:
        self._path = path
        self._data = dict(self._DEFAULTS)
        self._load()

    def _load(self) -> None:
        needs_resave = False
        if not self._path.exists():
            self._apply_session_reset()
            return
        try:
            text = self._path.read_text(encoding="utf-8")
            data = json.loads(text)
            if not isinstance(data, dict):
                raise ValueError("settings.json 不是物件")
        except (OSError, ValueError) as exc:
            self._quarantine(f"無法解析:{exc}")
            self._apply_session_reset()
            return
        original_version = int(data.get("version", 1) or 1)
        try:
            data = self._migrate(data)
        except Exception as exc:  # noqa: BLE001
            self._quarantine(f"升版失敗:{exc}")
            self._apply_session_reset()
            return
        for key, value in data.items():
            if key in self._DEFAULTS:
                self._data[key] = value
        if self._apply_session_reset(persisted=data):
            needs_resave = True
        if original_version < SETTINGS_VERSION or needs_resave:
            self._save()

    def _apply_session_reset(self, persisted: dict | None = None) -> bool:
        """把 _RESET_ON_LOAD 列出的 key 強制歸 False。
        回傳 True 代表跟磁碟上的值不一致,呼叫端應觸發落盤。
        """
        changed = False
        for key in self._RESET_ON_LOAD:
            self._data[key] = False
            if persisted is not None and persisted.get(key) is not False:
                changed = True
        return changed

    def _quarantine(self, reason: str) -> None:
        try:
            stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
            backup = self._path.with_name(f"settings.json.bad-{stamp}")
            self._path.rename(backup)
            sys.stderr.write(f"[settings] {reason};已備份為 {backup.name}\n")
        except OSError:
            pass

    @classmethod
    def _migrate(cls, data: dict) -> dict:
        version = int(data.get("version", 1) or 1)
        if version > SETTINGS_VERSION:
            raise ValueError(
                f"settings.json 版本 {version} 比程式支援的 {SETTINGS_VERSION} 新"
            )
        if version < 2:
            # v1→v2: 失焦自動暫停預設改 False。
            # 原本預設 True 在 SetForegroundWindow 被拒絕的遊戲上會誤觸發,
            # 把使用者的播放切碎(看起來像音樂跑掉)。
            data["auto_pause_on_focus_loss"] = False
        if version < 3:
            # v2→v3: 加入自動化任務設定。所有新 key 沿用 _DEFAULTS,
            # 不需要遷移舊值,只需要 bump 版本。
            pass
        if version < 4:
            # v3→v4: 加入自動化監控視窗顯示偏好。新 key 沿用 _DEFAULTS,
            # 不需遷移舊值。
            pass
        if version < 5:
            # v4→v5: 加入失焦自動靜音遊戲開關。新 key 沿用 _DEFAULTS(False)。
            pass
        if version < 6:
            # v5→v6: 加入自動音遊 delay 微調。新 key 沿用 _DEFAULTS(0)。
            pass
        if version < 7:
            # v6→v7: 加入 Piano Roll 依音高排序顯示模式。新 key 沿用 _DEFAULTS(False)。
            pass
        if version < 8:
            # v7→v8: 加入粉爪大劫案(heist_*)便利功能設定。新 key 沿用 _DEFAULTS,
            # 預設 heist_enabled=False。不需要遷移舊值。
            pass
        if version < 9:
            # v8→v9: 加入粉爪「全自動」與「快速奔跑」子選項。新 key 沿用
            # _DEFAULTS(全部預設關閉)。不需要遷移舊值。
            pass
        if version < 10:
            # v9→v10: 把「執行中功能」開關預設改 False。實際的強制歸零由
            # SettingsManager._apply_session_reset 每次啟動執行(見 _RESET_ON_LOAD),
            # migration 只負責 bump 版本。
            pass
        if version < 11:
            # v10→v11: 新增 roll_fps(預設 60)、show_piano_keyboard(預設 True)。
            # 都沿用 _DEFAULTS,不需遷移舊值。
            pass
        if version < 12:
            # v11→v12: 新增 focus_game_on_play(預設 True)。沿用 _DEFAULTS。
            pass
        if version < 13:
            # v12→v13: 新增 auto_trim_leading_silence(預設 True)。沿用 _DEFAULTS。
            pass
        if version < 14:
            # v13→v14: 譜面文字編輯器與 autosave 一起拿掉,對應 settings key 也清掉。
            # 改為 GUI 設定面板;主視窗永遠提示未存檔(不再有「未存檔提醒」開關)。
            data.pop("confirm_discard_unsaved", None)
            data.pop("autosave_restore_prompt", None)
        if version < 15:
            # v14→v15: 新增 smooth_scroll_enabled(預設 True)。沿用 _DEFAULTS。
            pass
        if version < 16:
            # v15→v16: 新增 confirm_discard_unsaved / confirm_delete_song
            # 兩個確認框開關(都預設 True 維持原本行為)。沿用 _DEFAULTS。
            # 注意 confirm_discard_unsaved 在 v13→v14 曾被砍掉,現在重新引入,
            # 含義也跟 v14 前不同:當時控制編輯器 autosave 提示,現在控制
            # 「切歌/開檔/匯入/關視窗」時的未存檔詢問。
            pass
        if version < 17:
            # v16→v17: 新增 smooth_scroll_pianoroll(預設 True)。沿用 _DEFAULTS。
            pass
        if version < 18:
            # v17→v18: 新增 smooth_zoom_pianoroll(預設 True)。沿用 _DEFAULTS。
            pass
        if version < 19:
            # v18→v19: 新增 custom_note_{h,m,l}_color + custom style。沿用 _DEFAULTS。
            pass
        if version < 20:
            # v19→v20: 新增 auto_update_check / last_update_check_ts / update_skip_version。
            # 三者沿用 _DEFAULTS,不需遷移舊值。
            pass
        if version < 21:
            # v20→v21: 新增 piano_sound_enabled / piano_sound_volume / preview_mode。
            # 沿用 _DEFAULTS;preview_mode 由 _RESET_ON_LOAD 每次啟動歸 False。
            pass
        data["version"] = SETTINGS_VERSION
        return data

    def get(self, key: str, default=None):
        if key in self._data:
            return self._data[key]
        if key in self._DEFAULTS:
            return self._DEFAULTS[key]
        return default

    def set(self, key: str, value) -> None:
        if self._data.get(key) == value:
            return
        self._data[key] = value
        self._save()

    def defer_set(self, key: str, value) -> None:
        """更新內部值但不立刻落盤;由 caller 用 QTimer 在 idle 後呼叫 flush() 寫盤。

        用途:slider 拖動連續 emit 時避免每像素都寫 atomic file。
        """
        if self._data.get(key) == value:
            return
        self._data[key] = value
        self._pending_flush = True

    def flush(self) -> None:
        """如有未落盤的 defer_set 內容,寫入磁碟一次。"""
        if getattr(self, "_pending_flush", False):
            self._pending_flush = False
            self._save()

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp, self._path)
        except OSError as exc:
            sys.stderr.write(f"[settings] 寫入失敗:{exc}\n")


class PianoKeyboardWidget(QWidget):
    """仿遊戲內 3x12 圓形音符鍵盤；被按下的音符以高亮色填充，支援滑鼠點擊插入。"""

    note_clicked = Signal(str)

    ATTACK_SECONDS = 0.08
    RELEASE_SECONDS = 0.22
    RING_SECONDS = 0.42
    PRESS_ATTACK_SECONDS = 0.10
    PRESS_RELEASE_SECONDS = 0.34

    def __init__(self, parent=None):
        super().__init__(parent)
        self._active = set()
        self._anim_state: dict[str, dict] = {}
        self._animations_enabled = True
        self._hovered_label: str | None = None
        self._anim_timer = QTimer(self)
        self._anim_timer.setInterval(16)
        self._anim_timer.timeout.connect(self._on_anim_tick)
        self.setMinimumHeight(220)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setCursor(Qt.PointingHandCursor)
        self.setMouseTracking(True)

    def sizeHint(self) -> QSize:
        return QSize(960, 240)

    def set_animations_enabled(self, enabled: bool) -> None:
        self._animations_enabled = bool(enabled)
        if not enabled:
            self._anim_state.clear()
            self._anim_timer.stop()
        self.update()

    @Slot(object)
    def set_active_labels(self, labels) -> None:
        new_set = set(labels) if labels else set()
        if new_set == self._active:
            return
        if self._animations_enabled:
            now = time.perf_counter()
            for lbl in new_set - self._active:
                self._anim_state[lbl] = {"active_since": now, "released_at": None}
            for lbl in self._active - new_set:
                st = self._anim_state.setdefault(lbl, {"active_since": now})
                st["released_at"] = now
            if self._anim_state and not self._anim_timer.isActive():
                self._anim_timer.start()
        self._active = new_set
        self.update()

    @Slot(object)
    def apply_active_delta(self, delta) -> None:
        """高頻路徑:接 worker.active_delta(adds, removes),只動差集 + update。

        避免每次 down/up 都跨 thread 跑 sorted full set。
        """
        adds, removes = delta
        if not adds and not removes:
            return
        if perf.enabled:
            perf.log("gui", "kb_delta", adds=len(adds), removes=len(removes))
        if self._animations_enabled:
            now = time.perf_counter()
            for lbl in adds:
                self._anim_state[lbl] = {"active_since": now, "released_at": None}
            for lbl in removes:
                st = self._anim_state.setdefault(lbl, {"active_since": now})
                st["released_at"] = now
            if self._anim_state and not self._anim_timer.isActive():
                self._anim_timer.start()
        self._active.update(adds)
        self._active.difference_update(removes)
        self.update()

    def _on_anim_tick(self) -> None:
        now = time.perf_counter()
        max_release = max(self.RELEASE_SECONDS, self.RING_SECONDS, self.PRESS_RELEASE_SECONDS)
        expired = [
            lbl for lbl, st in self._anim_state.items()
            if st.get("released_at") is not None
            and now - st["released_at"] > max_release
        ]
        for lbl in expired:
            self._anim_state.pop(lbl, None)
        if not self._anim_state:
            self._anim_timer.stop()
        self.update()

    def _glow_strength(self, label: str, now: float) -> float:
        if not self._animations_enabled:
            return 1.0 if label in self._active else 0.0
        st = self._anim_state.get(label)
        if label in self._active:
            if st is None:
                return 1.0
            t = now - st["active_since"]
            return min(1.0, max(0.0, t / self.ATTACK_SECONDS))
        if st and st.get("released_at") is not None:
            t = now - st["released_at"]
            return max(0.0, 1.0 - t / self.RELEASE_SECONDS)
        return 0.0

    def _ring_strength(self, label: str, now: float) -> float:
        """釋放後的擴散光環強度，0..1，0 表示沒有光環。"""
        if not self._animations_enabled:
            return 0.0
        st = self._anim_state.get(label)
        if not st:
            return 0.0
        released_at = st.get("released_at")
        if released_at is None:
            return 0.0
        t = now - released_at
        if t < 0.0 or t > self.RING_SECONDS:
            return 0.0
        return 1.0 - t / self.RING_SECONDS

    def _press_progress(self, label: str, now: float) -> float:
        """按下強度：0=原大，1=完全縮小；釋放期間以 ease-out-back 回彈，可短暫低於 0 形成 overshoot。"""
        if not self._animations_enabled:
            return 1.0 if label in self._active else 0.0
        st = self._anim_state.get(label)
        if label in self._active:
            if st is None:
                return 1.0
            t = (now - st["active_since"]) / self.PRESS_ATTACK_SECONDS
            return _ease_out_quad(max(0.0, min(1.0, t)))
        if st and st.get("released_at") is not None:
            t = (now - st["released_at"]) / self.PRESS_RELEASE_SECONDS
            if t >= 1.0:
                return 0.0
            return 1.0 - _ease_out_back(t)
        return 0.0

    def _layout_metrics(self):
        cols = len(CHROMATIC_LAYOUT)
        rows = len(ROW_LABELS)
        margin_left, margin_right = 64, 16
        margin_top, margin_bottom = 16, 12
        usable_w = max(0, self.width() - margin_left - margin_right)
        usable_h = max(0, self.height() - margin_top - margin_bottom)
        if usable_w <= 0 or usable_h <= 0:
            return None
        cell_w = usable_w / cols
        cell_h = usable_h / rows
        diameter = max(16.0, min(cell_w * 0.82, cell_h * 0.66))
        return {
            "rows": rows,
            "cols": cols,
            "margin_left": margin_left,
            "margin_top": margin_top,
            "cell_w": cell_w,
            "cell_h": cell_h,
            "diameter": diameter,
        }

    def _key_geometry(self):
        geo = {}
        metrics = self._layout_metrics()
        if metrics is None:
            return geo
        for row_index, (prefix, _) in enumerate(ROW_LABELS):
            row_y = metrics["margin_top"] + row_index * metrics["cell_h"]
            for col_index, (_, degree, accidental) in enumerate(CHROMATIC_LAYOUT):
                cx = metrics["margin_left"] + col_index * metrics["cell_w"] + metrics["cell_w"] / 2.0
                cy = row_y + metrics["cell_h"] / 2.0
                label = f"{prefix}{accidental}{degree}"
                geo[label] = (cx, cy, metrics["diameter"] / 2.0)
        return geo

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt override name.
        if event.button() != Qt.LeftButton:
            super().mousePressEvent(event)
            return
        pos = event.position()
        px, py = pos.x(), pos.y()
        for label, (cx, cy, r) in self._key_geometry().items():
            if (px - cx) ** 2 + (py - cy) ** 2 <= r * r:
                self.note_clicked.emit(label)
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt override name.
        pos = event.position()
        px, py = pos.x(), pos.y()
        new_hover: str | None = None
        for label, (cx, cy, r) in self._key_geometry().items():
            if (px - cx) ** 2 + (py - cy) ** 2 <= r * r:
                new_hover = label
                break
        if new_hover != self._hovered_label:
            self._hovered_label = new_hover
            self.update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802 - Qt override name.
        if self._hovered_label is not None:
            self._hovered_label = None
            self.update()
        super().leaveEvent(event)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), QColor(THEME["panel"]))

        metrics = self._layout_metrics()
        if metrics is None:
            return
        margin_left = metrics["margin_left"]
        margin_top = metrics["margin_top"]
        cell_w = metrics["cell_w"]
        cell_h = metrics["cell_h"]
        diameter = metrics["diameter"]

        for row_index, (prefix, row_label) in enumerate(ROW_LABELS):
            row_y = margin_top + row_index * cell_h

            painter.setPen(QColor(THEME["fg_dim"]))
            painter.setFont(QFont("Microsoft JhengHei UI", 10, QFont.Bold))
            painter.drawText(
                QRectF(0, row_y, margin_left - 10, cell_h),
                Qt.AlignVCenter | Qt.AlignRight,
                row_label,
            )

            now = time.perf_counter()
            for col_index, (display, degree, accidental) in enumerate(CHROMATIC_LAYOUT):
                cx = margin_left + col_index * cell_w + cell_w / 2.0
                cy = row_y + cell_h / 2.0
                label = f"{prefix}{accidental}{degree}"
                qwerty = BASE_KEYS[prefix][degree]
                glow = self._glow_strength(label, now)
                ring = self._ring_strength(label, now)
                press = self._press_progress(label, now)
                hovered = self._hovered_label == label
                self._draw_key(
                    painter, cx, cy, diameter,
                    prefix, degree, accidental, label, qwerty,
                    glow, ring, press, hovered,
                )

    def _draw_key(
        self,
        painter,
        cx,
        cy,
        diameter,
        prefix,
        degree,
        accidental,
        label,
        qwerty,
        glow=0.0,
        ring=0.0,
        press=0.0,
        hovered=False,
    ):
        base_radius = diameter / 2.0
        scale = max(0.78, min(1.14, 1.0 - 0.20 * press))
        radius = base_radius * scale
        diameter_scaled = diameter * scale
        cy_eff = cy

        inactive_face = QColor(THEME["key_face"])
        inactive_stroke = QColor(THEME["key_stroke"])
        active_face = QColor(THEME[f"{prefix}_active"])
        active_stroke = QColor(THEME[prefix])
        face = _blend_color(inactive_face, active_face, glow * 0.42)
        stroke = _blend_color(inactive_stroke, active_stroke, glow * 0.65)
        stroke_width = 1.4 + 0.45 * glow

        if glow > 0.01:
            for radius_mul, alpha_peak in ((2.05, 30), (1.55, 55), (1.22, 95)):
                halo_radius = base_radius * radius_mul
                halo_color = QColor(active_stroke)
                halo_grad = QRadialGradient(QPointF(cx, cy_eff), halo_radius)
                inner = QColor(halo_color)
                inner.setAlpha(int(alpha_peak * glow))
                outer = QColor(halo_color)
                outer.setAlpha(0)
                halo_grad.setColorAt(0.0, inner)
                halo_grad.setColorAt(1.0, outer)
                painter.setBrush(halo_grad)
                painter.setPen(Qt.NoPen)
                painter.drawEllipse(
                    QRectF(
                        cx - halo_radius,
                        cy_eff - halo_radius,
                        halo_radius * 2,
                        halo_radius * 2,
                    )
                )

        if ring > 0.05:
            expand = 1.0 - ring
            ring_outer = base_radius * (1.30 + 0.85 * expand)
            ring_color = QColor(active_stroke)
            ring_grad = QRadialGradient(QPointF(cx, cy_eff), ring_outer)
            transparent = QColor(ring_color)
            transparent.setAlpha(0)
            peak = QColor(ring_color)
            peak.setAlpha(int(110 * ring))
            inner_ratio = base_radius * (1.0 + 0.35 * expand) / ring_outer
            ring_grad.setColorAt(0.0, transparent)
            ring_grad.setColorAt(max(0.0, inner_ratio - 0.22), transparent)
            ring_grad.setColorAt(min(1.0, inner_ratio), peak)
            ring_grad.setColorAt(1.0, transparent)
            painter.setBrush(ring_grad)
            painter.setPen(Qt.NoPen)
            painter.drawEllipse(
                QRectF(
                    cx - ring_outer,
                    cy_eff - ring_outer,
                    ring_outer * 2,
                    ring_outer * 2,
                )
            )

        shadow_offset = max(0.6, 1.6 - 0.6 * press)
        shadow_color = QColor(0, 0, 0, 70)
        painter.setBrush(shadow_color)
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(
            QPointF(cx, cy_eff + shadow_offset),
            radius * 0.96,
            radius * 0.5,
        )

        rect = QRectF(cx - radius, cy_eff - radius, diameter_scaled, diameter_scaled)
        gradient = QLinearGradient(rect.topLeft(), rect.bottomLeft())
        gradient.setColorAt(0.0, face.lighter(112))
        gradient.setColorAt(1.0, face.darker(112))
        painter.setBrush(gradient)
        painter.setPen(QPen(stroke, stroke_width))
        painter.drawEllipse(rect)

        highlight_cx = cx
        highlight_cy = cy_eff - radius * (0.45 - 0.18 * glow)
        highlight_radius = radius * 0.85
        highlight_grad = _radial_alpha_gradient(
            highlight_cx,
            highlight_cy,
            highlight_radius,
            face.lighter(160),
            inner_alpha=170,
            outer_alpha=0,
        )
        painter.setBrush(highlight_grad)
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(rect)

        bottom_shadow = _radial_alpha_gradient(
            cx,
            cy_eff + radius * 0.55,
            radius * 0.78,
            face.darker(135),
            inner_alpha=70,
            outer_alpha=0,
        )
        painter.setBrush(bottom_shadow)
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(rect)

        if hovered and glow < 0.5:
            hover_color = QColor(active_stroke)
            hover_color.setAlpha(150)
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(hover_color, 1.4))
            painter.drawEllipse(rect.adjusted(-1.0, -1.0, 1.0, 1.0))

        if glow > 0.01:
            inner_glow = QColor(active_stroke)
            inner_glow_grad = QRadialGradient(QPointF(cx, cy_eff), radius * 1.05)
            inner_alpha = int(60 * glow)
            inner_c = QColor(inner_glow)
            inner_c.setAlpha(inner_alpha)
            outer_c = QColor(inner_glow)
            outer_c.setAlpha(0)
            inner_glow_grad.setColorAt(0.0, outer_c)
            inner_glow_grad.setColorAt(0.65, outer_c)
            inner_glow_grad.setColorAt(1.0, inner_c)
            painter.setBrush(inner_glow_grad)
            painter.setPen(Qt.NoPen)
            painter.drawEllipse(rect)

        painter.setPen(QColor(THEME["key_text"]))
        number_font = QFont("Cascadia Mono", max(10, int(diameter * 0.42)), QFont.Bold)
        painter.setFont(number_font)
        painter.drawText(rect, Qt.AlignCenter, str(degree))

        if accidental:
            acc_font = QFont("Cascadia Mono", max(8, int(diameter * 0.22)), QFont.Bold)
            painter.setFont(acc_font)
            metrics = QFontMetrics(acc_font)
            text_w = metrics.horizontalAdvance(accidental)
            number_metrics = QFontMetrics(number_font)
            number_w = number_metrics.horizontalAdvance(str(degree))
            ax = cx - number_w / 2.0 - text_w - 1
            ay = cy_eff - number_metrics.height() / 2.0 + metrics.ascent() - 2
            painter.drawText(QPointF(ax, ay), accidental)

        dot_radius = max(1.6, diameter * 0.045)
        painter.setBrush(QColor(THEME["key_text"]))
        painter.setPen(Qt.NoPen)
        if prefix == "H":
            painter.drawEllipse(QPointF(cx, cy_eff - radius * 0.62), dot_radius, dot_radius)
        elif prefix == "L":
            painter.drawEllipse(QPointF(cx, cy_eff + radius * 0.62), dot_radius, dot_radius)

        letter_text = qwerty.upper()
        if accidental == "#":
            letter_text = "S+" + letter_text
            letter_bg = QColor(THEME["accent"])
            letter_fg = QColor(THEME["bg"])
        elif accidental == "b":
            letter_text = "C+" + letter_text
            letter_bg = QColor("#7fa6ff")
            letter_fg = QColor(THEME["bg"])
        else:
            letter_bg = QColor(THEME["key_letter_bg"])
            letter_fg = QColor(THEME["key_letter_fg"])

        letter_radius = max(8.0, diameter * 0.18)
        font_factor = 0.95 if not accidental else 0.62
        letter_font = QFont("Cascadia Mono", max(7, int(letter_radius * font_factor)), QFont.Bold)
        font_metrics = QFontMetrics(letter_font)
        text_w = font_metrics.horizontalAdvance(letter_text)
        letter_h = letter_radius * 2
        letter_w = max(letter_h, text_w + 10)
        letter_cx = cx
        letter_cy = cy + base_radius + letter_h / 2 + 2
        max_y = self.height() - 4
        if letter_cy + letter_h / 2 > max_y:
            letter_cy = max_y - letter_h / 2
        letter_rect = QRectF(
            letter_cx - letter_w / 2,
            letter_cy - letter_h / 2,
            letter_w,
            letter_h,
        )

        shadow_rect = letter_rect.adjusted(0.0, 1.5, 0.0, 1.5)
        shadow_path = QPainterPath()
        shadow_path.addRoundedRect(shadow_rect, letter_h / 2, letter_h / 2)
        painter.fillPath(shadow_path, QColor(0, 0, 0, 90))

        painter.setBrush(letter_bg)
        painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(letter_rect, letter_h / 2, letter_h / 2)
        painter.setPen(letter_fg)
        painter.setFont(letter_font)
        painter.drawText(letter_rect, Qt.AlignCenter, letter_text)


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
        # 預設啟用;主視窗會依 settings.smooth_scroll_pianoroll 切換。
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
        # 右鍵 press → 等 release 決定是 resize 還是 context menu。
        self._right_press_pending = False
        self._right_press_global_pos = None
        self._right_press_local_pos = None
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
        """動畫每幀:把 float 推回 _browse_offset 並重繪。

        刻意不 emit timeline_changed 維持與原本 wheelEvent 同樣的同步範圍
        (scrollbar/overview 在未播放時本來就不會被滾輪更新)。
        """
        try:
            self._browse_offset = max(0.0, float(value))
        except (TypeError, ValueError):
            return
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
        if self._playing:
            new_pos = max(0.0, self.current_seconds() - notches * step)
            if total > 0:
                new_pos = min(new_pos, total)
            self.seek_requested.emit(new_pos)
        elif self._smooth_browse_enabled:
            # 平滑分支:把 delta 累加在「上一次動畫終點」上,動畫從目前值重啟到新目標,
            # 連續滾就會像連續變速;停滾 220ms 後自然停下。
            if self._smooth_browse_anim.state() == QVariantAnimation.Running:
                base = self._smooth_browse_target
            else:
                base = self._browse_offset
            target = base - notches * step
            target = max(0.0, target)
            if total > 0:
                target = min(target, total)
            self._smooth_browse_target = target
            self._smooth_browse_anim.stop()
            self._smooth_browse_anim.setStartValue(float(self._browse_offset))
            self._smooth_browse_anim.setEndValue(float(target))
            self._smooth_browse_anim.start()
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
            if idx is None or not (0 <= idx < len(self._sheet.events)):
                self._right_press_pending = True
                self._right_press_global_pos = event.globalPosition().toPoint()
                self._right_press_local_pos = pos
                return
            ev = self._sheet.events[idx]
            if ev.is_rest:
                self._right_press_pending = True
                self._right_press_global_pos = event.globalPosition().toPoint()
                self._right_press_local_pos = pos
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
        # 右鍵 pending → 沒命中音符那次按下,判斷是否有拖動(沒拖 = 彈 tempo 選單)
        if button == Qt.RightButton and self._right_press_pending:
            self._right_press_pending = False
            global_pos = self._right_press_global_pos
            local_pos = self._right_press_local_pos
            self._right_press_global_pos = None
            self._right_press_local_pos = None
            if local_pos is not None and global_pos is not None:
                dx = event.position().x() - local_pos.x()
                dy = event.position().y() - local_pos.y()
                if abs(dx) < 4 and abs(dy) < 4:
                    self._show_tempo_menu(local_pos, global_pos)
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

    def _show_tempo_menu(self, local_pos, global_pos) -> None:
        """空白處右鍵時彈出 tempo 變速選單(從 contextMenuEvent 抽出來)。"""
        if self._sheet is None:
            return
        seconds = self._seconds_at_x(local_pos.x())
        beats = self._sheet.seconds_to_beats(seconds)
        # 找附近的變速點 (容忍 0.4 拍內)
        near = None
        if self._sheet.tempo_changes:
            nearest = min(
                self._sheet.tempo_changes,
                key=lambda c: abs(c[0] - beats),
            )
            if abs(nearest[0] - beats) <= 0.4:
                near = nearest

        menu = QMenu(self)
        if near is not None:
            change_beat, change_tempo = near
            edit_act = menu.addAction(
                f"修改變速 ({change_tempo:g} BPM @ 第 {change_beat:g} 拍)…"
            )
            edit_act.triggered.connect(
                lambda _=False, b=change_beat, t=change_tempo:
                    self._prompt_edit_tempo_change(b, t)
            )
            remove_act = menu.addAction(
                f"移除此變速 ({change_tempo:g} BPM)"
            )
            remove_act.triggered.connect(
                lambda _=False, b=change_beat: self.tempo_change_remove.emit(b)
            )
        else:
            cur_tempo = self._sheet.tempo_at_beat(beats)
            add_act = menu.addAction(
                f"在第 {beats:.2f} 拍加入變速 (目前 {cur_tempo:g} BPM)…"
            )
            add_act.triggered.connect(
                lambda _=False, b=beats, t=cur_tempo:
                    self._prompt_add_tempo_change(b, t)
            )

        menu.addSeparator()
        if self._sheet.tempo_changes:
            tip = ", ".join(
                f"{t:g}@{b:g}" for b, t in self._sheet.tempo_changes[:5]
            )
            label_act = menu.addAction(f"目前變速:{tip}" + (" …" if len(self._sheet.tempo_changes) > 5 else ""))
            label_act.setEnabled(False)
        else:
            label_act = menu.addAction("尚無變速點 (起始 BPM 由 tempo 命令設定)")
            label_act.setEnabled(False)

        menu.exec(global_pos)

    def contextMenuEvent(self, event) -> None:  # noqa: N802 - Qt override.
        # 自訂右鍵流程已在 mousePressEvent/mouseReleaseEvent 處理(命中音符 → resize、
        # 空白處 → tempo 選單),這裡接住 Qt 預設的 contextMenuEvent 避免重複觸發。
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

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        bg_color = QColor(THEME["bg"])
        bg_dark = QColor(bg_color)
        bg_dark.setRed(max(0, bg_color.red() - 6))
        bg_dark.setGreen(max(0, bg_color.green() - 6))
        bg_dark.setBlue(max(0, bg_color.blue() - 6))
        bg_grad = QLinearGradient(QPointF(0.0, 0.0), QPointF(self.width(), 0.0))
        bg_grad.setColorAt(0.0, bg_dark)
        bg_grad.setColorAt(0.35, bg_color)
        bg_grad.setColorAt(1.0, bg_color)
        painter.fillRect(self.rect(), bg_grad)

        metrics = self._view_metrics()
        if metrics is None:
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

        n_tracks = len(TRACK_ORDER)
        for i, (prefix, _, _, accidental, _) in enumerate(TRACK_ORDER):
            y = margin_top + self._visual_index(i) * track_h
            base_panel = QColor(THEME["panel"]) if accidental else QColor(THEME["panel_alt"])
            tint = _blend_color(base_panel, QColor(THEME[prefix]), 0.05)
            painter.fillRect(QRectF(margin_left, y, usable_w, track_h), tint)

        painter.setPen(QPen(QColor(THEME["grid_strong"]), 1.0))
        for i in range(1, n_tracks):
            if TRACK_ORDER[i][0] != TRACK_ORDER[i - 1][0]:
                y = margin_top + self._visual_index(i) * track_h
                painter.drawLine(int(margin_left), int(y), int(margin_left + usable_w), int(y))

        painter.setFont(self._track_label_font)
        for i, (prefix, _, _, accidental, degree) in enumerate(TRACK_ORDER):
            y = margin_top + self._visual_index(i) * track_h
            painter.setPen(QColor(THEME[prefix]))
            painter.drawText(
                QRectF(0, y, margin_left - 6, track_h),
                Qt.AlignVCenter | Qt.AlignRight,
                f"{prefix}{accidental}{degree}",
            )

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

        ruler_rect = QRectF(margin_left, 0, usable_w, margin_top)
        painter.fillRect(ruler_rect, QColor(THEME["panel"]))
        painter.setPen(QPen(QColor(THEME["grid"]), 1.0))
        painter.drawLine(
            int(margin_left), int(margin_top - 0.5),
            int(margin_left + usable_w), int(margin_top - 0.5),
        )

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
                        hl_grad = QLinearGradient(
                            highlight_rect.topLeft(), highlight_rect.bottomLeft()
                        )
                        top_color = QColor(255, 255, 255, 80)
                        bot_color = QColor(255, 255, 255, 0)
                        hl_grad.setColorAt(0.0, top_color)
                        hl_grad.setColorAt(1.0, bot_color)
                        painter.setBrush(hl_grad)
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


class ImportOptionsDialog(QDialog):
    """匯入 MusicXML 的選項對話框：移調、左右手八度偏好、聲部模式。"""

    PREFER_OPTIONS = (
        ("最純原音（保留原音高關係）", "auto"),
        ("偏高音 H", "H"),
        ("偏中高 MH（M+H 兩排）", "MH"),
        ("偏中音 M", "M"),
        ("偏中低 ML（L+M 兩排）", "ML"),
        ("偏低音 L", "L"),
        ("無（不輸出此手）", "none"),
    )

    MELODY_MODES = (
        ("獨立（每個 voice 獨立 track）", "dense"),
        ("主旋律 + 簡化伴奏（右取最高、左取最低）", "skeleton"),
        ("只要主旋律（右手最高音、跳過左手）", "melody_only"),
    )

    def __init__(self, parent, suggested_transpose: int, suggested_range: int | None = None, is_mscz: bool = False) -> None:
        super().__init__(parent)
        self.setWindowTitle("匯入 MusicXML 選項")
        self.setMinimumWidth(460)
        self._suggested = suggested_transpose
        # 範圍感知建議:把右手主旋律 duration-weighted 中位數推到 MIDI 78(M 區中央)。
        # 若 importer 沒給就 fallback 到傳統建議(僅看 key signature)。
        self._suggested_range = (
            int(suggested_range) if suggested_range is not None else suggested_transpose
        )
        self._is_mscz = bool(is_mscz)
        # 預設值從 parent (PianoPlayerWindow) 的全域設定讀
        default_import_tempo = bool(getattr(parent, "_import_tempo_changes", True))
        self._default_import_tempo = default_import_tempo
        self.setStyleSheet(
            "QPushButton#transposeStep {"
            " font-size: 16pt; font-weight: 700;"
            " background-color: #2a2e36; color: #4dd0c2;"
            " border: 1px solid #4dd0c2; border-radius: 6px;"
            " padding: 0px;"
            "}"
            "QPushButton#transposeStep:hover {"
            " background-color: #4dd0c2; color: #16181d;"
            " border: 1px solid #a4f7c3;"
            "}"
            "QPushButton#transposeStep:pressed {"
            " background-color: #2dab9e; color: #0d0f12;"
            "}"
            "QRadioButton {"
            " color: #d0d4dc; padding: 4px 6px;"
            " spacing: 6px;"
            "}"
            "QRadioButton::indicator {"
            " width: 14px; height: 14px;"
            " border-radius: 8px;"
            " border: 2px solid #4dd0c2;"
            " background-color: #16181d;"
            "}"
            "QRadioButton::indicator:checked {"
            " background-color: #4dd0c2;"
            " border: 2px solid #a4f7c3;"
            "}"
            "QRadioButton:checked {"
            " color: #4dd0c2; font-weight: 700;"
            "}"
        )

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        layout.addWidget(QLabel(
            "選擇移調、左右手八度偏好、聲部模式；\n"
            "「主旋律」相關模式可避免 36 鍵塞太滿，確保旋律不被擠掉。"
        ))

        grid = QGridLayout()
        grid.addWidget(QLabel("移調半音數："), 0, 0)
        trans_box = QHBoxLayout()
        trans_box.setSpacing(6)
        self.transpose_minus_btn = QPushButton("−")
        self.transpose_minus_btn.setObjectName("transposeStep")
        self.transpose_minus_btn.setFixedSize(40, 34)
        self.transpose_minus_btn.setCursor(Qt.PointingHandCursor)
        self.transpose_minus_btn.setAutoRepeat(True)
        self.transpose_minus_btn.setAutoRepeatDelay(400)
        self.transpose_minus_btn.setAutoRepeatInterval(120)
        self.transpose_spin = QSpinBox()
        self.transpose_spin.setRange(-12, 12)
        self.transpose_spin.setValue(0)
        self.transpose_spin.setMinimumWidth(72)
        self.transpose_spin.setAlignment(Qt.AlignCenter)
        self.transpose_spin.setButtonSymbols(QAbstractSpinBox.NoButtons)
        self.transpose_plus_btn = QPushButton("+")
        self.transpose_plus_btn.setObjectName("transposeStep")
        self.transpose_plus_btn.setFixedSize(40, 34)
        self.transpose_plus_btn.setCursor(Qt.PointingHandCursor)
        self.transpose_plus_btn.setAutoRepeat(True)
        self.transpose_plus_btn.setAutoRepeatDelay(400)
        self.transpose_plus_btn.setAutoRepeatInterval(120)
        self.transpose_minus_btn.clicked.connect(
            lambda: self.transpose_spin.setValue(self.transpose_spin.value() - 1)
        )
        self.transpose_plus_btn.clicked.connect(
            lambda: self.transpose_spin.setValue(self.transpose_spin.value() + 1)
        )
        trans_box.addWidget(self.transpose_minus_btn)
        trans_box.addWidget(self.transpose_spin)
        trans_box.addWidget(self.transpose_plus_btn)
        trans_box.addStretch()
        grid.addLayout(trans_box, 0, 1)
        suggestion_text = (
            f"key 建議 {suggested_transpose:+d}（移到 C 大調 / A 小調）"
        )
        if self._suggested_range != suggested_transpose:
            suggestion_text += (
                f"  /  主旋律置中 {self._suggested_range:+d}（推到 M 區中央）"
            )
        else:
            suggestion_text += "  /  主旋律已落在 M 區中央"
        grid.addWidget(QLabel(suggestion_text), 0, 2)

        grid.addWidget(QLabel("右手譜偏好："), 1, 0)
        self.right_combo = QComboBox()
        for label, value in self.PREFER_OPTIONS:
            self.right_combo.addItem(label, value)
        grid.addWidget(self.right_combo, 1, 1, 1, 2)

        grid.addWidget(QLabel("左手譜偏好："), 2, 0)
        self.left_combo = QComboBox()
        for label, value in self.PREFER_OPTIONS:
            self.left_combo.addItem(label, value)
        grid.addWidget(self.left_combo, 2, 1, 1, 2)

        grid.addWidget(QLabel("聲部模式："), 3, 0)
        self.mode_combo = QComboBox()
        for label, value in self.MELODY_MODES:
            self.mode_combo.addItem(label, value)
        grid.addWidget(self.mode_combo, 3, 1, 1, 2)

        from PySide6.QtWidgets import QCheckBox, QButtonGroup, QRadioButton  # noqa: PLC0415 - 局部 import 不污染頂部
        self.import_tempo_check = QCheckBox("匯入原譜的變速 (tempo @ 標記)")
        self.import_tempo_check.setChecked(self._default_import_tempo)
        self.import_tempo_check.setToolTip(
            "勾選:把原譜中所有速度變化轉成 tempo @<beats> <bpm> 寫入。\n"
            "不勾:整曲只用單一起始速度,適合不希望被原譜 rubato 干擾的情境。"
        )
        grid.addWidget(self.import_tempo_check, 4, 0, 1, 3)

        self.save_to_songs_check = QCheckBox("匯入後另存到 songs/(出現在歌曲下拉選單)")
        self.save_to_songs_check.setChecked(False)
        self.save_to_songs_check.setToolTip(
            "勾選:匯入完成後自動把譜面以原檔名(.txt)存到 songs/ 目錄並切到該檔,\n"
            "下拉選單立刻顯示。\n"
            "不勾(預設):只在編輯器顯示為「(未存檔) 標題」,須按 Ctrl+S 手動另存\n"
            "才會正式進入 songs/ 目錄,避免暫存檔名或亂碼污染歌曲庫。"
        )
        grid.addWidget(self.save_to_songs_check, 5, 0, 1, 3)

        # mscz 專屬:選擇先把 mscz 轉成哪種中介格式再進入匯入流程。
        # MusicXML 保留音符 articulation/voice 細節,適合主旋律 + 伴奏分離較精細;
        # MIDI 結構單純、速度標記較統一,適合純按音高 + 時值對齊的情境。
        self.mscz_format_group: QButtonGroup | None = None
        self.mscz_format_xml_btn: QRadioButton | None = None
        self.mscz_format_midi_btn: QRadioButton | None = None
        if self._is_mscz:
            grid.addWidget(QLabel("MSCZ 轉換格式:"), 6, 0)
            mscz_row = QHBoxLayout()
            self.mscz_format_xml_btn = QRadioButton("MusicXML(預設,保留聲部)")
            self.mscz_format_midi_btn = QRadioButton("MIDI(只取音高+時值)")
            self.mscz_format_xml_btn.setChecked(True)
            self.mscz_format_group = QButtonGroup(self)
            self.mscz_format_group.addButton(self.mscz_format_xml_btn)
            self.mscz_format_group.addButton(self.mscz_format_midi_btn)
            mscz_row.addWidget(self.mscz_format_xml_btn)
            mscz_row.addWidget(self.mscz_format_midi_btn)
            mscz_row.addStretch()
            grid.addLayout(mscz_row, 6, 1, 1, 2)
        layout.addLayout(grid)

        presets_box = QHBoxLayout()
        recommended_btn = QPushButton("建議")
        recommended_btn.setToolTip(
            "推薦設定：移到 C 大調 / A 小調，右手 auto、左手固定 L，\n"
            "聲部模式採「獨立」（每個 voice 拆成獨立 track，三排都會用到）。"
        )
        recommended_btn.clicked.connect(self._preset_recommended)
        presets_box.addWidget(recommended_btn)
        presets_box.addStretch()
        layout.addLayout(presets_box)

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        layout.addWidget(bb)

        self._preset_recommended()  # 預設套用「建議」

    def _set_combo(self, combo: QComboBox, data_value: str) -> None:
        for i in range(combo.count()):
            if combo.itemData(i) == data_value:
                combo.setCurrentIndex(i)
                return

    def _preset_recommended(self) -> None:
        # 「建議」：移到 C 大調 / A 小調，右手 auto、左手 L，聲部模式採「獨立」(dense)。
        # 等同舊版的「三區複雜完整」。
        self.transpose_spin.setValue(self._suggested)
        self._set_combo(self.right_combo, "auto")
        self._set_combo(self.left_combo, "L")
        self._set_combo(self.mode_combo, "dense")

    def values(self) -> dict:
        mscz_format = "musicxml"
        if self._is_mscz and self.mscz_format_midi_btn is not None and self.mscz_format_midi_btn.isChecked():
            mscz_format = "midi"
        return {
            "transpose": self.transpose_spin.value(),
            "right_prefer": self.right_combo.currentData(),
            "left_prefer": self.left_combo.currentData(),
            "melody_mode": self.mode_combo.currentData(),
            "import_tempo_changes": self.import_tempo_check.isChecked(),
            "save_to_songs": self.save_to_songs_check.isChecked(),
            "mscz_format": mscz_format,
        }


class PianoPlayerWindow(QMainWindow):
    AUTOSAVE_PATH = _user_data_dir("autosave.txt")
    IMPORT_LOG_PATH = _user_data_dir("logs/import_error.log")

    # 高頻 slider 類 setting key:_on_panel_setting_changed 走 defer_set + 300ms flush。
    # 其他 toggle/combo key 立刻寫盤,維持原本的「按一下就落地」語意。
    _SETTINGS_DEBOUNCE_KEYS = frozenset({
        "playback_speed", "zoom_factor", "countdown_seconds",
        "piano_sound_volume",
        "dodge_threshold", "dodge_counter_threshold",
        "rhythm_loop_count", "rhythm_timeout_seconds", "rhythm_delay_ms",
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
        self._auto_trim_leading_silence = bool(self._settings.get("auto_trim_leading_silence", True))
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
        # F10/F11 全域熱鍵恆啟用,不再給開關。舊欄位/setting key 仍存在但只剩
        # backwards-compat 角色,新 code 一律當 True。
        self._automation_proxy = AutomationProxy()
        self._automation_task: AutomationTask | None = None
        # 自動更新 worker:沿用 AutomationProxy 模式,threading.Thread + QObject
        # signal,跨執行緒走 QueuedConnection,跟自動化任務同款。
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

        # 粉爪大劫案常駐 helper:跟 dodge/rhythm 不互斥,不占用 _automation_task
        # 槽位;status 訊息走自動化 log dock。滾輪固定啟用,不再拆獨立開關(整個
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
        self.piano_roll.set_smooth_browse_enabled(
            bool(self._settings.get("smooth_scroll_pianoroll", True))
        )
        self.piano_roll.set_smooth_zoom_enabled(
            bool(self._settings.get("smooth_zoom_pianoroll", True))
        )
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
        self._bridge.dodge_requested.connect(self.toggle_dodge_worker, Qt.QueuedConnection)
        self._bridge.rhythm_requested.connect(self.toggle_rhythm_worker, Qt.QueuedConnection)

        ok, message = self._hotkeys.start(automation_enabled=True)
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
        self._animations_enabled = True
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
        self.file_menu.addSeparator()
        self.act_delete_song = self.file_menu.addAction("刪除目前歌曲…")
        self.file_menu.addSeparator()
        # 大多數一般設定(動畫/失焦/聚焦/匯入變速/音高排序/顯示鍵盤/FPS/音色)都搬進
        # 右側設定面板(Ctrl+E)。這裡只留無法以面板替代的 runtime 操作。
        self.automation_menu = self.file_menu.addMenu("自動化")
        # toggle:跑著時 checked,點擊取消勾選 = 停止;沒任務時 disabled。
        # 啟動入口走右側設定面板的各 task「啟用」toggle。
        self.act_auto_stop = self.automation_menu.addAction("自動化執行中")
        self.act_auto_stop.setCheckable(True)
        self.act_auto_stop.setChecked(False)
        self.act_auto_stop.setEnabled(False)
        self.act_auto_stop.setToolTip("勾選代表任務跑著;點擊取消勾選即停止。")
        # 暫停/繼續 toggle:勾選 = 暫停,解勾 = 繼續。沒任務時也 disabled。
        self.act_auto_pause = self.automation_menu.addAction("暫停自動化")
        self.act_auto_pause.setCheckable(True)
        self.act_auto_pause.setChecked(False)
        self.act_auto_pause.setEnabled(False)
        self.act_auto_pause.setToolTip(
            "勾選暫停目前任務(維持狀態、不送鍵);再次點擊或解勾即繼續。"
        )
        self.automation_menu.addSeparator()
        self.act_auto_dock = self.automation_menu.addAction("顯示自動化監控\tCtrl+L")
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
        self.act_delete_song.triggered.connect(self.delete_current_song)
        # File / 自動化 menu 大幅瘦身,絕大多數設定改由右側設定面板控制。
        # 只剩下三個 runtime 操作的 toggle 還在 menu 裡。
        self.act_auto_stop.toggled.connect(self._on_auto_stop_toggled)
        self.act_auto_pause.toggled.connect(self._on_pause_automation_toggled)

        # AutomationProxy 在 main thread,signal 由 task 在 worker thread 觸發,
        # Qt 自動以 QueuedConnection 投遞 — 這就是繞過 OleInitialize 與跨緒
        # QTimer 警告的關鍵。connect 一次即可,task 結束不需 disconnect。
        self._automation_proxy.started.connect(
            self._on_automation_started, Qt.QueuedConnection
        )
        self._automation_proxy.status.connect(
            self._on_automation_status, Qt.QueuedConnection
        )
        self._automation_proxy.failed.connect(
            self._on_automation_failed, Qt.QueuedConnection
        )
        self._automation_proxy.finished.connect(
            self._on_automation_finished, Qt.QueuedConnection
        )
        self.act_auto_dock.toggled.connect(self._on_automation_dock_toggled)
        self._automation_dock.visibilityChanged.connect(
            self._on_automation_dock_visibility
        )
        # 自動更新:CheckUpdateTask / DownloadUpdateTask 在 worker thread,
        # signal 走 QueuedConnection 回 main thread,跟 AutomationProxy 同款。
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
        self.gap_spin.setSuffix(" s")
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
        self.mod_spin.setSuffix(" s")
        self.mod_spin.valueChanged.connect(lambda v: self._set_sheet_field("modifier_delay", v))

        # A/B 點(播放起點/終點,秒)。min = -1 視為「未設定」(用 setSpecialValueText
        # 顯示「未設定」),正值 = 該秒;更新時 _set_sheet_field("play_range_*_seconds")
        # 把 -1 還原成 None 寫進 sheet。
        self.play_a_spin = QDoubleSpinBox()
        self.play_a_spin.setRange(-1.0, 99999.0)
        self.play_a_spin.setSingleStep(0.1)
        self.play_a_spin.setDecimals(2)
        self.play_a_spin.setSuffix(" s")
        self.play_a_spin.setSpecialValueText("未設定")
        self.play_a_spin.valueChanged.connect(
            lambda v: self._set_sheet_field("play_range_start_seconds", v)
        )

        self.play_b_spin = QDoubleSpinBox()
        self.play_b_spin.setRange(-1.0, 99999.0)
        self.play_b_spin.setSingleStep(0.1)
        self.play_b_spin.setDecimals(2)
        self.play_b_spin.setSuffix(" s")
        self.play_b_spin.setSpecialValueText("未設定")
        self.play_b_spin.valueChanged.connect(
            lambda v: self._set_sheet_field("play_range_end_seconds", v)
        )

        for row, (text, widget, tip) in enumerate(
            (
                ("BPM", self.tempo_spin, "速度(每分鐘拍數)"),
                ("Beat", self.beat_spin, "每個 token 預設長度(beat 單位)"),
                ("Gap", self.gap_spin, "音符間最小間隙(秒)"),
                ("Hold", self.hold_spin, "按住比例(佔該音符長度)"),
                ("Mod", self.mod_spin, "Shift/Ctrl 修飾鍵延遲(秒)"),
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
            auto_trim_leading=self._auto_trim_leading_silence,
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
             - dodge/rhythm 的 *_active toggle:啟動或停止對應 task
             - 視覺(zoom/fps/pitch_sort/note_color_style/show_piano_keyboard/animations):
               同步給 piano_roll / overview_bar / 主視窗 flag
             - automation_hotkeys_enabled:重新註冊全域熱鍵
             - auto_pause_on_focus_loss / mute_on_focus_loss / focus_game_on_play /
               auto_trim_leading_silence / import_tempo_changes:更新對應 flag
        """
        # *_active 不寫入 settings.json,屬於 runtime 開關。其餘照寫。
        # 高頻 slider 類 key 走 defer_set + QTimer flush(300ms debounce),
        # 拖動時不會每像素 atomic-write settings.json。
        runtime_only = {"dodge_active", "rhythm_active"}
        if perf.enabled:
            debounced = key in self._SETTINGS_DEBOUNCE_KEYS
            perf.log("gui", "setting_change", key=key, value=value, debounced=debounced)
        if key not in runtime_only:
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

        # 兩個 task 的 *_active 開關
        if key == "dodge_active":
            self._set_panel_task_active("dodge", bool(value))
            return
        if key == "rhythm_active":
            self._set_panel_task_active("rhythm", bool(value))
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
        if key == "auto_trim_leading_silence":
            self._auto_trim_leading_silence = bool(value)
            return
        if key == "import_tempo_changes":
            self._import_tempo_changes = bool(value)
            return
        if key == "smooth_scroll_enabled":
            # 純設定;面板自己已即時把 SmoothScrollArea 開關掉,主視窗只需落盤。
            return
        if key == "smooth_scroll_pianoroll":
            self.piano_roll.set_smooth_browse_enabled(bool(value))
            return
        if key == "smooth_zoom_pianoroll":
            self.piano_roll.set_smooth_zoom_enabled(bool(value))
            return
        if key in ("confirm_discard_unsaved", "confirm_delete_song"):
            # 純設定;_confirm_discard_changes / delete_current_song 下一次呼叫時
            # 自己讀 settings,不需要立即推 side-effect。
            return
        if key == "automation_hotkeys_enabled":
            # 已移除 toggle,留分支吞掉舊 setting 的可能 emit,維持 forward-compat。
            return
        # 閃避/音遊的純參數(threshold/loop 等)只需寫進 settings,
        # 下一次 task 啟動時讀,不需要立刻 push 到 task。

    def _set_panel_task_active(self, which: str, active: bool) -> None:
        """把面板的 dodge/rhythm 啟用 toggle 對應到對應 task 的啟停。

        相同時間只能跑一個 task(沿用原本 _automation_task 互斥語意)。
        啟動失敗就把面板 toggle 反白(rollback)。
        """
        if active:
            if self._automation_task is not None:
                # 已有別的 task 跑著,提示並把 toggle 撥回 off
                self.statusBar().showMessage(
                    f"已有自動化任務在跑,請先停止後再切換到 {which}", 4000
                )
                self._refresh_panel_active_toggles()
                return
            try:
                if which == "dodge":
                    self.toggle_dodge_worker()
                elif which == "rhythm":
                    self.toggle_rhythm_worker()
            finally:
                self._refresh_panel_active_toggles()
        else:
            # 取消勾選 = 停止(只在當前跑著的 task 是這類型時才停)
            if self._automation_task is not None:
                self.stop_automation_task()
            self._refresh_panel_active_toggles()

    def _refresh_panel_active_toggles(self) -> None:
        """把面板 dodge_active/rhythm_active 兩個 toggle 重置成實際狀態。"""
        if not hasattr(self, "_settings_panel"):
            return
        task = self._automation_task
        label = getattr(task, "label", "") if task is not None else ""
        states = {
            "dodge_active": task is not None and "閃避" in label,
            "rhythm_active": task is not None and "音游" in label,
        }
        # 用 panel 自訂 getter 模式,直接 set widget 不會反向 emit
        for k, v in states.items():
            w = self._settings_panel.widget_for(k)
            if w is None:
                continue
            self._settings_panel._suppress = True
            try:
                w.setChecked(bool(v))
            finally:
                self._settings_panel._suppress = False

    @Slot(bool)
    def _on_animations_toggled(self, enabled: bool) -> None:
        self._animations_enabled = enabled
        self.piano_keyboard.set_animations_enabled(enabled)
        self.piano_roll.set_animations_enabled(enabled)
        if not enabled and self._dock_animation is not None and self._dock_animation.state() == QVariantAnimation.Running:
            self._dock_animation.stop()
            self._editor_dock.setFixedWidth(self._dock_natural_width)
            self._editor_dock.setGraphicsEffect(None)
        self.statusBar().showMessage(
            "動畫效果已開啟" if enabled else "動畫效果已關閉", 2000
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
    # _on_confirm_discard_toggled / _on_autosave_prompt_toggled 已刪除
    # (對應的 settings key confirm_discard_unsaved / autosave_restore_prompt 也已移除)。

    @Slot(bool)
    def _on_import_tempo_toggled(self, checked: bool) -> None:
        self._import_tempo_changes = bool(checked)
        self._settings.set("import_tempo_changes", self._import_tempo_changes)
        self.statusBar().showMessage(
            "匯入時將一起匯入變速" if checked else "匯入時將忽略原譜的變速", 2500
        )

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

    @Slot(bool)
    def _on_auto_trim_toggled(self, checked: bool) -> None:
        self._auto_trim_leading_silence = bool(checked)
        self._settings.set("auto_trim_leading_silence", self._auto_trim_leading_silence)
        self.statusBar().showMessage(
            "自動跳過譜面開頭空白已開啟" if checked else "自動跳過譜面開頭空白已關閉", 2000
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

    # open_dodge_dialog / open_rhythm_dialog 已合併進 _set_panel_task_active
    # (由 _on_panel_setting_changed 觸發)。toggle_dodge_worker /
    # toggle_rhythm_worker 仍保留供 F10/F11 hotkey 使用。

    @Slot()
    def toggle_dodge_worker(self) -> None:
        if self._automation_task is not None:
            self.stop_automation_task()
            return
        sample_dir = _resource_path("assets/sounds")
        sample_path = sample_dir / "dodge.wav"
        if not sample_path.exists():
            self.statusBar().showMessage(
                f"找不到閃避樣本 {sample_path};請打開「自動閃避…」對話框查看設定", 5000
            )
            return
        counter_path = sample_dir / "counter.wav"
        task = SoundCombatTask(
            self._automation_proxy,
            sample_path=str(sample_path),
            counter_sample_path=str(counter_path) if counter_path.exists() else None,
            threshold=float(self._settings.get("dodge_threshold", 0.13)),
            counter_threshold=float(self._settings.get("dodge_counter_threshold", 0.12)),
            dodge_key=str(self._settings.get("dodge_key", "shift")),
            counter_use_mouse=bool(self._settings.get("dodge_counter_use_mouse", True)),
            label="自動閃避",
        )
        self._start_automation_task(task)

    @Slot()
    def toggle_rhythm_worker(self) -> None:
        if self._automation_task is not None:
            self.stop_automation_task()
            return
        raw_keys = str(self._settings.get("rhythm_track_keys", "d,f,j,k"))
        keys = [k.strip() for k in raw_keys.split(",")]
        defaults = ["d", "f", "j", "k"]
        keys = [(keys[i] if i < len(keys) and keys[i] else defaults[i]) for i in range(4)]
        key_map = dict(zip(("d", "f", "j", "k"), keys))
        task = RhythmTask(
            self._automation_proxy,
            loop_count=int(self._settings.get("rhythm_loop_count", 0)),
            timeout_seconds=float(self._settings.get("rhythm_timeout_seconds", 180)),
            key_map=key_map,
            delay_ms=float(self._settings.get("rhythm_delay_ms", 0)),
            label="自動音游",
        )
        self._start_automation_task(task)

    @Slot()
    def stop_automation_task(self) -> None:
        task = self._automation_task
        if task is None:
            return
        try:
            task.request_stop()
        except Exception:
            pass
        self.statusBar().showMessage("正在停止自動化…", 2000)

    # 舊名相容(act_auto_stop.triggered.connect 仍可能用到):
    stop_automation_worker = stop_automation_task

    @Slot(bool)
    def _on_auto_stop_toggled(self, checked: bool) -> None:
        """『自動化執行中』toggle:checked=跑著,被取消勾選=請求停止。

        勾選方向(False→True)通常只由 _start_automation_task 透過 blockSignals
        設置,使用者直接點開時若沒有 task 跑著,UI 是 disabled 不會走到這。
        """
        if checked:
            # 沒任務時不該變 checked,但 paranoid 防 race:回 unchecked。
            if self._automation_task is None:
                self.act_auto_stop.blockSignals(True)
                self.act_auto_stop.setChecked(False)
                self.act_auto_stop.blockSignals(False)
            return
        # 取消勾選 = 使用者要停。沒任務就靜默不動作(避免 finished slot 把 UI
        # reset 為 False 後又被 toggled 回 callback 觸發 stop)。
        if self._automation_task is None:
            return
        self.stop_automation_task()

    @Slot(bool)
    def _on_pause_automation_toggled(self, checked: bool) -> None:
        """『暫停自動化』toggle:checked=暫停,unchecked=繼續。"""
        task = self._automation_task
        if task is None:
            # 沒任務還能 toggle 表示 race:回到正確狀態。
            self.act_auto_pause.blockSignals(True)
            self.act_auto_pause.setChecked(False)
            self.act_auto_pause.setEnabled(False)
            self.act_auto_pause.blockSignals(False)
            return
        try:
            if checked:
                task.request_pause()
                self.statusBar().showMessage(f"{task.label} 已暫停", 3000)
                self._append_automation_log(f"{task.label} 暫停")
            else:
                task.request_resume()
                self.statusBar().showMessage(f"{task.label} 已繼續", 3000)
                self._append_automation_log(f"{task.label} 繼續")
        except Exception as exc:  # noqa: BLE001
            self._append_automation_log(f"[錯誤] 暫停/繼續失敗: {exc}")

    def _automation_busy_warn(self) -> bool:
        if self._worker is not None:
            self.statusBar().showMessage("演奏中無法啟動自動化,請先 F7 停止", 4000)
            return True
        if self._automation_task is not None:
            self.statusBar().showMessage(
                "已有自動化任務在跑,請先按「停止自動化」或對應的 hotkey", 4000
            )
            return True
        return False

    def _start_automation_task(self, task: AutomationTask) -> None:
        """以 threading.Thread 啟動 task。

        對齊 ok-nte 的執行模型:不再用 QThread + moveToThread,避免 QThread 在
        Windows 預設 OleInitialize(STA) 與 soundcard/pycaw 子線程的 MTA 衝突,
        以及 QObject 跨線程被 Qt 自動管理 timer 帶來的 startTimer/killTimer 警告。

        proxy 是單一 main-thread QObject,task 透過 emit_* helper 將狀態送到
        proxy 的 signal,Qt 自動以 QueuedConnection 投遞到主視窗的 slot。

        啟動前先 ping 一次 find_game_window():沒偵測到 NTE 就直接拒絕,不開
        背景 thread。能避免 task 跑進 listener.start() / mss capture 後再退出
        的 1-2 秒空轉,並讓使用者即時看到「請先開遊戲」提示。
        """
        if self._worker is not None:
            self.statusBar().showMessage("演奏中無法啟動自動化,請先 F7 停止", 4000)
            return
        if self._automation_task is not None:
            self.statusBar().showMessage("已有自動化任務在跑,請先停止它", 4000)
            return
        # 保險層:啟動前確認遊戲視窗存在(HTGame.exe 或標題含 NTE 但非自己)
        game = find_game_window()
        if game is None:
            msg = "找不到 NTE 遊戲視窗(HTGame.exe / 視窗標題含 NTE),請先開啟遊戲"
            self.statusBar().showMessage(msg, 6000)
            self._append_automation_log(f"[錯誤] {msg}")
            return
        self._automation_task = task
        # toggle 進入「跑著」狀態;blockSignals 避免 setChecked 反向觸發 stop。
        self.act_auto_stop.blockSignals(True)
        self.act_auto_stop.setChecked(True)
        self.act_auto_stop.setEnabled(True)
        self.act_auto_stop.blockSignals(False)
        self.act_auto_pause.blockSignals(True)
        self.act_auto_pause.setChecked(False)
        self.act_auto_pause.setEnabled(True)
        self.act_auto_pause.blockSignals(False)
        self.statusBar().showMessage(
            f"啟動 {task.label}(已偵測到 {game.process_name})…", 3000
        )
        self._append_automation_log(
            f"啟動 {task.label}(目標: {game.process_name} '{game.title}')"
        )
        task.start()

    # 舊名相容(內部呼叫處改完,但保留 alias 防呼叫端漏改):
    def _start_automation_worker(self, worker, label=None):  # type: ignore[no-untyped-def]
        # 沿用舊簽名(worker, label),label 不再使用 — task 自帶 _label。
        self._start_automation_task(worker)

    @Slot(str)
    def _on_automation_started(self, label: str) -> None:
        self.statusBar().showMessage(f"{label} 進行中(按 F7 / 停止自動化 結束)", 4000)
        self._append_automation_log(f"{label} 進行中(按 F7 或選單停止)")

    @Slot(str)
    def _on_automation_status(self, message: str) -> None:
        self.statusBar().showMessage(message, 5000)
        self._append_automation_log(message)

    @Slot(str)
    def _on_automation_failed(self, message: str) -> None:
        # 失敗一律走 status bar + log,不彈 modal —
        # modal 期間 task 可能持續 emit status,event loop 阻塞會堆積 queue,
        # dispatch 時 task 已釋放容易拿到 stale ref。GUI 上 log 區夠醒目了。
        self._append_automation_log(f"[錯誤] {message}")
        self.statusBar().showMessage(f"自動化錯誤: {message}", 8000)

    @Slot(bool)
    def _on_automation_finished(self, stopped: bool) -> None:
        task = self._automation_task
        self._automation_task = None
        # 兩個 toggle 都 reset 回 unchecked + disabled。blockSignals 防止
        # setChecked(False) 反向觸發 stop/resume handler。
        self.act_auto_stop.blockSignals(True)
        self.act_auto_stop.setChecked(False)
        self.act_auto_stop.setEnabled(False)
        self.act_auto_stop.blockSignals(False)
        self.act_auto_pause.blockSignals(True)
        self.act_auto_pause.setChecked(False)
        self.act_auto_pause.setEnabled(False)
        self.act_auto_pause.blockSignals(False)
        if task is not None and task.is_alive():
            # task.run 已 emit_finished,但 thread 可能還在跑 finally cleanup。
            # join 一下避免下次重啟時 self.is_alive() 仍為 True。短 timeout 即可。
            try:
                task.join(timeout=1.5)
            except Exception:
                pass
        if stopped:
            self.statusBar().showMessage("自動化已停止", 3000)
            self._append_automation_log("自動化已停止")
        else:
            self.statusBar().showMessage("自動化結束", 3000)
            self._append_automation_log("自動化結束")

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
        if self._automation_task is not None:
            try:
                self._automation_task.request_stop()
            except Exception:
                pass
            try:
                # 給 task 最多 3 秒善後;dameon thread 即使沒 join 完也會隨主程式結束。
                self._automation_task.join(timeout=3.0)
            except Exception:
                pass
            self._automation_task = None
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


def parse_args(argv):
    parser = argparse.ArgumentParser(description=APP_TITLE)
    parser.add_argument("score", nargs="?", type=Path, help="要載入的譜面檔")
    return parser.parse_args(list(argv))


def main(argv=None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    # NTE_PERF=1 啟用效能日誌,寫到使用者目錄 logs/perf_*.log。在 _seed_default_songs
    # 之前打開,連啟動順序都能記錄。
    perf_path = init_perf_from_env(_user_data_dir("logs"))
    if perf_path is not None:
        print(f"[nte_perf] logging to {perf_path}", file=sys.stderr)
    # 首次啟動把 bundled 預設譜面 copy 到使用者 songs/。frozen / dev 都要跑;
    # dev 時 _resource_path 跟 _user_data_dir 同一個目錄,seed 會 no-op。
    _seed_default_songs()
    app = QApplication(sys.argv)
    icon_path = _resource_path("assets/icon.ico")
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))
    default_font = QFont("Microsoft JhengHei UI", 10)
    default_font.setStyleHint(QFont.SansSerif)
    app.setFont(default_font)
    window = PianoPlayerWindow(args.score)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
