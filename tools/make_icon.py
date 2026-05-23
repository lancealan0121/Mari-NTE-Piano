"""Generate NTEPiano.ico from scratch using Pillow.

3 rows × 5 dots simulating the H/M/L piano keyboard layout from THEME.
Output: assets/icon.ico (multi-size: 16/32/48/64/128/256).
"""
from __future__ import annotations

from pathlib import Path
from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "assets" / "icon.ico"

BG = (22, 24, 29, 255)      # THEME["bg"]
ROW_COLORS = [
    (255, 122, 89, 255),    # H orange
    (77, 208, 194, 255),    # M teal
    (138, 124, 255, 255),   # L purple
]


def render(size: int) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # rounded square background
    radius = int(size * 0.22)
    d.rounded_rectangle(
        (0, 0, size - 1, size - 1),
        radius=radius,
        fill=BG,
    )

    # 3 rows x 5 dots
    rows, cols = 3, 5
    margin = size * 0.18
    avail_w = size - margin * 2
    avail_h = size - margin * 2
    dot_d = min(avail_w / (cols + (cols - 1) * 0.25),
                avail_h / (rows + (rows - 1) * 0.45))
    gap_x = (avail_w - dot_d * cols) / max(1, cols - 1)
    gap_y = (avail_h - dot_d * rows) / max(1, rows - 1)

    for r in range(rows):
        cy = margin + r * (dot_d + gap_y)
        for c in range(cols):
            cx = margin + c * (dot_d + gap_x)
            d.ellipse(
                (cx, cy, cx + dot_d, cy + dot_d),
                fill=ROW_COLORS[r],
            )

    return img


def main() -> None:
    sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    master = render(256)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    master.save(OUT, format="ICO", sizes=sizes)
    print(f"Wrote {OUT}  ({OUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
