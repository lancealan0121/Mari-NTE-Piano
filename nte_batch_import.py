# NTE Piano - 自動演奏與自動化工具
# Copyright (C) 2026  Yulun
# Licensed under GPL-3.0-or-later. See LICENSE.
"""nte_batch_import — 批量匯入譜面用的 dialog 與處理流程。

對外提供:
    BatchImportResult       單檔批量匯入結果(成功/失敗、暫存路徑、統計)
    BatchImportOptionsDialog 統一設定對話框(無 transpose, 套用到全部檔)
    BatchImportResultsDialog 結果挑選對話框(QListWidget + checkbox)
    run_batch_import        執行整批轉檔, 回傳 list[BatchImportResult]
    SUPPORTED_EXTENSIONS    可批量匯入的副檔名集合

依賴:
    nte_importers (三個 importer + MSCZ prepare_*)
    PySide6
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
)

from nte_importers import MidiImporter, MsczImporter, MusicXMLImporter
from nte_import_dialogs import IMPORT_DIALOG_QSS, MELODY_MODES, PREFER_OPTIONS
from nte_paths import MIDI_EXTENSIONS, MSCZ_EXTENSIONS, MUSICXML_EXTENSIONS


SUPPORTED_EXTENSIONS = MIDI_EXTENSIONS | MUSICXML_EXTENSIONS | MSCZ_EXTENSIONS


@dataclass
class BatchImportResult:
    source_path: Path
    success: bool
    staged_path: Path | None = None
    error: str | None = None
    title: str = ""
    right_count: int = 0
    left_count: int = 0
    tempo: float = 0.0
    transpose: int = 0


class BatchImportOptionsDialog(QDialog):
    """批量匯入的統一設定對話框。

    不含 transpose 控制(每首各自用 suggest_transpose 自動算, 跟單檔「建議」按鈕一致),
    不含 save_to_songs(改在結果對話框決定要存哪些)。
    mscz_format radio 只在傳入的 files 含 mscz 時才顯示。
    """

    def __init__(self, parent, files: list[Path]) -> None:
        super().__init__(parent)
        self.setWindowTitle("批量匯入設定")
        self.setMinimumWidth(480)
        self._has_mscz = any(p.suffix.lower() in MSCZ_EXTENSIONS for p in files)
        self.setStyleSheet(IMPORT_DIALOG_QSS)

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        layout.addWidget(QLabel(
            f"將以同一組設定批量匯入 {len(files)} 個檔案。\n"
            "每首歌的移調會各自自動推算（依 key signature 與主旋律範圍）。"
        ))

        grid = QGridLayout()
        grid.addWidget(QLabel("右手譜偏好："), 0, 0)
        self.right_combo = QComboBox()
        for label, value in PREFER_OPTIONS:
            self.right_combo.addItem(label, value)
        grid.addWidget(self.right_combo, 0, 1, 1, 2)

        grid.addWidget(QLabel("左手譜偏好："), 1, 0)
        self.left_combo = QComboBox()
        for label, value in PREFER_OPTIONS:
            self.left_combo.addItem(label, value)
        grid.addWidget(self.left_combo, 1, 1, 1, 2)

        grid.addWidget(QLabel("聲部模式："), 2, 0)
        self.mode_combo = QComboBox()
        for label, value in MELODY_MODES:
            self.mode_combo.addItem(label, value)
        grid.addWidget(self.mode_combo, 2, 1, 1, 2)

        self.import_tempo_check = QCheckBox("匯入原譜的變速 (tempo @ 標記)")
        self.import_tempo_check.setChecked(True)
        self.import_tempo_check.setToolTip(
            "勾選:把原譜中所有速度變化轉成 tempo @<beats> <bpm> 寫入。\n"
            "不勾:整曲只用單一起始速度,適合不希望被原譜 rubato 干擾的情境。"
        )
        grid.addWidget(self.import_tempo_check, 3, 0, 1, 3)

        self.mscz_format_xml_btn: QRadioButton | None = None
        self.mscz_format_midi_btn: QRadioButton | None = None
        if self._has_mscz:
            grid.addWidget(QLabel("MSCZ 轉換格式:"), 4, 0)
            mscz_row = QHBoxLayout()
            self.mscz_format_xml_btn = QRadioButton("MusicXML(預設,保留聲部)")
            self.mscz_format_midi_btn = QRadioButton("MIDI(只取音高+時值)")
            self.mscz_format_xml_btn.setChecked(True)
            group = QButtonGroup(self)
            group.addButton(self.mscz_format_xml_btn)
            group.addButton(self.mscz_format_midi_btn)
            mscz_row.addWidget(self.mscz_format_xml_btn)
            mscz_row.addWidget(self.mscz_format_midi_btn)
            mscz_row.addStretch()
            grid.addLayout(mscz_row, 4, 1, 1, 2)

        layout.addLayout(grid)

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        layout.addWidget(bb)

        # 預設套用「建議」:右手 auto、左手 L、聲部「獨立」、匯入變速。
        self._set_combo(self.right_combo, "auto")
        self._set_combo(self.left_combo, "L")
        self._set_combo(self.mode_combo, "dense")

    @staticmethod
    def _set_combo(combo: QComboBox, data_value: str) -> None:
        for i in range(combo.count()):
            if combo.itemData(i) == data_value:
                combo.setCurrentIndex(i)
                return

    def values(self) -> dict:
        mscz_format = "musicxml"
        if self._has_mscz and self.mscz_format_midi_btn is not None and self.mscz_format_midi_btn.isChecked():
            mscz_format = "midi"
        return {
            "right_prefer": self.right_combo.currentData(),
            "left_prefer": self.left_combo.currentData(),
            "melody_mode": self.mode_combo.currentData(),
            "import_tempo_changes": self.import_tempo_check.isChecked(),
            "mscz_format": mscz_format,
        }


class BatchImportResultsDialog(QDialog):
    """批量匯入結果挑選對話框。

    QListWidget + checkbox(成功才可勾;失敗顯示錯誤、不能勾)。
    主人按下「儲存勾選」或「全部存」後,caller 透過 action() / checked_results()
    拿到使用者意圖再執行實際的 songs/ 落盤(重用 _save_imported_to_songs)。
    雙擊一列 → emit preview_requested,讓 caller 載入該首到編輯器試聽。
    """

    preview_requested = Signal(object)  # BatchImportResult

    def __init__(self, parent, results: list[BatchImportResult]) -> None:
        super().__init__(parent)
        self._results = list(results)
        self._action = "close"
        success_count = sum(1 for r in self._results if r.success)
        fail_count = len(self._results) - success_count
        self.setWindowTitle(f"批量匯入結果（成功 {success_count} / 失敗 {fail_count}）")
        self.setMinimumWidth(640)
        self.setMinimumHeight(420)
        # QDialog 預設 windowFlags 不含 minimize/maximize button,
        # Windows 下按 - 會走 hide 而非 minimize 到 taskbar,視覺上「消失」。
        # 顯式加上 min/max 讓 non-modal 結果視窗能正常縮到工作列。
        self.setWindowFlags(
            self.windowFlags()
            | Qt.WindowMinimizeButtonHint
            | Qt.WindowMaximizeButtonHint
        )

        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        layout.addWidget(QLabel(
            "雙擊一列可載入該首到編輯器試聽。\n"
            "預設全部勾選成功項目,按「儲存勾選」會把勾選的存到 songs/。"
        ))

        self.list_widget = QListWidget()
        self.list_widget.setSelectionMode(QAbstractItemView.SingleSelection)
        for result in self._results:
            item = QListWidgetItem(self._format_label(result))
            item.setData(Qt.UserRole, result)
            if result.success:
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                item.setCheckState(Qt.Checked)
            else:
                # 失敗列禁止勾選但仍可選取/雙擊(雙擊會走 preview,但 preview_requested 由 caller 自行決定要不要做事)
                item.setFlags(item.flags() & ~Qt.ItemIsUserCheckable)
                item.setForeground(Qt.gray)
            self.list_widget.addItem(item)
        self.list_widget.itemDoubleClicked.connect(self._on_double_click)
        layout.addWidget(self.list_widget, 1)

        select_row = QHBoxLayout()
        select_all_btn = QPushButton("全選")
        select_none_btn = QPushButton("全不選")
        select_all_btn.clicked.connect(lambda: self._set_all_checked(True))
        select_none_btn.clicked.connect(lambda: self._set_all_checked(False))
        select_row.addWidget(select_all_btn)
        select_row.addWidget(select_none_btn)
        select_row.addStretch()
        layout.addLayout(select_row)

        action_row = QHBoxLayout()
        save_checked_btn = QPushButton("儲存勾選到 songs/")
        save_all_btn = QPushButton("全部存到 songs/")
        close_btn = QPushButton("關閉")
        save_checked_btn.clicked.connect(self._on_save_checked)
        save_all_btn.clicked.connect(self._on_save_all)
        close_btn.clicked.connect(self.reject)
        if success_count == 0:
            save_checked_btn.setEnabled(False)
            save_all_btn.setEnabled(False)
        action_row.addStretch()
        action_row.addWidget(save_checked_btn)
        action_row.addWidget(save_all_btn)
        action_row.addWidget(close_btn)
        layout.addLayout(action_row)

    @staticmethod
    def _format_label(result: BatchImportResult) -> str:
        if not result.success:
            return f"[失敗] {result.source_path.name} — {result.error or '未知錯誤'}"
        return (
            f"{result.title or result.source_path.stem}  "
            f"({result.source_path.name}) — "
            f"右 {result.right_count} / 左 {result.left_count}, "
            f"tempo {result.tempo:g}, 移調 {result.transpose:+d}"
        )

    def _set_all_checked(self, checked: bool) -> None:
        state = Qt.Checked if checked else Qt.Unchecked
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            if item.flags() & Qt.ItemIsUserCheckable:
                item.setCheckState(state)

    def _on_double_click(self, item: QListWidgetItem) -> None:
        result: BatchImportResult = item.data(Qt.UserRole)
        if result is None or not result.success:
            return
        self.preview_requested.emit(result)

    def _on_save_checked(self) -> None:
        self._action = "save_checked"
        self.accept()

    def _on_save_all(self) -> None:
        self._action = "save_all"
        self.accept()

    def action(self) -> str:
        """回傳 'save_checked' / 'save_all' / 'close'。"""
        return self._action

    def checked_results(self) -> list[BatchImportResult]:
        out: list[BatchImportResult] = []
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            if not (item.flags() & Qt.ItemIsUserCheckable):
                continue
            if item.checkState() == Qt.Checked:
                out.append(item.data(Qt.UserRole))
        return out


def _safe_write(directory: Path, stem: str, text: str) -> Path:
    """同名衝突自動加 (2)/(3) 後綴,跟 _save_imported_to_songs 一致。"""
    target = directory / f"{stem}.txt"
    if target.exists():
        idx = 2
        while True:
            candidate = directory / f"{stem} ({idx}).txt"
            if not candidate.exists():
                target = candidate
                break
            idx += 1
    target.write_text(text, encoding="utf-8")
    return target


def _extract_title(text: str, fallback: str) -> str:
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            return line.lstrip("#").strip() or fallback
        break
    return fallback


def _process_single(path: Path, opts: dict, staging_dir: Path) -> BatchImportResult:
    """轉一首檔到 staging_dir。Exception 都被吃下並包成 success=False。"""
    suffix = path.suffix.lower()
    common = dict(
        right_prefer=opts["right_prefer"],
        left_prefer=opts["left_prefer"],
        melody_mode=opts["melody_mode"],
        import_tempo_changes=opts["import_tempo_changes"],
    )
    try:
        if suffix in MUSICXML_EXTENSIONS:
            root = MusicXMLImporter.load_score(path)
            transpose = MusicXMLImporter.suggest_transpose(root)
            text, stats = MusicXMLImporter.to_dsl(path, transpose=transpose, **common)
        elif suffix in MIDI_EXTENSIONS:
            transpose = MidiImporter.suggest_transpose(path)
            text, stats = MidiImporter.to_dsl(path, transpose=transpose, **common)
        elif suffix in MSCZ_EXTENSIONS:
            fmt = opts.get("mscz_format", "musicxml")
            if fmt == "midi":
                tmp = MsczImporter.prepare_midi(path)
                try:
                    transpose = MidiImporter.suggest_transpose(tmp)
                    text, stats = MidiImporter.to_dsl(tmp, transpose=transpose, **common)
                finally:
                    try:
                        tmp.unlink()
                    except OSError:
                        pass
            else:
                tmp = MsczImporter.prepare_musicxml(path)
                try:
                    root = MusicXMLImporter.load_score(tmp)
                    transpose = MusicXMLImporter.suggest_transpose(root)
                    text, stats = MusicXMLImporter.to_dsl(tmp, transpose=transpose, **common)
                finally:
                    try:
                        tmp.unlink()
                    except OSError:
                        pass
        else:
            return BatchImportResult(
                source_path=path,
                success=False,
                error=f"不支援的副檔名 {suffix}",
                title=path.stem,
            )

        staged = _safe_write(staging_dir, path.stem, text)
        return BatchImportResult(
            source_path=path,
            success=True,
            staged_path=staged,
            title=_extract_title(text, path.stem),
            right_count=int(stats.get("right_count", 0)),
            left_count=int(stats.get("left_count", 0)),
            tempo=float(stats.get("tempo", 0.0)),
            transpose=int(stats.get("transpose", 0)),
        )
    except Exception as exc:  # noqa: BLE001 - 跨多種 importer, 統一捕獲後降級為失敗列
        return BatchImportResult(
            source_path=path,
            success=False,
            error=f"{exc.__class__.__name__}: {exc}",
            title=path.stem,
        )


def run_batch_import(
    files: list[Path],
    opts: dict,
    staging_dir: Path,
    progress=None,
) -> list[BatchImportResult]:
    """逐一處理 files,把 DSL 寫到 staging_dir。

    progress 是可選的 QProgressDialog;若提供則每首處理前更新 label/value,
    每首處理後 processEvents 讓 UI 不凍結,並檢查 wasCanceled() 決定是否中止。

    label 用 QFontMetrics.elidedText 把檔名 elide 到固定寬度(320px),
    避免 dialog 因檔名長短跳動。

    回傳 list[BatchImportResult],順序與輸入一致。取消時只回傳已處理的部分。
    """
    results: list[BatchImportResult] = []
    total = len(files)
    fm = QFontMetrics(progress.font()) if progress is not None else None
    for i, path in enumerate(files):
        if progress is not None:
            if progress.wasCanceled():
                break
            elided = fm.elidedText(path.name, Qt.ElideMiddle, 320)
            progress.setLabelText(f"處理中 ({i + 1}/{total})  {elided}")
            progress.setValue(i)
            QApplication.processEvents()
        results.append(_process_single(path, opts, staging_dir))
        if progress is not None:
            progress.setValue(i + 1)
            QApplication.processEvents()
    return results
