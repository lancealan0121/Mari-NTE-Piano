# NTE Piano - 自動演奏與自動化工具
# Copyright (C) 2026  Yulun
# Licensed under GPL-3.0-or-later. See LICENSE.
"""nte_transport_button — 播放控制 transport 按鈕。

對外提供:
    _TransportButton  prev / play / pause / stop / next 按鈕, 用 QStyle 標準圖示

依賴 nte_theme.THEME 取出 play/stop/fg 三種色彩。
"""
from __future__ import annotations

from PySide6.QtCore import QEvent, QSize, Qt
from PySide6.QtGui import QColor, QFont, QIcon, QPainter
from PySide6.QtWidgets import QPushButton, QStyle, QWidget

from nte_theme import THEME


class _TransportButton(QPushButton):
    """播放控制按鈕：用 QStyle 標準圖示代替 emoji 文字。"""

    _ICON_MAP: dict[str, QStyle.StandardPixmap] = {
        "prev": QStyle.StandardPixmap.SP_MediaSkipBackward,
        "play": QStyle.StandardPixmap.SP_MediaPlay,
        "pause": QStyle.StandardPixmap.SP_MediaPause,
        "stop": QStyle.StandardPixmap.SP_MediaStop,
        "next": QStyle.StandardPixmap.SP_MediaSkipForward,
    }

    def __init__(
        self,
        icon_key: str,
        name: str,
        tip: str,
        font: QFont,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._icon_key = icon_key
        self.setObjectName(name)
        self.setFlat(True)
        self.setFont(font)
        self.setToolTip(tip)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._refresh_icon()

    def _refresh_icon(self) -> None:
        icon = self.style().standardIcon(
            self._ICON_MAP.get(self._icon_key, QStyle.StandardPixmap.SP_MediaPlay)
        )
        pixmap = icon.pixmap(QSize(22, 22))
        # 依 icon_key 選顏色:play / pause 走藍,stop 走紅,其他用前景色。
        if self._icon_key in ("play", "pause"):
            tint = THEME["play"]
        elif self._icon_key == "stop":
            tint = THEME["stop"]
        else:
            tint = THEME["fg"]
        painter = QPainter(pixmap)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
        painter.fillRect(pixmap.rect(), QColor(tint))
        painter.end()
        self.setIcon(QIcon(pixmap))
        self.setIconSize(QSize(22, 22))

    def changeEvent(self, event: QEvent | None) -> None:
        if event is not None and event.type() == QEvent.Type.StyleChange:
            self._refresh_icon()
        super().changeEvent(event)
