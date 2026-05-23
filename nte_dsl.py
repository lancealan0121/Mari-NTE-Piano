# NTE Piano - 自動演奏與自動化工具
# Copyright (C) 2026  Yulun
# Licensed under GPL-3.0-or-later. See LICENSE.
"""nte_dsl — 譜面 DSL 解析與資料模型。

本模組沒有 GUI / 後端依賴,只負責把譜面文字轉成不可變資料結構,
讓 piano_player.py、nte_importers.py 與工具腳本共用。

公開項目:
    BASE_KEYS / CHROMATIC_LAYOUT / ROW_LABELS / REST_TOKENS
    DEFAULT_BEATS_PER_BAR
    TRACK_ORDER / TRACK_INDEX
    KeyStroke / NoteEvent / Sheet / SheetParseError
    SheetParser
    make_stroke / _fmt_num
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field


DEFAULT_BEATS_PER_BAR = 4

BASE_KEYS: dict[str, dict[int, str]] = {
    "H": {1: "q", 2: "w", 3: "e", 4: "r", 5: "t", 6: "y", 7: "u"},
    "M": {1: "a", 2: "s", 3: "d", 4: "f", 5: "g", 6: "h", 7: "j"},
    "L": {1: "z", 2: "x", 3: "c", 4: "v", 5: "b", 6: "n", 7: "m"},
}

ROW_LABELS = (("H", "高音"), ("M", "中音"), ("L", "低音"))
CHROMATIC_LAYOUT = (
    ("1", 1, ""),
    ("#1", 1, "#"),
    ("2", 2, ""),
    ("b3", 3, "b"),
    ("3", 3, ""),
    ("4", 4, ""),
    ("#4", 4, "#"),
    ("5", 5, ""),
    ("#5", 5, "#"),
    ("6", 6, ""),
    ("b7", 7, "b"),
    ("7", 7, ""),
)
REST_TOKENS = {"0", "-", ".", "R", "REST"}


def _build_track_order():
    """Return 36-track order tuples: (prefix, label, display, accidental, degree)."""
    order = []
    for prefix, _ in ROW_LABELS:
        for display, degree, accidental in CHROMATIC_LAYOUT:
            label = f"{prefix}{accidental}{degree}"
            order.append((prefix, label, display, accidental, degree))
    return tuple(order)


TRACK_ORDER = _build_track_order()
TRACK_INDEX = {entry[1]: i for i, entry in enumerate(TRACK_ORDER)}


def _fmt_num(value: float) -> str:
    if abs(value - round(value)) < 1e-9:
        return f"{int(round(value))}"
    return f"{value:g}"


class SheetParseError(ValueError):
    """Raised when a score cannot be parsed."""


@dataclass(frozen=True)
class KeyStroke:
    key: str
    modifiers: tuple = ()
    label: str = ""

    @property
    def display(self) -> str:
        if not self.modifiers:
            return self.key.upper()
        mods = "+".join(mod.title() for mod in self.modifiers)
        return f"{mods}+{self.key.upper()}"


@dataclass(frozen=True)
class NoteEvent:
    start_beats: float
    duration_beats: float
    strokes: tuple
    source: str
    line: int
    track: str

    @property
    def is_rest(self) -> bool:
        return not self.strokes


@dataclass
class Sheet:
    tempo: float = 120.0
    beat: float = 0.5
    gap: float = 0.03
    hold: float = 0.86
    modifier_delay: float = 0.012
    beats_per_bar: int = DEFAULT_BEATS_PER_BAR
    events: list = field(default_factory=list)
    # 變速點: (start_beats, tempo) 排序列表;不含 beat 0 的初始 tempo。
    tempo_changes: list = field(default_factory=list)
    # A/B 播放範圍(秒);None = 不設,從頭播到尾。
    # 用秒不用 beats:輸入直觀,且 BPM 變動時 AB 不會跟著漂移。
    # 由 PlaybackWorker 起播時讀:A → initial_offset;B → 達到即 stop。
    play_range_start_seconds: float | None = None
    play_range_end_seconds: float | None = None

    def _sorted_tempo_changes(self):
        # 過濾 <=0 的位置 (那些屬於初始 tempo) 並排序
        return sorted(
            (b, t) for b, t in self.tempo_changes if b > 1e-9 and t > 0
        )

    def beats_to_seconds(self, beats: float) -> float:
        if beats <= 0:
            return 0.0
        if not self.tempo_changes:
            return 60.0 / self.tempo * beats
        seconds = 0.0
        prev_beat = 0.0
        prev_tempo = self.tempo if self.tempo > 0 else 120.0
        for change_beat, change_tempo in self._sorted_tempo_changes():
            if change_beat >= beats:
                break
            seconds += (change_beat - prev_beat) * 60.0 / prev_tempo
            prev_beat = change_beat
            prev_tempo = change_tempo if change_tempo > 0 else prev_tempo
        seconds += (beats - prev_beat) * 60.0 / prev_tempo
        return seconds

    def seconds_to_beats(self, seconds: float) -> float:
        if seconds <= 0:
            return 0.0
        if not self.tempo_changes:
            tempo = self.tempo if self.tempo > 0 else 120.0
            return seconds * tempo / 60.0
        remaining = seconds
        beat_cursor = 0.0
        prev_tempo = self.tempo if self.tempo > 0 else 120.0
        prev_beat = 0.0
        for change_beat, change_tempo in self._sorted_tempo_changes():
            seg_seconds = (change_beat - prev_beat) * 60.0 / prev_tempo
            if remaining <= seg_seconds:
                return prev_beat + remaining * prev_tempo / 60.0
            remaining -= seg_seconds
            prev_beat = change_beat
            prev_tempo = change_tempo if change_tempo > 0 else prev_tempo
        return prev_beat + remaining * prev_tempo / 60.0

    def tempo_at_beat(self, beats: float) -> float:
        tempo = self.tempo if self.tempo > 0 else 120.0
        for change_beat, change_tempo in self._sorted_tempo_changes():
            if change_beat <= beats + 1e-9 and change_tempo > 0:
                tempo = change_tempo
            else:
                break
        return tempo

    @property
    def playable_events(self) -> int:
        return sum(1 for event in self.events if not event.is_rest)

    @property
    def total_beats(self) -> float:
        if not self.events:
            return 0.0
        return max(event.start_beats + event.duration_beats for event in self.events)

    @property
    def total_bars(self) -> int:
        bpb = self.beats_per_bar if self.beats_per_bar > 0 else DEFAULT_BEATS_PER_BAR
        return max(1, math.ceil(self.total_beats / bpb)) if self.total_beats else 0

    def bar_of_beat(self, beats: float) -> int:
        bpb = self.beats_per_bar if self.beats_per_bar > 0 else DEFAULT_BEATS_PER_BAR
        return int(max(0.0, beats) // bpb) + 1

    def to_text(self) -> str:
        """規範化序列化,可由 SheetParser 讀回。原檔案的註解與排版不會保留。"""
        lines = [
            f"tempo {_fmt_num(self.tempo)}",
            f"beat {_fmt_num(self.beat)}",
            f"gap {_fmt_num(self.gap)}",
            f"hold {_fmt_num(self.hold)}",
            f"modifier_delay {_fmt_num(self.modifier_delay)}",
        ]
        for change_beat, change_tempo in self._sorted_tempo_changes():
            lines.append(f"tempo @{_fmt_num(change_beat)} {_fmt_num(change_tempo)}")
        if self.beats_per_bar != DEFAULT_BEATS_PER_BAR:
            lines.append(f"time {int(self.beats_per_bar)}/4")
        if self.play_range_start_seconds is not None and self.play_range_start_seconds > 0:
            lines.append(f"play_range_start {_fmt_num(self.play_range_start_seconds)}")
        if self.play_range_end_seconds is not None and self.play_range_end_seconds > 0:
            lines.append(f"play_range_end {_fmt_num(self.play_range_end_seconds)}")
        lines.append("")
        if not self.events:
            return "\n".join(lines).rstrip() + "\n"
        by_track: dict[str, list] = {}
        for event in self.events:
            by_track.setdefault(event.track, []).append(event)
        beat = self.beat if self.beat > 0 else 0.5
        for track in sorted(by_track):
            events = sorted(by_track[track], key=lambda e: e.start_beats)
            lines.append(f"track {track}")
            cursor = 0.0
            tokens = []
            for event in events:
                if event.start_beats > cursor + 1e-6:
                    gap_beats = event.start_beats - cursor
                    gap_mult = gap_beats / beat
                    tokens.append("-" if abs(gap_mult - 1.0) < 1e-6 else f"-*{_fmt_num(gap_mult)}")
                    cursor += gap_beats
                mult = event.duration_beats / beat
                suffix = "" if abs(mult - 1.0) < 1e-6 else f"*{_fmt_num(mult)}"
                if not event.strokes:
                    tokens.append(f"-{suffix}" if suffix else "-")
                elif len(event.strokes) == 1:
                    tokens.append(f"{event.strokes[0].label}{suffix}")
                else:
                    inner = "+".join(s.label for s in event.strokes)
                    tokens.append(f"[{inner}]{suffix}")
                cursor = max(cursor, event.start_beats + event.duration_beats)
            if tokens:
                lines.append(" ".join(tokens))
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"


def make_stroke(octave: str, degree: int, accidental: str = "") -> KeyStroke:
    key = BASE_KEYS[octave][degree]
    modifiers = ()
    label_accidental = ""
    if accidental == "#":
        modifiers = ("shift",)
        label_accidental = "#"
    elif accidental == "b":
        modifiers = ("ctrl",)
        label_accidental = "b"
    return KeyStroke(key=key, modifiers=modifiers, label=f"{octave}{label_accidental}{degree}")


class SheetParser:
    """Parses H/M/L numbered notation with chromatics, chords, and tracks."""

    NOTE_RE = re.compile(r"^(?P<oct>[HMLhml])(?P<acc1>[#♯bB♭]?)(?P<num>[1-7])(?P<acc2>[#♯bB♭]?)$")
    COMMANDS = {
        "tempo", "beat", "gap", "hold", "mod_delay", "modifier_delay", "time",
        "play_range_start", "play_range_end",
    }

    @classmethod
    def parse(cls, text: str) -> Sheet:
        sheet = Sheet()
        track_cursors = {"main": 0.0}
        current_track = "main"
        tempo_set = False

        for line_no, raw_line in enumerate(text.splitlines(), start=1):
            line = cls._strip_comment(raw_line).strip()
            if not line:
                continue

            tokens = line.split()
            command = tokens[0].lower()

            if command == "track":
                if len(tokens) < 2:
                    raise SheetParseError(f"第 {line_no} 行:track 需要名稱")
                current_track = tokens[1].rstrip(":")
                track_cursors.setdefault(current_track, 0.0)
                if len(tokens) == 2:
                    continue
                tokens = tokens[2:]
            elif command.rstrip(":") in cls.COMMANDS and not command.endswith(":"):
                max_cursor = max(track_cursors.values()) if track_cursors else 0.0
                cls._apply_command(sheet, command, tokens, line_no, max_cursor, tempo_set)
                if command == "tempo":
                    tempo_set = True
                continue

            track = current_track
            if tokens and tokens[0].endswith(":") and len(tokens[0]) > 1:
                track = tokens[0][:-1]
                track_cursors.setdefault(track, 0.0)
                tokens = tokens[1:]

            if not tokens:
                continue

            cursor = track_cursors.setdefault(track, 0.0)
            for token in tokens:
                if token == "|":
                    continue
                event = cls._parse_event(token, sheet.beat, line_no, track, cursor)
                sheet.events.append(event)
                cursor += event.duration_beats
            track_cursors[track] = cursor

        sheet.events.sort(key=lambda event: (event.start_beats, event.line, event.track))
        sheet.tempo_changes = sorted(
            (b, t) for b, t in sheet.tempo_changes if b > 1e-9 and t > 0
        )
        return sheet

    @staticmethod
    def _strip_comment(raw_line: str) -> str:
        for index, char in enumerate(raw_line):
            if char != "#":
                continue
            if index == 0:
                return raw_line[:index]

            previous_char = raw_line[index - 1]
            next_char = raw_line[index + 1] if index + 1 < len(raw_line) else ""
            if previous_char.isspace() and (not next_char or next_char.isspace()):
                return raw_line[:index]
        return raw_line

    @staticmethod
    def _apply_command(sheet: Sheet, command: str, tokens, line_no: int,
                       max_cursor: float = 0.0, tempo_set: bool = False) -> None:
        if command == "time":
            if len(tokens) != 2:
                raise SheetParseError(f"第 {line_no} 行:`time` 需要 N/M 拍號")
            spec = tokens[1]
            if "/" not in spec:
                raise SheetParseError(f"第 {line_no} 行:拍號 `{spec}` 應為 N/M 形式")
            num_str, _, _ = spec.partition("/")
            try:
                numerator = int(num_str)
            except ValueError as exc:
                raise SheetParseError(
                    f"第 {line_no} 行:拍號分子 `{num_str}` 不是整數"
                ) from exc
            if numerator <= 0:
                raise SheetParseError(f"第 {line_no} 行:拍號分子必須大於 0")
            sheet.beats_per_bar = numerator
            return

        # 變速語法: tempo @<beats> <value>  或  tempo @bar:<bar_no> <value>
        if command == "tempo" and len(tokens) >= 2 and tokens[1].startswith("@"):
            if len(tokens) != 3:
                raise SheetParseError(
                    f"第 {line_no} 行:`tempo @<位置> <BPM>` 需要兩個參數"
                )
            position_spec = tokens[1][1:]
            try:
                value = float(tokens[2])
            except ValueError as exc:
                raise SheetParseError(
                    f"第 {line_no} 行:`{tokens[2]}` 不是有效 BPM"
                ) from exc
            if value <= 0:
                raise SheetParseError(f"第 {line_no} 行:tempo 必須大於 0")
            position_beats = SheetParser._parse_position(position_spec, sheet, line_no)
            if position_beats <= 1e-9:
                sheet.tempo = value
            else:
                sheet.tempo_changes.append((position_beats, value))
            return

        if len(tokens) != 2:
            raise SheetParseError(f"第 {line_no} 行:`{command}` 需要一個數值")

        try:
            value = float(tokens[1])
        except ValueError as exc:
            raise SheetParseError(f"第 {line_no} 行:`{tokens[1]}` 不是有效數值") from exc

        if command == "tempo":
            if value <= 0:
                raise SheetParseError(f"第 {line_no} 行:tempo 必須大於 0")
            # 第一個 tempo 命令、或還沒任何事件被 parse 時 → 初始 tempo
            # 否則視為「在當前游標位置變速」
            if not tempo_set or max_cursor <= 1e-9:
                sheet.tempo = value
                if max_cursor > 1e-9:
                    sheet.tempo_changes.append((max_cursor, value))
            else:
                sheet.tempo_changes.append((max_cursor, value))
            return
        elif command == "beat":
            if value <= 0:
                raise SheetParseError(f"第 {line_no} 行:beat 必須大於 0")
            sheet.beat = value
        elif command == "gap":
            if value < 0:
                raise SheetParseError(f"第 {line_no} 行:gap 不可小於 0")
            sheet.gap = value
        elif command == "hold":
            if not 0 < value <= 1:
                raise SheetParseError(f"第 {line_no} 行:hold 必須介於 0 到 1")
            sheet.hold = value
        elif command in {"mod_delay", "modifier_delay"}:
            if value < 0:
                raise SheetParseError(f"第 {line_no} 行:modifier_delay 不可小於 0")
            sheet.modifier_delay = value
        elif command == "play_range_start":
            # 負值 = 清除;0 等同從頭播,允許但實際上等同未設。
            sheet.play_range_start_seconds = None if value < 0 else value
        elif command == "play_range_end":
            sheet.play_range_end_seconds = None if value < 0 else value

    @staticmethod
    def _parse_position(spec: str, sheet: Sheet, line_no: int) -> float:
        """解析 `tempo @<位置>` 的位置字串。
        支援:
            - 純拍數: "12" → 第 12 拍
            - 小節:   "bar:4" 或 "b:4" → 第 4 小節開頭(1-indexed)
        """
        if not spec:
            raise SheetParseError(f"第 {line_no} 行:tempo @ 後缺少位置")
        lowered = spec.lower()
        if lowered.startswith("bar:") or lowered.startswith("b:"):
            tail = spec.split(":", 1)[1]
            try:
                bar = int(tail)
            except ValueError as exc:
                raise SheetParseError(
                    f"第 {line_no} 行:小節號 `{tail}` 不是整數"
                ) from exc
            if bar < 1:
                raise SheetParseError(f"第 {line_no} 行:小節號需 >= 1")
            bpb = sheet.beats_per_bar if sheet.beats_per_bar > 0 else DEFAULT_BEATS_PER_BAR
            return float((bar - 1) * bpb)
        try:
            value = float(spec)
        except ValueError as exc:
            raise SheetParseError(
                f"第 {line_no} 行:位置 `{spec}` 不是有效拍數"
            ) from exc
        if value < 0:
            raise SheetParseError(f"第 {line_no} 行:位置必須 >= 0")
        return value

    @classmethod
    def _parse_event(cls, token, default_beats, line_no, track, start_beats):
        normalized = token.strip()
        if not normalized:
            raise SheetParseError(f"第 {line_no} 行:空白音符")

        body, duration_multiplier = cls._split_duration(normalized)
        if body.startswith("[") and body.endswith("]"):
            body = body[1:-1]

        chord_parts = body.split("+")
        if not all(chord_parts):
            raise SheetParseError(f"第 {line_no} 行:和弦 `{token}` 格式錯誤")

        duration = default_beats * duration_multiplier
        strokes = []
        for part in chord_parts:
            stripped = part.strip()
            if stripped.upper() in REST_TOKENS:
                if len(chord_parts) > 1:
                    raise SheetParseError(f"第 {line_no} 行:休止符不能放在和弦 `{token}` 裡")
                return NoteEvent(start_beats, duration, (), token, line_no, track)
            strokes.append(cls._parse_note(stripped, line_no))

        deduped = tuple(dict.fromkeys(strokes))
        return NoteEvent(start_beats, duration, deduped, token, line_no, track)

    @classmethod
    def _parse_note(cls, note, line_no):
        match = cls.NOTE_RE.match(note)
        if not match:
            raise SheetParseError(f"第 {line_no} 行:不支援的音符 `{note}`")

        octave = match.group("oct").upper()
        degree = int(match.group("num"))
        accidental_a = cls._normalize_accidental(match.group("acc1"))
        accidental_b = cls._normalize_accidental(match.group("acc2"))
        if accidental_a and accidental_b:
            raise SheetParseError(f"第 {line_no} 行:`{note}` 只能有一個升降記號")
        return make_stroke(octave, degree, accidental_a or accidental_b)

    @staticmethod
    def _normalize_accidental(value: str) -> str:
        if value in {"#", "♯"}:
            return "#"
        if value in {"b", "B", "♭"}:
            return "b"
        return ""

    @staticmethod
    def _split_duration(token: str):
        body, sep, duration = token.rpartition("*")
        if not sep:
            return token, 1.0

        if not body:
            raise SheetParseError(f"`{token}` 缺少音符")
        try:
            multiplier = float(duration)
        except ValueError as exc:
            raise SheetParseError(f"`{token}` 的長度倍率無效") from exc
        if multiplier <= 0:
            raise SheetParseError(f"`{token}` 的長度倍率必須大於 0")
        return body, multiplier
