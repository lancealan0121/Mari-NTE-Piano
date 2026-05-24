# NTE Piano - 自動演奏與自動化工具
# Copyright (C) 2026  Yulun
# Licensed under GPL-3.0-or-later. See LICENSE.
"""nte_perf — 效能追蹤日誌(對外:perf, init_perf_from_env)。

設定 NTE_PERF=1 啟用,日誌寫到 logs/perf_YYYYMMDD_HHMMSS.log。
disabled 時所有 log 呼叫都是一個 attribute load + bool 比較,接近零成本。

事件格式(每行):
    [ +12.345ms] thread=Qt-1 cat=worker ev=action_go scheduled=1.000 drift=+0.4 ...

讀法:
    drift = 實際發生秒數 - (started_at + scheduled);正值表示遲到。
    若 worker.action_go 的 drift 持續上升,代表 _chord_down 的 sleep
    讓播放越跑越落後。
"""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any


class PerfLogger:
    __slots__ = ("_lock", "_file", "_origin", "_enabled", "_path")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._file = None
        self._origin: float | None = None
        self._enabled = False
        self._path: Path | None = None

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def path(self) -> Path | None:
        return self._path

    def enable(self, log_dir: Path) -> Path | None:
        with self._lock:
            if self._file is not None:
                return self._path
            try:
                log_dir = Path(log_dir)
                log_dir.mkdir(parents=True, exist_ok=True)
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                p = log_dir / f"perf_{ts}.log"
                self._file = open(p, "w", encoding="utf-8", buffering=1)
                self._origin = time.perf_counter()
                self._enabled = True
                self._path = p
                self._file.write(
                    f"# nte_perf opened {datetime.now().isoformat()}\n"
                    f"# format: [    +ms] thread=NAME cat=CATEGORY ev=EVENT k=v ...\n"
                )
                return p
            except OSError:
                self._file = None
                self._enabled = False
                return None

    def disable(self) -> None:
        with self._lock:
            self._enabled = False
            if self._file is not None:
                try:
                    self._file.close()
                except OSError:
                    pass
                self._file = None

    def log(self, category: str, event: str, **kv: Any) -> None:
        if not self._enabled:
            return
        ms = (time.perf_counter() - self._origin) * 1000.0  # type: ignore[operator]
        thread = threading.current_thread().name
        # 用簡單 key=value(不轉 JSON),好 grep 也好讀。
        parts = [f"[{ms:10.3f}ms]", f"thread={thread}", f"cat={category}", f"ev={event}"]
        for k, v in kv.items():
            parts.append(f"{k}={v}")
        line = " ".join(parts) + "\n"
        try:
            self._file.write(line)  # type: ignore[union-attr]
        except (OSError, ValueError):
            # 檔案可能在 close 中,直接吞掉
            pass


perf = PerfLogger()


def init_perf_from_env(default_log_dir: Path | None = None) -> Path | None:
    """啟動入口呼叫:看 NTE_PERF 是否要打開。回傳 log 路徑或 None。"""
    flag = os.environ.get("NTE_PERF", "").strip().lower()
    if flag not in ("1", "true", "yes", "on"):
        return None
    target = default_log_dir or Path("logs")
    return perf.enable(target)
