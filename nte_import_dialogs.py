# NTE Piano - 自動演奏與自動化工具
# Copyright (C) 2026  Yulun
# Licensed under GPL-3.0-or-later. See LICENSE.
"""nte_import_dialogs — 匯入用的 dialog 類與共用常數。

對外提供:
    ImportOptionsDialog 單檔匯入 (MusicXML / MIDI / MSCZ) 的選項對話框
    PREFER_OPTIONS      左右手譜偏好下拉的 (label, value) 列表
    MELODY_MODES        聲部模式下拉的 (label, value) 列表
    IMPORT_DIALOG_QSS   匯入對話框共用 stylesheet (transpose 按鈕 + radio)

ImportOptionsDialog 跟 BatchImportOptionsDialog (nte_batch_import.py) 共用
PREFER_OPTIONS / MELODY_MODES / IMPORT_DIALOG_QSS, 集中在這個模組維護。
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QVBoxLayout,
)


PREFER_OPTIONS = (
    ("最純原音（保留原音高關係）", "auto"),
    ("偏高音 H", "H"),
    ("偏中高 MH（M+H 兩排）", "MH"),
    ("偏中音 M", "M"),
    ("偏中低 ML（L+M 兩排）", "ML"),
    ("偏低音 L", "L"),
    ("無（不輸出此手）", "none"),
)

MELODY_MODES = (
    ("獨立（每個 voice 獨立 track）", "dense"),
    ("主旋律 + 簡化伴奏（右取最高、左取最低）", "skeleton"),
    ("只要主旋律（右手最高音、跳過左手）", "melody_only"),
)


IMPORT_DIALOG_QSS = (
    "QPushButton#transposeStep {"
    " font-size: 16pt; font-weight: 700;"
    " background-color: #2a2e36; color: #4dd0c2;"
    " border: 1px solid #4dd0c2; border-radius: 6px;"
    " padding: 0px;"
    "}"
    "QPushButton#transposeStep:hover {"
    " background-color: #4dd0c2; color: #16181d;"
    " border: 1px solid #a4f7c3;"
    "}"
    "QPushButton#transposeStep:pressed {"
    " background-color: #2dab9e; color: #0d0f12;"
    "}"
    "QRadioButton {"
    " color: #d0d4dc; padding: 4px 6px;"
    " spacing: 6px;"
    "}"
    "QRadioButton::indicator {"
    " width: 14px; height: 14px;"
    " border-radius: 8px;"
    " border: 2px solid #4dd0c2;"
    " background-color: #16181d;"
    "}"
    "QRadioButton::indicator:checked {"
    " background-color: #4dd0c2;"
    " border: 2px solid #a4f7c3;"
    "}"
    "QRadioButton:checked {"
    " color: #4dd0c2; font-weight: 700;"
    "}"
)


class ImportOptionsDialog(QDialog):
    """匯入 MusicXML 的選項對話框：移調、左右手八度偏好、聲部模式。"""

    def __init__(self, parent, suggested_transpose: int, suggested_range: int | None = None, is_mscz: bool = False) -> None:
        super().__init__(parent)
        self.setWindowTitle("匯入 MusicXML 選項")
        self.setMinimumWidth(460)
        self._suggested = suggested_transpose
        # 範圍感知建議:把右手主旋律 duration-weighted 中位數推到 MIDI 78(M 區中央)。
        # 若 importer 沒給就 fallback 到傳統建議(僅看 key signature)。
        self._suggested_range = (
            int(suggested_range) if suggested_range is not None else suggested_transpose
        )
        self._is_mscz = bool(is_mscz)
        # 預設值從 parent (PianoPlayerWindow) 的全域設定讀
        default_import_tempo = bool(getattr(parent, "_import_tempo_changes", True))
        self._default_import_tempo = default_import_tempo
        self.setStyleSheet(IMPORT_DIALOG_QSS)

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        layout.addWidget(QLabel(
            "選擇移調、左右手八度偏好、聲部模式；\n"
            "「主旋律」相關模式可避免 36 鍵塞太滿，確保旋律不被擠掉。"
        ))

        grid = QGridLayout()
        grid.addWidget(QLabel("移調半音數："), 0, 0)
        trans_box = QHBoxLayout()
        trans_box.setSpacing(6)
        self.transpose_minus_btn = QPushButton("−")
        self.transpose_minus_btn.setObjectName("transposeStep")
        self.transpose_minus_btn.setFixedSize(40, 34)
        self.transpose_minus_btn.setCursor(Qt.PointingHandCursor)
        self.transpose_minus_btn.setAutoRepeat(True)
        self.transpose_minus_btn.setAutoRepeatDelay(400)
        self.transpose_minus_btn.setAutoRepeatInterval(120)
        self.transpose_spin = QSpinBox()
        self.transpose_spin.setRange(-12, 12)
        self.transpose_spin.setValue(0)
        self.transpose_spin.setMinimumWidth(72)
        self.transpose_spin.setAlignment(Qt.AlignCenter)
        self.transpose_spin.setButtonSymbols(QAbstractSpinBox.NoButtons)
        self.transpose_plus_btn = QPushButton("+")
        self.transpose_plus_btn.setObjectName("transposeStep")
        self.transpose_plus_btn.setFixedSize(40, 34)
        self.transpose_plus_btn.setCursor(Qt.PointingHandCursor)
        self.transpose_plus_btn.setAutoRepeat(True)
        self.transpose_plus_btn.setAutoRepeatDelay(400)
        self.transpose_plus_btn.setAutoRepeatInterval(120)
        self.transpose_minus_btn.clicked.connect(
            lambda: self.transpose_spin.setValue(self.transpose_spin.value() - 1)
        )
        self.transpose_plus_btn.clicked.connect(
            lambda: self.transpose_spin.setValue(self.transpose_spin.value() + 1)
        )
        trans_box.addWidget(self.transpose_minus_btn)
        trans_box.addWidget(self.transpose_spin)
        trans_box.addWidget(self.transpose_plus_btn)
        trans_box.addStretch()
        grid.addLayout(trans_box, 0, 1)
        suggestion_text = (
            f"key 建議 {suggested_transpose:+d}（移到 C 大調 / A 小調）"
        )
        if self._suggested_range != suggested_transpose:
            suggestion_text += (
                f"  /  主旋律置中 {self._suggested_range:+d}（推到 M 區中央）"
            )
        else:
            suggestion_text += "  /  主旋律已落在 M 區中央"
        grid.addWidget(QLabel(suggestion_text), 0, 2)

        grid.addWidget(QLabel("右手譜偏好："), 1, 0)
        self.right_combo = QComboBox()
        for label, value in PREFER_OPTIONS:
            self.right_combo.addItem(label, value)
        grid.addWidget(self.right_combo, 1, 1, 1, 2)

        grid.addWidget(QLabel("左手譜偏好："), 2, 0)
        self.left_combo = QComboBox()
        for label, value in PREFER_OPTIONS:
            self.left_combo.addItem(label, value)
        grid.addWidget(self.left_combo, 2, 1, 1, 2)

        grid.addWidget(QLabel("聲部模式："), 3, 0)
        self.mode_combo = QComboBox()
        for label, value in MELODY_MODES:
            self.mode_combo.addItem(label, value)
        grid.addWidget(self.mode_combo, 3, 1, 1, 2)

        self.import_tempo_check = QCheckBox("匯入原譜的變速 (tempo @ 標記)")
        self.import_tempo_check.setChecked(self._default_import_tempo)
        self.import_tempo_check.setToolTip(
            "勾選:把原譜中所有速度變化轉成 tempo @<beats> <bpm> 寫入。\n"
            "不勾:整曲只用單一起始速度,適合不希望被原譜 rubato 干擾的情境。"
        )
        grid.addWidget(self.import_tempo_check, 4, 0, 1, 3)

        self.save_to_songs_check = QCheckBox("匯入後另存到 songs/(出現在歌曲下拉選單)")
        self.save_to_songs_check.setChecked(False)
        self.save_to_songs_check.setToolTip(
            "勾選:匯入完成後自動把譜面以原檔名(.txt)存到 songs/ 目錄並切到該檔,\n"
            "下拉選單立刻顯示。\n"
            "不勾(預設):只在編輯器顯示為「(未存檔) 標題」,須按 Ctrl+S 手動另存\n"
            "才會正式進入 songs/ 目錄,避免暫存檔名或亂碼污染歌曲庫。"
        )
        grid.addWidget(self.save_to_songs_check, 5, 0, 1, 3)

        # mscz 專屬:選擇先把 mscz 轉成哪種中介格式再進入匯入流程。
        # MusicXML 保留音符 articulation/voice 細節,適合主旋律 + 伴奏分離較精細;
        # MIDI 結構單純、速度標記較統一,適合純按音高 + 時值對齊的情境。
        self.mscz_format_group: QButtonGroup | None = None
        self.mscz_format_xml_btn: QRadioButton | None = None
        self.mscz_format_midi_btn: QRadioButton | None = None
        if self._is_mscz:
            grid.addWidget(QLabel("MSCZ 轉換格式:"), 6, 0)
            mscz_row = QHBoxLayout()
            self.mscz_format_xml_btn = QRadioButton("MusicXML(預設,保留聲部)")
            self.mscz_format_midi_btn = QRadioButton("MIDI(只取音高+時值)")
            self.mscz_format_xml_btn.setChecked(True)
            self.mscz_format_group = QButtonGroup(self)
            self.mscz_format_group.addButton(self.mscz_format_xml_btn)
            self.mscz_format_group.addButton(self.mscz_format_midi_btn)
            mscz_row.addWidget(self.mscz_format_xml_btn)
            mscz_row.addWidget(self.mscz_format_midi_btn)
            mscz_row.addStretch()
            grid.addLayout(mscz_row, 6, 1, 1, 2)
        layout.addLayout(grid)

        presets_box = QHBoxLayout()
        recommended_btn = QPushButton("建議")
        recommended_btn.setToolTip(
            "推薦設定：移到 C 大調 / A 小調，右手 auto、左手固定 L，\n"
            "聲部模式採「獨立」（每個 voice 拆成獨立 track，三排都會用到）。"
        )
        recommended_btn.clicked.connect(self._preset_recommended)
        presets_box.addWidget(recommended_btn)
        presets_box.addStretch()
        layout.addLayout(presets_box)

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        layout.addWidget(bb)

        self._preset_recommended()  # 預設套用「建議」

    def _set_combo(self, combo: QComboBox, data_value: str) -> None:
        for i in range(combo.count()):
            if combo.itemData(i) == data_value:
                combo.setCurrentIndex(i)
                return

    def _preset_recommended(self) -> None:
        # 「建議」：移到 C 大調 / A 小調，右手 auto、左手 L，聲部模式採「獨立」(dense)。
        # 等同舊版的「三區複雜完整」。
        self.transpose_spin.setValue(self._suggested)
        self._set_combo(self.right_combo, "auto")
        self._set_combo(self.left_combo, "L")
        self._set_combo(self.mode_combo, "dense")

    def values(self) -> dict:
        mscz_format = "musicxml"
        if self._is_mscz and self.mscz_format_midi_btn is not None and self.mscz_format_midi_btn.isChecked():
            mscz_format = "midi"
        return {
            "transpose": self.transpose_spin.value(),
            "right_prefer": self.right_combo.currentData(),
            "left_prefer": self.left_combo.currentData(),
            "melody_mode": self.mode_combo.currentData(),
            "import_tempo_changes": self.import_tempo_check.isChecked(),
            "save_to_songs": self.save_to_songs_check.isChecked(),
            "mscz_format": mscz_format,
        }
