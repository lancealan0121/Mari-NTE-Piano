# NTE Piano - 自動演奏與自動化工具
# Copyright (C) 2026  Yulun
# Licensed under GPL-3.0-or-later. See LICENSE.
"""nte_paths — 路徑 helpers 與檔案副檔名常數。

對外提供:
    _is_frozen / _resource_path / _user_data_dir / _seed_default_songs
    SETTINGS_DIR / SETTINGS_PATH (使用者設定 JSON 路徑)
    MIDI_EXTENSIONS / MUSICXML_EXTENSIONS / MSCZ_EXTENSIONS / DSL_EXTENSIONS

處理 PyInstaller 打包後 (frozen) 與 dev 環境的差異:
    - dev:Path(__file__).resolve().parent 同時當 bundled 資源根 + 使用者資料根
    - frozen onedir / onefile:
        * bundled 資源在 sys._MEIPASS(onedir 6.x 指向 _internal/、onefile 指向
          解壓 temp);PyInstaller 5.x 老 onedir 沒設 _MEIPASS → fallback exe 旁
        * 使用者資料(歌曲、autosave、log、npy cache)放 exe 同目錄,使用者能直接
          看到並丟譜面、看 log
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path


SETTINGS_DIR = Path.home() / ".nte_piano"
SETTINGS_PATH = SETTINGS_DIR / "settings.json"

MIDI_EXTENSIONS = {".mid", ".midi"}
MUSICXML_EXTENSIONS = {".mxl", ".xml", ".musicxml"}
MSCZ_EXTENSIONS = {".mscz", ".mscx"}
DSL_EXTENSIONS = {".txt", ".score"}


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
