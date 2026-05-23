# NTE Piano - 自動演奏與自動化工具
# Copyright (C) 2026  Yulun
# Licensed under GPL-3.0-or-later. See LICENSE.
"""nte_checker — NTE 遊戲視窗探測器與顯示元件。

本模組提供:
    NTECheckerProbe   - 在 main thread QTimer 1Hz tick 偵測遊戲視窗狀態
    NTECheckerWidget  - 顯示視窗存在/前景燈號與基本資訊的 widget

設計刻意輕量:不截圖、不做場景顏色推測,只回答兩個問題:
    1. NTE 視窗找到了沒?
    2. NTE 視窗是前景嗎?

加上一些副資訊:視窗標題、process name、client area 解析度。

進階場景偵測由 nte_automation.py 內各 worker 自行處理(都會持續 emit
status 給 GUI log dock 顯示)。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from PySide6.QtCore import QObject, QTimer, Qt, Signal, Slot
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QHBoxLayout, QLabel, QSizePolicy, QWidget

from nte_playback import (
    find_game_window,
    foreground_hwnd,
    is_window_alive,
)


@dataclass(frozen=True)
class NTECheckerState:
    """探測結果。frozen 讓 dict-like 比較好做、跨 thread 也安全。"""

    window_found: bool = False
    is_foreground: bool = False
    hwnd: int = 0
    title: str = ""
    process_name: str = ""
    client_size: Optional[tuple[int, int]] = None
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "window_found": self.window_found,
            "is_foreground": self.is_foreground,
            "hwnd": self.hwnd,
            "title": self.title,
            "process_name": self.process_name,
            "client_size": self.client_size,
            "note": self.note,
        }


def _resolve_state() -> NTECheckerState:
    """同步取得當前狀態。安全地呼叫 nte_playback 的 Win32 wrapper。"""
    try:
        info = find_game_window()
    except Exception as exc:  # noqa: BLE001
        return NTECheckerState(note=f"視窗列舉錯誤: {exc}")
    if info is None:
        return NTECheckerState(note="未偵測到 NTE / HTGame.exe")
    if not is_window_alive(info.hwnd):
        return NTECheckerState(note="視窗已關閉")
    is_fg = False
    try:
        is_fg = foreground_hwnd() == info.hwnd
    except Exception:  # noqa: BLE001
        is_fg = False
    client_size = _client_size(info.hwnd)
    return NTECheckerState(
        window_found=True,
        is_foreground=is_fg,
        hwnd=int(info.hwnd),
        title=info.title,
        process_name=info.process_name,
        client_size=client_size,
        note="",
    )


def _client_size(hwnd: int) -> Optional[tuple[int, int]]:
    """讀 GetClientRect 得寬高。讀不到回 None。"""
    import ctypes
    import sys
    from ctypes import wintypes

    if sys.platform != "win32" or not hwnd:
        return None
    try:
        user32 = ctypes.windll.user32
        rect = wintypes.RECT()
        if not user32.GetClientRect(wintypes.HWND(hwnd), ctypes.byref(rect)):
            return None
        w = int(rect.right - rect.left)
        h = int(rect.bottom - rect.top)
        if w <= 0 or h <= 0:
            return None
        return (w, h)
    except Exception:  # noqa: BLE001
        return None


class NTECheckerProbe(QObject):
    """每秒一次主動檢查 NTE 遊戲視窗狀態。

    必須在 main thread 建立(QTimer parent 是 self,affinity 跟著 main thread)。
    state_changed signal 在每次 tick 都會 emit;widget 端可選擇只在實質變化時更新。
    """

    state_changed = Signal(object)  # NTECheckerState

    def __init__(self, parent: Optional[QObject] = None, interval_ms: int = 1000) -> None:
        super().__init__(parent)
        self._timer = QTimer(self)
        self._timer.setInterval(int(interval_ms))
        self._timer.timeout.connect(self._tick)
        self._last_state: Optional[NTECheckerState] = None

    def start(self) -> None:
        if not self._timer.isActive():
            # 立刻 tick 一次,讓 widget 不會空白等到下一秒
            self._tick()
            self._timer.start()

    def stop(self) -> None:
        if self._timer.isActive():
            self._timer.stop()

    @Slot()
    def _tick(self) -> None:
        state = _resolve_state()
        # 即使沒變化也 emit,讓 widget 能更新「最後檢查時間」
        self._last_state = state
        self.state_changed.emit(state)

    def last_state(self) -> Optional[NTECheckerState]:
        return self._last_state


class _StatusDot(QWidget):
    """固定大小的小圓點;color = 'on'/'off'/'warn'。"""

    SIZE = 12

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFixedSize(self.SIZE, self.SIZE)
        self._on = False

    def set_on(self, on: bool) -> None:
        if on != self._on:
            self._on = bool(on)
            self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802 — Qt override name
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        if self._on:
            color = QColor("#7ed957")  # 綠
            ring = QColor("#3a7a2f")
        else:
            color = QColor("#ff6b6b")  # 紅
            ring = QColor("#7a2f2f")
        rect = self.rect().adjusted(1, 1, -1, -1)
        painter.setBrush(color)
        painter.setPen(ring)
        painter.drawEllipse(rect)


class NTECheckerWidget(QWidget):
    """顯示 NTE 視窗找到/前景燈號 + 視窗資訊。

    搭配 NTECheckerProbe 用:
        probe.state_changed.connect(widget.apply_state, Qt.QueuedConnection)
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("nteCheckerWidget")
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMinimumHeight(28)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(8)

        prefix_label = QLabel("NTE")
        prefix_label.setObjectName("nteCheckerPrefix")
        prefix_label.setStyleSheet(
            "color: #c8c8c8; font-weight: 700;"
        )
        layout.addWidget(prefix_label)

        self._window_dot = _StatusDot(self)
        layout.addWidget(self._window_dot)
        self._window_label = QLabel("視窗:未偵測到")
        layout.addWidget(self._window_label)

        layout.addSpacing(12)

        self._fg_dot = _StatusDot(self)
        layout.addWidget(self._fg_dot)
        self._fg_label = QLabel("前景:無")
        layout.addWidget(self._fg_label)

        layout.addSpacing(12)

        self._info_label = QLabel("")
        self._info_label.setStyleSheet("color: #9aa0a8;")
        layout.addWidget(self._info_label, 1)

    @Slot(object)
    def apply_state(self, state: object) -> None:
        if not isinstance(state, NTECheckerState):
            return
        self._window_dot.set_on(state.window_found)
        self._fg_dot.set_on(state.is_foreground)
        if state.window_found:
            self._window_label.setText(f"視窗:已偵測 (hwnd 0x{state.hwnd:08X})")
        else:
            self._window_label.setText(f"視窗:{state.note or '未偵測到'}")
        self._fg_label.setText("前景:是" if state.is_foreground else "前景:否")

        info_parts: list[str] = []
        if state.title:
            info_parts.append(state.title)
        if state.process_name:
            info_parts.append(state.process_name)
        if state.client_size is not None:
            info_parts.append(f"{state.client_size[0]}x{state.client_size[1]}")
        self._info_label.setText("  ·  ".join(info_parts) if info_parts else "")
