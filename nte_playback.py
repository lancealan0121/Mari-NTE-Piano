# NTE Piano - 自動演奏與自動化工具
# Copyright (C) 2026  Yulun
# Licensed under GPL-3.0-or-later. See LICENSE.
"""nte_playback — 按鍵後端、Win32 視窗整合與播放 Worker。

對外提供:
    Win32:
        _configure_winapi / is_running_as_admin
        WindowInfo / find_game_window / focus_window /
        is_window_alive / foreground_hwnd / is_target_foreground
    後端:
        KeyBackend / PynputBackend / PyDirectInputBackend
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

from PySide6.QtCore import QObject, Signal, Slot

from nte_dsl import KeyStroke, NoteEvent, Sheet


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
        info = WindowInfo(int(hwnd), title, int(pid.value), _process_name(pid.value))
        if is_game_window(info):
            found.append(info)
            return False
        return True

    enum_callback = _ENUM_WINDOWS_PROC(callback)
    user32.EnumWindows(enum_callback, 0)
    return found[0] if found else None


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


def create_backend(name: str) -> KeyBackend:
    if name == PynputBackend.name:
        return PynputBackend()
    if name == PyDirectInputBackend.name:
        return PyDirectInputBackend()
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
    ):
        super().__init__()
        self._sheet = sheet
        self._start_delay = start_delay
        self._target_hwnd = target_hwnd
        self._focus_before_play = focus_before_play
        self._speed = max(0.1, float(speed))
        self._initial_offset = max(0.0, float(initial_offset_seconds)) / self._speed
        self._auto_trim_leading = bool(auto_trim_leading)
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
            backend, _ = create_backend_with_fallback()
            actions = self._build_schedule()
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
                    i += 1
                    continue

                if action.kind == "progress":
                    self.progress.emit(
                        action.event_index,
                        f"{action.event.track}: {action.event.source}",
                        [stroke.label for stroke in action.event.strokes],
                    )
                elif action.kind == "down":
                    self._chord_down(backend, action.event.strokes)
                    self.active_notes.emit(sorted(self._active_label_counts))
                elif action.kind == "up":
                    for stroke in reversed(action.event.strokes):
                        self._stroke_up(backend, stroke)
                    self.active_notes.emit(sorted(self._active_label_counts))
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
        if self._target_hwnd and self._focus_before_play:
            try:
                focus_window(self._target_hwnd)
            except Exception:
                pass

    SETTLE_AFTER_RELEASE = 0.03

    def _settle_for_key(self, key: str) -> None:
        last = self._last_release_at.get(key)
        if last is None:
            return
        wait = self.SETTLE_AFTER_RELEASE - (time.perf_counter() - last)
        if wait > 0:
            time.sleep(wait)

    def _stroke_down(self, backend: KeyBackend, stroke: KeyStroke) -> None:
        self._settle_for_key(stroke.key)
        for modifier in stroke.modifiers:
            backend.key_down(modifier)
            if self._sheet.modifier_delay:
                time.sleep(self._sheet.modifier_delay)

        self._press_active(backend, stroke.key)
        self._active_label_counts[stroke.label] = self._active_label_counts.get(stroke.label, 0) + 1

        for modifier in reversed(stroke.modifiers):
            if self._sheet.modifier_delay:
                time.sleep(self._sheet.modifier_delay)
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
        order = sorted(groups.keys(), key=lambda mods: (len(mods), mods))
        for mod_set in order:
            settle_until = 0.0
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
                if self._sheet.modifier_delay:
                    time.sleep(self._sheet.modifier_delay)
            for stroke in groups[mod_set]:
                self._press_active(backend, stroke.key)
                self._active_label_counts[stroke.label] = (
                    self._active_label_counts.get(stroke.label, 0) + 1
                )
            for modifier in reversed(mod_set):
                if self._sheet.modifier_delay:
                    time.sleep(self._sheet.modifier_delay)
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


class GlobalHotkeys:
    def __init__(self, bridge: HotkeyBridge) -> None:
        self._bridge = bridge
        self._listener = None
        self._automation_enabled = False

    def start(self, automation_enabled: bool = False):
        try:
            from pynput import keyboard
        except ImportError:
            return False, "未安裝 pynput,全域快捷鍵停用"

        self._automation_enabled = bool(automation_enabled)
        mapping = {
            "<f6>": self._bridge.play_requested.emit,
            "<f7>": self._bridge.stop_requested.emit,
            "<f8>": self._bridge.pause_requested.emit,
            # F10/F11 改為恆註冊;automation_enabled 參數保留供 API 相容,
            # 不再決定是否註冊熱鍵。子任務各自有 _active toggle 決定是否反應。
            "<f10>": self._bridge.dodge_requested.emit,
            "<f11>": self._bridge.rhythm_requested.emit,
        }

        try:
            self._listener = keyboard.GlobalHotKeys(mapping)
            self._listener.start()
        except Exception as exc:  # noqa: BLE001
            self._listener = None
            return False, f"全域快捷鍵啟動失敗:{exc}"
        if self._automation_enabled:
            return True, "全域快捷鍵已啟動 (F6 播放 / F7 停止 / F8 暫停 / F9 釣魚 / F10 閃避 / F11 音游)"
        return True, "全域快捷鍵已啟動 (F6 播放 / F7 停止 / F8 暫停)"

    def restart(self, automation_enabled: bool):
        self.stop()
        return self.start(automation_enabled=automation_enabled)

    def stop(self) -> None:
        if self._listener is not None:
            self._listener.stop()
            self._listener = None
