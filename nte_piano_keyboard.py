# NTE Piano - 自動演奏與自動化工具
# Copyright (C) 2026  Yulun
# Licensed under GPL-3.0-or-later. See LICENSE.
"""nte_piano_keyboard — 仿遊戲內 3x12 圓形音符鍵盤 widget.

對外提供:
    PianoKeyboardWidget  3x12 鍵盤;set_active_labels() 餵入目前按下的 NoteEvent labels,
                         widget 自動畫 attack/release/halo/ring 動畫;點擊發 note_clicked signal.

依賴:
    PySide6.QtCore/QtGui/QtWidgets
    nte_dsl (BASE_KEYS / CHROMATIC_LAYOUT / ROW_LABELS)
    nte_theme (THEME + 色彩 helper + easing + _radial_alpha_gradient)
"""
from __future__ import annotations

import time

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QTimer, Signal, Slot
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QRadialGradient,
)
from PySide6.QtWidgets import QSizePolicy, QWidget

from nte_dsl import BASE_KEYS, CHROMATIC_LAYOUT, ROW_LABELS
from nte_perf import perf
from nte_theme import (
    THEME,
    _blend_color,
    _ease_out_back,
    _ease_out_quad,
    _radial_alpha_gradient,
)


class PianoKeyboardWidget(QWidget):
    """仿遊戲內 3x12 圓形音符鍵盤；被按下的音符以高亮色填充，支援滑鼠點擊插入。"""

    note_clicked = Signal(str)

    ATTACK_SECONDS = 0.08
    RELEASE_SECONDS = 0.22
    RING_SECONDS = 0.42
    PRESS_ATTACK_SECONDS = 0.10
    PRESS_RELEASE_SECONDS = 0.34

    def __init__(self, parent=None):
        super().__init__(parent)
        self._active = set()
        self._anim_state: dict[str, dict] = {}
        self._animations_enabled = True
        self._hovered_label: str | None = None
        self._anim_timer = QTimer(self)
        self._anim_timer.setInterval(16)
        self._anim_timer.timeout.connect(self._on_anim_tick)
        self.setMinimumHeight(220)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setCursor(Qt.PointingHandCursor)
        self.setMouseTracking(True)

    def sizeHint(self) -> QSize:
        return QSize(960, 240)

    def set_animations_enabled(self, enabled: bool) -> None:
        self._animations_enabled = bool(enabled)
        if not enabled:
            self._anim_state.clear()
            self._anim_timer.stop()
        self.update()

    @Slot(object)
    def set_active_labels(self, labels) -> None:
        new_set = set(labels) if labels else set()
        if new_set == self._active:
            return
        if self._animations_enabled:
            now = time.perf_counter()
            for lbl in new_set - self._active:
                self._anim_state[lbl] = {"active_since": now, "released_at": None}
            for lbl in self._active - new_set:
                st = self._anim_state.setdefault(lbl, {"active_since": now})
                st["released_at"] = now
            if self._anim_state and not self._anim_timer.isActive():
                self._anim_timer.start()
        self._active = new_set
        self.update()

    @Slot(object)
    def apply_active_delta(self, delta) -> None:
        """高頻路徑:接 worker.active_delta(adds, removes),只動差集 + update。

        避免每次 down/up 都跨 thread 跑 sorted full set。
        """
        adds, removes = delta
        if not adds and not removes:
            return
        if perf.enabled:
            perf.log("gui", "kb_delta", adds=len(adds), removes=len(removes))
        if self._animations_enabled:
            now = time.perf_counter()
            for lbl in adds:
                self._anim_state[lbl] = {"active_since": now, "released_at": None}
            for lbl in removes:
                st = self._anim_state.setdefault(lbl, {"active_since": now})
                st["released_at"] = now
            if self._anim_state and not self._anim_timer.isActive():
                self._anim_timer.start()
        self._active.update(adds)
        self._active.difference_update(removes)
        self.update()

    def _on_anim_tick(self) -> None:
        now = time.perf_counter()
        max_release = max(self.RELEASE_SECONDS, self.RING_SECONDS, self.PRESS_RELEASE_SECONDS)
        expired = [
            lbl for lbl, st in self._anim_state.items()
            if st.get("released_at") is not None
            and now - st["released_at"] > max_release
        ]
        for lbl in expired:
            self._anim_state.pop(lbl, None)
        if not self._anim_state:
            self._anim_timer.stop()
        self.update()

    def _glow_strength(self, label: str, now: float) -> float:
        if not self._animations_enabled:
            return 1.0 if label in self._active else 0.0
        st = self._anim_state.get(label)
        if label in self._active:
            if st is None:
                return 1.0
            t = now - st["active_since"]
            return min(1.0, max(0.0, t / self.ATTACK_SECONDS))
        if st and st.get("released_at") is not None:
            t = now - st["released_at"]
            return max(0.0, 1.0 - t / self.RELEASE_SECONDS)
        return 0.0

    def _ring_strength(self, label: str, now: float) -> float:
        """釋放後的擴散光環強度，0..1，0 表示沒有光環。"""
        if not self._animations_enabled:
            return 0.0
        st = self._anim_state.get(label)
        if not st:
            return 0.0
        released_at = st.get("released_at")
        if released_at is None:
            return 0.0
        t = now - released_at
        if t < 0.0 or t > self.RING_SECONDS:
            return 0.0
        return 1.0 - t / self.RING_SECONDS

    def _press_progress(self, label: str, now: float) -> float:
        """按下強度：0=原大，1=完全縮小；釋放期間以 ease-out-back 回彈，可短暫低於 0 形成 overshoot。"""
        if not self._animations_enabled:
            return 1.0 if label in self._active else 0.0
        st = self._anim_state.get(label)
        if label in self._active:
            if st is None:
                return 1.0
            t = (now - st["active_since"]) / self.PRESS_ATTACK_SECONDS
            return _ease_out_quad(max(0.0, min(1.0, t)))
        if st and st.get("released_at") is not None:
            t = (now - st["released_at"]) / self.PRESS_RELEASE_SECONDS
            if t >= 1.0:
                return 0.0
            return 1.0 - _ease_out_back(t)
        return 0.0

    def _layout_metrics(self):
        cols = len(CHROMATIC_LAYOUT)
        rows = len(ROW_LABELS)
        margin_left, margin_right = 64, 16
        margin_top, margin_bottom = 16, 12
        usable_w = max(0, self.width() - margin_left - margin_right)
        usable_h = max(0, self.height() - margin_top - margin_bottom)
        if usable_w <= 0 or usable_h <= 0:
            return None
        cell_w = usable_w / cols
        cell_h = usable_h / rows
        diameter = max(16.0, min(cell_w * 0.82, cell_h * 0.66))
        return {
            "rows": rows,
            "cols": cols,
            "margin_left": margin_left,
            "margin_top": margin_top,
            "cell_w": cell_w,
            "cell_h": cell_h,
            "diameter": diameter,
        }

    def _key_geometry(self):
        geo = {}
        metrics = self._layout_metrics()
        if metrics is None:
            return geo
        for row_index, (prefix, _) in enumerate(ROW_LABELS):
            row_y = metrics["margin_top"] + row_index * metrics["cell_h"]
            for col_index, (_, degree, accidental) in enumerate(CHROMATIC_LAYOUT):
                cx = metrics["margin_left"] + col_index * metrics["cell_w"] + metrics["cell_w"] / 2.0
                cy = row_y + metrics["cell_h"] / 2.0
                label = f"{prefix}{accidental}{degree}"
                geo[label] = (cx, cy, metrics["diameter"] / 2.0)
        return geo

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt override name.
        if event.button() != Qt.LeftButton:
            super().mousePressEvent(event)
            return
        pos = event.position()
        px, py = pos.x(), pos.y()
        for label, (cx, cy, r) in self._key_geometry().items():
            if (px - cx) ** 2 + (py - cy) ** 2 <= r * r:
                self.note_clicked.emit(label)
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt override name.
        pos = event.position()
        px, py = pos.x(), pos.y()
        new_hover: str | None = None
        for label, (cx, cy, r) in self._key_geometry().items():
            if (px - cx) ** 2 + (py - cy) ** 2 <= r * r:
                new_hover = label
                break
        if new_hover != self._hovered_label:
            self._hovered_label = new_hover
            self.update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802 - Qt override name.
        if self._hovered_label is not None:
            self._hovered_label = None
            self.update()
        super().leaveEvent(event)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), QColor(THEME["panel"]))

        metrics = self._layout_metrics()
        if metrics is None:
            return
        margin_left = metrics["margin_left"]
        margin_top = metrics["margin_top"]
        cell_w = metrics["cell_w"]
        cell_h = metrics["cell_h"]
        diameter = metrics["diameter"]

        for row_index, (prefix, row_label) in enumerate(ROW_LABELS):
            row_y = margin_top + row_index * cell_h

            painter.setPen(QColor(THEME["fg_dim"]))
            painter.setFont(QFont("Microsoft JhengHei UI", 10, QFont.Bold))
            painter.drawText(
                QRectF(0, row_y, margin_left - 10, cell_h),
                Qt.AlignVCenter | Qt.AlignRight,
                row_label,
            )

            now = time.perf_counter()
            for col_index, (display, degree, accidental) in enumerate(CHROMATIC_LAYOUT):
                cx = margin_left + col_index * cell_w + cell_w / 2.0
                cy = row_y + cell_h / 2.0
                label = f"{prefix}{accidental}{degree}"
                qwerty = BASE_KEYS[prefix][degree]
                glow = self._glow_strength(label, now)
                ring = self._ring_strength(label, now)
                press = self._press_progress(label, now)
                hovered = self._hovered_label == label
                self._draw_key(
                    painter, cx, cy, diameter,
                    prefix, degree, accidental, label, qwerty,
                    glow, ring, press, hovered,
                )

    def _draw_key(
        self,
        painter,
        cx,
        cy,
        diameter,
        prefix,
        degree,
        accidental,
        label,
        qwerty,
        glow=0.0,
        ring=0.0,
        press=0.0,
        hovered=False,
    ):
        base_radius = diameter / 2.0
        scale = max(0.78, min(1.14, 1.0 - 0.20 * press))
        radius = base_radius * scale
        diameter_scaled = diameter * scale
        cy_eff = cy

        inactive_face = QColor(THEME["key_face"])
        inactive_stroke = QColor(THEME["key_stroke"])
        active_face = QColor(THEME[f"{prefix}_active"])
        active_stroke = QColor(THEME[prefix])
        face = _blend_color(inactive_face, active_face, glow * 0.42)
        stroke = _blend_color(inactive_stroke, active_stroke, glow * 0.65)
        stroke_width = 1.4 + 0.45 * glow

        if glow > 0.01:
            for radius_mul, alpha_peak in ((2.05, 30), (1.55, 55), (1.22, 95)):
                halo_radius = base_radius * radius_mul
                halo_color = QColor(active_stroke)
                halo_grad = QRadialGradient(QPointF(cx, cy_eff), halo_radius)
                inner = QColor(halo_color)
                inner.setAlpha(int(alpha_peak * glow))
                outer = QColor(halo_color)
                outer.setAlpha(0)
                halo_grad.setColorAt(0.0, inner)
                halo_grad.setColorAt(1.0, outer)
                painter.setBrush(halo_grad)
                painter.setPen(Qt.NoPen)
                painter.drawEllipse(
                    QRectF(
                        cx - halo_radius,
                        cy_eff - halo_radius,
                        halo_radius * 2,
                        halo_radius * 2,
                    )
                )

        if ring > 0.05:
            expand = 1.0 - ring
            ring_outer = base_radius * (1.30 + 0.85 * expand)
            ring_color = QColor(active_stroke)
            ring_grad = QRadialGradient(QPointF(cx, cy_eff), ring_outer)
            transparent = QColor(ring_color)
            transparent.setAlpha(0)
            peak = QColor(ring_color)
            peak.setAlpha(int(110 * ring))
            inner_ratio = base_radius * (1.0 + 0.35 * expand) / ring_outer
            ring_grad.setColorAt(0.0, transparent)
            ring_grad.setColorAt(max(0.0, inner_ratio - 0.22), transparent)
            ring_grad.setColorAt(min(1.0, inner_ratio), peak)
            ring_grad.setColorAt(1.0, transparent)
            painter.setBrush(ring_grad)
            painter.setPen(Qt.NoPen)
            painter.drawEllipse(
                QRectF(
                    cx - ring_outer,
                    cy_eff - ring_outer,
                    ring_outer * 2,
                    ring_outer * 2,
                )
            )

        shadow_offset = max(0.6, 1.6 - 0.6 * press)
        shadow_color = QColor(0, 0, 0, 70)
        painter.setBrush(shadow_color)
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(
            QPointF(cx, cy_eff + shadow_offset),
            radius * 0.96,
            radius * 0.5,
        )

        rect = QRectF(cx - radius, cy_eff - radius, diameter_scaled, diameter_scaled)
        gradient = QLinearGradient(rect.topLeft(), rect.bottomLeft())
        gradient.setColorAt(0.0, face.lighter(112))
        gradient.setColorAt(1.0, face.darker(112))
        painter.setBrush(gradient)
        painter.setPen(QPen(stroke, stroke_width))
        painter.drawEllipse(rect)

        highlight_cx = cx
        highlight_cy = cy_eff - radius * (0.45 - 0.18 * glow)
        highlight_radius = radius * 0.85
        highlight_grad = _radial_alpha_gradient(
            highlight_cx,
            highlight_cy,
            highlight_radius,
            face.lighter(160),
            inner_alpha=170,
            outer_alpha=0,
        )
        painter.setBrush(highlight_grad)
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(rect)

        bottom_shadow = _radial_alpha_gradient(
            cx,
            cy_eff + radius * 0.55,
            radius * 0.78,
            face.darker(135),
            inner_alpha=70,
            outer_alpha=0,
        )
        painter.setBrush(bottom_shadow)
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(rect)

        if hovered and glow < 0.5:
            hover_color = QColor(active_stroke)
            hover_color.setAlpha(150)
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(hover_color, 1.4))
            painter.drawEllipse(rect.adjusted(-1.0, -1.0, 1.0, 1.0))

        if glow > 0.01:
            inner_glow = QColor(active_stroke)
            inner_glow_grad = QRadialGradient(QPointF(cx, cy_eff), radius * 1.05)
            inner_alpha = int(60 * glow)
            inner_c = QColor(inner_glow)
            inner_c.setAlpha(inner_alpha)
            outer_c = QColor(inner_glow)
            outer_c.setAlpha(0)
            inner_glow_grad.setColorAt(0.0, outer_c)
            inner_glow_grad.setColorAt(0.65, outer_c)
            inner_glow_grad.setColorAt(1.0, inner_c)
            painter.setBrush(inner_glow_grad)
            painter.setPen(Qt.NoPen)
            painter.drawEllipse(rect)

        painter.setPen(QColor(THEME["key_text"]))
        number_font = QFont("Cascadia Mono", max(10, int(diameter * 0.42)), QFont.Bold)
        painter.setFont(number_font)
        painter.drawText(rect, Qt.AlignCenter, str(degree))

        if accidental:
            acc_font = QFont("Cascadia Mono", max(8, int(diameter * 0.22)), QFont.Bold)
            painter.setFont(acc_font)
            metrics = QFontMetrics(acc_font)
            text_w = metrics.horizontalAdvance(accidental)
            number_metrics = QFontMetrics(number_font)
            number_w = number_metrics.horizontalAdvance(str(degree))
            ax = cx - number_w / 2.0 - text_w - 1
            ay = cy_eff - number_metrics.height() / 2.0 + metrics.ascent() - 2
            painter.drawText(QPointF(ax, ay), accidental)

        dot_radius = max(1.6, diameter * 0.045)
        painter.setBrush(QColor(THEME["key_text"]))
        painter.setPen(Qt.NoPen)
        if prefix == "H":
            painter.drawEllipse(QPointF(cx, cy_eff - radius * 0.62), dot_radius, dot_radius)
        elif prefix == "L":
            painter.drawEllipse(QPointF(cx, cy_eff + radius * 0.62), dot_radius, dot_radius)

        letter_text = qwerty.upper()
        if accidental == "#":
            letter_text = "S+" + letter_text
            letter_bg = QColor(THEME["accent"])
            letter_fg = QColor(THEME["bg"])
        elif accidental == "b":
            letter_text = "C+" + letter_text
            letter_bg = QColor("#7fa6ff")
            letter_fg = QColor(THEME["bg"])
        else:
            letter_bg = QColor(THEME["key_letter_bg"])
            letter_fg = QColor(THEME["key_letter_fg"])

        letter_radius = max(8.0, diameter * 0.18)
        font_factor = 0.95 if not accidental else 0.62
        letter_font = QFont("Cascadia Mono", max(7, int(letter_radius * font_factor)), QFont.Bold)
        font_metrics = QFontMetrics(letter_font)
        text_w = font_metrics.horizontalAdvance(letter_text)
        letter_h = letter_radius * 2
        letter_w = max(letter_h, text_w + 10)
        letter_cx = cx
        letter_cy = cy + base_radius + letter_h / 2 + 2
        max_y = self.height() - 4
        if letter_cy + letter_h / 2 > max_y:
            letter_cy = max_y - letter_h / 2
        letter_rect = QRectF(
            letter_cx - letter_w / 2,
            letter_cy - letter_h / 2,
            letter_w,
            letter_h,
        )

        shadow_rect = letter_rect.adjusted(0.0, 1.5, 0.0, 1.5)
        shadow_path = QPainterPath()
        shadow_path.addRoundedRect(shadow_rect, letter_h / 2, letter_h / 2)
        painter.fillPath(shadow_path, QColor(0, 0, 0, 90))

        painter.setBrush(letter_bg)
        painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(letter_rect, letter_h / 2, letter_h / 2)
        painter.setPen(letter_fg)
        painter.setFont(letter_font)
        painter.drawText(letter_rect, Qt.AlignCenter, letter_text)
