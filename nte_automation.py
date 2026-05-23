# NTE Piano - 自動演奏與自動化工具
# Copyright (C) 2026  Yulun
# Licensed under GPL-3.0-or-later. See LICENSE.
"""nte_automation — 遊戲內自動化任務(聲音閃避、自動音遊)。

本模組提供 ok-nte 衍生的自動化路徑,共用一組視窗截圖工具與 KeyBackend。
為對齊 ok-nte 的執行模型(避免 PySide6 QThread 帶來的 OleInitialize / 跨執行緒
QTimer 衝突),所有任務都繼承 AutomationTask(threading.Thread)而非 QObject;
與 GUI 的 signal/slot 通訊一律透過 main-thread 的 AutomationProxy(QObject)轉發。

對外提供:
    WindowedScreenCapture - mss 截圖 + find_game_window 視窗矩形換算
    AutomationProxy       - main-thread Qt signal hub,worker thread 透過它送事件
    AutomationTask        - threading.Thread 抽象基底,提供 stop/wait/key tap helpers
    SoundListener         - audio loopback + cross-correlation(GPL-3.0,衍生自
                            ZZZSoundTrigger by ImLaoBJie 經 ok-nte 改造)
    DodgeCounterTrigger   - 閃避/反擊節流與動作派發(同上 GPL 衍生)
    SoundCombatTask       - 把 SoundListener + DodgeCounterTrigger 包成 task
    RhythmTask            - 一次截 client area + slice 4 點 + 非同步按鍵 + 結算重試
    (SoundCombatWorker / RhythmWorker 為向後相容別名)

依賴(在模組層級 try import,缺套件時對應 worker 標 disabled 並在 run() emit failed):
    mss              - 視窗截圖
    numpy            - 影像/音訊張量
    opencv-python    - HSV 色彩過濾
    librosa          - 音訊樣本載入
    soundcard        - 系統 audio loopback
    scipy            - butter highpass + cross-correlation
    scikit-learn     - scale 標準化

樣本檔(自動閃避必要):
    assets/sounds/dodge.wav    閃避警示音
    assets/sounds/counter.wav  反擊提示音(可選)

詳細實作請見 ok-nte-main/src/ 對照。
"""
from __future__ import annotations

import ctypes
import os
import sys
import threading
import time
import warnings
from collections import deque
from ctypes import wintypes
from dataclasses import dataclass
from typing import Callable, Optional

from PySide6.QtCore import QObject, Signal

from nte_playback import (
    KeyBackend,
    create_backend_with_fallback,
    find_game_window,
    focus_window,
    foreground_hwnd,
    is_target_foreground,
    is_window_alive,
)


# soundcard 在 Windows mediafoundation 偶爾噴 "data discontinuity in recording"
# 這是 buffer underrun(系統忙碌時送音不及),純警告不影響偵測;對齊
# ok-nte-main/src/sound_trigger/SoundListener.py:21 一律靜音。
warnings.filterwarnings("ignore", message="data discontinuity in recording")


# ============================================================================
# main thread COM 模式釘樁 — soundcard mediafoundation.py:116 在 module level
# 跑 _com = _COMLibrary(),其 __init__ 呼叫 CoInitializeEx(NULL, COINIT_MULTITHREADED)
# 會把 main thread 鎖死 MTA。之後 QApplication() 內部 OleInitialize(STA) 收到
# RPC_E_CHANGED_MODE (0x80010106) 失敗,QFileDialog 開 native dialog 時 OLE
# 未正確初始化會 access violation,整個進程被 Windows 殺掉(exit code
# -805306369 = 0xCFFFFFFF),點「匯入 MuseScore (MSCZ)…」最常觸發。
# 修法:在 import soundcard 之前先 OleInitialize(NULL) 把 main thread 釘成
# STA + OLE;soundcard 之後嘗試 MTA 會收到 RPC_E_CHANGED_MODE 並自行吞掉
# (見 mediafoundation.py:58-69 的 com_loaded=False 路徑),功能不受影響
# (實際錄音在 worker thread 自己 CoInitialize,跟 main thread 模式無關)。
# ============================================================================
if sys.platform == "win32":
    try:
        ctypes.windll.ole32.OleInitialize(None)
    except Exception:  # noqa: BLE001
        pass


# ============================================================================
# 模組層級依賴 import — 對齊 ok-nte 寫法,直接 import + try/except,
# 缺套件時以 None 標記,worker 在 run() 入口檢查並 emit failed。
# ============================================================================

try:
    import mss as _mss  # type: ignore[import]
except Exception:  # noqa: BLE001 — 缺套件保持模組可載入
    _mss = None  # type: ignore[assignment]

try:
    import numpy as _np  # type: ignore[import]
except Exception:  # noqa: BLE001
    _np = None  # type: ignore[assignment]

try:
    import cv2 as _cv2  # type: ignore[import]
except Exception:  # noqa: BLE001
    _cv2 = None  # type: ignore[assignment]

# 音訊類整組要在一起,因為 SoundListener 需要這 4 個套件互相配合;
# 缺其中任一個就把整組當作不可用,避免一半可用一半 NoneType 報錯。
try:
    import librosa as _librosa  # type: ignore[import]
    import soundcard as _sc  # type: ignore[import]
    from scipy.signal import (  # type: ignore[import]
        butter as _scipy_butter,
        correlate as _scipy_correlate,
        filtfilt as _scipy_filtfilt,
    )
    from sklearn.preprocessing import scale as _sk_scale  # type: ignore[import]
    _SOUND_LIBS_OK = True
    _SOUND_LIBS_ERR = ""
except Exception as _sound_import_exc:  # noqa: BLE001
    _librosa = None  # type: ignore[assignment]
    _sc = None  # type: ignore[assignment]
    _scipy_butter = None  # type: ignore[assignment]
    _scipy_correlate = None  # type: ignore[assignment]
    _scipy_filtfilt = None  # type: ignore[assignment]
    _sk_scale = None  # type: ignore[assignment]
    _SOUND_LIBS_OK = False
    _SOUND_LIBS_ERR = f"{type(_sound_import_exc).__name__}: {_sound_import_exc}"

# pycaw + comtypes — 失焦自動靜音用。
#
# 關鍵:不能在 main thread 的 module load 時 import comtypes / pycaw。
# 原因:comtypes/__init__.py 在 import 時會呼叫 CoInitialize(STA);但 Piano
# Player 的 main thread 已經被 PySide6/Qt 設成 STA(或被 Qt 內部設成 MTA),
# comtypes 嘗試設不同 mode 會丟 OSError [WinError -2147417850]
# (RPC_E_CHANGED_MODE 0x80010106)。
#
# 修法:module level 只用 importlib.util.find_spec 檢查兩個套件存在,不 import。
# 真正的 import 延後到 BackgroundAudioMuter._run() 在獨立 daemon thread 內做,
# 該 thread 從零開始 COM 未初始化,可任選 mode 不衝突。
try:
    import importlib.util as _importlib_util
    _pycaw_missing = [
        name for name in ("comtypes", "pycaw")
        if _importlib_util.find_spec(name) is None
    ]
    if _pycaw_missing:
        _PYCAW_OK = False
        _PYCAW_ERR = f"ModuleNotFoundError: {', '.join(_pycaw_missing)}"
    else:
        _PYCAW_OK = True
        _PYCAW_ERR = ""
except Exception as _pycaw_check_exc:  # noqa: BLE001
    _PYCAW_OK = False
    _PYCAW_ERR = f"{type(_pycaw_check_exc).__name__}: {_pycaw_check_exc}"


# ============================================================================
# Win32 / Mouse / 截圖工具
# ============================================================================

SW_RESTORE = 9
_AUTOMATION_WINAPI_READY = False


def _ensure_winapi_for_automation() -> None:
    """設定 client rect / mouse 函式簽名,避免 64-bit ctypes 失誤。

    nte_playback._configure_winapi 設了 keybd_event 與視窗列舉相關函式,
    但沒包含 GetClientRect / ClientToScreen / SetCursorPos / mouse_event,
    這四個是自動化專用,在這裡單獨設定。
    """
    global _AUTOMATION_WINAPI_READY
    if _AUTOMATION_WINAPI_READY or sys.platform != "win32":
        return
    user32 = ctypes.windll.user32
    user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetClientRect.restype = wintypes.BOOL
    user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
    user32.ClientToScreen.restype = wintypes.BOOL
    user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
    user32.SetCursorPos.restype = wintypes.BOOL
    user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
    user32.GetCursorPos.restype = wintypes.BOOL
    user32.mouse_event.argtypes = [
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
    ]
    user32.mouse_event.restype = None
    _AUTOMATION_WINAPI_READY = True


@dataclass(frozen=True)
class _ClientRect:
    x: int
    y: int
    width: int
    height: int


def _get_client_rect(hwnd: int) -> Optional[_ClientRect]:
    """回 (左上角 screen 座標, client 寬高)。"""
    if sys.platform != "win32" or not hwnd:
        return None
    if not is_window_alive(hwnd):
        return None
    _ensure_winapi_for_automation()
    user32 = ctypes.windll.user32
    rect = wintypes.RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
        return None
    pt = wintypes.POINT(0, 0)
    if not user32.ClientToScreen(hwnd, ctypes.byref(pt)):
        return None
    width = int(rect.right - rect.left)
    height = int(rect.bottom - rect.top)
    if width <= 0 or height <= 0:
        return None
    return _ClientRect(int(pt.x), int(pt.y), width, height)


# mouse_event flags
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010


def _send_mouse_click(screen_x: int, screen_y: int, button: str = "left") -> None:
    """SetCursorPos + mouse_event;呼叫前後保存/還原使用者游標位置避免搶滑鼠。

    SetCursorPos 會把游標瞬間搬到目標位置,使用者正在操作滑鼠時會看到游標跳一下。
    這裡在送點擊事件前後保存原始位置,事件送完立刻還原,把感受降到最低。
    遊戲若拒絕後台輸入(常見於 DirectInput 鎖滑鼠的遊戲)點擊可能無效,
    但這已是最通用方案。
    """
    if sys.platform != "win32":
        return
    _ensure_winapi_for_automation()
    user32 = ctypes.windll.user32
    orig_pt = wintypes.POINT()
    cursor_saved = False
    try:
        if user32.GetCursorPos(ctypes.byref(orig_pt)):
            cursor_saved = True
    except Exception:  # noqa: BLE001
        cursor_saved = False
    user32.SetCursorPos(int(screen_x), int(screen_y))
    if button == "right":
        down, up = MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP
    else:
        down, up = MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP
    time.sleep(0.01)
    user32.mouse_event(down, 0, 0, 0, 0)
    time.sleep(0.04)
    user32.mouse_event(up, 0, 0, 0, 0)
    if cursor_saved:
        try:
            user32.SetCursorPos(int(orig_pt.x), int(orig_pt.y))
        except Exception:  # noqa: BLE001
            pass


