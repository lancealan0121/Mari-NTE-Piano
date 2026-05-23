"""midi_to_dsl.py — 把 .mid / .midi 轉成本專案的 piano DSL 譜檔。

這支腳本是 piano_player.MidiImporter 的 CLI 薄殼，所有解析、量化與
音域折疊邏輯都在那邊；GUI「匯入 MIDI」走同一條路徑，行為一致。

用法：
    python tools/midi_to_dsl.py "input.mid" "examples/output.txt" [--transpose -5]

預設 --transpose 0；若要由曲目 KeySignature 自動推算改用 --auto-transpose。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nte_importers import MidiImporter  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("input", type=Path)
    ap.add_argument("output", type=Path)
    transpose_group = ap.add_mutually_exclusive_group()
    transpose_group.add_argument(
        "--transpose", type=int, default=0,
        help="移調的半音數，預設 0",
    )
    transpose_group.add_argument(
        "--auto-transpose", action="store_true",
        help="由 MIDI KeySignature 自動推算移調量",
    )
    transpose_group.add_argument(
        "--auto-center", action="store_true",
        help="範圍感知:先做 key 移調,再把右手主旋律 duration-weighted 中位數推到 M 區中央 (MIDI 78)",
    )
    ap.add_argument("--tempo", type=float, default=None, help="覆蓋曲目 BPM")
    ap.add_argument(
        "--right-prefer", choices=["H", "M", "L", "auto"], default="auto",
    )
    ap.add_argument(
        "--left-prefer", choices=["H", "M", "L", "auto"], default="L",
    )
    ap.add_argument(
        "--melody-mode",
        choices=["full", "skeleton", "melody_only", "dense"],
        default="full",
    )
    ap.add_argument("--gap", type=float, default=0.02)
    ap.add_argument("--hold", type=float, default=0.85)
    ap.add_argument("--modifier-delay", type=float, default=0.012)
    args = ap.parse_args()

    transpose = args.transpose
    if args.auto_transpose:
        transpose = MidiImporter.suggest_transpose(args.input)
    elif args.auto_center:
        transpose = MidiImporter.suggest_transpose_for_range(args.input)

    text, stats = MidiImporter.to_dsl(
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
