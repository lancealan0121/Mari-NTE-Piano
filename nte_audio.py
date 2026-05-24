# NTE Piano - 自動演奏與自動化工具
# Copyright (C) 2026  Yulun
# Licensed under GPL-3.0-or-later. See LICENSE.
"""nte_audio — 本機鋼琴音色播放。

把 assets/sounds/piano/*.ogg 載入成 QSoundEffect voice pool,
讓 PlaybackWorker 在送鍵的同時(或預先聆聽模式)發本機音。

對外:
    PianoSoundPlayer

設計:
    - QSoundEffect 必須在有 event loop 的 thread 建立。PianoSoundPlayer 由
      GUI 主執行緒持有與呼叫,worker 透過 signal/QueuedConnection 把 label 推
      過來,QSoundEffect 永遠在主執行緒 play。
    - 同一個 QSoundEffect 同時被 play 兩次,後者會等前者播完才接上,聽起來像
      斷音。為支援快速重彈與和弦內同 label 重疊,每個 label 配 voices_per_label
      個 instance,round-robin 輪播。
    - CHROMATIC_LAYOUT 用 b3/b7 表示 enharmonic 降音,音檔只提供 # 版本,
      因此把 Hb3 → H#2、Hb7 → H#6,L/M 同理。
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Iterable

from PySide6.QtCore import QObject, QUrl
from PySide6.QtMultimedia import QSoundEffect

from nte_perf import perf


_ENHARMONIC_FOLD = {
    "b3": "#2",
    "b7": "#6",
}


def _fold_label(label: str) -> str:
    """Hb3 → H#2 / Hb7 → H#6;其餘原樣回傳。"""
    if len(label) < 3:
        return label
    octave = label[0]
    rest = label[1:]
    folded = _ENHARMONIC_FOLD.get(rest)
    if folded is None:
        return label
    return f"{octave}{folded}"


class PianoSoundPlayer(QObject):
    """鋼琴音色 voice pool。

    用法:
        player = PianoSoundPlayer(Path("assets/sounds/piano"))
        player.set_volume(0.7)
        player.set_enabled(True)
        player.play("M3")           # 單音
        player.play_chord(["M1", "M3", "M5"])

    `play_chord` 是 slot,可直接接 PlaybackWorker.note_pressed signal。
    """

    def __init__(
        self,
        sounds_dir: Path,
        voices_per_label: int = 6,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._sounds_dir = Path(sounds_dir)
        self._voices_per_label = max(1, int(voices_per_label))
        self._enabled: bool = True
        self._volume: float = 0.7
        # label -> list[QSoundEffect],lazy 建立 — 啟動只掃檔案 index,
        # 第一次 play(label) 時才為該 label 配 voices_per_label 個 QSoundEffect。
        # 216 個 QSoundEffect.setSource 一次跑會凍 UI 約半秒~1 秒。
        self._voices: dict[str, list[QSoundEffect]] = {}
        # label -> 對應 wav/ogg 檔絕對路徑(由 _scan_index 一次性建立)。
        self._sources: dict[str, QUrl] = {}
        # label -> 下一個要使用的 voice index(round-robin)。
        self._cursors: dict[str, int] = {}
        # 沒有對應 ogg 的 label(查表後仍找不到),用來給上層提示。
        self._missing: set[str] = set()
        self._scan_index()

    # ----- 載入 -----

    def _scan_index(self) -> None:
        """掃 sounds_dir 建立 label -> QUrl 映射。不建任何 QSoundEffect。"""
        if not self._sounds_dir.exists() or not self._sounds_dir.is_dir():
            return
        # QSoundEffect 在 Windows 上只支援 WAV/AIFF;同 label 有 wav/ogg 以 wav 優先。
        for pattern in ("*.wav", "*.ogg"):
            for path in self._sounds_dir.glob(pattern):
                label = path.stem
                if label in self._sources:
                    continue
                self._sources[label] = QUrl.fromLocalFile(str(path))

    def _ensure_pool(self, label: str) -> list[QSoundEffect] | None:
        """確保某 label 的 voice pool 已建立,回傳 pool;查無對應檔案回 None。"""
        pool = self._voices.get(label)
        if pool is not None:
            return pool
        url = self._sources.get(label)
        if url is None:
            return None
        t0 = time.perf_counter() if perf.enabled else 0.0
        pool = []
        for _ in range(self._voices_per_label):
            effect = QSoundEffect(self)
            effect.setSource(url)
            effect.setVolume(self._volume)
            pool.append(effect)
        self._voices[label] = pool
        self._cursors[label] = 0
        if perf.enabled:
            perf.log(
                "audio",
                "pool_build",
                label=label,
                voices=self._voices_per_label,
                build_ms=f"{(time.perf_counter() - t0) * 1000.0:.2f}",
            )
        return pool

    # ----- 公開 API -----

    def is_ready(self) -> bool:
        """掃到至少一個來源檔就算 ready(實際 voice 是 lazy 建立的)。"""
        return bool(self._sources)

    def missing_labels(self) -> list[str]:
        """執行期間查不到 ogg 的 label 清單(累積)。"""
        return sorted(self._missing)

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)

    def set_volume(self, volume: float) -> None:
        v = float(volume)
        if v < 0.0:
            v = 0.0
        elif v > 1.0:
            v = 1.0
        self._volume = v
        # 只更新已實際建出的 pool;尚未 lazy 建的 label 之後建時會吃當下 _volume。
        for pool in self._voices.values():
            for effect in pool:
                effect.setVolume(v)

    def play(self, label: str) -> None:
        if not self._enabled or not label:
            return
        resolved = _fold_label(label)
        pool = self._ensure_pool(resolved)
        if pool is None:
            self._missing.add(label)
            if perf.enabled:
                perf.log("audio", "play_miss", label=label)
            return
        # 優先撿閒置 voice。stop()+play() 會在 sustain 中段強制截斷 PCM,產生
        # 階躍 → 喇叭 pop click;密集音時每秒被 stop 數十次,聽起來像電子 beepbeep。
        # 改成只在沒任何閒置 voice 時才 retrigger 最舊那個(不 stop,讓 Qt 自己處理)。
        for effect in pool:
            if not effect.isPlaying():
                effect.play()
                if perf.enabled:
                    perf.log("audio", "play_idle", label=resolved)
                return
        idx = self._cursors.get(resolved, 0)
        self._cursors[resolved] = (idx + 1) % len(pool)
        pool[idx].play()
        if perf.enabled:
            # 所有 voice 都在播 → 輪播覆蓋。密集和弦時最容易看到。
            perf.log("audio", "play_busy", label=resolved, voices=len(pool), idx=idx)

    def play_chord(self, labels: Iterable[str]) -> None:
        """slot:接 PlaybackWorker.note_pressed(payload=list[str])。"""
        if not self._enabled:
            return
        if labels is None:
            return
        if perf.enabled:
            labels = list(labels)  # 為了重複迭代
            perf.log("audio", "chord_recv", n=len(labels), labels=",".join(labels))
        for label in labels:
            self.play(label)