class WindowedScreenCapture:
    """以 mss 為後端的視窗截圖封裝。

    所有比例座標(0-1)都以遊戲視窗的 client area 為基準,呼叫前不需手動換算。
    缺 mss / numpy 時退化為 grab_* 回 None,呼叫端應自行處理。
    """

    def __init__(self, hwnd: Optional[int] = None) -> None:
        self._hwnd = hwnd
        self._sct = None
        self._sct_lock = threading.Lock()

    def set_hwnd(self, hwnd: Optional[int]) -> None:
        self._hwnd = hwnd

    @property
    def hwnd(self) -> Optional[int]:
        return self._hwnd

    def is_available(self) -> bool:
        return _mss is not None and _np is not None

    def _ensure_sct(self):
        if _mss is None:
            return None
        if self._sct is None:
            with self._sct_lock:
                if self._sct is None:
                    self._sct = _mss.mss()
        return self._sct

    def _client_rect(self) -> Optional[_ClientRect]:
        if not self._hwnd:
            return None
        return _get_client_rect(self._hwnd)

    def grab_full(self):
        """整個遊戲視窗 client area;回 BGR ndarray (H, W, 3) 或 None。"""
        rect = self._client_rect()
        if rect is None:
            return None
        return self._grab_screen_box(rect.x, rect.y, rect.width, rect.height)

    def grab_box(self, x_pct: float, y_pct: float, w_pct: float, h_pct: float):
        """以 client area 為基準的比例 box(左上 + 寬高皆 0-1);回 BGR ndarray 或 None。"""
        rect = self._client_rect()
        if rect is None:
            return None
        x = rect.x + int(rect.width * max(0.0, min(1.0, x_pct)))
        y = rect.y + int(rect.height * max(0.0, min(1.0, y_pct)))
        w = max(1, int(rect.width * max(0.0, min(1.0, w_pct))))
        h = max(1, int(rect.height * max(0.0, min(1.0, h_pct))))
        return self._grab_screen_box(x, y, w, h)

    def grab_pixel(self, x_pct: float, y_pct: float, radius_x: int, radius_y: int):
        """以中心點為基準的小區塊(像素半徑);回 BGR ndarray 或 None。"""
        rect = self._client_rect()
        if rect is None:
            return None
        cx = rect.x + int(rect.width * x_pct)
        cy = rect.y + int(rect.height * y_pct)
        x = cx - max(1, int(radius_x))
        y = cy - max(1, int(radius_y))
        w = max(1, int(radius_x) * 2 + 1)
        h = max(1, int(radius_y) * 2 + 1)
        return self._grab_screen_box(x, y, w, h)

    def client_to_screen(self, x_pct: float, y_pct: float) -> Optional[tuple[int, int]]:
        rect = self._client_rect()
        if rect is None:
            return None
        return (
            rect.x + int(rect.width * x_pct),
            rect.y + int(rect.height * y_pct),
        )

    def _grab_screen_box(self, x: int, y: int, w: int, h: int):
        sct = self._ensure_sct()
        if sct is None or _np is None:
            return None
        region = {"left": int(x), "top": int(y), "width": int(w), "height": int(h)}
        try:
            with self._sct_lock:
                shot = sct.grab(region)
        except Exception:  # noqa: BLE001 — 抓不到一幀不該整個流程崩
            return None
        # mss 回 BGRA, 轉成 BGR(H, W, 3)
        arr = _np.frombuffer(shot.rgb, dtype=_np.uint8).reshape((shot.height, shot.width, 3))
        return arr[:, :, ::-1].copy()  # rgb→bgr

    def close(self) -> None:
        with self._sct_lock:
            if self._sct is not None:
                try:
                    self._sct.close()
                except Exception:  # noqa: BLE001
                    pass
                self._sct = None


def _color_percentage(image, color_range: dict) -> float:
    """計算 image (BGR ndarray) 中符合 color_range 的像素比例。

    color_range 是 {"r": (min, max), "g": (min, max), "b": (min, max)}。
    """
    if _np is None or image is None or image.size == 0:
        return 0.0
    b = image[:, :, 0]
    g = image[:, :, 1]
    r = image[:, :, 2]
    rmin, rmax = color_range["r"]
    gmin, gmax = color_range["g"]
    bmin, bmax = color_range["b"]
    mask = (
        (r >= rmin)
        & (r <= rmax)
        & (g >= gmin)
        & (g <= gmax)
        & (b >= bmin)
        & (b <= bmax)
    )
    return float(mask.mean())


def _find_runs(mask_1d) -> list[tuple[int, int]]:
    """對 1D bool ndarray 找連續 True 區段,回 [(start, end_exclusive), ...]。

    用於 column-wise mask 找連續綠/黃像素區段。空陣列或全 False 回 []。
    """
    if _np is None:
        return []
    arr = _np.asarray(mask_1d, dtype=bool)
    if arr.size == 0:
        return []
    # 找 0→1 與 1→0 的邊界
    edges = _np.diff(arr.astype(_np.int8), prepend=0, append=0)
    starts = _np.where(edges == 1)[0]
    ends = _np.where(edges == -1)[0]
    return list(zip(starts.tolist(), ends.tolist()))


# Template 快取 — module-level dict,key=template 相對路徑,value=灰階 ndarray。
# 第一次用到才載入(lazy),避免 import 期就要 OpenCV;載入失敗回 None 並記 cache。
_TEMPLATE_CACHE: dict = {}


def _load_template_gray(rel_path: str):
    """載入 template PNG → 灰階 ndarray。找不到或 OpenCV 缺則回 None。

    路徑解析:frozen (PyInstaller) 走 sys._MEIPASS,dev 走 nte_automation.py 旁;
    這兩個目錄都是 bundled 唯讀資源根,assets/ 子資料夾在 .spec 內以 datas
    加進去。
    """
    if rel_path in _TEMPLATE_CACHE:
        return _TEMPLATE_CACHE[rel_path]
    if _cv2 is None or _np is None:
        _TEMPLATE_CACHE[rel_path] = None
        return None
    try:
        from pathlib import Path as _Path  # 局部 import 避免污染 module namespace
        if getattr(sys, "frozen", False):
            base = _Path(getattr(sys, "_MEIPASS", "") or _Path(sys.executable).resolve().parent)
        else:
            base = _Path(__file__).resolve().parent
        full = base / rel_path
        if not full.exists():
            _TEMPLATE_CACHE[rel_path] = None
            return None
        raw = _np.fromfile(str(full), dtype=_np.uint8)
        img = _cv2.imdecode(raw, _cv2.IMREAD_COLOR)
        if img is None:
            _TEMPLATE_CACHE[rel_path] = None
            return None
        gray = _cv2.cvtColor(img, _cv2.COLOR_BGR2GRAY)
        _TEMPLATE_CACHE[rel_path] = gray
        return gray
    except Exception:  # noqa: BLE001
        _TEMPLATE_CACHE[rel_path] = None
        return None


def _match_template_in_box(
    capture,
    template_rel_path: str,
    template_width_ratio: float,
    search_box: tuple,
) -> float:
    """在 search_box(client area 比例)內找 template,回最大 normalized correlation。

    template_width_ratio 是 template 寬佔 client area 寬的比例 — 執行時會把
    template 等比縮放到匹配的大小,讓不同遊戲解析度都能對得上。

    回 0.0 表示比對失敗(OpenCV / template / capture 任一缺)或分數為 0。
    用 TM_CCOEFF_NORMED 對亮度/對比變化有正規化,不易被天氣/時段干擾。
    """
    if _cv2 is None or _np is None:
        return 0.0
    tpl_gray = _load_template_gray(template_rel_path)
    if tpl_gray is None:
        return 0.0
    x0, y0, x1, y1 = search_box
    img = capture.grab_box(x0, y0, x1 - x0, y1 - y0)
    if img is None or img.size == 0:
        return 0.0
    sub_gray = _cv2.cvtColor(img, _cv2.COLOR_BGR2GRAY)
    sub_h, sub_w = sub_gray.shape[:2]
    # 把 template resize 到目標寬,維持比例。
    # 目標寬 = capture 寬 × template_width_ratio。capture 寬 = sub_w / (x1 - x0)。
    capture_w = sub_w / max(1e-6, (x1 - x0))
    target_w = max(16, int(round(capture_w * template_width_ratio)))
    tpl_h0, tpl_w0 = tpl_gray.shape[:2]
    scale = target_w / float(tpl_w0)
    target_h = max(16, int(round(tpl_h0 * scale)))
    if target_h > sub_h or target_w > sub_w:
        return 0.0
    tpl_resized = _cv2.resize(tpl_gray, (target_w, target_h), interpolation=_cv2.INTER_AREA)
    res = _cv2.matchTemplate(sub_gray, tpl_resized, _cv2.TM_CCOEFF_NORMED)
    _, max_val, _, _ = _cv2.minMaxLoc(res)
    return float(max_val)


# ============================================================================
# AutomationProxy + AutomationTask 抽象基底
# ============================================================================


# ============================================================================
# AutomationProxy + AutomationTask 基底
# ============================================================================
# 設計理由(對齊 ok-nte 的執行模型):
#
# ok-nte 的 task 在普通 threading.Thread 內執行,不依賴 PySide6 QThread。
# 本專案早期把 worker 包成 QObject + QThread + moveToThread,結果在 Windows 上
# 觸發兩類問題:
#   1. QThread 預設 OleInitialize(STA),soundcard/pycaw 子執行緒走 MTA 後互沖,
#      log 印出 "OleInitialize() failed: COM error 0x80010106"。
#   2. QStatusBar.showMessage(msg, timeout) 內部 startTimer,當 GUI modal 阻塞時
#      worker 持續 emit 進佇列,稍後 dispatch 時 QTimer 已被 cleanup 反覆觸發
#      "QObject::startTimer/killTimer: Timers cannot be ... from another thread"。
#
# 改成 threading.Thread + main-thread proxy 後,worker thread 不掛 Qt event loop,
# 不會 OleInitialize;訊息回 GUI 一律走 proxy.signal.emit(...) — receiver 在
# main thread,Qt 自動以 QueuedConnection 跨緒投遞,不會碰到 timer 衝突。


