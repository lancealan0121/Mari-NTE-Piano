# NTE Piano - 自動演奏與自動化工具
# Copyright (C) 2026  Yulun
# Licensed under GPL-3.0-or-later. See LICENSE.
"""nte_theme — 主題色彩、配色 style、easing 函式、徑向漸層 helper。

對外提供:
    THEME                  全域主題色字典(會被 apply_note_color_style 動態覆寫 H/M/L)
    NOTE_COLOR_STYLES      預設配色字典(default / ocean / sunset / forest / mono / neon / candy / custom)
    apply_note_color_style 套用配色到 THEME, 回傳實際 style key
    _derive_active_color   從基色推導 active 變體 (lighter 135%)
    _blend_color           兩個 QColor 線性混合
    _ease_out_quad / _ease_in_out_sine / _ease_out_back  easing 函式
    _radial_alpha_gradient 中心 alpha 高、邊緣透明的徑向漸層

純 helper, 不依賴任何 nte_* 模組。
"""
from __future__ import annotations

import math

from PySide6.QtCore import QPointF
from PySide6.QtGui import QColor, QRadialGradient


THEME = {
    "bg": "#16181d",
    "panel": "#1a1d23",
    "panel_alt": "#1f232a",
    "fg": "#e6e8ec",
    "fg_dim": "#9aa1ad",
    "fg_subtle": "#6b7280",
    "accent": "#ff7a59",
    "stop": "#ff4d6d",
    "play": "#4d8cff",
    "grid": "#262a33",
    "grid_strong": "#3a414d",
    "cursor": "#ff4d6d",
    "key_face": "#f4f5f7",
    "key_stroke": "#9ea4b0",
    "key_text": "#1f232a",
    "key_letter_bg": "#1f232a",
    "key_letter_fg": "#e6e8ec",
    "H": "#ff7a59",
    "M": "#4dd0c2",
    "L": "#8a7cff",
    "H_active": "#ffd166",
    "M_active": "#a4f7c3",
    "L_active": "#c5b6ff",
}


# 音符配色預設;每個 style 提供 (H, M, L, H_active, M_active, L_active)
NOTE_COLOR_STYLES: dict[str, dict[str, str]] = {
    "default": {
        "label": "預設(橘綠紫)",
        "H": "#ff7a59", "M": "#4dd0c2", "L": "#8a7cff",
        "H_active": "#ffd166", "M_active": "#a4f7c3", "L_active": "#c5b6ff",
    },
    "ocean": {
        "label": "海洋(冷色)",
        "H": "#7dd3fc", "M": "#60a5fa", "L": "#a78bfa",
        "H_active": "#bae6fd", "M_active": "#bfdbfe", "L_active": "#ddd6fe",
    },
    "sunset": {
        "label": "日落(暖色)",
        "H": "#fbbf24", "M": "#fb7185", "L": "#f472b6",
        "H_active": "#fde68a", "M_active": "#fecdd3", "L_active": "#fbcfe8",
    },
    "forest": {
        "label": "森林(綠系)",
        "H": "#a3e635", "M": "#22c55e", "L": "#14b8a6",
        "H_active": "#d9f99d", "M_active": "#86efac", "L_active": "#99f6e4",
    },
    "mono": {
        "label": "單色(灰白)",
        "H": "#e5e7eb", "M": "#9ca3af", "L": "#6b7280",
        "H_active": "#f9fafb", "M_active": "#d1d5db", "L_active": "#9ca3af",
    },
    "neon": {
        "label": "霓虹(高對比)",
        "H": "#ff00aa", "M": "#00f5d4", "L": "#9b5de5",
        "H_active": "#ff70c8", "M_active": "#7df9e6", "L_active": "#c89bee",
    },
    "candy": {
        "label": "糖果(粉嫩)",
        "H": "#fda4af", "M": "#a5f3fc", "L": "#c4b5fd",
        "H_active": "#fecdd3", "M_active": "#cffafe", "L_active": "#ddd6fe",
    },
    # custom 的 H/M/L 由主視窗從 settings 動態餵入 apply_note_color_style;
    # 這裡只給一份 fallback 預設(同 default),避免 callsite 沒帶 custom_colors
    # 卻又選了 custom 時崩潰。_active 變體在 apply 內由基色 lighter 派生。
    "custom": {
        "label": "自訂",
        "H": "#ff7a59", "M": "#4dd0c2", "L": "#8a7cff",
        "H_active": "#ffd166", "M_active": "#a4f7c3", "L_active": "#c5b6ff",
    },
}


