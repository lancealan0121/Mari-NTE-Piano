# NTE Piano - 自動演奏與自動化工具
# Copyright (C) 2026  Yulun
# Licensed under GPL-3.0-or-later. See LICENSE.
"""NTE Piano Auto Player — CLI 入口。

實際 GUI 邏輯在 nte_main_window.PianoPlayerWindow,本檔只負責 argparse / 啟動 QApplication /
seed 預設譜面 / 設定 perf 日誌。所有 import 都集中在這裡才把 Qt 主迴圈打起來。

新增功能優先延伸對應 nte_*.py 模組,不要把無關邏輯倒回這裡。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PySide6.QtGui import QFont, QIcon
from PySide6.QtWidgets import QApplication

from nte_main_window import APP_TITLE, PianoPlayerWindow
from nte_paths import _resource_path, _seed_default_songs, _user_data_dir
from nte_perf import init_perf_from_env


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