class AutomationProxy(QObject):
    """Main-thread Qt signal hub。worker thread 透過 emit_* helper 送事件給 GUI。

    所有 signal 的 receiver 都應該在 main thread(Qt 會自動 QueuedConnection)。
    GUI 持有單一 proxy 並在啟動 task 時注入,task 結束後不需要 disconnect。

    Helper 命名刻意全部用 emit_ 前綴,避免與 task 內 `self.<x>.emit(...)`
    pattern 撞名,讓重構期間的批次取代不會誤改 proxy 自身的 signal emit。
    """

    started = Signal(str)             # label
    status = Signal(str)
    failed = Signal(str)
    finished = Signal(bool)           # stopped (True 表使用者主動停止)
    score_update = Signal(float, float)   # SoundCombatTask 用 (dodge, counter)

    def emit_started(self, label: str) -> None:
        self.started.emit(str(label))

    def emit_status(self, message: str) -> None:
        self.status.emit(str(message))

    def emit_failed(self, message: str) -> None:
        self.failed.emit(str(message))

    def emit_finished(self, stopped: bool) -> None:
        self.finished.emit(bool(stopped))

    def emit_score(self, dodge: float, counter: float) -> None:
        self.score_update.emit(float(dodge), float(counter))


class AutomationTask(threading.Thread):
    """所有自動化 task 的基底,跑在 daemon threading.Thread。

    子類覆寫 run() 實作業務邏輯,以 self._stop_event 為 cancellation primitive,
    透過 self._proxy.emit_* helper 將狀態送回 GUI(main thread)。
    """

    def __init__(self, proxy: "AutomationProxy", label: str) -> None:
        super().__init__(daemon=True, name=label)
        self._proxy = proxy
        self._label = label
        self._stop_event = threading.Event()
        # pause Event 語意:set = 暫停中,clear = 正常跑。預設 clear。
        # 用 Event 是因為要 block-wait_for resume 而不忙等,_check_pause 用
        # wait(timeout) 配合 stop_event 達成「暫停期間隨時可被 stop 中斷」。
        self._pause_event = threading.Event()

    @property
    def label(self) -> str:
        return self._label

    def request_stop(self) -> None:
        self._stop_event.set()
        # 順手 wake 暫停中的 task,讓它能立刻看到 stop 訊號退出。
        self._pause_event.set()

    def is_stopping(self) -> bool:
        return self._stop_event.is_set()

    def request_pause(self) -> None:
        """請求暫停。task 下一個 _check_pause() 會 block 在這。"""
        self._pause_event.set()

    def request_resume(self) -> None:
        """解除暫停。block 在 _check_pause() 的 task 會立刻被喚醒。"""
        if self._stop_event.is_set():
            return
        self._pause_event.clear()

    def is_paused(self) -> bool:
        """暫停中且尚未被 stop。stop 會把 pause_event 也 set,要排除。"""
        return self._pause_event.is_set() and not self._stop_event.is_set()

    def _check_pause(self) -> bool:
        """在主 loop tick 開頭呼叫。暫停中會 block 直到 resume 或 stop。

        回 True 表示 task 應該結束(stop 被觸發)。子類用法:
            if self._check_pause(): return  # 退出 run()
        """
        if not self._pause_event.is_set():
            return self._stop_event.is_set()
        # 暫停中 — 用 0.2s 為單位 wait,給 stop_event 即時 break 出口。
        while self._pause_event.is_set():
            if self._stop_event.is_set():
                return True
            # 沒有專用「resume」event,用短輪詢避免再開一個 Event。
            time.sleep(0.2)
        return self._stop_event.is_set()

    def _wait(self, seconds: float) -> bool:
        """等到時間到或 stop 觸發。回 True 表已 stop。"""
        if seconds <= 0:
            return self._stop_event.is_set()
        return self._stop_event.wait(seconds)

    def _send_key_tap(self, backend: KeyBackend, key: str, hold: float = 0.05) -> None:
        """單次按下並放開 key,常用於 F/ESC/E/Q 等互動鍵。"""
        try:
            backend.key_down(key)
            time.sleep(max(0.01, hold))
            backend.key_up(key)
        except Exception:  # noqa: BLE001
            try:
                backend.key_up(key)
            except Exception:  # noqa: BLE001
                pass

    def run(self) -> None:  # pragma: no cover - 抽象
        raise NotImplementedError


