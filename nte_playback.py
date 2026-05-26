# NTE Piano - 自動演奏與自動化工具
# Copyright (C) 2026  Yulun
# Licensed under GPL-3.0-or-later. See LICENSE.
"""nte_playback — 按鍵後端、Win32 視窗整合與播放 Worker。

對外提供:
    Win32:
        _configure_winapi / is_running_as_admin
        WindowInfo / find_game_window / invalidate_window_cache / focus_window /
        is_window_alive / foreground_hwnd / is_target_foreground
    後台送鍵 (PostMessage):
        WM_KEYDOWN / WM_KEYUP
        name_to_vk / post_key_down / post_key_up / post_key_to_window
    後端:
        KeyBackend / PynputBackend / PyDirectInputBackend / PostMessageBackend
        create_backend / create_backend_with_fallback
    播放:
        ScheduledAction / PlaybackWorker
    Hotkey:
        HotkeyBridge / GlobalHotkeys

依賴:
    nte_dsl (KeyStroke / NoteEvent / Sheet)
    PySide6.QtCore (QObject / Signal / Slot)
    pynput (惰性匯入)
    pydirectinput / pydirectinput-rgx (惰性匯入)
"""
from __future__ import annotations

import bisect
import ctypes
import sys
import os
import re
import threading
import time
from collections import defaultdict
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, Signal, Slot

from nte_dsl import KeyStroke, NoteEvent, Sheet
from nte_perf import perf


GAME_PROCESS_NAME = "HTGame.exe"
GAME_TITLE_HINT = "NTE"
SW_RESTORE = 9
VK_MENU = 0x12
KEYEVENTF_KEYUP = 0x0002
DEFAULT_BACKEND = "pydirectinput"
FALLBACK_BACKEND = "pynput"

_WINAPI_READY = False
_ENUM_WINDOWS_PROC = None


def _configure_winapi() -> None:
    global _WINAPI_READY, _ENUM_WINDOWS_PROC
    if _WINAPI_READY or sys.platform != "win32":
        return

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    shell32 = ctypes.windll.shell32

    _ENUM_WINDOWS_PROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    shell32.IsUserAnAdmin.argtypes = []
    shell32.IsUserAnAdmin.restype = wintypes.BOOL

    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.EnumWindows.argtypes = [_ENUM_WINDOWS_PROC, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.IsWindow.argtypes = [wintypes.HWND]
    user32.IsWindow.restype = wintypes.BOOL
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.BringWindowToTop.argtypes = [wintypes.HWND]
    user32.BringWindowToTop.restype = wintypes.BOOL
    user32.keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ctypes.c_ulong]
    user32.keybd_event.restype = None

    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL

    # PostMessage 後台送鍵用。VK→scan code 與 message 投遞兩個函式。
    user32.PostMessageW.argtypes = [
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    ]
    user32.PostMessageW.restype = wintypes.BOOL
    user32.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
    user32.MapVirtualKeyW.restype = wintypes.UINT

    _WINAPI_READY = True


def is_running_as_admin() -> bool:
    if sys.platform != "win32":
        return True
    try:
        _configure_winapi()
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


@dataclass(frozen=True)
class WindowInfo:
    hwnd: int
    title: str
    pid: int
    process_name: str

    @property
    def display(self) -> str:
        name = self.process_name or f"PID {self.pid}"
        return f"{self.title} ({name})"


def _window_text(hwnd: int) -> str:
    if sys.platform != "win32":
        return ""
    _configure_winapi()
    user32 = ctypes.windll.user32
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value


def _process_name(pid: int) -> str:
    if sys.platform != "win32" or pid <= 0:
        return ""

    kernel32 = ctypes.windll.kernel32
    process_query_limited_information = 0x1000
    _configure_winapi()
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return ""

    try:
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return ""
        return Path(buffer.value).name
    finally:
        kernel32.CloseHandle(handle)


# 標題命中是 fallback。為了避免下列誤判,啟用一份排除清單:
#   - NTE Piano 自身視窗(python.exe + 標題「NTE Piano Auto Player」)
#   - 檔案總管開到 nte_piano 資料夾(explorer.exe + 標題含「nte_piano」)
#   - 編輯器/終端機視窗碰巧含「nte」字樣
# 真的遊戲一定是 HTGame.exe(主路徑命中);這份清單只擋掉不可能是遊戲的 process。
_NON_GAME_PROCESSES = frozenset(
    {
        "explorer.exe",
        "python.exe",
        "pythonw.exe",
        "code.exe",
        "cmd.exe",
        "powershell.exe",
        "pwsh.exe",
        "windowsterminal.exe",
        "chrome.exe",
        "firefox.exe",
        "msedge.exe",
        "notepad.exe",
        "notepad++.exe",
    }
)

# title hint 用 word boundary 嚴格比對,避免被 "ceNTEr" / "iNTErnet" 之類子字串誤判。
# \b 在 ASCII 模式下表示 [A-Za-z0-9_] 與其它字元的邊界,因此「NTE」前後可以是空格、
# 連字號、標點等,但不能是字母或數字 — "NTE Game" / "[NTE]" 都命中,"center" 不命中。
_GAME_TITLE_RE = re.compile(rf"\b{re.escape(GAME_TITLE_HINT)}\b", re.IGNORECASE)

