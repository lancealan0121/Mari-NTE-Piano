# NTE Piano - 自動演奏與自動化工具
# Copyright (C) 2026  Yulun
# Licensed under GPL-3.0-or-later. See LICENSE.
"""nte_settings — `SettingsManager` 設定管理。

對外提供:
    SettingsManager   JSON-backed key/value store, atomic 寫入 + 壞檔備份

載入採「schema-merge 自動適應」: 只吸收 _DEFAULTS 內的 key, 磁碟上多出的(舊版
殘留、未來版本才有的)一律忽略, 缺的補預設。因此不需要版本號或遷移邏輯——任何
舊 / 新 / 跨版本的 settings.json 都能安全載入, 降版也不會清空設定。

依賴 nte_paths.SETTINGS_PATH 決定落盤位置(~/.nte_piano/settings.json)。
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import sys
from pathlib import Path

from nte_paths import SETTINGS_PATH


class SettingsManager:
    """JSON-backed key/value store at ~/.nte_piano/settings.json.

    壞檔自動備份回退預設;atomic 寫入避免半寫檔。
    """

    _DEFAULTS = {
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
        # 自動化全域熱鍵總開關(F6-F8 播放控制熱鍵)
        "automation_hotkeys_enabled": False,
        # 自動化監控視窗(NTE Checker + log dock)
        "automation_dock_visible": False,
        # 遊戲失焦時自動把 HTGame.exe 靜音(pycaw 控制 audio session)
        "mute_on_focus_loss": False,
        # Piano Roll 依音高排序顯示。預設 False = H 在頂、各段內按簡譜 1..7;
        # True = 反轉各段內 12 半音,讓 H7(MIDI 95) 在最頂、L1(MIDI 60) 在最底,
        # 整個 y 軸嚴格按 MIDI 由高到低排列。
        "pitch_sort_mode": False,
        # 粉爪大劫案便利功能。常駐 helper,透過 GetAsyncKeyState polling 不註冊
        # 全域 hotkey(否則會搶 F 鍵)。只在 NTE 視窗為前景時生效,編輯器打字不會
        # 誤觸發。滾輪固定啟用(整個功能叫「快速拾取」就是 F 連點 + 滾輪交替)。
        "heist_enabled": False,
        "heist_trigger_key": "f",
        "heist_auto_mode": False,
        # 全自動 toggle 熱鍵 — 遊戲為前景時按一下即切換 _auto_mode。
        # 空字串視為停用此熱鍵。預設 F8(避開 F6/F7/F10/F11 已用)。
        "heist_auto_mode_hotkey": "f8",
        # Piano Roll 重繪 FPS;30/60/120 三檔。
        # 預設 60 兼顧流暢與功耗;120 對應高更新率螢幕或對延遲敏感的使用者。
        "roll_fps": 60,
        # 底部遊戲鋼琴鍵盤是否顯示。預設 True(維持原本看到的版面)。
        "show_piano_keyboard": True,
        # 播放時自動聚焦遊戲視窗。預設 True(維持原本行為)。
        "focus_game_on_play": True,
        # 動畫效果總開關:note 動畫 + 設定面板/音樂編輯區平滑捲動/縮放。
        # 預設 True;關掉後一切瞬間切換、無過場動畫。此鍵持久化(不在 _RESET_ON_LOAD)。
        "animations_enabled": True,
        # 確認對話框開關。預設都 True 維持原本「保護性提示」行為。
        # confirm_discard_unsaved=False:切歌/開檔/匯入/關閉視窗時不再詢問,直接覆蓋。
        # confirm_delete_song=False:刪歌不再詢問,點到就立刻刪(此檔離開磁碟)。
        "confirm_discard_unsaved": True,
        "confirm_delete_song": True,
        # 啟動 5 秒後背景查 GitHub Releases latest tag,有新版會跳提示對話框。
        # 6 小時節流(last_update_check_ts)避免頻繁打 API;按「略過此版本」
        # 會寫入 update_skip_version,該版本以前的自動提示會被吃掉(手動檢查不受影響)。
        "auto_update_check": True,
        "last_update_check_ts": 0,
        "update_skip_version": "",
        # 本機鋼琴音色播放(assets/sounds/piano/*.ogg)。
        # piano_sound_enabled:播放譜面時是否同步用 QSoundEffect 出聲;關掉就只送鍵不發聲。
        # piano_sound_volume:0.0~1.0,UI 用 0~100 slider 內部 /100 落盤。
        # preview_mode:預先聆聽模式 — worker 走完整 schedule 但不送任何鍵、不聚焦遊戲視窗,
        # 只發本機音。持久化記住上次狀態(不再每次啟動歸零)。
        "piano_sound_enabled": True,
        "piano_sound_volume": 0.7,
        "preview_mode": False,
        # 全域熱鍵可設定。hotkey_*:KeybindCaptureWidget 改設,空字串表示停用該熱鍵。
        "hotkey_play": "f6",
        "hotkey_stop": "f7",
        "hotkey_pause": "f8",
    }

    # 「執行中功能」開關 — 每次啟動強制歸 False,不論 settings.json 上次存什麼。
    # 使用者在 menu 勾選會即時落盤(維持當下 session 行為),但下次重開又是關的。
    # 數值/按鍵字串/靜音之類的純設定不在此列。
    _RESET_ON_LOAD = (
        "automation_hotkeys_enabled",
        "automation_dock_visible",
        "heist_enabled",
        "heist_auto_mode",
    )

    def __init__(self, path: Path = SETTINGS_PATH) -> None:
        self._path = path
        self._data = dict(self._DEFAULTS)
        self._load()

    def _load(self) -> None:
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
        # schema-merge:只吸收 _DEFAULTS 內的 key。磁碟上多出的(舊版殘留、未來
        # 版本才有的)一律忽略,缺的維持預設。任何版本的 settings.json 都能安全
        # 載入,降版也不會清空設定。
        needs_resave = set(data.keys()) != set(self._DEFAULTS)
        for key, value in data.items():
            if key in self._DEFAULTS:
                self._data[key] = value
        if self._apply_session_reset(persisted=data):
            needs_resave = True
        # 磁碟 key 與正規 schema 不一致(殘留舊 key 或缺新 key),落盤一次正規化。
        if needs_resave:
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