def _derive_active_color(base_hex: str) -> str:
    """從基色派生 active 變體(亮度 +35%)。custom style 用。"""
    c = QColor(base_hex)
    if not c.isValid():
        return base_hex
    return c.lighter(135).name()


def apply_note_color_style(name: str, custom_colors: dict | None = None) -> str:
    """套用配色到 THEME 並回傳實際使用的 style key (找不到時退回 default)。

    name="custom" 時優先從 custom_colors 取 H/M/L 三色,_active 變體由基色派生
    (lighter 135%);custom_colors 為 None 時退回 custom entry 內的 fallback。
    """
    style = NOTE_COLOR_STYLES.get(name) or NOTE_COLOR_STYLES["default"]
    actual = name if name in NOTE_COLOR_STYLES else "default"
    if actual == "custom" and custom_colors:
        base = {
            "H": str(custom_colors.get("H", style["H"])),
            "M": str(custom_colors.get("M", style["M"])),
            "L": str(custom_colors.get("L", style["L"])),
        }
        for k in ("H", "M", "L"):
            THEME[k] = base[k]
            THEME[f"{k}_active"] = _derive_active_color(base[k])
        return actual
    for key in ("H", "M", "L", "H_active", "M_active", "L_active"):
        THEME[key] = style[key]
    return actual


def _blend_color(c0: QColor, c1: QColor, t: float) -> QColor:
    """線性混合兩個 QColor，t=0 回傳 c0、t=1 回傳 c1。"""
    t = max(0.0, min(1.0, t))
    return QColor(
        int(c0.red() + (c1.red() - c0.red()) * t),
        int(c0.green() + (c1.green() - c0.green()) * t),
        int(c0.blue() + (c1.blue() - c0.blue()) * t),
        int(c0.alpha() + (c1.alpha() - c0.alpha()) * t),
    )


def _ease_out_quad(t: float) -> float:
    """OutQuad 緩動，給光環/光暈衰減用。"""
    t = max(0.0, min(1.0, t))
    return 1.0 - (1.0 - t) * (1.0 - t)


def _ease_in_out_sine(t: float) -> float:
    """SineInOut 緩動，給呼吸動畫用，輸出 0..1。"""
    t = max(0.0, min(1.0, t))
    return 0.5 - 0.5 * math.cos(math.pi * t)


def _ease_out_back(t: float) -> float:
    """OutBack 緩動，回彈到 1.0 前略超過再退回，給按鍵彈性釋放用。"""
    t = max(0.0, min(1.0, t))
    c1 = 3.0
    c3 = c1 + 1.0
    return 1.0 + c3 * (t - 1.0) ** 3 + c1 * (t - 1.0) ** 2


def _radial_alpha_gradient(
    cx: float,
    cy: float,
    radius: float,
    inner_color: QColor,
    inner_alpha: int = 200,
    outer_alpha: int = 0,
) -> QRadialGradient:
    """建立中心 alpha 高、邊緣透明（或反之）的徑向漸層。"""
    g = QRadialGradient(QPointF(cx, cy), max(0.5, radius))
    inner = QColor(inner_color)
    inner.setAlpha(max(0, min(255, inner_alpha)))
    outer = QColor(inner_color)
    outer.setAlpha(max(0, min(255, outer_alpha)))
    g.setColorAt(0.0, inner)
    g.setColorAt(1.0, outer)
    return g