# find_game_window 每次跑 EnumWindows + QueryFullProcessImageName per window,
# 在背景有上百個窗口時單次成本 0.5-2ms。HeistController 100Hz polling 一秒
# 燒 50-200ms,RhythmTask / NTECheckerProbe / 後台送鍵 hwnd 查詢也都共用。
# 5 秒 TTL + 失效時驗證 is_window_alive,99% 路徑變成一次 dict lookup。
_WINDOW_CACHE_TTL = 5.0
_window_cache: dict = {"info": None, "ts": 0.0, "hit": 0, "miss": 0, "last_flush": 0.0}
_window_cache_lock = threading.Lock()


def invalidate_window_cache() -> None:
    """主動失效 find_game_window cache(例如已知遊戲剛重啟)。"""
    with _window_cache_lock:
        _window_cache["info"] = None
        _window_cache["ts"] = 0.0


def _flush_window_cache_stats_locked(now: float) -> None:
    """1 秒一次 flush hit/miss 累計到 perf log,避免每次 hit 都打 log 反而拖效能。"""
    if not perf.enabled:
        return
    last = float(_window_cache["last_flush"])
    if now - last < 1.0:
        return
    hit = int(_window_cache["hit"])
    miss = int(_window_cache["miss"])
    if hit + miss == 0:
        return
    perf.log(
        "window",
        "cache_stats",
        hit=hit,
        miss=miss,
        ratio=f"{hit / (hit + miss):.3f}",
    )
    _window_cache["hit"] = 0
    _window_cache["miss"] = 0
    _window_cache["last_flush"] = now


def is_game_window(window: WindowInfo) -> bool:
    process_name = window.process_name.lower()
    if process_name == GAME_PROCESS_NAME.lower():
        return True
    # 排除自身 + 常見不會是遊戲的 process,再看 title hint。
    if window.pid == os.getpid():
        return False
    if process_name in _NON_GAME_PROCESSES:
        return False
    return bool(_GAME_TITLE_RE.search(window.title))


