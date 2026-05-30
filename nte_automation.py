# NTE Piano - 自動演奏與自動化工具
# Copyright (C) 2026  Yulun
# Licensed under GPL-3.0-or-later. See LICENSE.
"""nte_automation — 遊戲內自動化便利功能。

對齊 ok-nte 的執行模型(避免 PySide6 QThread 帶來的 OleInitialize / 跨執行緒
衝突),常駐 helper 都跑在獨立 threading.Thread,以 callback 回報狀態給 GUI。

對外提供:
    BackgroundAudioMuter - 遊戲失焦時自動把 HTGame.exe 靜音(pycaw 控制 audio session)
    HeistController      - 粉爪大劫案便利:F 連點 + 滾輪交替拾取

依賴(module 層級檢查,缺套件時對應功能標 disabled):
    pycaw + comtypes - Windows Core Audio session 控制(失焦自動靜音用)
"""
from __future__ import annotations

import ctypes
import sys
import threading
import time
from ctypes import wintypes
from typing import Callable, Optional

from nte_playback import (
    KeyBackend,
    create_backend_with_fallback,
    find_game_window,
    foreground_hwnd,
    is_target_foreground,
    is_window_alive,
    post_key_to_window,
)

# 保留歷史名稱供 HeistController 內部呼叫(_post_key_to_window),
# 實作已搬到 nte_playback.post_key_to_window。
_post_key_to_window = post_key_to_window


# ============================================================================
# 把 main thread 釘成 STA + OLE,確保 QFileDialog 開 native 對話框時 OLE 已
# 正確初始化(Qt 通常會做,但 nte_automation 在 QApplication 建立前就被 import,
# 顯式呼叫一次更保險;未初始化時 native dialog 可能 access violation)。
# ============================================================================
if sys.platform == "win32":
    try:
        ctypes.windll.ole32.OleInitialize(None)
    except Exception:  # noqa: BLE001
        pass


# ============================================================================
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
# ============================================================================
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
# Win32 滑鼠工具 — 粉爪滾輪拾取用
# ============================================================================

_AUTOMATION_WINAPI_READY = False


def _ensure_winapi_for_automation() -> None:
    """設定 mouse_event 函式簽名,供 _scroll_wheel 送滾輪事件(粉爪拾取用)。

    nte_playback._configure_winapi 設了 keybd_event 與視窗列舉相關函式,
    但沒包含 mouse_event,在這裡單獨設定。
    """
    global _AUTOMATION_WINAPI_READY
    if _AUTOMATION_WINAPI_READY or sys.platform != "win32":
        return
    user32 = ctypes.windll.user32
    user32.mouse_event.argtypes = [
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
    ]
    user32.mouse_event.restype = None
    _AUTOMATION_WINAPI_READY = True


# ============================================================================
# 失焦自動靜音(BackgroundAudioMuter)
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
# 1. 跑成獨立 daemon thread,跟失焦自動靜音不互斥(都是常駐 helper)。
# 2. 不註冊全域 hotkey — 直接用 GetAsyncKeyState polling,避免搶走 F 鍵在其他
#    場合的輸入(瀏覽器、文字編輯器都會用到 F)。
# 3. 只在 NTE 視窗為前景時送 key/scroll,避免在 piano editor 打字時誤觸發。
# 4. 原版的「快速奔跑切角色」需要 scene 偵測(is_char_at_index),這裡用不到也跑
#    不起來,故略過 — 只實作 F 連點 + 滾輪交替這兩個核心便利。
# ============================================================================


# user32.mouse_event 額外的 wheel flag
MOUSEEVENTF_WHEEL = 0x0800
WHEEL_DELTA = 120


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
