"""mxl_to_dsl.py — 把 MusicXML (.mxl 或 .xml) 轉成本專案的 piano DSL 譜檔。

這支腳本是 piano_player.MusicXMLImporter 的 CLI 薄殼，所有解析、轉調與
音域折疊邏輯都在那邊；GUI「匯入 MusicXML」走同一條路徑，行為一致。

用法：
    python tools/mxl_to_dsl.py "input.mxl" "examples/output.txt" [--transpose -5]

預設 --transpose -5 會把 F major 轉到 C major（譜更乾淨）。
其他常見：D major (+2 fifths) 用 -2，G major (+1) 用 -7，Bb major (-2) 用 +2。
不知道調號時改用 --auto-transpose，由曲目 fifths 自動推算。

音域對應：
    L (低音) = MIDI 60..71 (C4..B4)
    M (中音) = MIDI 72..83 (C5..B5)
    H (高音) = MIDI 84..95 (C6..B6)

每隻手會自動 octave-fold 到三排內，超出範圍的音會升/降八度。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nte_importers import MusicXMLImporter  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("input", type=Path)
    ap.add_argument("output", type=Path)
    transpose_group = ap.add_mutually_exclusive_group()
    transpose_group.add_argument(
        "--transpose", type=int, default=-5,
        help="移調的半音數，預設 -5（F→C）",
    )
    transpose_group.add_argument(
        "--auto-transpose", action="store_true",
        help="由曲目 fifths 自動推算移調量，覆蓋 --transpose",
    )
    transpose_group.add_argument(
        "--auto-center", action="store_true",
        help="範圍感知:先做 key 移調,再把右手主旋律 duration-weighted 中位數推到 M 區中央 (MIDI 78)",
    )
    ap.add_argument("--tempo", type=float, default=None, help="覆蓋曲目 BPM")
    ap.add_argument(
        "--right-prefer", choices=["H", "MH", "M", "ML", "L", "auto", "none"], default="H",
        help="右手音域偏好；auto 跟蹤前一拍最高音；MH/ML 取兩排中間值；none 跳過此手",
    )
    ap.add_argument(
        "--left-prefer", choices=["H", "MH", "M", "ML", "L", "auto", "none"], default="L",
    )
    ap.add_argument(
        "--melody-mode",
        choices=["full", "skeleton", "melody_only", "dense"],
        default="full",
        help=(
            "full=保留所有聲部；"
            "skeleton=右手取每拍最高音、左手取最低音；"
            "melody_only=只取最高音為單旋律；"
            "dense=每 (staff,voice) 拆獨立 track"
        ),
    )
    ap.add_argument("--gap", type=float, default=0.02)
    ap.add_argument("--hold", type=float, default=0.85)
    ap.add_argument("--modifier-delay", type=float, default=0.012)
    args = ap.parse_args()

    transpose = args.transpose
    if args.auto_transpose:
        root = MusicXMLImporter.load_score(args.input)
        transpose = MusicXMLImporter.suggest_transpose(root)
    elif args.auto_center:
        root = MusicXMLImporter.load_score(args.input)
        transpose = MusicXMLImporter.suggest_transpose_for_range(root)

    text, stats = MusicXMLImporter.to_dsl(
        args.input,
        transpose=transpose,
        right_prefer=args.right_prefer,
        left_prefer=args.left_prefer,
        gap=args.gap,
        hold=args.hold,
        modifier_delay=args.modifier_delay,
        tempo_override=args.tempo,
        melody_mode=args.melody_mode,
    )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(text, encoding="utf-8")

    print(f"wrote {args.output}")
    print(
        f"  tempo={stats['tempo']:g}, transpose={stats['transpose']:+d}, "
        f"mode={stats['melody_mode']}"
    )
    print(
        f"  right: {stats['right_count']} tokens "
        f"(skipped overlap: {stats['right_skip']})"
    )
    print(
        f"  left:  {stats['left_count']} tokens "
        f"(skipped overlap: {stats['left_skip']})"
    )
    if stats.get("extra_tracks"):
        print(f"  extra tracks: {stats['extra_tracks']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