def find_game_window():
    if sys.platform != "win32":
        return None
    _configure_winapi()
    now = time.perf_counter()
    with _window_cache_lock:
        info = _window_cache["info"]
        ts = float(_window_cache["ts"])
        if info is not None and (now - ts) < _WINDOW_CACHE_TTL:
            # cache 未過期,但 hwnd 可能已死(遊戲關閉)。is_window_alive 是
            # 一次 IsWindow syscall,比完整 EnumWindows 便宜兩個量級。
            if is_window_alive(info.hwnd):
                _window_cache["hit"] = int(_window_cache["hit"]) + 1
                _flush_window_cache_stats_locked(now)
                return info
            _window_cache["info"] = None
            _window_cache["ts"] = 0.0

    user32 = ctypes.windll.user32
    found = []

    def callback(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        title = _window_text(hwnd).strip()
        if not title:
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        win_info = WindowInfo(int(hwnd), title, int(pid.value), _process_name(pid.value))
        if is_game_window(win_info):
            found.append(win_info)
            return False
        return True

    enum_callback = _ENUM_WINDOWS_PROC(callback)
    user32.EnumWindows(enum_callback, 0)
    result = found[0] if found else None
    with _window_cache_lock:
        _window_cache["info"] = result
        _window_cache["ts"] = now
        _window_cache["miss"] = int(_window_cache["miss"]) + 1
        _flush_window_cache_stats_locked(now)
    return result


def is_window_alive(hwnd: int) -> bool:
    if sys.platform != "win32" or not hwnd:
        return False
    _configure_winapi()
    return bool(ctypes.windll.user32.IsWindow(hwnd))


def foreground_hwnd() -> int:
    if sys.platform != "win32":
        return 0
    _configure_winapi()
    hwnd = ctypes.windll.user32.GetForegroundWindow()
    if hwnd is None:
        return 0
    return int(hwnd)


def focus_window(hwnd: int) -> bool:
    if sys.platform != "win32":
        return True
    if not is_window_alive(hwnd):
        return False
    _configure_winapi()
    user32 = ctypes.windll.user32
    hwnd_value = wintypes.HWND(hwnd)
    user32.ShowWindow(hwnd_value, SW_RESTORE)
    user32.BringWindowToTop(hwnd_value)
    user32.SetForegroundWindow(hwnd_value)
    if foreground_hwnd() == hwnd:
        return True

    user32.keybd_event(VK_MENU, 0, 0, 0)
    try:
        user32.SetForegroundWindow(hwnd_value)
        user32.BringWindowToTop(hwnd_value)
    finally:
        user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
    time.sleep(0.05)
    return foreground_hwnd() == hwnd


def is_target_foreground(hwnd: int) -> bool:
    if sys.platform != "win32":
        return True
    return foreground_hwnd() == hwnd


# ============================================================================
# PostMessage 後台送鍵 — 不需視窗在前景,把 WM_KEYDOWN/WM_KEYUP 直接 post 到
# 指定 hwnd 的訊息佇列。系統 keyboard state 完全不動,因此:
#   - 使用者按住實體鍵時不會被 SendInput key_up 蓋掉
#   - 視窗失焦時(切離前景)仍能送到遊戲
#
# 對 Unity 舊 Input Manager 與多數走 Windows message 迴圈的遊戲有效;
# 對使用 RawInput / 新 Input System Package 的遊戲無效(此時 hwnd 收得到
# 訊息但遊戲沒監聽)。NTE 經 HeistController 實測有效。
# ============================================================================

WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101

# 特殊鍵名 → Windows VK code 對照。字母 a-z 與數字 0-9 直接從 ord('A')/ord('0')
# 算出來,不入這張表。F1-F12 也走專屬分支。其他用 dict 寫死。
_SPECIAL_NAME_TO_VK = {
    "shift": 0x10,
    "ctrl": 0x11,
    "control": 0x11,
    "alt": 0x12,
    "space": 0x20,
    "enter": 0x0D,
    "return": 0x0D,
    "esc": 0x1B,
    "escape": 0x1B,
    "tab": 0x09,
    "backspace": 0x08,
}


def name_to_vk(key: str) -> int | None:
    """把按鍵名稱('a' / 'f6' / 'shift')轉成 Windows VK code。找不到回 None。

    跟 nte_automation._heist_vk_code 邏輯一致但不依賴(避免循環 import);
    後者保留給 HeistController 內部 polling 用,本函式給 PostMessageBackend 用。
    """
    if not key:
        return None
    name = str(key).strip().lower()
    if not name:
        return None
    if name in _SPECIAL_NAME_TO_VK:
        return _SPECIAL_NAME_TO_VK[name]
    if name.startswith("f") and name[1:].isdigit():
        idx = int(name[1:])
        if 1 <= idx <= 12:
            return 0x70 + idx - 1  # VK_F1..VK_F12
    if len(name) == 1:
        # a-z / 0-9 的 VK code 直接等於 ASCII 大寫,跳過 VkKeyScanW
        # (某些 keyboard layout 切換時 VkKeyScanW 會回 -1)。
        code = ord(name.upper())
        if 0x41 <= code <= 0x5A or 0x30 <= code <= 0x39:
            return code
    return None


def post_key_down(hwnd: int, vk: int) -> bool:
    """PostMessage WM_KEYDOWN 到 hwnd,**不影響系統 keyboard state**。

    回 True 表示訊息成功 post 到佇列(不保證遊戲有反應)。
    """
    if not hwnd or vk is None or sys.platform != "win32":
        return False
    _configure_winapi()
    user32 = ctypes.windll.user32
    try:
        # MapVirtualKey vk→sc(MAPVK_VK_TO_VSC = 0);Unity / Unreal 等
        # 引擎會驗 scan code,缺了 modifier 就完全失效。
        scan = int(user32.MapVirtualKeyW(int(vk), 0)) & 0xFF
        # lparam: bit 0-15 = repeat(1) / bit 16-23 = scan / bit 30 = prev down(0)
        lparam = (scan << 16) | 1
        ok = user32.PostMessageW(int(hwnd), WM_KEYDOWN, int(vk), lparam)
        if perf.enabled:
            perf.log("postmsg", "key_down", hwnd=hwnd, vk=hex(vk), ok=int(bool(ok)))
        return bool(ok)
    except Exception:  # noqa: BLE001
        return False


def post_key_up(hwnd: int, vk: int) -> bool:
    """PostMessage WM_KEYUP 到 hwnd,搭配 post_key_down 成對使用。"""
    if not hwnd or vk is None or sys.platform != "win32":
        return False
    _configure_winapi()
    user32 = ctypes.windll.user32
    try:
        scan = int(user32.MapVirtualKeyW(int(vk), 0)) & 0xFF
        # lparam: bit 30 = prev down(1) / bit 31 = release(1)
        lparam = (1 << 31) | (1 << 30) | (scan << 16) | 1
        ok = user32.PostMessageW(int(hwnd), WM_KEYUP, int(vk), lparam)
        if perf.enabled:
            perf.log("postmsg", "key_up", hwnd=hwnd, vk=hex(vk), ok=int(bool(ok)))
        return bool(ok)
    except Exception:  # noqa: BLE001
        return False


def post_key_to_window(hwnd: int, vk: int, down_time: float = 0.02) -> bool:
    """down → sleep(down_time) → up 一次送完,HeistController 等 tap-style 用。

    回 True 表示 down 與 up 兩個 PostMessage 都成功。
    """
    if not post_key_down(hwnd, vk):
        return False
    if down_time > 0:
        time.sleep(down_time)
    return post_key_up(hwnd, vk)


class KeyBackend:
    name = "base"

    def key_down(self, key: str) -> None:
        raise NotImplementedError

    def key_up(self, key: str) -> None:
        raise NotImplementedError


class PynputBackend(KeyBackend):
    name = "pynput"

    def __init__(self) -> None:
        try:
            from pynput.keyboard import Controller, Key
        except ImportError as exc:
            raise RuntimeError("尚未安裝 pynput,請先安裝 requirements.txt") from exc
        self._controller = Controller()
        self._key_cls = Key

    def key_down(self, key: str) -> None:
        self._controller.press(self._to_key(key))

    def key_up(self, key: str) -> None:
        self._controller.release(self._to_key(key))

    def _to_key(self, key: str):
        if key == "shift":
            return self._key_cls.shift
        if key == "ctrl":
            return self._key_cls.ctrl
        return key


class PyDirectInputBackend(KeyBackend):
    name = "pydirectinput"

    def __init__(self) -> None:
        module = None
        errors = []
        for module_name in ("pydirectinput", "pydirectinput_rgx"):
            try:
                module = __import__(module_name)
                break
            except ImportError as exc:
                errors.append(exc)
        if module is None:
            raise RuntimeError("尚未安裝 pydirectinput-rgx,請先安裝 requirements.txt") from errors[-1]

        module.PAUSE = 0
        if hasattr(module, "FAILSAFE"):
            module.FAILSAFE = False
        self._module = module

    def key_down(self, key: str) -> None:
        self._module.keyDown(key)

    def key_up(self, key: str) -> None:
        self._module.keyUp(key)


class NullBackend(KeyBackend):
    """預先聆聽模式用:接收 key_down/up 但不送任何系統事件。

    這樣 PlaybackWorker 的 _chord_down / _release_all 不必到處插
    `if silent_mode` 條件,靠多型直接接到 no-op 即可。"""

    name = "null"

    def key_down(self, key: str) -> None:  # noqa: D401
        return None

    def key_up(self, key: str) -> None:  # noqa: D401
        return None


class PostMessageBackend(KeyBackend):
    """後台送鍵 backend — PostMessage 直送指定 hwnd 的訊息佇列。

    跟 PynputBackend / PyDirectInputBackend 的差異:後兩者走 SendInput /
    keybd_event,事件注入系統輸入佇列,使用者實體鍵與遊戲共用 keyboard state;
    PostMessage 把 message 投到指定 hwnd,系統 state 完全不動,因此:
        - 視窗不在前景也能送鍵(背景模式)
        - 使用者實體按鍵不會被 backend 的 key_up 蓋掉

    限制:對使用 RawInput / 新 Input System 的遊戲無效。NTE 經 HeistController
    粉爪實測有效。

    hwnd_provider 每次 key_down/up 都會呼叫,可以動態回最新 hwnd
    (遊戲重啟、視窗 handle 改變等場景)。建議配 find_game_window 的 cache
    使用,呼叫成本接近零。
    """

    name = "postmessage"

    def __init__(self, hwnd_provider: Callable[[], int]) -> None:
        self._hwnd_provider = hwnd_provider
        # PostMessage 沒有「鍵當前是否 down」概念,自己維護:
        #   - 同一鍵連送 key_down → 遊戲只收第一次,後續被當 auto-repeat 略過
        #   - 沒 down 就 key_up → 浪費 PostMessage 也可能引發奇怪狀態
        self._down: set[str] = set()

    def _hwnd(self) -> int:
        try:
            value = self._hwnd_provider()
        except Exception:  # noqa: BLE001
            return 0
        if value is None:
            return 0
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    def key_down(self, key: str) -> None:
        if not key or key in self._down:
            return
        vk = name_to_vk(key)
        if vk is None:
            return
        hwnd = self._hwnd()
        if not hwnd:
            return
        if post_key_down(hwnd, vk):
            self._down.add(key)

    def key_up(self, key: str) -> None:
        if not key or key not in self._down:
            return
        vk = name_to_vk(key)
        if vk is not None:
            hwnd = self._hwnd()
            if hwnd:
                post_key_up(hwnd, vk)
        # 即使 PostMessage 失敗也清掉 _down — 否則之後 key_down 永遠被擋。
        self._down.discard(key)

    def release_all(self) -> None:
        """切換 backend 時用,把所有當前認為 down 的鍵發 key_up。"""
        for key in list(self._down):
            self.key_up(key)


def create_backend(
    name: str, hwnd_provider: Callable[[], int] | None = None
) -> KeyBackend:
    if name == PynputBackend.name:
        return PynputBackend()
    if name == PyDirectInputBackend.name:
        return PyDirectInputBackend()
    if name == PostMessageBackend.name:
        if hwnd_provider is None:
            raise RuntimeError("postmessage backend 需要 hwnd_provider")
        return PostMessageBackend(hwnd_provider)
    raise RuntimeError(f"未知的按鍵後端:{name}")


def create_backend_with_fallback():
    try:
        return create_backend(DEFAULT_BACKEND), ""
    except RuntimeError as primary:
        try:
            return create_backend(FALLBACK_BACKEND), f"{DEFAULT_BACKEND} 不可用,已退而使用 {FALLBACK_BACKEND}"
        except RuntimeError as secondary:
            raise RuntimeError(f"{primary}; {secondary}") from secondary


@dataclass(frozen=True)
class ScheduledAction:
    seconds: float
    priority: int
    kind: str
    event_index: int
    event: NoteEvent


class PlaybackWorker(QObject):
    progress = Signal(int, str, object)
    active_notes = Signal(object)
    # 高頻路徑:每個 down/up emit (adds, removes) tuple,接收端只動 set 差集 + update。
    # 取代「每次 emit 整份 sorted active set」的 O(N log N) + 跨 thread 重序列化成本。
    active_delta = Signal(object)
    note_pressed = Signal(object)
    started = Signal(float)
    failed = Signal(str)
    finished = Signal(bool)

    SKIP_THRESHOLD_SECONDS = 0.05

    def __init__(
        self,
        sheet: Sheet,
        start_delay: float,
        target_hwnd=None,
        focus_before_play: bool = False,
        initial_offset_seconds: float = 0.0,
        speed: float = 1.0,
        auto_trim_leading: bool = True,
        loop_end_seconds: float | None = None,
        silent_mode: bool = False,
        force_background: bool = False,
        hwnd_provider: Callable[[], int] | None = None,
    ):
        super().__init__()
        self._sheet = sheet
        self._start_delay = start_delay
        self._target_hwnd = target_hwnd
        self._focus_before_play = focus_before_play
        self._speed = max(0.1, float(speed))
        self._initial_offset = max(0.0, float(initial_offset_seconds)) / self._speed
        self._auto_trim_leading = bool(auto_trim_leading)
        self._silent_mode = bool(silent_mode)
        # 播放區間結尾(原始秒數,未除 speed);超過此秒數的 action 不送並停止播放。
        # None 表示無限制,跑到譜面結尾為止。
        if loop_end_seconds is None:
            self._loop_end = None
        else:
            self._loop_end = max(0.0, float(loop_end_seconds)) / self._speed
        self._stop_event = threading.Event()
        self._active_counts = {}
        self._last_release_at: dict[str, float] = {}
        self._active_label_counts = {}
        self._lock = threading.Lock()
        self._paused = False
        self._pause_started_at = 0.0
        self._seek_pending = False
        self._seek_target = 0.0
        self._started_at = 0.0
        self._speed_dirty = False
        # 後台送鍵切換:
        #   _fg_backend     = pydirectinput / pynput(視窗在前景時用)
        #   _bg_backend     = PostMessageBackend(視窗不在前景或強制後台時用)
        #   _force_background 為 True 時強制走 bg_backend
        # backend 切換在 run() loop 內每 200ms 檢查一次,避免每個 action 都做
        # foreground syscall。
        self._force_background = bool(force_background)
        self._hwnd_provider = hwnd_provider
        self._fg_backend: KeyBackend | None = None
        self._bg_backend: KeyBackend | None = None
        self._last_backend_check_ts = 0.0

    def request_stop(self) -> None:
        self._stop_event.set()

    def set_speed(self, speed: float) -> None:
        speed = max(0.1, float(speed))
        with self._lock:
            if abs(speed - self._speed) < 1e-6:
                return
            if self._started_at == 0.0:
                initial_music = self._initial_offset * self._speed
                self._speed = speed
                self._initial_offset = initial_music / speed
                self._speed_dirty = True
                return
            ref = self._pause_started_at if self._paused else time.perf_counter()
            music_seconds = max(0.0, (ref - self._started_at) * self._speed)
            self._speed = speed
            self._started_at = ref - music_seconds / speed
            if self._seek_pending:
                self._seek_target = max(0.0, music_seconds) / speed
            self._speed_dirty = True

    def set_force_background(self, value: bool) -> None:
        """切換「強制後台模式」。播放途中可呼叫;下次 _resolve_backend 立即生效。"""
        new_value = bool(value)
        with self._lock:
            if self._force_background == new_value:
                return
            self._force_background = new_value
            # 不等 200ms 防抖,讓使用者立刻看到切換效果。
            self._last_backend_check_ts = 0.0

    def request_pause(self) -> None:
        with self._lock:
            if self._paused or self._stop_event.is_set():
                return
            self._paused = True
            self._pause_started_at = time.perf_counter()

    def request_resume(self) -> None:
        with self._lock:
            if not self._paused:
                return
            self._started_at += time.perf_counter() - self._pause_started_at
            self._paused = False

    def request_seek(self, position_seconds: float) -> None:
        with self._lock:
            self._seek_target = max(0.0, float(position_seconds)) / self._speed
            self._seek_pending = True

    def current_position(self) -> float:
        with self._lock:
            ref = self._pause_started_at if self._paused else time.perf_counter()
        return max(0.0, ref - self._started_at) * self._speed

    @property
    def speed(self) -> float:
        return self._speed

    @Slot()
    def run(self) -> None:
        backend = None
        stopped = False
        try:
            if self._silent_mode:
                backend = NullBackend()
                self._fg_backend = backend
                self._bg_backend = None
            else:
                backend, _ = create_backend_with_fallback()
                self._fg_backend = backend
                # bg_backend 只在有 hwnd_provider 時建,沒有就永遠走 fg。
                # PostMessageBackend 不需重型初始化,建構基本上零成本。
                if self._hwnd_provider is not None:
                    try:
                        self._bg_backend = PostMessageBackend(self._hwnd_provider)
                    except Exception:  # noqa: BLE001
                        self._bg_backend = None
                else:
                    self._bg_backend = None
            self._last_backend_check_ts = 0.0
            actions = self._build_schedule()
            if perf.enabled:
                perf.log(
                    "worker",
                    "run_start",
                    actions=len(actions),
                    silent=self._silent_mode,
                    speed=self._speed,
                    backend=backend.name,
                    bg_available=int(self._bg_backend is not None),
                    force_bg=int(self._force_background),
                )
            # auto-trim leading silence:若譜面首音落在 SKIP_THRESHOLD 之後,把
            # _initial_offset 設成首音秒數,等同把 cursor 快轉到首音前一刻。
            # 只在沒指定 initial_offset(非 seek 場景)且 auto_trim 開啟時生效。
            # piano_roll 的 cursor 同樣從 started_at 起算 → 自動跟著快轉,視覺對齊。
            if self._auto_trim_leading and self._initial_offset == 0.0:
                first_down = next(
                    (a.seconds for a in actions if a.kind == "down"),
                    None,
                )
                if first_down is not None and first_down > self.SKIP_THRESHOLD_SECONDS:
                    self._initial_offset = first_down
            if self._wait(self._start_delay):
                stopped = True
                return
            self._focus_target_window()

            self._started_at = time.perf_counter() - self._initial_offset
            self.started.emit(self._started_at)

            i = 0
            if self._initial_offset > 0:
                i = next(
                    (j for j, a in enumerate(actions) if a.seconds >= self._initial_offset),
                    len(actions),
                )

            while i < len(actions):
                if self._stop_event.is_set():
                    stopped = True
                    break

                # backend 自動切換(前景/後台)。內部 200ms 防抖,所以即使每個
                # action 都呼叫也不會頻繁做 foreground syscall。
                backend = self._resolve_backend(backend)

                with self._lock:
                    speed_dirty = self._speed_dirty
                    if speed_dirty:
                        self._speed_dirty = False
                if speed_dirty:
                    actions = self._build_schedule()
                    self._release_all(backend)
                    self.active_notes.emit([])
                    current_real = max(0.0, time.perf_counter() - self._started_at)
                    i = next(
                        (j for j, a in enumerate(actions) if a.seconds >= current_real),
                        len(actions),
                    )
                    continue

                consumed_target = self._consume_seek(backend)
                if consumed_target is not None:
                    i = next(
                        (j for j, a in enumerate(actions) if a.seconds >= consumed_target),
                        len(actions),
                    )
                    continue

                action = actions[i]
                # loop_end:超過區間結尾立刻終止播放(視為自然結束,不是 stop)。
                if self._loop_end is not None and action.seconds > self._loop_end:
                    break
                result = self._wait_until_action(action.seconds, backend)
                if result == "stop":
                    stopped = True
                    break
                if result == "seek":
                    continue
                if result == "speed":
                    continue
                if result == "skip":
                    if perf.enabled:
                        perf.log(
                            "worker",
                            "skip",
                            idx=action.event_index,
                            kind=action.kind,
                            sched=f"{action.seconds:.3f}",
                            late_ms=f"{(time.perf_counter() - (self._started_at + action.seconds)) * 1000.0:+.2f}",
                        )
                    i += 1
                    continue

                if action.kind == "progress":
                    self.progress.emit(
                        action.event_index,
                        f"{action.event.track}: {action.event.source}",
                        [stroke.label for stroke in action.event.strokes],
                    )
                elif action.kind == "down":
                    if perf.enabled:
                        drift_ms = (time.perf_counter() - (self._started_at + action.seconds)) * 1000.0
                        perf.log(
                            "worker",
                            "down_begin",
                            idx=action.event_index,
                            sched=f"{action.seconds:.3f}",
                            drift=f"{drift_ms:+.2f}",
                            n=len(action.event.strokes),
                        )
                        t_chord = time.perf_counter()
                    self._chord_down(backend, action.event.strokes)
                    if perf.enabled:
                        perf.log(
                            "worker",
                            "down_end",
                            idx=action.event_index,
                            dur_ms=f"{(time.perf_counter() - t_chord) * 1000.0:.2f}",
                        )
                    self.note_pressed.emit([stroke.label for stroke in action.event.strokes])
                    # delta:_chord_down 後 count==1 的 label 表示這次新加入 active set。
                    adds = [
                        s.label for s in action.event.strokes
                        if self._active_label_counts.get(s.label, 0) == 1
                    ]
                    if adds:
                        self.active_delta.emit((adds, []))
                elif action.kind == "up":
                    if perf.enabled:
                        drift_ms = (time.perf_counter() - (self._started_at + action.seconds)) * 1000.0
                        perf.log(
                            "worker",
                            "up_begin",
                            idx=action.event_index,
                            sched=f"{action.seconds:.3f}",
                            drift=f"{drift_ms:+.2f}",
                            n=len(action.event.strokes),
                        )
                    removes = []
                    for stroke in reversed(action.event.strokes):
                        before = self._active_label_counts.get(stroke.label, 0)
                        self._stroke_up(backend, stroke)
                        if before == 1 and stroke.label not in self._active_label_counts:
                            removes.append(stroke.label)
                    if removes:
                        self.active_delta.emit(([], removes))
                i += 1

        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))
        finally:
            if backend is not None:
                self._release_all(backend)
            self.finished.emit(stopped or self._stop_event.is_set())

    def _consume_seek(self, backend: KeyBackend):
        with self._lock:
            if not self._seek_pending:
                return None
            target = self._seek_target
            self._seek_pending = False
            self._started_at = time.perf_counter() - target
            if self._paused:
                self._pause_started_at = time.perf_counter()
        self._release_all(backend)
        self.active_notes.emit([])
        return target

    BACKEND_CHECK_INTERVAL = 0.2

    def _resolve_backend(self, current: KeyBackend) -> KeyBackend:
        """200ms 防抖內回傳當前該用的 backend;切換時 release_all 舊 backend。

        無 hwnd_provider 或 silent_mode → 直接回原 backend(永遠 fg / null)。
        force_background True → 一律 bg_backend。
        否則:視窗在前景 → fg_backend;不在前景 → bg_backend。

        切 backend 會把舊 backend 認為 down 的鍵 key_up,使用者最多在切換瞬間
        聽到一個音被截斷。
        """
        if self._silent_mode or self._bg_backend is None:
            return current
        now = time.perf_counter()
        if now - self._last_backend_check_ts < self.BACKEND_CHECK_INTERVAL:
            return current
        self._last_backend_check_ts = now
        with self._lock:
            force_bg = self._force_background
        if force_bg:
            target = self._bg_backend
        elif self._target_hwnd and not is_target_foreground(self._target_hwnd):
            target = self._bg_backend
        else:
            target = self._fg_backend
        if target is current:
            return current
        # 切換瞬間先 release 舊 backend 持有的鍵,否則舊 backend 內部 _down
        # set / pydirectinput state 會留住。
        self._release_all(current)
        if perf.enabled:
            perf.log(
                "worker",
                "backend_switch",
                from_=current.name,
                to=target.name,
                force_bg=int(force_bg),
            )
        return target

    def _build_schedule(self):
        actions = []
        speed = self._speed if self._speed > 0 else 1.0

        retrigger_gap = self._sheet.gap if self._sheet.gap > 0 else 0.012
        retrigger_gap = max(retrigger_gap, 0.012)
        per_key_downs: dict[str, list[float]] = defaultdict(list)
        for event in self._sheet.events:
            if event.is_rest:
                continue
            ev_start = self._sheet.beats_to_seconds(event.start_beats) / speed
            for stroke in event.strokes:
                per_key_downs[stroke.key].append(ev_start)
        for downs in per_key_downs.values():
            downs.sort()

        for index, event in enumerate(self._sheet.events):
            start_seconds = self._sheet.beats_to_seconds(event.start_beats) / speed
            duration_seconds = self._sheet.beats_to_seconds(event.duration_beats) / speed
            hold_seconds = duration_seconds * self._sheet.hold
            if self._sheet.gap > 0:
                hold_seconds = min(hold_seconds, max(0.0, duration_seconds - self._sheet.gap))
            release_seconds = start_seconds + max(0.0, hold_seconds)

            if not event.is_rest:
                for stroke in event.strokes:
                    downs = per_key_downs.get(stroke.key)
                    if not downs:
                        continue
                    idx = bisect.bisect_right(downs, start_seconds + 1e-9)
                    if idx < len(downs):
                        next_down = downs[idx]
                        cutoff = max(start_seconds, next_down - retrigger_gap)
                        if cutoff < release_seconds:
                            release_seconds = cutoff

            actions.append(ScheduledAction(start_seconds, 1, "progress", index, event))
            if not event.is_rest:
                actions.append(ScheduledAction(start_seconds, 2, "down", index, event))
                actions.append(ScheduledAction(release_seconds, 0, "up", index, event))

        actions.sort(key=lambda action: (action.seconds, action.priority, action.event_index))
        return actions

    def _focus_target_window(self) -> None:
        if self._silent_mode:
            return
        if self._target_hwnd and self._focus_before_play:
            try:
                focus_window(self._target_hwnd)
            except Exception:
                pass

    SETTLE_AFTER_RELEASE = 0.03

    def _settle_for_key(self, key: str) -> None:
        # silent_mode 下 NullBackend 不送鍵,settle 是給遊戲鍵盤子系統喘息用的,
        # 完全沒意義 — 而且 30ms × 密集音 = 整曲累積數秒 drift,後段會 skip 漏音。
        if self._silent_mode:
            return
        last = self._last_release_at.get(key)
        if last is None:
            return
        wait = self.SETTLE_AFTER_RELEASE - (time.perf_counter() - last)
        if wait > 0:
            time.sleep(wait)

    def _stroke_down(self, backend: KeyBackend, stroke: KeyStroke) -> None:
        self._settle_for_key(stroke.key)
        # modifier_delay 同理:silent_mode 沒送鍵,不需要等遊戲偵測 Shift/Ctrl 邊緣。
        mod_delay = 0.0 if self._silent_mode else self._sheet.modifier_delay
        for modifier in stroke.modifiers:
            backend.key_down(modifier)
            if mod_delay:
                time.sleep(mod_delay)

        self._press_active(backend, stroke.key)
        self._active_label_counts[stroke.label] = self._active_label_counts.get(stroke.label, 0) + 1

        for modifier in reversed(stroke.modifiers):
            if mod_delay:
                time.sleep(mod_delay)
            backend.key_up(modifier)

    def _chord_down(self, backend: KeyBackend, strokes) -> None:
        """同 event 的多個 strokes 一起按下;同 modifier set 的 strokes 共用一次 mod 按放,
        避免每個 stroke 都各自 press/release modifier 造成的階梯時序。
        """
        if not strokes:
            return
        if len(strokes) == 1:
            self._stroke_down(backend, strokes[0])
            return
        groups: dict[tuple, list] = {}
        for stroke in strokes:
            mod_key = tuple(stroke.modifiers)
            groups.setdefault(mod_key, []).append(stroke)
        if perf.enabled and len(groups) > 1:
            # 同一 chord 內出現多種 modifier set 表示要切換 shift/ctrl 狀態。
            # 若同 key 在不同 group 出現,代表「裸鍵」與「shift+同鍵」並存,shift
            # 按下時會把先前已 down 的裸鍵在遊戲端解讀成 shift+鍵 → 變升降音。
            key_to_groups: dict[str, set] = {}
            for mods, items in groups.items():
                for stk in items:
                    key_to_groups.setdefault(stk.key, set()).add(mods)
            conflicts = [k for k, gs in key_to_groups.items() if len(gs) > 1]
            perf.log(
                "worker",
                "chord_modgroups",
                groups=len(groups),
                conflicts=",".join(conflicts) if conflicts else "none",
            )
        order = sorted(groups.keys(), key=lambda mods: (len(mods), mods))
        mod_delay = 0.0 if self._silent_mode else self._sheet.modifier_delay
        for mod_set in order:
            settle_until = 0.0
            if not self._silent_mode:
                for stroke in groups[mod_set]:
                    last = self._last_release_at.get(stroke.key)
                    if last is not None:
                        settle_until = max(settle_until, last + self.SETTLE_AFTER_RELEASE)
            if settle_until > 0:
                wait = settle_until - time.perf_counter()
                if wait > 0:
                    time.sleep(wait)
            for modifier in mod_set:
                backend.key_down(modifier)
                if mod_delay:
                    time.sleep(mod_delay)
            for stroke in groups[mod_set]:
                self._press_active(backend, stroke.key)
                self._active_label_counts[stroke.label] = (
                    self._active_label_counts.get(stroke.label, 0) + 1
                )
            for modifier in reversed(mod_set):
                if mod_delay:
                    time.sleep(mod_delay)
                backend.key_up(modifier)

    def _stroke_up(self, backend: KeyBackend, stroke: KeyStroke) -> None:
        self._release_active(backend, stroke.key)
        count = self._active_label_counts.get(stroke.label, 0)
        if count <= 1:
            self._active_label_counts.pop(stroke.label, None)
        else:
            self._active_label_counts[stroke.label] = count - 1

    def _press_active(self, backend: KeyBackend, key: str) -> None:
        count = self._active_counts.get(key, 0)
        if count == 0:
            backend.key_down(key)
        self._active_counts[key] = count + 1

    def _release_active(self, backend: KeyBackend, key: str) -> None:
        count = self._active_counts.get(key, 0)
        if count <= 1:
            self._active_counts.pop(key, None)
            backend.key_up(key)
            self._last_release_at[key] = time.perf_counter()
        else:
            self._active_counts[key] = count - 1

    def _release_all(self, backend: KeyBackend) -> None:
        for key in list(self._active_counts):
            try:
                backend.key_up(key)
            except Exception:
                pass
        self._active_counts.clear()
        self._active_label_counts.clear()
        self._last_release_at.clear()
        for modifier in ("shift", "ctrl"):
            try:
                backend.key_up(modifier)
            except Exception:
                pass

    def _wait(self, seconds: float) -> bool:
        if seconds <= 0:
            return self._stop_event.is_set()
        return self._stop_event.wait(seconds)

    def _wait_until_action(self, action_seconds: float, backend: KeyBackend) -> str:
        """等到該 action 的目標時間。回傳 'stop' / 'seek' / 'speed' / 'skip' / 'go'。"""
        released_for_pause = False
        while not self._stop_event.is_set():
            with self._lock:
                currently_paused = self._paused
                seek_pending = self._seek_pending
                speed_dirty = self._speed_dirty
            if speed_dirty:
                return "speed"
            if seek_pending:
                return "seek"
            if currently_paused:
                if not released_for_pause:
                    self._release_all(backend)
                    self.active_notes.emit([])
                    released_for_pause = True
                self._stop_event.wait(0.05)
                continue
            released_for_pause = False
            target = self._started_at + action_seconds
            remaining = target - time.perf_counter()
            if remaining < -self.SKIP_THRESHOLD_SECONDS:
                return "skip"
            if remaining <= 0:
                return "go"
            self._stop_event.wait(min(remaining, 0.05))
        return "stop"


