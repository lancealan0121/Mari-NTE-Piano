# NTE Piano - 自動演奏與自動化工具
# Copyright (C) 2026  Yulun
# Licensed under GPL-3.0-or-later. See LICENSE.
"""nte_importers — MusicXML / MIDI / MuseScore (MSCZ) 匯入器。

對外提供:
    MusicXMLImporter  從 .mxl / .xml / .musicxml 解析並轉 DSL
    MidiImporter      從 .mid / .midi 解析並轉 DSL
    MsczImporter      從 .mscz / .mscx 透過 MuseScore CLI 轉成 MusicXML 後交給 MusicXMLImporter

依賴:
    nte_dsl (DEFAULT_BEATS_PER_BAR — 全域命令的預設拍號)
    stdlib: xml.etree, zipfile, tempfile, subprocess, shutil, struct, os, pathlib, dataclasses
"""
from __future__ import annotations

import os
import shutil
import struct
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from collections import defaultdict
from dataclasses import replace
from pathlib import Path

from nte_dsl import DEFAULT_BEATS_PER_BAR


class MusicXMLImporter:
    """把 MusicXML（.mxl 或 .xml）轉成本專案 piano DSL 文字。

    音域對應（轉調後）：
      L = MIDI 60..71 (C4..B4)
      M = MIDI 72..83 (C5..B5)
      H = MIDI 84..95 (C6..B6)

    每隻手會自動 octave-fold 到三排內，超出範圍的音會升/降八度。
    """

    _STEP_TO_SEMI = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
    _PC_TO_LABEL = {
        0:  ("1", ""),
        1:  ("1", "#"),
        2:  ("2", ""),
        3:  ("3", "b"),
        4:  ("3", ""),
        5:  ("4", ""),
        6:  ("4", "#"),
        7:  ("5", ""),
        8:  ("5", "#"),
        9:  ("6", ""),
        10: ("7", "b"),
        11: ("7", ""),
    }
    _OCTAVE_TARGETS = {"H": 89, "MH": 83, "M": 77, "ML": 71, "L": 65}

    @classmethod
    def load_score(cls, path: Path):
        if path.suffix.lower() == ".mxl":
            with zipfile.ZipFile(path) as z:
                inner = next(
                    (n for n in z.namelist()
                     if n.endswith(".xml") and not n.startswith("META-INF")),
                    None,
                )
                if inner is None:
                    raise RuntimeError(f"{path.name} 內找不到 score xml")
                with z.open(inner) as f:
                    return ET.parse(f).getroot()
        return ET.parse(path).getroot()

    @classmethod
    def suggest_transpose(cls, root) -> int:
        """根據 fifths 建議移調量，把該調式移到 C major / A minor。"""
        fifths_el = root.find(".//attributes/key/fifths")
        if fifths_el is None or fifths_el.text is None:
            return 0
        try:
            fifths = int(fifths_el.text)
        except ValueError:
            return 0
        t = -((fifths * 7) % 12)
        if t < -6:
            t += 12
        elif t > 5:
            t -= 12
        return t

    # M 區段 MIDI 範圍 72..83;78 是 M 區中央偏上(Gb5),預留主旋律向下發揮空間。
    _MELODY_CENTER_MIDI = 78
    # L 區段 MIDI 範圍 60..71;65 是 L 區中央(F4),左手 bass voice 自然落點。
    _BASS_ANCHOR_MIDI = 65

    @classmethod
    def suggest_transpose_for_range(cls, root) -> int:
        """在 key signature transpose 的基礎上,再多推一次讓右手主旋律的 duration-weighted
        中位數落到 M 區中央(MIDI 78)。clamp 額外移調量到 [-12, 12]。

        策略:只看 staff=1 且 voice=1 的非休止音(MusicXML 慣例:top voice 即主旋律),
        每個音的權重 = <duration>,長音(旋律骨幹)權重高、裝飾音/跑句權重低。
        """
        key_transpose = cls.suggest_transpose(root)
        samples: list[tuple[int, int]] = []  # (duration, midi)
        for part in root.findall("part"):
            for measure in part.findall("measure"):
                for note in measure.findall("note"):
                    if note.find("rest") is not None:
                        continue
                    staff = note.findtext("staff") or "1"
                    voice = note.findtext("voice") or "1"
                    if staff != "1" or voice != "1":
                        continue
                    pitch_el = note.find("pitch")
                    if pitch_el is None:
                        continue
                    try:
                        midi = cls._midi_of_pitch(pitch_el) + key_transpose
                    except (KeyError, TypeError, ValueError):
                        continue
                    try:
                        dur = int(note.findtext("duration") or "1")
                    except ValueError:
                        dur = 1
                    if dur <= 0:
                        dur = 1
                    samples.append((dur, midi))
        if not samples:
            return key_transpose
        samples.sort(key=lambda x: x[1])
        total_w = sum(d for d, _ in samples)
        half = total_w / 2.0
        acc = 0
        median = samples[-1][1]
        for d, m in samples:
            acc += d
            if acc >= half:
                median = m
                break
        extra = cls._MELODY_CENTER_MIDI - median
        extra = max(-12, min(12, extra))
        return key_transpose + extra

    @classmethod
    def metadata(cls, root) -> dict:
        return {
            "title": root.findtext("work/work-title") or "",
            "composer": root.findtext('identification/creator[@type="composer"]') or "",
            "tempo": cls._first_tempo(root),
            "fifths": cls._fifths(root),
            "beats_per_bar": cls._first_time_signature(root),
        }

    @classmethod
    def _fifths(cls, root) -> int:
        el = root.find(".//attributes/key/fifths")
        try:
            return int(el.text) if el is not None and el.text else 0
        except ValueError:
            return 0

    @classmethod
    def _first_time_signature(cls, root) -> int:
        time_el = root.find(".//attributes/time")
        if time_el is None:
            return DEFAULT_BEATS_PER_BAR
        try:
            beats = int(time_el.findtext("beats") or "4")
        except ValueError:
            return DEFAULT_BEATS_PER_BAR
        return beats if beats > 0 else DEFAULT_BEATS_PER_BAR

    @classmethod
    def _first_tempo(cls, root) -> float:
        for sound in root.iter("sound"):
            t = sound.attrib.get("tempo")
            if t:
                try:
                    return float(t)
                except ValueError:
                    pass
        return 100.0

    @classmethod
    def _collect_tempo_changes(cls, root) -> list[tuple[float, float]]:
        """掃過 part[0] 的 measure 列表,擷取所有 <sound tempo=...>。
        回傳 [(beat_position, bpm), ...] 排序、去除位置 0 與重複。
        """
        parts = root.findall("part")
        if not parts:
            return []
        part = parts[0]
        part_divisions = 4
        part_measure_units = 16
        measure_offset = 0
        cursor = 0
        raw: list[tuple[float, float]] = []  # (beat_position, tempo)

        for measure in part.findall("measure"):
            attrs = measure.find("attributes")
            if attrs is not None:
                d = attrs.findtext("divisions")
                if d:
                    try:
                        part_divisions = int(d)
                    except ValueError:
                        pass
                time_el = attrs.find("time")
                if time_el is not None:
                    try:
                        beats = int(time_el.findtext("beats") or "4")
                        beat_type = int(time_el.findtext("beat-type") or "4")
                        if beat_type > 0:
                            part_measure_units = part_divisions * 4 * beats // beat_type
                    except (ValueError, TypeError):
                        pass

            cursor = measure_offset

            for child in measure:
                tag = child.tag
                if tag == "sound":
                    t = child.attrib.get("tempo")
                    if t:
                        try:
                            raw.append((cursor / max(1, part_divisions), float(t)))
                        except ValueError:
                            pass
                elif tag == "direction":
                    for s in child.iter("sound"):
                        t = s.attrib.get("tempo")
                        if t:
                            try:
                                raw.append((cursor / max(1, part_divisions), float(t)))
                            except ValueError:
                                pass
                elif tag == "note":
                    if child.find("grace") is not None:
                        continue
                    if child.find("chord") is not None:
                        # 和弦音符不推進 cursor
                        continue
                    try:
                        duration = int(child.findtext("duration") or "0")
                    except ValueError:
                        duration = 0
                    cursor += duration
                elif tag == "backup":
                    try:
                        cursor -= int(child.findtext("duration") or "0")
                    except ValueError:
                        pass
                elif tag == "forward":
                    try:
                        cursor += int(child.findtext("duration") or "0")
                    except ValueError:
                        pass

            measure_offset += part_measure_units

        # 去除起點與相鄰重複,sort
        raw.sort(key=lambda x: (x[0], x[1]))
        deduped: list[tuple[float, float]] = []
        for beat, tempo in raw:
            if beat <= 1e-6 or tempo <= 0:
                continue
            if deduped and abs(deduped[-1][1] - tempo) < 1e-3:
                continue
            if deduped and abs(deduped[-1][0] - beat) < 1e-6:
                deduped[-1] = (beat, tempo)
                continue
            deduped.append((beat, tempo))
        return deduped

    @classmethod
    def _midi_of_pitch(cls, pitch_el) -> int:
        step = pitch_el.findtext("step")
        octave = int(pitch_el.findtext("octave"))
        alter = int(pitch_el.findtext("alter") or "0")
        return (octave + 1) * 12 + cls._STEP_TO_SEMI[step] + alter

    @classmethod
    def _fold(cls, midi: int, prefer: str) -> tuple[str, int]:
        if prefer == "auto":
            m = midi
            while m < 60:
                m += 12
            while m > 95:
                m -= 12
            if m >= 84:
                return "H", m
            if m >= 72:
                return "M", m
            return "L", m
        target = cls._OCTAVE_TARGETS[prefer]
        best = None
        for k in range(-6, 7):
            m = midi + 12 * k
            if 60 <= m <= 95:
                d = abs(m - target)
                if best is None or d < best[1]:
                    best = (m, d)
        if best is None:
            best = (max(60, min(95, midi)), 0)
        m = best[0]
        if m >= 84:
            return "H", m
        if m >= 72:
            return "M", m
        return "L", m

    @classmethod
    def _label_for(cls, midi: int, prefer: str) -> str:
        octv, m = cls._fold(midi, prefer)
        deg, acc = cls._PC_TO_LABEL[m % 12]
        return f"{octv}{acc}{deg}"

    @classmethod
    def _collect_events(cls, root, transpose: int):
        parts = root.findall("part")
        if not parts:
            raise RuntimeError("找不到 part")

        by_staff: dict[int, list] = defaultdict(list)
        by_voice: dict[tuple[int, int, int], list] = defaultdict(list)
        divisions = 4
        measure_units = 16

        for part_idx, part in enumerate(parts):
            part_divisions = 4
            part_measure_units = 16
            measure_offset = 0
            cursor = 0
            last_event_per_voice: dict[tuple[int, int], list] = {}
            tie_targets: dict[tuple[int, int, int], list] = {}

            for measure in part.findall("measure"):
                attrs = measure.find("attributes")
                if attrs is not None:
                    d = attrs.findtext("divisions")
                    if d:
                        try:
                            part_divisions = int(d)
                        except ValueError:
                            pass
                    time_el = attrs.find("time")
                    if time_el is not None:
                        try:
                            beats = int(time_el.findtext("beats") or "4")
                            beat_type = int(time_el.findtext("beat-type") or "4")
                            if beat_type > 0:
                                part_measure_units = part_divisions * 4 * beats // beat_type
                        except (ValueError, TypeError):
                            pass

                cursor = measure_offset

                for child in measure:
                    tag = child.tag
                    if tag == "note":
                        if child.find("grace") is not None:
                            continue
                        try:
                            duration = int(child.findtext("duration") or "0")
                        except ValueError:
                            duration = 0
                        try:
                            staff = int(child.findtext("staff") or "1")
                        except ValueError:
                            staff = 1
                        try:
                            voice = int(child.findtext("voice") or "1")
                        except ValueError:
                            voice = 1
                        if child.find("rest") is not None:
                            cursor += duration
                            continue
                        pitch_el = child.find("pitch")
                        if pitch_el is None:
                            cursor += duration
                            continue
                        midi = cls._midi_of_pitch(pitch_el) + transpose
                        tie_types = {
                            t.attrib.get("type") for t in child.findall("tie")
                        }
                        is_chord = child.find("chord") is not None
                        tie_key = (staff, voice, midi)

                        if not is_chord and "stop" in tie_types:
                            prev = tie_targets.pop(tie_key, None)
                            if prev is not None:
                                prev[1] += duration
                                cursor += duration
                                if "start" in tie_types:
                                    tie_targets[tie_key] = prev
                                continue

                        if is_chord:
                            last = last_event_per_voice.get((staff, voice))
                            if last is not None:
                                last[2].append(midi)
                                if "start" in tie_types:
                                    tie_targets[tie_key] = last
                        else:
                            ev = [cursor, duration, [midi]]
                            by_staff[staff].append(ev)
                            by_voice[(part_idx, staff, voice)].append(ev)
                            last_event_per_voice[(staff, voice)] = ev
                            if "start" in tie_types:
                                tie_targets[tie_key] = ev
                            cursor += duration
                    elif tag == "backup":
                        try:
                            cursor -= int(child.findtext("duration") or "0")
                        except ValueError:
                            pass
                    elif tag == "forward":
                        try:
                            cursor += int(child.findtext("duration") or "0")
                        except ValueError:
                            pass

                measure_offset += part_measure_units

            divisions = max(divisions, part_divisions)
            measure_units = max(measure_units, part_measure_units)

        return by_staff, by_voice, divisions

    @classmethod
    def _merge_same_onset(cls, events):
        """把相同 onset 的事件合成 chord，保留多 voice 在同拍的和聲。"""
        if not events:
            return []
        bucket: dict[int, list] = {}
        for onset, dur, midis in events:
            if onset in bucket:
                existing = bucket[onset]
                existing[1] = max(existing[1], dur)
                existing[2].extend(midis)
            else:
                bucket[onset] = [onset, dur, list(midis)]
        return [bucket[k] for k in sorted(bucket)]

    @classmethod
    def _staff_anchor(cls, events, fallback: int) -> int:
        """回傳該 staff 的 duration-weighted median(clamped 進 [60, 95])作 anchor。

        events 為空時回 fallback(右手 _MELODY_CENTER_MIDI,左手 _BASS_ANCHOR_MIDI)。
        這讓自然落在 H 區的右手譜 anchor=83 而非 78,避免被壓回 M;落在 L 區的
        左手譜 anchor 也跟著走,避免被推到 M。
        """
        midis: list[int] = []
        for ev in events:
            if len(ev) < 3:
                continue
            ms = ev[2]
            if ms:
                midis.extend(ms)
        if not midis:
            return fallback
        midis.sort()
        median = midis[len(midis) // 2]
        return max(60, min(95, median))

    @classmethod
    def _compute_piece_shift(cls, events_iterable, anchor: int | None = None) -> int:
        """算出讓樂譜中位音對齊 [60, 95] 範圍中央的 octave shift (semitones, 12 倍數)。

        evaluates each k ∈ [-6, 6]: 優先 maximize 範圍內音數;tie-break 最小化
        (median 到 anchor 距離);再 tie-break |k|。

        anchor 預設為 _MELODY_CENTER_MIDI (78, 右手主旋律)。傳 _BASS_ANCHOR_MIDI (65)
        可給左手 bass voice 自己的 shift；傳整首 events 不指定 anchor 相當於 v11
        全曲 piece_shift 行為，但 v12 起改為每隻手各自呼叫一次（per-staff），
        讓右手保留在 M 區、左手保留在 L 區，避免右手被左手低音平均拉到 H 區。
        """
        target = cls._MELODY_CENTER_MIDI if anchor is None else anchor
        midis: list[int] = []
        for evs in events_iterable:
            for _, _, ms in evs:
                if ms:
                    midis.extend(ms)
        if not midis:
            return 0
        sorted_m = sorted(midis)
        median = sorted_m[len(sorted_m) // 2]
        best = None
        best_k = 0
        for k in range(-6, 7):
            in_range = sum(1 for m in midis if 60 <= m + 12 * k <= 95)
            center_dist = abs((median + 12 * k) - target)
            key = (-in_range, center_dist, abs(k))
            if best is None or key < best:
                best = key
                best_k = k
        return best_k * 12

    @classmethod
    def _fold_chord_stateful(cls, midis, prefer: str, prev_top, voice_shift: int = 0):
        """把 chord 的 midi 群 fold 進 [60, 95]。

        v11 chord-aware 演算法 (auto 模式):
        1. 套用 piece-level voice_shift (整首中位音對齊 NTE 中央, 保留左右手相對位置)
        2. 若 chord 已全部在 [60, 95]: 不動,保留原本 zone 分配
        3. 若 chord span > 35 semi (不可能塞進 3 octave): per-note fold (必然丟音)
        4. 否則找最小 |k| 讓 chord 整個進 [60, 95] (preserve voicing + octave doublings)

        前代 (per-note) 對 octave doublings (如 [C3, C5]) 兩個音都 fold 到同一鍵,丟音;
        v11 chord-aware unit shift 避免這個問題。voice_shift 在 to_dsl 算一次,所有
        track 共用,確保 melody 不跟 bass 黏在同一 zone。
        """
        midis = sorted(set(midis))
        if not midis:
            return [], prev_top
        if prefer != "auto":
            folded = [cls._fold(m, prefer)[1] for m in midis]
            return folded, max(folded) if folded else prev_top
        shifted = sorted(set(m + voice_shift for m in midis))
        if all(60 <= m <= 95 for m in shifted):
            return shifted, max(shifted)
        low = min(shifted)
        high = max(shifted)
        if high - low > 35:
            folded: list[int] = []
            for m in shifted:
                x = m
                while x > 95:
                    x -= 12
                while x < 60:
                    x += 12
                folded.append(x)
            folded = sorted(set(folded))
            return folded, max(folded) if folded else prev_top
        import math
        k_min = math.ceil((60 - low) / 12)
        k_max = math.floor((95 - high) / 12)
        if k_min > k_max:
            k = 0
        elif k_min <= 0 <= k_max:
            k = 0
        elif k_max < 0:
            k = k_max
        else:
            k = k_min
        folded_shifted = [m + 12 * k for m in shifted]
        folded_shifted = [max(60, min(95, m)) for m in folded_shifted]
        folded_shifted = sorted(set(folded_shifted))
        return folded_shifted, max(folded_shifted) if folded_shifted else prev_top

    @classmethod
    def _emit_track(cls, events, prefer_octave, pitch_select: str = "all",
                    voice_shift: int = 0) -> tuple[list[str], int]:
        if prefer_octave == "none":
            return [], 0
        events = cls._merge_same_onset(events)
        events.sort(key=lambda e: (e[0], -len(e[2])))
        tokens: list[str] = []
        cursor = 0
        skipped = 0
        prev_top = None
        for onset, dur, midis in events:
            if onset < cursor:
                skipped += 1
                continue
            if onset > cursor:
                gap = onset - cursor
                tokens.append(f"0*{gap}" if gap > 1 else "0")
                cursor = onset
            if dur <= 0:
                dur = 1
            if pitch_select == "top":
                midis = [max(midis)]
            elif pitch_select == "bottom":
                midis = [min(midis)]
            folded, prev_top = cls._fold_chord_stateful(midis, prefer_octave, prev_top, voice_shift)
            labels: list[str] = []
            for m in folded:
                octv = "H" if m >= 84 else "M" if m >= 72 else "L"
                deg, acc = cls._PC_TO_LABEL[m % 12]
                lbl = f"{octv}{acc}{deg}"
                if lbl not in labels:
                    labels.append(lbl)
            tok = labels[0] if len(labels) == 1 else "[" + "+".join(labels) + "]"
            if dur != 1:
                tok += f"*{dur}"
            tokens.append(tok)
            cursor = onset + dur
        return tokens, skipped

    @classmethod
    def to_dsl(
        cls,
        path: Path,
        *,
        transpose: int = 0,
        right_prefer: str = "auto",
        left_prefer: str = "auto",
        gap: float = 0.02,
        hold: float = 0.85,
        modifier_delay: float = 0.012,
        tempo_override: float | None = None,
        melody_mode: str = "dense",
        import_tempo_changes: bool = True,
    ) -> tuple[str, dict]:
        """轉換並回傳 (DSL 文字, 統計 dict)。

        melody_mode:
          - "dense": 每個 (staff, voice) 拆成獨立 track，最大化保留所有聲部（預設）
          - "skeleton": 右手只取每拍最高音、左手只取最低音；保留節奏骨幹
          - "melody_only": 只匯入主旋律（取所有 staff 同拍最高音為單一聲線）
          - "full": 完整匯入左右手、保留和聲（GUI 已不暴露，內部與 MIDI dense 退化時使用）

        音域折疊一律採 v11 策略：全曲統一 piece_shift、左右手共用，保留 staff 間
        音高關係，實聽效果最自然。
        """
        root = cls.load_score(path)
        meta = cls.metadata(root)
        tempo = tempo_override if tempo_override is not None else meta["tempo"]
        tempo_changes = cls._collect_tempo_changes(root) if import_tempo_changes else []
        # 若使用者覆寫了起始 tempo,變化點仍照原譜寫出 (使用者只調整起點)
        by_staff, by_voice, divisions = cls._collect_events(root, transpose)
        beat = 1.0 / divisions

        extra_tracks: list[tuple[str, list[str]]] = []

        right_events = list(by_staff.get(1, []))
        left_events = list(by_staff.get(2, []))

        # voice shift 計算：全曲統一 piece_shift，左右手共用，保留 staff 間音高關係
        piece_shift = cls._compute_piece_shift(by_staff.values())
        right_shift = left_shift = merged_shift = piece_shift

        if melody_mode == "melody_only":
            merged_events: list = []
            for evs in by_staff.values():
                merged_events.extend(list(e) for e in evs)
            right_tokens, right_skip = cls._emit_track(
                merged_events, right_prefer, pitch_select="top", voice_shift=merged_shift
            )
            left_tokens, left_skip = [], 0
        elif melody_mode == "skeleton":
            right_tokens, right_skip = cls._emit_track(
                [list(e) for e in right_events], right_prefer,
                pitch_select="top", voice_shift=right_shift,
            )
            left_tokens, left_skip = cls._emit_track(
                [list(e) for e in left_events], left_prefer,
                pitch_select="bottom", voice_shift=left_shift,
            )
        elif melody_mode == "dense":
            right_tokens, right_skip = [], 0
            left_tokens, left_skip = [], 0
            total_skip_right = 0
            total_skip_left = 0
            multi_part = len({k[0] for k in by_voice}) > 1
            for (part_idx, staff, voice) in sorted(by_voice):
                evs = by_voice[(part_idx, staff, voice)]
                if not evs:
                    continue
                prefer = right_prefer if staff == 1 else left_prefer
                shift = right_shift if staff == 1 else left_shift
                tokens, skip = cls._emit_track(
                    [list(e) for e in evs], prefer, voice_shift=shift,
                )
                if not tokens:
                    continue
                if multi_part:
                    track_name = f"p{part_idx}s{staff}v{voice}"
                else:
                    track_name = f"s{staff}v{voice}"
                extra_tracks.append((track_name, tokens))
                if staff == 1:
                    total_skip_right += skip
                else:
                    total_skip_left += skip
            right_skip = total_skip_right
            left_skip = total_skip_left
        else:
            right_tokens, right_skip = cls._emit_track(
                [list(e) for e in right_events], right_prefer, voice_shift=right_shift,
            )
            left_tokens, left_skip = cls._emit_track(
                [list(e) for e in left_events], left_prefer, voice_shift=left_shift,
            )

        lines: list[str] = []
        title = meta["title"] or path.stem
        lines.append(f"# {title}")
        if meta["composer"]:
            lines.append(f"# composer: {meta['composer']}")
        lines.append(
            f"# imported from {path.name}; transpose {transpose:+d} semitones; "
            f"divisions={divisions}; fifths={meta['fifths']}; mode={melody_mode}"
        )
        lines.append("")
        lines.append(f"tempo {tempo:g}")
        lines.append(f"beat {beat:g}")
        lines.append(f"gap {gap:g}")
        lines.append(f"hold {hold:g}")
        lines.append(f"modifier_delay {modifier_delay:g}")
        bpb = int(meta.get("beats_per_bar", DEFAULT_BEATS_PER_BAR) or DEFAULT_BEATS_PER_BAR)
        if bpb != DEFAULT_BEATS_PER_BAR:
            lines.append(f"time {bpb}/4")
        # 變速點 (位於初始之後)
        for change_beat, change_tempo in tempo_changes:
            lines.append(f"tempo @{change_beat:g} {change_tempo:g}")

        if right_tokens:
            lines.append("")
            lines.append("track right" if melody_mode != "melody_only" else "track melody")
            for i in range(0, len(right_tokens), 16):
                lines.append("  " + " ".join(right_tokens[i:i + 16]))
        if left_tokens:
            lines.append("")
            lines.append("track left")
            for i in range(0, len(left_tokens), 16):
                lines.append("  " + " ".join(left_tokens[i:i + 16]))
        for track_name, tokens in extra_tracks:
            if not tokens:
                continue
            lines.append("")
            lines.append(f"track {track_name}")
            for i in range(0, len(tokens), 16):
                lines.append("  " + " ".join(tokens[i:i + 16]))
        lines.append("")

        right_total = len(right_tokens) + sum(
            len(toks) for name, toks in extra_tracks if "s1" in name
        )
        left_total = len(left_tokens) + sum(
            len(toks) for name, toks in extra_tracks if "s2" in name
        )

        stats = {
            "title": title,
            "composer": meta["composer"],
            "tempo": tempo,
            "transpose": transpose,
            "melody_mode": melody_mode,
            "right_count": right_total,
            "left_count": left_total,
            "right_skip": right_skip,
            "left_skip": left_skip,
            "extra_tracks": len(extra_tracks),
        }
        return "\n".join(lines), stats


class MidiImporter:
    """把 .mid / .midi (Type 0/1) 解析成內部結構,再轉成 piano DSL。

    與 MusicXMLImporter 共用 _OCTAVE_TARGETS / fold / emit 邏輯;
    主要差別在來源:MIDI 用 absolute ticks + tempo events,而非 measure/divisions。
    """

    @classmethod
    def to_dsl(
        cls,
        path: Path,
        *,
        transpose: int = 0,
        right_prefer: str = "auto",
        left_prefer: str = "auto",
        gap: float = 0.02,
        hold: float = 0.85,
        modifier_delay: float = 0.012,
        tempo_override: float | None = None,
        melody_mode: str = "dense",
        import_tempo_changes: bool = True,
    ) -> tuple[str, dict]:
        meta = cls._parse(path)
        tempo = tempo_override if tempo_override is not None else meta["tempo"]
        ticks_per_beat = meta["ticks_per_beat"]
        # 量化到 1/16 拍,與 MusicXMLImporter._emit_track 整數時長對齊。
        units_per_beat = 4
        unit_ticks = max(1, ticks_per_beat // units_per_beat)
        beat = 1.0 / units_per_beat

        right_events_raw = meta["right_events"]
        left_events_raw = meta["left_events"]

        def quantize(events_raw):
            quantized: list[list] = []
            for onset, dur, midis in events_raw:
                qo = max(0, int(round(onset / unit_ticks)))
                qd = max(1, int(round(dur / unit_ticks)))
                shifted = [m + transpose for m in midis]
                quantized.append([qo, qd, shifted])
            quantized.sort(key=lambda e: (e[0], -len(e[2])))
            return quantized

        right_q = quantize(right_events_raw)
        left_q = quantize(left_events_raw)

        # voice shift: 同 MusicXMLImporter 策略，全曲統一 piece_shift、左右手共用
        piece_shift = MusicXMLImporter._compute_piece_shift([right_q, left_q])
        right_shift = left_shift = merged_shift = piece_shift

        if melody_mode == "melody_only":
            merged = right_q + left_q
            merged.sort(key=lambda e: e[0])
            right_tokens, right_skip = MusicXMLImporter._emit_track(
                merged, right_prefer, pitch_select="top", voice_shift=merged_shift,
            )
            left_tokens, left_skip = [], 0
        elif melody_mode == "skeleton":
            right_tokens, right_skip = MusicXMLImporter._emit_track(
                right_q, right_prefer, pitch_select="top", voice_shift=right_shift,
            )
            left_tokens, left_skip = MusicXMLImporter._emit_track(
                left_q, left_prefer, pitch_select="bottom", voice_shift=left_shift,
            )
        else:
            # full / dense: MIDI 沒有 voice 概念,dense 退化成 full。
            right_tokens, right_skip = MusicXMLImporter._emit_track(
                right_q, right_prefer, voice_shift=right_shift,
            )
            left_tokens, left_skip = MusicXMLImporter._emit_track(
                left_q, left_prefer, voice_shift=left_shift,
            )

        title = meta["title"] or path.stem
        lines = [f"# {title}"]
        if meta.get("composer"):
            lines.append(f"# composer: {meta['composer']}")
        lines.append(
            f"# imported from {path.name} (MIDI); transpose {transpose:+d} semitones; "
            f"ticks_per_beat={ticks_per_beat}; mode={melody_mode}"
        )
        lines.append("")
        lines.append(f"tempo {tempo:g}")
        lines.append(f"beat {beat:g}")
        lines.append(f"gap {gap:g}")
        lines.append(f"hold {hold:g}")
        lines.append(f"modifier_delay {modifier_delay:g}")
        bpb = int(meta.get("beats_per_bar", DEFAULT_BEATS_PER_BAR) or DEFAULT_BEATS_PER_BAR)
        if bpb != DEFAULT_BEATS_PER_BAR:
            lines.append(f"time {bpb}/4")
        for change_beat, change_tempo in meta.get("tempo_changes", []):
            if not import_tempo_changes:
                break
            lines.append(f"tempo @{change_beat:g} {change_tempo:g}")

        if right_tokens:
            lines.append("")
            lines.append("track right" if melody_mode != "melody_only" else "track melody")
            for i in range(0, len(right_tokens), 16):
                lines.append("  " + " ".join(right_tokens[i:i + 16]))
        if left_tokens:
            lines.append("")
            lines.append("track left")
            for i in range(0, len(left_tokens), 16):
                lines.append("  " + " ".join(left_tokens[i:i + 16]))
        lines.append("")

        stats = {
            "title": title,
            "composer": meta.get("composer", ""),
            "tempo": tempo,
            "transpose": transpose,
            "melody_mode": melody_mode,
            "right_count": len(right_tokens),
            "left_count": len(left_tokens),
            "right_skip": right_skip,
            "left_skip": left_skip,
            "extra_tracks": 0,
        }
        return "\n".join(lines), stats

    @classmethod
    def suggest_transpose(cls, path: Path) -> int:
        """無調號資訊時回 0;若 MIDI 含 KeySignature meta 則依 fifths 推算。"""
        try:
            meta = cls._parse(path)
        except Exception:  # noqa: BLE001
            return 0
        fifths = meta.get("fifths", 0)
        t = -((fifths * 7) % 12)
        if t < -6:
            t += 12
        elif t > 5:
            t -= 12
        return t

    @classmethod
    def suggest_transpose_for_range(cls, path: Path) -> int:
        """key transpose 之後再多推一次,把右手 events 的 duration-weighted 中位數
        推到 MIDI 78(M 區中央偏上)。clamp 額外移調量到 [-12, 12]。

        每個 right event 取 chord 內 max midi(top voice 即旋律音),
        以 duration tick 加權算中位數。
        """
        try:
            meta = cls._parse(path)
        except Exception:  # noqa: BLE001
            return 0
        fifths = meta.get("fifths", 0)
        key_transpose = -((fifths * 7) % 12)
        if key_transpose < -6:
            key_transpose += 12
        elif key_transpose > 5:
            key_transpose -= 12
        right_events = meta.get("right_events") or []
        samples: list[tuple[int, int]] = []
        for evt in right_events:
            if len(evt) < 3:
                continue
            _onset, dur, midis = evt[0], evt[1], evt[2]
            if not midis:
                continue
            try:
                dur_int = int(dur)
            except (TypeError, ValueError):
                continue
            if dur_int <= 0:
                dur_int = 1
            top = max(int(m) for m in midis) + key_transpose
            samples.append((dur_int, top))
        if not samples:
            return key_transpose
        samples.sort(key=lambda x: x[1])
        total_w = sum(d for d, _ in samples)
        half = total_w / 2.0
        acc = 0
        median = samples[-1][1]
        for d, m in samples:
            acc += d
            if acc >= half:
                median = m
                break
        extra = MusicXMLImporter._MELODY_CENTER_MIDI - median
        extra = max(-12, min(12, extra))
        return key_transpose + extra

    @classmethod
    def _parse(cls, path: Path) -> dict:
        data = Path(path).read_bytes()
        pos = 0

        def need(n: int) -> bytes:
            nonlocal pos
            if pos + n > len(data):
                raise RuntimeError(f"MIDI 檔案在偏移 {pos} 處意外結束")
            chunk = data[pos:pos + n]
            pos += n
            return chunk

        if need(4) != b"MThd":
            raise RuntimeError("不是有效的 MIDI 檔(缺少 MThd)")
        header_len = struct.unpack(">I", need(4))[0]
        header = need(header_len)
        fmt, num_tracks, division = struct.unpack(">HHH", header[:6])
        if division & 0x8000:
            # SMPTE timing 暫不支援
            raise RuntimeError("不支援 SMPTE timing 的 MIDI(division 高位為 1)")
        ticks_per_beat = max(1, int(division))

        tempo_us_per_beat = 500000  # default 120 BPM
        first_tempo = None
        # 變速點: (abs_tick, us_per_beat); 第一個與 first_tempo 相同
        tempo_events_raw: list[tuple[int, int]] = []
        first_time_sig = DEFAULT_BEATS_PER_BAR
        title = ""
        composer = ""
        fifths = 0

        # tracks[i] = list of (abs_tick, channel, status, data1, data2)
        all_notes: list[list] = []  # 每軌獨立,後面再拆 right/left
        track_index = -1

        while pos < len(data):
            if need(4) != b"MTrk":
                raise RuntimeError("MIDI 軌道區塊缺少 MTrk header")
            track_len = struct.unpack(">I", need(4))[0]
            end = pos + track_len
            track_index += 1
            running_status = 0
            abs_tick = 0
            # 每個 (channel, key) 的開啟事件:list[(onset_tick, velocity, list_index)]
            open_notes: dict[tuple[int, int], tuple[int, int]] = {}
            track_events: list[tuple[int, int, list[int]]] = []  # (onset, dur, [midis])

            while pos < end:
                # delta time (variable-length)
                delta = 0
                while True:
                    b = need(1)[0]
                    delta = (delta << 7) | (b & 0x7F)
                    if not (b & 0x80):
                        break
                abs_tick += delta

                status_byte = need(1)[0]
                if status_byte < 0x80:
                    # running status:status_byte 其實是 data1
                    if running_status == 0:
                        raise RuntimeError("MIDI 中遇到 running status 但無 status 可重用")
                    status = running_status
                    data1 = status_byte
                else:
                    status = status_byte
                    if status < 0xF0:
                        running_status = status
                    if status in (0xF0, 0xF7):
                        # SysEx,跳過
                        length = 0
                        while True:
                            b = need(1)[0]
                            length = (length << 7) | (b & 0x7F)
                            if not (b & 0x80):
                                break
                        need(length)
                        continue
                    if status == 0xFF:
                        meta_type = need(1)[0]
                        length = 0
                        while True:
                            b = need(1)[0]
                            length = (length << 7) | (b & 0x7F)
                            if not (b & 0x80):
                                break
                        meta_data = need(length)
                        if meta_type == 0x51 and len(meta_data) >= 3:
                            tu = (meta_data[0] << 16) | (meta_data[1] << 8) | meta_data[2]
                            if tu > 0:
                                tempo_us_per_beat = tu
                                if first_tempo is None:
                                    first_tempo = tu
                                tempo_events_raw.append((abs_tick, tu))
                        elif meta_type == 0x58 and len(meta_data) >= 2:
                            if track_index == 0 and abs_tick == 0:
                                first_time_sig = max(1, meta_data[0])
                        elif meta_type == 0x59 and len(meta_data) >= 2:
                            sf = meta_data[0]
                            if sf >= 128:
                                sf -= 256
                            if track_index == 0:
                                fifths = int(sf)
                        elif meta_type == 0x03 and not title:
                            try:
                                title = meta_data.decode("utf-8", errors="ignore").strip()
                            except Exception:  # noqa: BLE001
                                pass
                        elif meta_type == 0x02 and not composer:
                            try:
                                composer = meta_data.decode("utf-8", errors="ignore").strip()
                            except Exception:  # noqa: BLE001
                                pass
                        continue
                    data1 = need(1)[0]

                upper = status & 0xF0
                channel = status & 0x0F
                if upper in (0x80, 0x90, 0xA0, 0xB0, 0xE0):
                    data2 = need(1)[0]
                    if upper == 0x90 and data2 > 0:
                        open_notes[(channel, data1)] = (abs_tick, data2)
                    elif upper == 0x80 or (upper == 0x90 and data2 == 0):
                        opened = open_notes.pop((channel, data1), None)
                        if opened is not None:
                            onset, _vel = opened
                            dur = max(1, abs_tick - onset)
                            track_events.append((onset, dur, [int(data1)]))
                elif upper in (0xC0, 0xD0):
                    pass  # 1 byte data,已讀
                else:
                    pass

            # 軌道結束時,若還有未關閉的 note,給個預設長度
            for (channel, key), (onset, _vel) in list(open_notes.items()):
                track_events.append((onset, ticks_per_beat // 2, [int(key)]))

            if track_events:
                # 同 onset 合成 chord
                merged: dict[int, list] = {}
                for onset, dur, midis in track_events:
                    if onset in merged:
                        ex = merged[onset]
                        ex[1] = max(ex[1], dur)
                        ex[2].extend(midis)
                    else:
                        merged[onset] = [onset, dur, list(midis)]
                events = [merged[k] for k in sorted(merged)]
                all_notes.append(events)

            pos = end

        # 將軌道分為右手/左手:依平均音高,高的那組當右手。Type 0 (單軌) 直接整軌當右手。
        right_events: list[list] = []
        left_events: list[list] = []
        if not all_notes:
            pass
        elif len(all_notes) == 1:
            right_events = all_notes[0]
        else:
            track_avgs = []
            for events in all_notes:
                if not events:
                    continue
                pitches = [m for ev in events for m in ev[2]]
                avg = sum(pitches) / len(pitches) if pitches else 0
                track_avgs.append((avg, events))
            track_avgs.sort(key=lambda x: x[0], reverse=True)
            half = max(1, len(track_avgs) // 2)
            for _avg, events in track_avgs[:half]:
                right_events.extend([list(e) for e in events])
            for _avg, events in track_avgs[half:]:
                left_events.extend([list(e) for e in events])
            right_events.sort(key=lambda e: e[0])
            left_events.sort(key=lambda e: e[0])

        bpm = 60_000_000.0 / (first_tempo if first_tempo else tempo_us_per_beat)

        # 把 tempo_events_raw 從 (abs_tick, us_per_beat) 轉成 (beat, bpm),去除起點/重複
        tempo_changes: list[tuple[float, float]] = []
        for abs_tick, tu in sorted(tempo_events_raw):
            beat = abs_tick / max(1, ticks_per_beat)
            tempo_bpm = 60_000_000.0 / tu
            if beat <= 1e-6 or tempo_bpm <= 0:
                continue
            if tempo_changes and abs(tempo_changes[-1][1] - tempo_bpm) < 1e-3:
                continue
            tempo_changes.append((beat, tempo_bpm))

        return {
            "title": title,
            "composer": composer,
            "tempo": bpm,
            "tempo_changes": tempo_changes,
            "ticks_per_beat": ticks_per_beat,
            "fifths": fifths,
            "beats_per_bar": first_time_sig,
            "right_events": right_events,
            "left_events": left_events,
            "format": fmt,
            "num_tracks": num_tracks,
        }


class MsczImporter:
    """MuseScore .mscz / .mscx 匯入器。

    全部都走 MusicXML 流程：
      1. .mscz 內若直接含 .mxl/.musicxml，解出後直接交給 MusicXMLImporter。
      2. 否則呼叫 MuseScore CLI（MuseScore4.exe / MuseScore3.exe / mscore）
         把檔案轉成 .musicxml 暫存檔，再交給 MusicXMLImporter。
    找不到 MuseScore 可執行檔時直接 RuntimeError，不再退回內建解析。
    """

    _MUSESCORE_NAMES = (
        "MuseScore4.exe", "MuseScore4",
        "MuseScore3.exe", "MuseScore3",
        "MuseScore.exe", "MuseScore",
        "mscore.exe", "mscore",
    )

    @classmethod
    def _find_musescore(cls) -> Path | None:
        for name in cls._MUSESCORE_NAMES:
            found = shutil.which(name)
            if found:
                return Path(found)
        candidates: list[Path] = []
        env_keys = ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432")
        for key in env_keys:
            base_str = os.environ.get(key)
            if not base_str:
                continue
            base = Path(base_str)
            for ver in ("MuseScore 4", "MuseScore 3"):
                candidates.append(base / ver / "bin" / f"{ver.replace(' ', '')}.exe")
        for p in candidates:
            if p.exists():
                return p
        return None

    @classmethod
    def _convert_via_cli(cls, input_path: Path, output_path: Path) -> None:
        exe = cls._find_musescore()
        if exe is None:
            raise RuntimeError(
                "找不到 MuseScore 可執行檔，無法匯入此檔案。\n"
                "請安裝 MuseScore 4（建議）或 MuseScore 3，或將其加入系統 PATH；\n"
                "或先用 MuseScore 把譜面匯出為 .musicxml / .mxl 後再匯入。"
            )
        creationflags = 0
        if sys.platform == "win32":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

        # MuseScore 4 在某些 Windows 環境下用預設 Qt platform plugin 啟動 CLI 會回
        # 奇怪的 exit code(如 1320)且 stderr 為空。改設 QT_QPA_PLATFORM=offscreen
        # 強制無頭模式,跳過 GUI 初始化。同時為避免日文/特殊字元路徑造成 MuseScore
        # 解析失敗,失敗時自動把輸入檔複製到 ASCII 暫存路徑再試。
        attempts: list[dict] = [
            {"label": "預設環境", "env_extra": {}, "use_ascii_copy": False},
            {
                "label": "QT_QPA_PLATFORM=offscreen",
                "env_extra": {"QT_QPA_PLATFORM": "offscreen"},
                "use_ascii_copy": False,
            },
            {
                "label": "QT_QPA_PLATFORM=offscreen + ASCII 路徑",
                "env_extra": {"QT_QPA_PLATFORM": "offscreen"},
                "use_ascii_copy": True,
            },
        ]

        diagnostics: list[str] = []
        for attempt in attempts:
            env = os.environ.copy()
            env.update(attempt["env_extra"])
            ascii_tmp: Path | None = None
            actual_input = input_path
            if attempt["use_ascii_copy"]:
                try:
                    with tempfile.NamedTemporaryFile(
                        suffix=input_path.suffix, delete=False, dir=tempfile.gettempdir()
                    ) as tf:
                        tf.write(input_path.read_bytes())
                        ascii_tmp = Path(tf.name)
                    actual_input = ascii_tmp
                except OSError as exc:
                    diagnostics.append(
                        f"[{attempt['label']}] 複製到 ASCII 路徑失敗: {exc}"
                    )
                    continue
            try:
                try:
                    result = subprocess.run(
                        [str(exe), "-o", str(output_path), str(actual_input)],
                        capture_output=True,
                        timeout=90,
                        creationflags=creationflags,
                        env=env,
                    )
                except subprocess.TimeoutExpired as exc:
                    diagnostics.append(f"[{attempt['label']}] 逾時 >90s")
                    last_exc = exc
                    continue
                if (
                    result.returncode == 0
                    and output_path.exists()
                    and output_path.stat().st_size > 0
                ):
                    return  # 成功
                stderr = result.stderr.decode("utf-8", errors="replace").strip()[:400]
                stdout = result.stdout.decode("utf-8", errors="replace").strip()[:400]
                diagnostics.append(
                    f"[{attempt['label']}] exit={result.returncode} "
                    f"stderr={stderr or '(空)'} stdout={stdout or '(空)'}"
                )
            finally:
                if ascii_tmp is not None:
                    try:
                        ascii_tmp.unlink()
                    except OSError:
                        pass

        diag_text = "\n  ".join(diagnostics) if diagnostics else "(無診斷資訊)"
        raise RuntimeError(
            f"MuseScore 轉檔失敗（所有嘗試皆失敗，exe={exe.name}）：\n  {diag_text}\n\n"
            f"建議:用 MuseScore 開啟原始檔，「檔案 → 匯出 → MusicXML (.mxl)」後改用 "
            f"「匯入 MusicXML…」匯入。"
        )

    @classmethod
    def prepare_musicxml(cls, path: Path) -> Path:
        """把 .mscz/.mscx 轉成暫存的 .musicxml/.xml，回傳 Path，由 caller 負責刪除。

        路徑 A：mscz 內若直接含 musicxml 就解壓出來。
        路徑 B：否則呼叫 MuseScore CLI 轉成 musicxml。
        """
        path = Path(path)
        if path.suffix.lower() == ".mscz":
            try:
                with zipfile.ZipFile(path) as z:
                    names = z.namelist()
                    xml_inner = next(
                        (
                            n for n in names
                            if n.lower().endswith((".mxl", ".musicxml", ".xml"))
                            and not n.startswith("META-INF")
                            and not n.lower().endswith("container.xml")
                        ),
                        None,
                    )
                    if xml_inner is not None:
                        suffix = Path(xml_inner).suffix
                        with z.open(xml_inner) as f:
                            data = f.read()
                        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tf:
                            tf.write(data)
                            return Path(tf.name)
            except zipfile.BadZipFile:
                pass

        with tempfile.NamedTemporaryFile(suffix=".musicxml", delete=False) as tf:
            out_path = Path(tf.name)
        try:
            cls._convert_via_cli(path, out_path)
        except Exception:
            try:
                out_path.unlink()
            except OSError:
                pass
            raise
        return out_path

    @classmethod
    def prepare_midi(cls, path: Path) -> Path:
        """把 .mscz/.mscx 轉成暫存的 .mid,回傳 Path,由 caller 負責刪除。

        只走 CLI 路徑(MuseScore -o tmp.mid),不能像 prepare_musicxml 那樣從 mscz
        內嵌檔抓 — mscz 內嵌的是 musicxml,不是 midi。
        """
        path = Path(path)
        with tempfile.NamedTemporaryFile(suffix=".mid", delete=False) as tf:
            out_path = Path(tf.name)
        try:
            cls._convert_via_cli(path, out_path)
        except Exception:
            try:
                out_path.unlink()
            except OSError:
                pass
            raise
        return out_path

    @classmethod
    def to_dsl(
        cls,
        path: Path,
        *,
        transpose: int = 0,
        right_prefer: str = "auto",
        left_prefer: str = "L",
        gap: float = 0.02,
        hold: float = 0.85,
        modifier_delay: float = 0.012,
        tempo_override: float | None = None,
        melody_mode: str = "dense",
        import_tempo_changes: bool = True,
    ) -> tuple[str, dict]:
        path = Path(path)
        tmp_path = cls.prepare_musicxml(path)
        try:
            return MusicXMLImporter.to_dsl(
                tmp_path,
                transpose=transpose,
                right_prefer=right_prefer,
                left_prefer=left_prefer,
                gap=gap,
                hold=hold,
                modifier_delay=modifier_delay,
                tempo_override=tempo_override,
                melody_mode=melody_mode,
                import_tempo_changes=import_tempo_changes,
            )
        finally:
            try:
                tmp_path.unlink()
            except OSError:
                pass

    @classmethod
    def suggest_transpose(cls, path: Path) -> int:
        try:
            tmp_path = cls.prepare_musicxml(path)
        except Exception:
            return 0
        try:
            root = MusicXMLImporter.load_score(tmp_path)
            return MusicXMLImporter.suggest_transpose(root)
        except Exception:
            return 0
        finally:
            try:
                tmp_path.unlink()
            except OSError:
                pass