class SoundListener:
    """Audio loopback + cross-correlation 觸發器。

    啟動後從系統 default speaker 的 loopback 讀 audio,每 detection_interval
    取 sample_len 長度的窗口,經 highpass + scale 標準化後與已載入的樣本做
    correlate(),max corr 超過 threshold 時呼叫對應 callback。

    Ported from ok-nte-main/src/sound_trigger/SoundListener.py
    """

    used_sr = 32000
    used_channel = 2
    chunk_size = 1600
    sample_len = 0.2
    detection_interval = 0.1
    log_interval = 50

    degree = 4
    cut_off = 1000

    def __init__(
        self,
        sample_path: str,
        counter_attack_sample_path: Optional[str] = None,
        threshold: float = 0.13,
        counter_attack_threshold: float = 0.12,
        expansion_ratio: float = 1.0,
        is_allow_successive_trigger: bool = False,
        log_callback: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.sample_path = sample_path
        self.counter_attack_sample_path = counter_attack_sample_path
        self.threshold = float(threshold)
        self.counter_attack_threshold = float(counter_attack_threshold)
        self.expansion_ratio = float(expansion_ratio)
        self.is_allow_successive_trigger = bool(is_allow_successive_trigger)
        self._log = log_callback or (lambda msg: None)

        self._running = False
        self._listener_thread: Optional[threading.Thread] = None
        self._last_trigger_time = 0.0
        self._trigger_interval = 0.5

        self._sample_waveform = None
        self._counter_sample_waveform = None
        self._b = None
        self._a = None
        self._loaded = False
        self._load_error: Optional[str] = None

        self.on_dodge_triggered: Optional[Callable[[], None]] = None
        self.on_counter_triggered: Optional[Callable[[], None]] = None
        self.on_score_update: Optional[Callable[[float, float], None]] = None

    def is_loaded(self) -> bool:
        return self._loaded

    def load_error(self) -> Optional[str]:
        return self._load_error

    def load_samples(self) -> bool:
        if not _SOUND_LIBS_OK:
            self._load_error = f"音訊依賴未就緒: {_SOUND_LIBS_ERR}"
            return False
        if not os.path.exists(self.sample_path):
            self._load_error = f"找不到樣本檔: {self.sample_path}"
            return False
        try:
            self._b, self._a = _scipy_butter(
                self.degree,
                self.cut_off,
                btype="highpass",
                output="ba",
                fs=self.used_sr,
            )
            self._sample_waveform = self._load_and_cache(self.sample_path)
            if (
                self.counter_attack_sample_path
                and os.path.exists(self.counter_attack_sample_path)
            ):
                self._counter_sample_waveform = self._load_and_cache(
                    self.counter_attack_sample_path
                )
            self._loaded = True
            self._log(f"音訊樣本已載入(sr={self.used_sr})")
            return True
        except Exception as exc:  # noqa: BLE001
            self._load_error = f"載入樣本失敗: {exc}"
            return False

    def _load_and_cache(self, path: str):
        cache_path = f"{path}_{self.used_sr}_{self.degree}_{self.cut_off}.npy"
        if (
            os.path.exists(cache_path)
            and os.path.exists(path)
            and os.path.getmtime(cache_path) > os.path.getmtime(path)
        ):
            return _np.load(cache_path)
        waveform, _ = _librosa.load(path, sr=self.used_sr)
        waveform = _scipy_filtfilt(self._b, self._a, waveform)
        try:
            _np.save(cache_path, waveform)
        except Exception:  # noqa: BLE001
            pass
        return waveform

    def _filtering(self, waveform):
        if _scipy_filtfilt is None:
            return waveform
        return _scipy_filtfilt(self._b, self._a, waveform)

    def matching(self, stream_waveform, sample_waveform) -> float:
        if (
            _scipy_correlate is None
            or _sk_scale is None
            or _np is None
        ):
            return 0.0
        stream_waveform = self._filtering(stream_waveform)
        norm_stream = _sk_scale(stream_waveform, with_mean=False)
        norm_sample = _sk_scale(sample_waveform, with_mean=False)
        if norm_stream.shape[0] > norm_sample.shape[0]:
            correlation = (
                _scipy_correlate(norm_stream, norm_sample, mode="same", method="fft")
                / norm_stream.shape[0]
            )
        else:
            correlation = (
                _scipy_correlate(norm_sample, norm_stream, mode="same", method="fft")
                / norm_sample.shape[0]
            )
        return float(_np.max(correlation) * self.expansion_ratio)

    def start(self) -> bool:
        if self._running:
            return True
        if not self._loaded:
            self._log("樣本未載入,SoundListener 不啟動")
            return False
        self._running = True
        self._listener_thread = threading.Thread(target=self._listen_loop, daemon=True)
        self._listener_thread.start()
        return True

    def stop(self) -> None:
        self._running = False
        if self._listener_thread is not None:
            self._listener_thread.join(timeout=2.0)
            self._listener_thread = None

    def _listen_loop(self) -> None:
        if not _SOUND_LIBS_OK:
            self._log("音訊依賴未就緒,監聽停止")
            self._running = False
            return
        try:
            default_speaker = _sc.default_speaker()
            loopback = _sc.get_microphone(
                id=str(default_speaker.name), include_loopback=True
            )
            self._log(f"loopback 裝置: {loopback.name}")
            audio_instance = loopback.recorder(
                samplerate=self.used_sr, channels=self.used_channel
            )
            check_count = 0
            with audio_instance as audio_recorder:
                self._log("開始音訊監聽")
                max_samples = int(self.used_sr * self.sample_len)
                chunks_per_interval = int(
                    self.used_sr * self.detection_interval / self.chunk_size
                )
                if chunks_per_interval < 1:
                    chunks_per_interval = 1
                new_samples_per_interval = chunks_per_interval * self.chunk_size

                ring_buffer = _np.zeros(max_samples * 2, dtype=_np.float64)
                buffer_pos = 0
                total_written = 0

                while self._running:
                    current_frame = _np.empty(
                        new_samples_per_interval, dtype=_np.float64
                    )
                    idx = 0
                    for _ in range(chunks_per_interval):
                        stream_data = audio_recorder.record(numframes=self.chunk_size)
                        read_chunks = _librosa.to_mono(stream_data.T)
                        current_frame[idx : idx + self.chunk_size] = read_chunks
                        idx += self.chunk_size

                    end_pos = buffer_pos + new_samples_per_interval
                    if end_pos <= max_samples * 2:
                        ring_buffer[buffer_pos:end_pos] = current_frame
                    else:
                        first_part = max_samples * 2 - buffer_pos
                        ring_buffer[buffer_pos:] = current_frame[:first_part]
                        ring_buffer[: end_pos - max_samples * 2] = current_frame[
                            first_part:
                        ]
                    buffer_pos = end_pos % (max_samples * 2)
                    total_written += new_samples_per_interval

                    if total_written < max_samples:
                        continue

                    if buffer_pos >= max_samples:
                        window = ring_buffer[buffer_pos - max_samples : buffer_pos]
                    else:
                        window = _np.concatenate(
                            [
                                ring_buffer[-(max_samples - buffer_pos) :],
                                ring_buffer[:buffer_pos],
                            ]
                        )

                    dodge_score = self.matching(window, self._sample_waveform)
                    counter_score = 0.0
                    if self._counter_sample_waveform is not None:
                        counter_score = self.matching(
                            window, self._counter_sample_waveform
                        )

                    if self.on_score_update is not None:
                        try:
                            self.on_score_update(dodge_score, counter_score)
                        except Exception:  # noqa: BLE001
                            pass

                    self._check_triggers(dodge_score, counter_score)

                    check_count += 1
                    if check_count % self.log_interval == 0:
                        self._log(
                            f"監聽中 dodge={dodge_score:.4f} (T={self.threshold}) "
                            f"counter={counter_score:.4f} (T={self.counter_attack_threshold})"
                        )
        except Exception as exc:  # noqa: BLE001
            self._log(f"音訊監聽錯誤: {exc}")
        finally:
            self._running = False
            self._log("音訊監聽已停止")

    def _check_triggers(self, dodge_score: float, counter_score: float) -> None:
        now = time.time()
        if (
            not self.is_allow_successive_trigger
            and now - self._last_trigger_time < self._trigger_interval
        ):
            return
        if dodge_score > 0 and dodge_score > self.threshold:
            if self.on_dodge_triggered is not None:
                self._log(f"閃避觸發 dodge_score={dodge_score:.4f}")
                self.on_dodge_triggered()
                self._last_trigger_time = now
                return
        if counter_score > 0 and counter_score > self.counter_attack_threshold:
            if self.on_counter_triggered is not None:
                self._log(f"反擊觸發 counter_score={counter_score:.4f}")
                self.on_counter_triggered()
                self._last_trigger_time = now


class DodgeCounterTrigger:
    """節流 + 動作分派,避免單次音訊觸發引發連發。

    Ported from ok-nte-main/src/sound_trigger/DodgeCounterTrigger.py
    """

    def __init__(
        self,
        execute_action: Callable[[], None],
        counter_execute_action: Optional[Callable[[], None]] = None,
        min_dodge_interval: float = 0.5,
        min_counter_interval: float = 1.0,
        log_callback: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.execute_action = execute_action
        self.counter_execute_action = counter_execute_action
        self._is_executing = False
        self._execute_lock = threading.Lock()
        self._last_dodge_time = 0.0
        self._last_counter_time = 0.0
        self._min_dodge_interval = float(min_dodge_interval)
        self._min_counter_interval = float(min_counter_interval)
        self._log = log_callback or (lambda msg: None)

    def execute_dodge(self) -> None:
        now = time.time()
        if now - self._last_dodge_time < self._min_dodge_interval:
            return
        with self._execute_lock:
            if self._is_executing:
                return
            self._is_executing = True
        try:
            self._log("執行閃避")
            self.execute_action()
            self._last_dodge_time = now
        except Exception as exc:  # noqa: BLE001
            self._log(f"閃避執行錯誤: {exc}")
        finally:
            self._is_executing = False

    def execute_counter_attack(self) -> None:
        if self.counter_execute_action is None:
            return
        now = time.time()
        if now - self._last_counter_time < self._min_counter_interval:
            return
        with self._execute_lock:
            if self._is_executing:
                return
            self._is_executing = True
        try:
            self._log("執行反擊")
            self.counter_execute_action()
            self._last_counter_time = now
        except Exception as exc:  # noqa: BLE001
            self._log(f"反擊執行錯誤: {exc}")
        finally:
            self._is_executing = False


class SoundCombatTask(AutomationTask):
    """把 SoundListener + DodgeCounterTrigger 包成 task。

    啟動時從 SoundListener 載樣本 → 啟動監聽 thread → 阻塞等 stop_event。
    觸發時透過 KeyBackend 送閃避鍵(預設 shift)或滑鼠左鍵(反擊)。
    """

    def __init__(
        self,
        proxy: AutomationProxy,
        sample_path: str,
        counter_sample_path: Optional[str] = None,
        threshold: float = 0.13,
        counter_threshold: float = 0.12,
        dodge_key: str = "shift",
        counter_use_mouse: bool = True,
        label: str = "自動閃避",
    ) -> None:
        super().__init__(proxy, label)
        self._sample_path = sample_path
        self._counter_sample_path = counter_sample_path
        self._threshold = float(threshold)
        self._counter_threshold = float(counter_threshold)
        self._dodge_key = dodge_key
        self._counter_use_mouse = counter_use_mouse
        self._listener: Optional[SoundListener] = None

    def run(self) -> None:
        backend: Optional[KeyBackend] = None
        try:
            if not _SOUND_LIBS_OK:
                self._proxy.emit_failed(f"自動閃避需要音訊依賴: {_SOUND_LIBS_ERR}")
                return
            if not os.path.exists(self._sample_path):
                self._proxy.emit_failed(f"找不到閃避樣本: {self._sample_path}")
                return
            backend, _ = create_backend_with_fallback()

            def log(msg: str) -> None:
                # 從 listener thread / trigger thread 進來;proxy 在 main thread,
                # Qt 會自動以 QueuedConnection 跨緒投遞。
                self._proxy.emit_status(msg)

            listener = SoundListener(
                self._sample_path,
                counter_attack_sample_path=(
                    self._counter_sample_path
                    if self._counter_sample_path
                    and os.path.exists(self._counter_sample_path)
                    else None
                ),
                threshold=self._threshold,
                counter_attack_threshold=self._counter_threshold,
                log_callback=log,
            )
            if not listener.load_samples():
                self._proxy.emit_failed(listener.load_error() or "樣本載入失敗")
                return

            game = find_game_window()
            game_hwnd = game.hwnd if game is not None else None

            def dodge_action() -> None:
                # 暫停期間吃掉觸發,不送鍵。
                if self.is_paused():
                    return
                # 單擊閃避 — 原本寫成雙擊「保險」,但 NTE 接收單擊就會觸發,
                # 雙擊反而讓角色連閃兩次,造成「每次都閃兩次」的回報問題。
                try:
                    backend.key_down(self._dodge_key)
                    time.sleep(0.03)
                    backend.key_up(self._dodge_key)
                except Exception:  # noqa: BLE001
                    pass

            def counter_action() -> None:
                if self.is_paused():
                    return
                if not self._counter_use_mouse or game_hwnd is None:
                    return
                rect = _get_client_rect(game_hwnd)
                if rect is None:
                    return
                _send_mouse_click(
                    rect.x + rect.width // 2, rect.y + rect.height // 2
                )

            trigger = DodgeCounterTrigger(
                execute_action=dodge_action,
                counter_execute_action=(
                    counter_action if self._counter_use_mouse else None
                ),
                log_callback=log,
            )
            listener.on_dodge_triggered = trigger.execute_dodge
            listener.on_counter_triggered = trigger.execute_counter_attack
            listener.on_score_update = lambda d, c: self._proxy.emit_score(
                float(d), float(c)
            )
            self._listener = listener

            if not listener.start():
                self._proxy.emit_failed("SoundListener 啟動失敗")
                return

            self._proxy.emit_started(self._label)
            self._proxy.emit_status(f"自動閃避監聽中(閃避鍵={self._dodge_key})")

            while not self._stop_event.is_set():
                # listener callback 已 guard is_paused();主 loop 沿用 pause 在這
                # 等待,避免暫停期間 wait(0.2) 還在轉但實際毫無意義。
                if self._check_pause():
                    break
                self._stop_event.wait(0.2)
        except Exception as exc:  # noqa: BLE001
            self._proxy.emit_failed(f"SoundCombatTask 錯誤: {exc}")
        finally:
            if self._listener is not None:
                self._listener.stop()
            if backend is not None:
                # 釋放可能被卡住的閃避鍵
                try:
                    backend.key_up(self._dodge_key)
                except Exception:  # noqa: BLE001
                    pass
            self._proxy.emit_finished(self._stop_event.is_set())


# ============================================================================
# Phase F — 自動音遊
# ============================================================================
# Ported from ok-nte-main/src/tasks/RhythmTask.py
# ============================================================================


class RhythmTask(AutomationTask):
    """異環鼓組音遊:一次截 client area + slice 4 點亮度 + 非同步按鍵 + 結算重試。

    Ported from ok-nte-main/src/tasks/RhythmTask.py

    迴圈:點開始演奏 → 偵測按鍵(D/F/J/K)→ 結算 → 關閉結算 → 回選歌界面 → 下一輪。
    使用者需先把遊戲調到「選歌界面」,task 才能正確點到開始演奏鈕。

    與 ok-nte 對齊的關鍵:每幀只截一次 client area(grab_full),四個偵測點都在
    這張 frame 上做 numpy slice — 比起連 4 次 mss.grab(1×1) 快數倍,timing 抖動
    顯著降低,音遊命中率對齊原版 perfect-full-combo 表現。
    """

    DETECT_POINTS = {
        "d": (0.2301, 0.7715),
        "f": (0.4055, 0.7715),
        "j": (0.5941, 0.7715),
        "k": (0.7699, 0.7715),
    }
    BRIGHTNESS_THRESHOLD = 100
    DETECT_RADIUS_X = 5
    DETECT_RADIUS_Y = 10
    DARK_RATIO_THRESHOLD = 0.06
    RETRIGGER_INTERVAL = 0.085
    KEY_DOWN_TIME = 0.005

    SONG_START_POS = (0.8313, 0.9313)
    FINISH_CLOSE_POS = (0.5402, 0.0437)
    FINISH_CHECK_INTERVAL = 2.0

    FINISH_YELLOW_BOX = (0.2211, 0.6625, 0.3156, 0.6965)
    FINISH_RED_BOX = (0.4555, 0.6625, 0.5445, 0.6965)
    # 原 ok-nte 取按鈕上方一段 (0.7441, 0.8306, 0.9336, 0.8632),
    # 但實機上常被選中歌曲的大卡片右下角延伸 / 卡片陰影遮到,導致粉色比例
    # 跌出 0.9 閾值。改用更穩的兩塊區域 OR 判斷:
    #   1. 底部漸層粉色帶 — 跨選歌畫面任何狀態都存在,x 取中段避開左下
    #      UID 文字、右下「開始演奏」按鈕本體。
    #   2. 「開始演奏」按鈕周圍光暈 — 卡片再大也不會蓋到按鈕,作為備援。
    SONG_SELECT_PINK_BOX_BOTTOM = (0.30, 0.955, 0.65, 0.985)
    SONG_SELECT_PINK_BOX_BUTTON = (0.7400, 0.9100, 0.9300, 0.9700)
    # 保留原 box 作為相容欄位,外部若有腳本引用不會 AttributeError。
    SONG_SELECT_PINK_BOX = SONG_SELECT_PINK_BOX_BOTTOM

    FINISH_YELLOW = {"r": (220, 230), "g": (170, 180), "b": (85, 90)}
    FINISH_RED = {"r": (220, 230), "g": (90, 100), "b": (85, 90)}
    # 原 ok-nte 範圍 R=180-220, G=35-50, B=100-120 太窄,實機渲染的 hot pink
    # 漸層在 anti-alias / 不同顯示器色彩設定下會跑出範圍。放寬到涵蓋整個
    # NTE 招牌粉的漸層(從深粉 #B82A6E 到鮮粉 #FF73B8 都納入)。
    SONG_SELECT_PINK = {"r": (170, 255), "g": (30, 140), "b": (95, 200)}

    def __init__(
        self,
        proxy: AutomationProxy,
        loop_count: int = 0,
        timeout_seconds: float = 180.0,
        key_map: Optional[dict[str, str]] = None,
        delay_ms: float = 0.0,
        label: str = "自動音游",
    ) -> None:
        super().__init__(proxy, label)
        self._loop_count = max(0, int(loop_count))
        self._timeout_seconds = max(10.0, float(timeout_seconds))
        if key_map is None:
            key_map = {"d": "d", "f": "f", "j": "j", "k": "k"}
        self._key_map = dict(key_map)
        # delay_ms 正值:偵測到 note 後延遲送 key(整體往後挪),補償系統 input lag 過低時
        #          負值:把亮度閾值加上 |delay_ms| 提早觸發(note 還沒完全變黑就按)
        # 範圍 clamp 在 [-100, 200],過大會偏離正常節奏。
        self._delay_ms = max(-100.0, min(200.0, float(delay_ms)))
        self._press_delay_sec = max(0.0, self._delay_ms) / 1000.0
        self._effective_brightness_threshold = float(
            self.BRIGHTNESS_THRESHOLD + max(0.0, -self._delay_ms)
        )

        self._prev_state: dict[str, bool] = {k: False for k in self.DETECT_POINTS}
        self._last_press_time: dict[str, float] = {k: 0.0 for k in self.DETECT_POINTS}
        self._last_finish_check = 0.0

        self._key_queue: deque = deque()
        self._key_queue_cv = threading.Condition()
        self._key_worker: Optional[threading.Thread] = None
        self._key_worker_stop = False
        self._key_backend: Optional[KeyBackend] = None

        # client area 像素座標快取(根據 frame 尺寸算一次,後續 slice 用)
        self._px_cache: Optional[dict[str, tuple[int, int]]] = None
        self._cache_shape: Optional[tuple[int, int]] = None

    def run(self) -> None:
        capture: Optional[WindowedScreenCapture] = None
        try:
            missing = []
            if _np is None:
                missing.append("numpy")
            if _mss is None:
                missing.append("mss")
            if missing:
                self._proxy.emit_failed(f"自動音遊需要 {', '.join(missing)}")
                return
            backend, _ = create_backend_with_fallback()
            self._key_backend = backend
            game = find_game_window()
            if game is None:
                self._proxy.emit_failed("找不到遊戲視窗(NTE / HTGame.exe),請先開啟遊戲")
                return
            capture = WindowedScreenCapture(game.hwnd)
            self._proxy.emit_started(self._label)
            self._proxy.emit_status(
                f"自動音游啟動(目標循環={'∞' if self._loop_count == 0 else self._loop_count})"
            )

            endless = self._loop_count == 0
            total = self._loop_count
            count = 0

            while endless or count < total:
                if self._stop_event.is_set():
                    break
                count += 1
                label = f"第 {count} 次" + ("" if endless else f"/{total}")
                self._proxy.emit_status(f"{label}: 點選開始演奏鈕")

                pos = capture.client_to_screen(*self.SONG_START_POS)
                if pos is None:
                    self._proxy.emit_failed("無法取得遊戲視窗座標")
                    return
                _send_mouse_click(pos[0], pos[1])

                # 等待離開選歌(最多 15s)
                self._proxy.emit_status("等待進入音游介面")
                entered = False
                deadline_load = time.time() + 15
                while time.time() < deadline_load and not self._stop_event.is_set():
                    if self._stop_event.wait(0.3):
                        break
                    if not self._is_song_select(capture):
                        entered = True
                        break
                if self._stop_event.is_set():
                    break
                if not entered:
                    self._proxy.emit_failed("15 秒內未進入音游介面")
                    return

                if self._stop_event.wait(1.0):
                    break

                # 重置每曲狀態
                self._prev_state = {k: False for k in self.DETECT_POINTS}
                self._last_press_time = {k: 0.0 for k in self.DETECT_POINTS}
                self._last_finish_check = 0.0
                self._start_key_worker()
                try:
                    self._run_single(capture)
                finally:
                    self._stop_key_worker()

                if self._stop_event.is_set():
                    break
                self._handle_finish(capture)

                if self._stop_event.is_set():
                    break
                if endless or count < total:
                    self._proxy.emit_status("等待回到選歌介面")
                    if self._stop_event.wait(1.0):
                        break
                    back_to_select = False
                    deadline = time.time() + 10
                    while time.time() < deadline and not self._stop_event.is_set():
                        if self._is_song_select(capture):
                            back_to_select = True
                            break
                        if self._stop_event.wait(0.5):
                            break
                    if self._stop_event.is_set():
                        break
                    if not back_to_select:
                        self._proxy.emit_failed("10 秒內未回到選歌介面")
                        return

            if not self._stop_event.is_set():
                self._proxy.emit_status(f"自動音游結束,共完成 {count} 次")
        except Exception as exc:  # noqa: BLE001
            self._proxy.emit_failed(f"RhythmTask 錯誤: {exc}")
        finally:
            self._stop_key_worker()
            if capture is not None:
                capture.close()
            self._proxy.emit_finished(self._stop_event.is_set())

    def _run_single(self, capture: WindowedScreenCapture) -> None:
        deadline = time.time() + self._timeout_seconds
        self._proxy.emit_status("音游打擊中(D/F/J/K)")
        while time.time() < deadline and not self._stop_event.is_set():
            # 暫停中 block 在這,期間 tick/送鍵全停。
            if self._pause_event.is_set():
                if self._check_pause():
                    return
                # 暫停期間時鐘繼續走,但 deadline 不延長 — 暫停太久就讓單曲超時退場。
            now = time.time()
            if now - self._last_finish_check >= self.FINISH_CHECK_INTERVAL:
                self._last_finish_check = now
                if self._is_finished(capture):
                    self._proxy.emit_status("檢測到結算介面")
                    return
            self._tick(capture)
            # 加 3ms sleep 限速 ≈ 333Hz polling,遠超人類反應時間,不會錯過鼓點。
            # 沒有這個 sleep,grab_full() + 處理迴圈會把單核打滿 → 使用者滑鼠
            # 卡頓 + 遊戲視窗截圖頻繁觸發 DWM 重繪 → 看起來會閃。
            if self._stop_event.wait(0.003):
                return
        if not self._stop_event.is_set():
            self._proxy.emit_status(f"單曲超時 {self._timeout_seconds:.0f}s")

    def _tick(self, capture: WindowedScreenCapture) -> None:
        # 一次截整個 client area,再對 4 點 slice — 對齊 ok-nte 的 self.frame
        # 模式,避免 4 次 mss.grab 各自呼叫 BitBlt 帶來的 timing 抖動。
        frame = capture.grab_full()
        if frame is None:
            return
        state = self._detect_notes(frame)
        now = time.time()
        for track, has_note in state.items():
            prev = self._prev_state[track]
            can_retrigger = (
                has_note
                and prev
                and now - self._last_press_time[track] >= self.RETRIGGER_INTERVAL
            )
            if has_note and (not prev or can_retrigger):
                actual_key = self._key_map.get(track, track)
                self._queue_press(actual_key)
                self._last_press_time[track] = now
            self._prev_state[track] = has_note

    def _detect_notes(self, frame) -> dict[str, bool]:
        """對 frame 做 4 點 slice + 亮度偵測。Frame 尺寸只在第一次或 resize 時重算座標。"""
        if frame is None or _np is None:
            return {k: False for k in self.DETECT_POINTS}
        fh, fw = frame.shape[:2]
        if self._cache_shape != (fh, fw):
            self._px_cache = {
                key: (int(xp * fw), int(yp * fh))
                for key, (xp, yp) in self.DETECT_POINTS.items()
            }
            self._cache_shape = (fh, fw)
        rx, ry = self.DETECT_RADIUS_X, self.DETECT_RADIUS_Y
        threshold = self._effective_brightness_threshold
        result: dict[str, bool] = {}
        for key, (px, py) in self._px_cache.items():
            x1 = max(0, px - rx)
            x2 = min(fw, px + rx + 1)
            y1 = max(0, py - ry)
            y2 = min(fh, py + ry + 1)
            roi = frame[y1:y2, x1:x2]
            pixel_brightness = roi.mean(axis=2) if roi.ndim == 3 else roi
            dark_ratio = float((pixel_brightness < threshold).mean())
            result[key] = dark_ratio >= self.DARK_RATIO_THRESHOLD
        return result

    def _start_key_worker(self) -> None:
        if self._key_worker is not None and self._key_worker.is_alive():
            return
        with self._key_queue_cv:
            self._key_queue.clear()
            self._key_worker_stop = False
        self._key_worker = threading.Thread(target=self._key_worker_loop, daemon=True)
        self._key_worker.start()

    def _stop_key_worker(self) -> None:
        with self._key_queue_cv:
            self._key_worker_stop = True
            self._key_queue.clear()
            self._key_queue_cv.notify_all()
        worker = self._key_worker
        if worker is not None:
            worker.join(timeout=1.0)
            self._key_worker = None

    def _queue_press(self, key: str) -> None:
        with self._key_queue_cv:
            self._key_queue.append(key)
            self._key_queue_cv.notify()

    def _key_worker_loop(self) -> None:
        backend = self._key_backend
        if backend is None:
            return
        press_delay = self._press_delay_sec
        while True:
            with self._key_queue_cv:
                while not self._key_queue and not self._key_worker_stop:
                    self._key_queue_cv.wait(timeout=0.05)
                if self._key_worker_stop and not self._key_queue:
                    return
                key = self._key_queue.popleft()
            # 正 delay:偵測到 note 後延後送 key,讓打擊整體往後挪一點
            if press_delay > 0:
                time.sleep(press_delay)
                if self._key_worker_stop:
                    return
            try:
                backend.key_down(key)
                time.sleep(self.KEY_DOWN_TIME)
                backend.key_up(key)
            except Exception:  # noqa: BLE001
                pass

    def _is_song_select(self, capture: WindowedScreenCapture) -> bool:
        """選歌畫面判斷:底部漸層帶粉色比例 OR 開始演奏按鈕周邊粉色光暈。

        - 底部漸層帶:選歌頁無論卡片大小/選哪首都會有,最穩定;閾值取 0.7
          因為帶內可能疊有「NEVERNESS TO EVERNESS」等較暗噪字。
        - 按鈕周邊:卡片再大也不會蓋掉右下按鈕;按鈕本體是黑底白字,只有
          外圍 ~30% 像素是粉光暈,所以閾值較低(0.18)。
        - 任一通過視為在選歌畫面。雙重判斷讓不同顯示器/解析度都比較不會炸。
        """
        bottom_img = self._grab_box(capture, self.SONG_SELECT_PINK_BOX_BOTTOM)
        if _color_percentage(bottom_img, self.SONG_SELECT_PINK) > 0.7:
            return True
        button_img = self._grab_box(capture, self.SONG_SELECT_PINK_BOX_BUTTON)
        return _color_percentage(button_img, self.SONG_SELECT_PINK) > 0.18

    def _is_finished(self, capture: WindowedScreenCapture) -> bool:
        yellow_pct = _color_percentage(
            self._grab_box(capture, self.FINISH_YELLOW_BOX), self.FINISH_YELLOW
        )
        red_pct = _color_percentage(
            self._grab_box(capture, self.FINISH_RED_BOX), self.FINISH_RED
        )
        return red_pct > 0.5 or yellow_pct > 0.5

    @staticmethod
    def _grab_box(
        capture: WindowedScreenCapture,
        box: tuple[float, float, float, float],
    ):
        x0, y0, x1, y1 = box
        return capture.grab_box(x0, y0, x1 - x0, y1 - y0)

    def _handle_finish(self, capture: WindowedScreenCapture) -> None:
        if self._stop_event.is_set():
            return
        self._proxy.emit_status("關閉結算介面")
        if self._stop_event.wait(1.5):
            return
        pos = capture.client_to_screen(*self.FINISH_CLOSE_POS)
        if pos is not None:
            _send_mouse_click(pos[0], pos[1])
        self._stop_event.wait(1.0)


# ============================================================================
# Phase G — 失焦自動靜音(BackgroundAudioMuter)
# ============================================================================
# NTE 沒有內建「失焦自動靜音」選項,背景仍會持續發出聲音。本類用 pycaw 拿到
# HTGame.exe 的 SimpleAudioVolume,在背景 thread 中依視窗焦點切 mute
# (失焦→True / 回前景→False)。pycaw 缺套件時 is_available() 為 False,
# GUI 端會 disable 對應選項。
# ============================================================================


class BackgroundAudioMuter:
    """遊戲失焦時自動把 HTGame.exe 靜音,回到前景時還原。

    POLL_INTERVAL 設 0.3s — 使用者切視窗到聽見聲音之間最長延遲 ~0.3s。
    `_currently_muted` 記錄上次主動寫入的 mute 狀態,避免每 tick 都 SetMute
    (pycaw 的 COM call 不便宜)。stop() 結束前一定 SetMute(False),不能讓
    遊戲留在被我們靜音的狀態。

    比對策略:取得遊戲 hwnd 對應的 PID,跟 audio session.Process.pid 比對。
    比 process.name() 比對穩 — 因為某些 session 的 Process 在跨權限/已釋放
    時 name() 會丟 AccessDenied,但 ProcessId 仍可讀;且不受 process 改名干擾。
    """

    POLL_INTERVAL = 0.3

    def __init__(
        self,
        process_names: tuple = ("HTGame.exe",),
        log_callback: Optional[Callable[[str], None]] = None,
    ) -> None:
        # process_names 保留當 fallback:當 hwnd 拿不到 PID 時(遊戲未開或剛關)
        # 仍可走名字比對。主用 PID。
        self._process_names = tuple(n.lower() for n in process_names)
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._log = log_callback or (lambda msg: None)
        self._currently_muted = False
        # pycaw / comtypes lazy import 後存在 instance 上(從 _run() thread 內 import,
        # 避免 main thread 在 module load 時 import comtypes 觸發 COM init 衝突)。
        self._comtypes = None
        self._audio_utils = None
        self._simple_audio_volume = None

    @staticmethod
    def is_available() -> bool:
        return _PYCAW_OK

    @staticmethod
    def availability_error() -> str:
        return _PYCAW_ERR

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> bool:
        if not _PYCAW_OK:
            self._log(f"失焦自動靜音需要 pycaw: {_PYCAW_ERR}")
            return False
        if self.is_running():
            return True
        self._stop_event.clear()
        self._currently_muted = False
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="BgAudioMuter"
        )
        self._thread.start()
        self._log("失焦自動靜音已啟用")
        return True

    def stop(self) -> None:
        was_running = self.is_running()
        # 只有曾經主動 mute 過(_currently_muted=True)才需要兜底 unmute —
        # 沒啟動過 / 啟動了沒 mute 過 → 完全跳過 _force_unmute_sync,避免
        # 每次關 GUI 都要花 0.5-2s 跑 pycaw GetAllSessions(該函式同步呼叫
        # 本身在某些 Windows 環境慢,是 closeEvent 最大慢點)。
        needs_force_unmute = bool(self._currently_muted)
        if was_running:
            self._stop_event.set()
            thread = self._thread
            self._thread = None
            if thread is not None:
                thread.join(timeout=1.5)
        # 跨 thread COM 偶爾會在 _run() finally 階段失敗(GetAllSessions 拋例外
        # 後 thread 結束但 session mute 沒被清掉),這時用獨立 daemon thread
        # 重新 import + CoInit/Uninit 強制 unmute 一次當兜底。沒 mute 過就不用跑。
        if needs_force_unmute:
            self._force_unmute_sync()
        self._currently_muted = False
        if was_running:
            self._log("失焦自動靜音已停用")

    def _force_unmute_sync(self) -> None:
        """同步 fallback:獨立 daemon thread 跑一次 SetMute(False),最長等 2 秒。

        用獨立 thread 是因為 pycaw 的 import 跟 SetMute 都需要 COM,而 main thread
        的 COM mode 已被 Qt 設定;獨立 thread 從零開始 CoInitialize,不會衝突。
        """
        if not _PYCAW_OK:
            return

        process_names = self._process_names
        get_target_pid = self._get_target_pid

        def _worker() -> None:
            try:
                import comtypes as _ct  # noqa: PLC0415
                from pycaw.pycaw import (  # noqa: PLC0415
                    AudioUtilities as _AU,
                    ISimpleAudioVolume as _ISAV,
                )
            except Exception:  # noqa: BLE001
                return
            com_ok = False
            try:
                _ct.CoInitialize()
                com_ok = True
            except Exception:  # noqa: BLE001
                return
            try:
                game = find_game_window()
                target_pid = get_target_pid(game.hwnd) if game is not None else None
                try:
                    sessions = _AU.GetAllSessions()
                except Exception:  # noqa: BLE001
                    return
                for s in sessions:
                    try:
                        proc = s.Process
                        match = False
                        if target_pid is not None and proc is not None:
                            try:
                                if int(proc.pid) == int(target_pid):
                                    match = True
                            except Exception:  # noqa: BLE001
                                pass
                        if not match and proc is not None:
                            try:
                                name = (proc.name() or "").lower()
                                if name in process_names:
                                    match = True
                            except Exception:  # noqa: BLE001
                                pass
                        if not match:
                            continue
                        vol = s._ctl.QueryInterface(_ISAV)
                        vol.SetMute(False, None)
                    except Exception:  # noqa: BLE001
                        continue
            finally:
                if com_ok:
                    try:
                        _ct.CoUninitialize()
                    except Exception:  # noqa: BLE001
                        pass

        t = threading.Thread(
            target=_worker, daemon=True, name="BgAudioMuter-unmute"
        )
        t.start()
        t.join(timeout=2.0)

    def _get_target_pid(self, hwnd: Optional[int]) -> Optional[int]:
        """從 hwnd 取 PID。失敗回 None。"""
        if not hwnd or sys.platform != "win32":
            return None
        try:
            user32 = ctypes.windll.user32
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(int(hwnd), ctypes.byref(pid))
            return int(pid.value) if pid.value else None
        except Exception:  # noqa: BLE001
            return None

    def _run(self) -> None:
        # 在獨立 daemon thread 內 lazy import pycaw / comtypes —
        # 此 thread 由 threading.Thread 新建,COM 尚未初始化,可任選 mode。
        # main thread 已被 Qt 設過 COM mode,不能由此處重設。
        try:
            import comtypes as _ct  # noqa: PLC0415
            from pycaw.pycaw import (  # noqa: PLC0415
                AudioUtilities as _AU,
                ISimpleAudioVolume as _ISAV,
            )
        except Exception as exc:  # noqa: BLE001
            self._log(f"BgAudioMuter import pycaw 失敗: {exc}")
            return
        self._comtypes = _ct
        self._audio_utils = _AU
        self._simple_audio_volume = _ISAV

        com_ok = False
        try:
            _ct.CoInitialize()
            com_ok = True
        except Exception as exc:  # noqa: BLE001
            self._log(f"BgAudioMuter CoInitialize 失敗: {exc}")
            return
        target_hwnd: Optional[int] = None
        target_pid: Optional[int] = None
        first_log = True
        try:
            while not self._stop_event.wait(self.POLL_INTERVAL):
                if target_hwnd is None or not is_window_alive(target_hwnd):
                    game = find_game_window()
                    target_hwnd = game.hwnd if game is not None else None
                    target_pid = self._get_target_pid(target_hwnd)
                    if target_hwnd is None:
                        if self._currently_muted:
                            if self._apply_mute_state(False, target_pid=None):
                                self._currently_muted = False
                        continue
                    if first_log:
                        self._log(f"BgAudioMuter 鎖定遊戲 PID={target_pid}")
                        first_log = False
                in_foreground = foreground_hwnd() == target_hwnd
                desired_mute = not in_foreground
                if desired_mute == self._currently_muted:
                    continue
                if self._apply_mute_state(desired_mute, target_pid=target_pid):
                    self._currently_muted = desired_mute
                    self._log(
                        f"BgAudioMuter SetMute({desired_mute}) on PID={target_pid}"
                    )
                else:
                    if desired_mute:
                        self._log(
                            f"BgAudioMuter 找不到 PID={target_pid} 的 audio session,"
                            f"請確認遊戲已發出至少一次聲音"
                        )
        finally:
            # thread 結束前一定 SetMute(False),不能讓遊戲留在被我們靜音的狀態
            try:
                if self._currently_muted:
                    self._apply_mute_state(False, target_pid=target_pid)
                else:
                    # 即使我們沒主動 mute 過,也保險清一次(走 name fallback)
                    self._apply_mute_state(False, target_pid=None)
            except Exception:  # noqa: BLE001
                pass
            if com_ok:
                try:
                    _ct.CoUninitialize()
                except Exception:  # noqa: BLE001
                    pass

    def _apply_mute_state(
        self,
        mute: bool,
        target_pid: Optional[int],
    ) -> bool:
        """掃所有 audio session,符合 target_pid (或 fallback process name) 的 session
        設定 SetMute(mute)。回 True 表至少有一個 session 被處理。

        必須從 _run() thread 內呼叫(self._audio_utils / self._simple_audio_volume
        是 lazy import 後填的 — 從 main thread 呼叫會是 None,代表 import 還沒做)。
        """
        if self._audio_utils is None or self._simple_audio_volume is None:
            return False
        try:
            sessions = self._audio_utils.GetAllSessions()
        except Exception as exc:  # noqa: BLE001
            self._log(f"BgAudioMuter GetAllSessions 失敗: {exc}")
            return False
        applied = False
        for session in sessions:
            try:
                proc = session.Process
                match = False
                if target_pid is not None and proc is not None:
                    try:
                        if int(proc.pid) == int(target_pid):
                            match = True
                    except Exception:  # noqa: BLE001
                        pass
                if not match and proc is not None:
                    try:
                        name = (proc.name() or "").lower()
                        if name in self._process_names:
                            match = True
                    except Exception:  # noqa: BLE001
                        pass
                if not match:
                    continue
                volume = session._ctl.QueryInterface(self._simple_audio_volume)
                volume.SetMute(bool(mute), None)
                applied = True
            except Exception:  # noqa: BLE001
                continue
        return applied


# ============================================================================
# 粉爪大劫案便利功能 — port 自 ok-nte-main/src/tasks/trigger/HeistTask.py
# ============================================================================
# 設計差異:
# 1. 不繼承 AutomationTask,跑成獨立 daemon thread,跟 dodge/rhythm 不互斥
#    (粉爪是常駐 helper,跟其他 task 同時用沒衝突)。
# 2. 不註冊全域 hotkey — 直接用 GetAsyncKeyState polling,避免搶走 F 鍵在其他
#    場合的輸入(瀏覽器、文字編輯器都會用到 F)。
# 3. 只在 NTE 視窗為前景時送 key/scroll,避免在 piano editor 打字時誤觸發。
# 4. 原版的「快速奔跑切角色」需要 scene 偵測(is_char_at_index),這裡用不到也跑
#    不起來,故略過 — 只實作 F 連點 + 滾輪交替這兩個核心便利。
# ============================================================================


# user32.mouse_event 額外的 wheel flag(自動化模組原本只用 LEFT/RIGHT 點擊)
MOUSEEVENTF_WHEEL = 0x0800
WHEEL_DELTA = 120

# Win32 keyboard message constants — 用於 PostMessage 直送 hwnd。
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101


# 觸發鍵名稱 → Windows VK code,對齊 HeistTask.KEY_MAP。
_HEIST_VK_MAP = {
    "space": 0x20,
    "shift": 0x10,
    "ctrl": 0x11,
    "control": 0x11,
    "alt": 0x12,
    "esc": 0x1B,
    "escape": 0x1B,
    "tab": 0x09,
    "enter": 0x0D,
    "return": 0x0D,
    "backspace": 0x08,
    # 滑鼠鍵 — GetAsyncKeyState 也能讀(粉爪保留接口,目前未實際使用)。
    "lmb": 0x01,
    "left_mouse": 0x01,
    "rmb": 0x02,
    "right_mouse": 0x02,
    "mmb": 0x04,
    "middle_mouse": 0x04,
}


def _heist_vk_code(key: str) -> Optional[int]:
    """把按鍵名稱(f / shift / f5 ...)轉成 VK code,用於 GetAsyncKeyState。

    回 None 表示找不到對應 VK code。
    """
    if sys.platform != "win32" or not key:
        return None
    name = str(key).strip().lower()
    if not name:
        return None
    if name in _HEIST_VK_MAP:
        return _HEIST_VK_MAP[name]
    if name.startswith("f") and name[1:].isdigit():
        idx = int(name[1:])
        if 1 <= idx <= 12:
            return 0x70 + idx - 1  # VK_F1..VK_F12
    if len(name) == 1:
        # 對 a-z 與 0-9,VK code 直接 = ord(upper)。完全不用 VkKeyScanW,
        # 避免某些環境(輸入法切換 / 跨執行緒 keyboard layout)下回 -1。
        ch = name.upper()
        code = ord(ch)
        if 0x41 <= code <= 0x5A or 0x30 <= code <= 0x39:
            return code
        # 其他單字元符號(/、空白等)再走 VkKeyScanW 試試。
        try:
            user32 = ctypes.windll.user32
            vk = user32.VkKeyScanW(ord(name))
        except Exception:  # noqa: BLE001
            return None
        if vk == -1:
            return None
        return int(vk) & 0xFF
    return None


def _scroll_wheel(delta: int) -> None:
    """送一格滑鼠滾輪事件;delta 正向上、負向下,單位是 WHEEL_DELTA(120)。"""
    if sys.platform != "win32":
        return
    _ensure_winapi_for_automation()
    user32 = ctypes.windll.user32
    user32.mouse_event(MOUSEEVENTF_WHEEL, 0, 0, int(delta), None)


def _post_key_to_window(hwnd: int, vk: int, down_time: float = 0.02) -> bool:
    """PostMessage WM_KEYDOWN+WM_KEYUP 直送 hwnd,**不影響系統 keyboard state**。

    這跟 SendInput 的本質差異:SendInput 把事件注入系統輸入佇列,所有應用程式
    (含 GetAsyncKeyState 的 polling)都看得到狀態變化;PostMessage 把 message
    直接 post 到指定 hwnd 的訊息佇列,系統 keyboard state 完全不動。

    為什麼粉爪要用這個:使用者實體按住 F 時,SendInput key_up 會把系統 F 翻成
    up,GetAsyncKeyState 立刻回 false,程式以為使用者鬆開 → 停止連點 → 拾取
    壞掉。改 PostMessage 後遊戲端照樣收到 WM_KEYDOWN/WM_KEYUP(衍生新的拾取
    事件),系統 F 狀態不變,GetAsyncKeyState 仍然回 true(因為使用者實體還
    按著)→ 連點循環持續。

    ── lparam 細節 ──
    bit 0-15  = repeat count (1)
    bit 16-23 = scan code (透過 MapVirtualKey vk→sc 算出來)
    bit 24    = extended key flag (對非 extended 鍵 = 0)
    bit 29    = context code (alt down) — 一般為 0
    bit 30    = previous key state (down=1 / up=0)
    bit 31    = transition state (release=1 / press=0)

    為什麼一定要 scan code:很多遊戲(尤其是 Unity / Unreal 用 Input Manager
    處理輸入的)會把 wParam(VK)跟 lParam scan code 對照,scan code = 0 直接
    當無效訊息扔掉。字母數字鍵鬆一點可能放過,但 modifier keys
    (shift/ctrl/alt)幾乎都驗證 scan code,缺了 shift 衝刺就完全無效。

    回 True 表示 PostMessage 兩次都成功(不保證遊戲有反應)。失敗回 False,
    呼叫端應該 fallback 到 SendInput backend。
    """
    if not hwnd or vk is None or sys.platform != "win32":
        return False
    user32 = ctypes.windll.user32
    try:
        # MAPVK_VK_TO_VSC = 0;mouse buttons 等沒對應 scan code 會回 0,
        # 對它們本來也不該走 WM_KEYDOWN(應該用 WM_LBUTTONDOWN 等),這裡
        # 不處理 mouse 按鈕(目前所有觸發鍵都是鍵盤鍵)。
        scan = int(user32.MapVirtualKeyW(int(vk), 0)) & 0xFF
        lparam_down = (scan << 16) | 1
        # bit 30 (prev down=1) + bit 31 (release=1) + scan + repeat
        lparam_up = (1 << 31) | (1 << 30) | (scan << 16) | 1
        ok1 = user32.PostMessageW(int(hwnd), WM_KEYDOWN, int(vk), lparam_down)
        if down_time > 0:
            time.sleep(down_time)
        ok2 = user32.PostMessageW(int(hwnd), WM_KEYUP, int(vk), lparam_up)
        return bool(ok1 and ok2)
    except Exception:  # noqa: BLE001
        return False


class HeistController:
    """粉爪大劫案常駐 helper:F 連點 + 滾輪交替拾取。

    啟動(start)後跑在 daemon thread,每 ~10ms 檢查觸發鍵狀態。
    F 連點節奏對齊 ok-nte HeistTask:每 250ms 送一次 F + 滾輪事件(滾輪每 3
    下換方向);實測這個間隔遊戲端最不卡,改更快(0.12s)反而會出現按鍵 debounce
    導致殘按、UI 拖慢的「卡卡」感。

    兩組功能彼此獨立可開關:
        F 連點(按住觸發鍵生效)        — 預設開
        全自動模式(視窗為前景就一直送) — 預設關

    對外:start/stop/is_running、update_config(運行時改設定)、
    status_callback(每次 enable/disable/錯誤都會 callback 一次字串訊息)。
    """

    CHECK_INTERVAL = 0.01
    # 對齊 ok-nte HeistTask.SEND_KEY_INTERVAL = 0.25。實測這個值最順,
    # 0.12s 太快會造成 SendInput 把 keydown 塞進遊戲輸入佇列來不及消化。
    SEND_KEY_INTERVAL = 0.25
    KEY_TAP_HOLD = 0.02  # down→up 之間的微小延遲,對齊 ok-nte send_key 的 down_time。

    def __init__(
        self,
        trigger_key: str = "f",
        use_scroll: bool = True,
        auto_mode: bool = False,
        auto_mode_hotkey: str = "f8",
        pickup_enabled: bool = True,
        status_callback: Optional[Callable[[str], None]] = None,
        auto_mode_changed_callback: Optional[Callable[[bool], None]] = None,
    ) -> None:
        # pickup_enabled 為 True 時 start() 才會送 F;False 時 controller 不啟動。
        self._pickup_enabled = bool(pickup_enabled)
        self._trigger_key = str(trigger_key or "f").strip().lower() or "f"
        self._use_scroll = bool(use_scroll)
        self._auto_mode = bool(auto_mode)
        # 全自動 toggle 熱鍵:遊戲為前景時偵測 rising edge 切換 _auto_mode。
        # 空字串視為停用此熱鍵。
        self._auto_mode_hotkey = str(auto_mode_hotkey or "").strip().lower()
        self._status_callback = status_callback
        self._auto_mode_changed_callback = auto_mode_changed_callback
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._backend: Optional[KeyBackend] = None

    @property
    def trigger_key(self) -> str:
        return self._trigger_key

    @property
    def use_scroll(self) -> bool:
        return self._use_scroll

    @property
    def auto_mode(self) -> bool:
        return self._auto_mode

    @property
    def auto_mode_hotkey(self) -> str:
        return self._auto_mode_hotkey

    @property
    def pickup_enabled(self) -> bool:
        return self._pickup_enabled

    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def update_config(
        self,
        trigger_key: Optional[str] = None,
        use_scroll: Optional[bool] = None,
        auto_mode: Optional[bool] = None,
        auto_mode_hotkey: Optional[str] = None,
        pickup_enabled: Optional[bool] = None,
    ) -> None:
        """運行時改設定,執行緒下一個 tick 會讀到新值。"""
        with self._lock:
            if pickup_enabled is not None:
                self._pickup_enabled = bool(pickup_enabled)
            if trigger_key is not None:
                key = str(trigger_key or "").strip().lower()
                if key:
                    self._trigger_key = key
            if use_scroll is not None:
                self._use_scroll = bool(use_scroll)
            if auto_mode is not None:
                self._auto_mode = bool(auto_mode)
            if auto_mode_hotkey is not None:
                # 允許空字串(= 停用熱鍵)。
                self._auto_mode_hotkey = str(auto_mode_hotkey or "").strip().lower()

    def start(self) -> bool:
        if self.is_running():
            return True
        if sys.platform != "win32":
            self._emit_status("粉爪大劫案:此功能僅支援 Windows")
            return False
        vk = _heist_vk_code(self._trigger_key)
        if vk is None:
            self._emit_status(
                f"粉爪大劫案:無法解析觸發鍵 '{self._trigger_key}' "
                f"(repr={self._trigger_key!r}, len={len(self._trigger_key)}, "
                f"codes={[hex(ord(c)) for c in self._trigger_key]})"
            )
            return False
        try:
            backend, fallback_msg = create_backend_with_fallback()
        except Exception as exc:  # noqa: BLE001
            self._emit_status(f"粉爪大劫案:後端建立失敗 {exc}")
            self._backend = None
            return False
        self._backend = backend
        if fallback_msg:
            self._emit_status(f"粉爪大劫案:{fallback_msg}")
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="HeistController",
            daemon=True,
        )
        self._thread.start()
        # 啟用訊息把目前模式也報出去,使用者一眼看到生效中的功能。
        if self._auto_mode:
            mode_str = "全自動"
        else:
            mode_str = f"按住 {self._trigger_key.upper()}"
        self._emit_status(f"粉爪大劫案已啟用({mode_str})")
        return True

    def stop(self) -> None:
        if not self.is_running():
            self._thread = None
            return
        self._stop_event.set()
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(timeout=1.5)
        self._backend = None
        self._emit_status("粉爪大劫案已停用")

    def _emit_status(self, message: str) -> None:
        cb = self._status_callback
        if cb is None:
            return
        try:
            cb(message)
        except Exception:  # noqa: BLE001
            pass

    def _emit_auto_mode_changed(self, new_value: bool) -> None:
        cb = self._auto_mode_changed_callback
        if cb is None:
            return
        try:
            cb(bool(new_value))
        except Exception:  # noqa: BLE001
            pass

    def _tap_key(self, key: str, hwnd: int = 0) -> bool:
        """送一次 down→sleep→up 給遊戲。

        優先用 PostMessage 直送 hwnd(不影響系統 keyboard state,使用者實體
        按住 F 時 GetAsyncKeyState 不會被誤判鬆開,連點循環不會中斷)。
        hwnd=0 或 PostMessage 失敗時 fallback 用 SendInput backend。

        ── 為什麼不再用 force_release_first hack ──
        舊版本程式 SendInput key_up 會蓋掉系統 keyboard state,GetAsyncKeyState
        立刻回 false,polling 邏輯就以為使用者鬆開了,「按住 F 第一次成功、
        後續失效」就是這個原因。PostMessage 完全繞開這個問題。
        """
        vk = _heist_vk_code(key)
        if hwnd and vk is not None and _post_key_to_window(int(hwnd), int(vk), self.KEY_TAP_HOLD):
            return True
        # fallback:SendInput(會影響系統 keyboard state,但至少能送出去)。
        backend = self._backend
        if backend is None:
            return False
        try:
            backend.key_down(key)
            time.sleep(self.KEY_TAP_HOLD)
            backend.key_up(key)
            return True
        except Exception as exc:  # noqa: BLE001
            self._emit_status(f"粉爪大劫案:送鍵 {key!r} 失敗 {type(exc).__name__}: {exc}")
            try:
                backend.key_up(key)
            except Exception:  # noqa: BLE001
                pass
            return False

    def _run(self) -> None:
        """背景 loop:polling 觸發鍵 + 視窗焦點,按住才送 key/scroll。"""
        user32 = ctypes.windll.user32
        last_send = 0.0
        scroll_count = 0
        scroll_switch = False
        key_pressed_prev = False
        # 全自動 toggle 熱鍵狀態
        auto_hotkey_prev = False
        # VK code 快取(避免每 tick 重算)
        cached_vk_for: tuple[str, Optional[int]] = ("", None)
        cached_auto_hotkey_for: tuple[str, Optional[int]] = ("", None)

        while not self._stop_event.is_set():
            try:
                with self._lock:
                    pickup_enabled = self._pickup_enabled
                    trigger_key = self._trigger_key
                    use_scroll = self._use_scroll
                    auto_mode = self._auto_mode
                    auto_hotkey_name = self._auto_mode_hotkey

                if cached_vk_for[0] != trigger_key:
                    cached_vk_for = (trigger_key, _heist_vk_code(trigger_key))
                vk = cached_vk_for[1]
                if vk is None:
                    if self._stop_event.wait(0.5):
                        break
                    continue

                # 只在 NTE 視窗為前景時生效,編輯器打字不會誤觸發。
                game = find_game_window()
                if game is None or not is_target_foreground(game.hwnd):
                    key_pressed_prev = False
                    auto_hotkey_prev = False
                    if self._stop_event.wait(0.1):
                        break
                    continue
                game_hwnd = int(game.hwnd)

                now = time.perf_counter()

                # === 全自動 toggle 熱鍵 ===
                # 偵測 rising edge → toggle _auto_mode + emit callback。
                # callback 由 GUI 端落盤 settings + 同步對話框狀態。
                # 空字串 hotkey 視為停用此功能;pickup_enabled=False 時也跳過
                # (全自動 toggle 是粉爪拾取的子功能)。
                if pickup_enabled and auto_hotkey_name:
                    if cached_auto_hotkey_for[0] != auto_hotkey_name:
                        cached_auto_hotkey_for = (
                            auto_hotkey_name,
                            _heist_vk_code(auto_hotkey_name),
                        )
                    auto_hk_vk = cached_auto_hotkey_for[1]
                    if auto_hk_vk is not None:
                        hk_pressed = bool(user32.GetAsyncKeyState(int(auto_hk_vk)) & 0x8000)
                        if hk_pressed and not auto_hotkey_prev:
                            with self._lock:
                                self._auto_mode = not self._auto_mode
                                new_value = self._auto_mode
                            auto_mode = new_value  # 本 tick 生效
                            self._emit_status(
                                f"全自動拾取:{'開' if new_value else '關'}"
                                f"(按 {auto_hotkey_name.upper()} 切換)"
                            )
                            self._emit_auto_mode_changed(new_value)
                        auto_hotkey_prev = hk_pressed
                    else:
                        auto_hotkey_prev = False
                else:
                    auto_hotkey_prev = False

                # === F 連點 / 全自動拾取 ===
                if not pickup_enabled:
                    key_pressed_prev = False
                    if self._stop_event.wait(self.CHECK_INTERVAL):
                        break
                    continue

                if auto_mode:
                    pressed = True
                else:
                    pressed = bool(user32.GetAsyncKeyState(int(vk)) & 0x8000)

                if not pressed:
                    key_pressed_prev = False
                    if self._stop_event.wait(self.CHECK_INTERVAL):
                        break
                    continue

                if not key_pressed_prev:
                    # 剛開始按下,重置滾輪節奏(對齊 HeistTask._trigger_key_pressed)。
                    scroll_count = 0
                    scroll_switch = False
                    key_pressed_prev = True

                if now - last_send >= self.SEND_KEY_INTERVAL:
                    # 送 F 走 PostMessage 直送 hwnd,不影響系統 keyboard state,
                    # 使用者按住 F 時 GetAsyncKeyState 不會被誤判鬆開。
                    self._tap_key(trigger_key, hwnd=game_hwnd)
                    if use_scroll:
                        try:
                            _scroll_wheel(WHEEL_DELTA if scroll_switch else -WHEEL_DELTA)
                        except Exception:  # noqa: BLE001
                            pass
                        scroll_count += 1
                        if scroll_count >= 3:
                            scroll_count = 0
                            scroll_switch = not scroll_switch
                    last_send = now

                if self._stop_event.wait(self.CHECK_INTERVAL):
                    break
            except Exception as exc:  # noqa: BLE001
                # 任何意外都 log 一下並退避,不讓 daemon 跑掉。
                self._emit_status(f"粉爪大劫案 loop 例外:{exc}")
                if self._stop_event.wait(0.5):
                    break


# ============================================================================
# 向後相容別名 — 舊匯入名稱保留,讓尚未更新的呼叫端不會立刻炸。
# 新程式碼請改用 *Task / AutomationTask / AutomationProxy。
# ============================================================================
SoundCombatWorker = SoundCombatTask
RhythmWorker = RhythmTask
AutomationWorker = AutomationTask