class HotkeyBridge(QObject):
    play_requested = Signal()
    stop_requested = Signal()
    pause_requested = Signal()
    dodge_requested = Signal()
    rhythm_requested = Signal()


# 全域熱鍵動作 → 中文短標籤,給 start() 組訊息用("F6 播放 / F7 停止 ...")。
_HOTKEY_ACTION_LABELS = {
    "play": "播放",
    "stop": "停止",
    "pause": "暫停",
    "dodge": "閃避",
    "rhythm": "音游",
}


class GlobalHotkeys:
    def __init__(self, bridge: HotkeyBridge) -> None:
        self._bridge = bridge
        self._listener = None
        self._automation_enabled = False
        self._current_map: dict[str, str] = {}

    def start(
        self,
        hotkey_map: dict[str, str] | None = None,
        automation_enabled: bool = False,
    ):
        """以 hotkey_map 動態註冊全域熱鍵。

        hotkey_map: {"play": "f6", "stop": "f7", "pause": "f8",
                     "dodge": "f10", "rhythm": "f11"}
        值為空字串視為停用該動作的熱鍵。未提供的 action 不註冊。

        為了向後相容,hotkey_map=None 時退回原本 hardcode 預設。
        """
        try:
            from pynput import keyboard
        except ImportError:
            return False, "未安裝 pynput,全域快捷鍵停用"

        self._automation_enabled = bool(automation_enabled)
        if hotkey_map is None:
            hotkey_map = {
                "play": "f6",
                "stop": "f7",
                "pause": "f8",
                "dodge": "f10",
                "rhythm": "f11",
            }
        self._current_map = dict(hotkey_map)

        mapping = {}
        label_parts = []
        for action, key in hotkey_map.items():
            if not key:
                continue
            signal_name = f"{action}_requested"
            if not hasattr(self._bridge, signal_name):
                continue
            normalized = str(key).strip().lower()
            if not normalized:
                continue
            mapping[f"<{normalized}>"] = getattr(self._bridge, signal_name).emit
            label_parts.append(
                f"{normalized.upper()} {_HOTKEY_ACTION_LABELS.get(action, action)}"
            )

        if not mapping:
            return False, "全域快捷鍵未啟動(無有效熱鍵設定)"

        try:
            self._listener = keyboard.GlobalHotKeys(mapping)
            self._listener.start()
        except Exception as exc:  # noqa: BLE001
            self._listener = None
            return False, f"全域快捷鍵啟動失敗:{exc}"

        return True, "全域快捷鍵已啟動 (" + " / ".join(label_parts) + ")"

    def restart(
        self,
        hotkey_map: dict[str, str] | None = None,
        automation_enabled: bool | None = None,
    ):
        """stop + start。任一參數未指定就沿用上次的值。"""
        self.stop()
        if hotkey_map is None:
            hotkey_map = dict(self._current_map) if self._current_map else None
        if automation_enabled is None:
            automation_enabled = self._automation_enabled
        return self.start(hotkey_map=hotkey_map, automation_enabled=automation_enabled)

    def stop(self) -> None:
        if self._listener is not None:
            self._listener.stop()
            self._listener = None
