"""NTE Piano 設定面板:取代原本 Ctrl+E 開的譜面文字編輯抽屜。

把所有 automation 開關(粉爪/閃避/音遊)與一般設定(動畫/視覺/匯入/焦點)都集中
到右側 dock,用 iOS 風格左右開關與拉桿即時操作。原本散落的 modal QDialog 全部
停用,改在這個 panel 內直接拉。

對外只有兩件事:
  - signal `setting_changed(key, value)` — 任何 widget 變動時 emit;主視窗收到後
    自己 `settings.set(key, value)` 並做 controller / piano roll 同步。
  - `refresh_from_settings()` — 外部變動(例如 hotkey 觸發 toggle)時呼叫,把
    最新值 pull 進 widget(過程中不會反向觸發 setting_changed)。

設計刻意不持有 SettingsManager:面板讀預設值是建構時一次性,之後完全靠
setting_changed 出 + refresh_from_settings 入。這樣 panel 不會在 settings.json
落盤鏈裡多繞一層,主視窗端能集中處理 side-effect。
"""

from __future__ import annotations

from typing import Any, Callable

from PySide6.QtCore import (
    Property,
    QEasingCurve,
    QPropertyAnimation,
    QRectF,
    Qt,
    Signal,
)
from PySide6.QtGui import QColor, QKeyEvent, QPainter, QPalette, QPen, QWheelEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QComboBox,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)


# --- SmoothScrollArea -------------------------------------------------------
# 設定面板 scroll 容器:啟用後把滾輪滾動改成 QPropertyAnimation 推 scrollbar,
# 看起來是「滑動到目標位置」而不是瞬間跳。停用時直接走父類預設行為。

class SmoothScrollArea(QScrollArea):
    """支援開關的平滑捲動 QScrollArea。

    啟用時:wheelEvent 攔住滑鼠/觸控板的 delta,累加到 `_target_value`,
    用單一 QPropertyAnimation 動態推 verticalScrollBar 到該值。連續滾動時
    重新從目前動畫值出發、累加 delta 後重啟動畫,所以多次轉動會疊加而不突兀。
    停用時 `_smooth = False`,wheelEvent 直接 super 走 Qt 預設(瞬間跳)。
    """

    _DEFAULT_DURATION_MS = 220
    _STEP_PIXELS = 90  # 每 120 delta 的滾動距離(模擬 Qt 預設 3 行 ~= 60-90px)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._smooth: bool = True
        self._target_value: int = 0
        self._anim = QPropertyAnimation(self.verticalScrollBar(), b"value", self)
        self._anim.setDuration(self._DEFAULT_DURATION_MS)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)

    def is_smooth_enabled(self) -> bool:
        return self._smooth

    def set_smooth_enabled(self, enabled: bool) -> None:
        new_state = bool(enabled)
        if new_state == self._smooth:
            return
        self._smooth = new_state
        if not new_state:
            # 關閉時把進行中的動畫停掉,後續事件走父類預設。
            self._anim.stop()

    def wheelEvent(self, event: QWheelEvent) -> None:
        if not self._smooth:
            super().wheelEvent(event)
            return
        bar = self.verticalScrollBar()
        # 水平滾動或 modifier(Ctrl 縮放等)交回父類處理,不要攔。
        if event.angleDelta().y() == 0 or event.modifiers() != Qt.NoModifier:
            super().wheelEvent(event)
            return
        # 動畫進行中:從「當前動畫終點」累加,避免使用者連滾時被尚未到達的舊目標卡住。
        if self._anim.state() == QPropertyAnimation.Running:
            start_value = self._target_value
        else:
            start_value = bar.value()
        delta_y = event.angleDelta().y()  # 一格滾輪通常 ±120
        pixels = int(round(delta_y / 120.0 * self._STEP_PIXELS))
        new_target = max(bar.minimum(), min(bar.maximum(), start_value - pixels))
        self._target_value = new_target
        self._anim.stop()
        self._anim.setStartValue(bar.value())
        self._anim.setEndValue(new_target)
        self._anim.start()
        event.accept()


# --- OpaqueComboPopupView ---------------------------------------------------
# QComboBox 預設 view 在某些 Windows 風格下,popup 會以 translucent top-level
# window 呈現:Qt 對 popup container 套 WA_TranslucentBackground=True,讓系統
# 動畫期間透出桌面/視窗背景。QSS 對這個外層 popup window 完全無效,setPalette
# 也只影響子 widget,造成展開過程純黑、完成後穿透露桌面。
#
# 解法:換掉 view,自己:
#   (1) 對 view 自身強制不透明、用 palette base 自畫底色
#   (2) 第一次 show 時抓住已被 reparent 的 popup window,對它關掉
#       WA_TranslucentBackground、開 autoFillBackground、推一份不透明 palette
# 顏色寫死成 panel_alt 同值 #1f232a,跟主視窗 THEME 一致。

class OpaqueComboPopupView(QListView):
    """不透明的 QComboBox popup view。

    Qt 預設行為在 Windows 上會把 popup container 設成 translucent top-level
    window 讓系統做淡入動畫,代價是動畫期間/結束後背景會穿透。本類別:
    - 對自身設 WA_OpaquePaintEvent / 關閉 WA_TranslucentBackground
    - 用 palette QPalette.Base 自畫底色,避免依賴 QSS popup frame 選擇器
    - showEvent 內補強 popup window:每次顯示都重套一份不透明設定。把工作
      集中在「view 已 reparent 進 popup container」這個時間點,語意最乾淨。

    為什麼還是會「第一次黑」:`setStyleSheet` 首次套用會觸發 Qt 重跑 QSS
    resolution + style polish + layout,期間 popup window 已 show 但尚未繪,
    OS 用 system black 補。修法是把這些昂貴呼叫從 showEvent 全部搬到建構
    階段(panel `_harden_combobox_popups` 內 `setView` 後立即執行),配合
    `ensurePolished()` 預熱 style cache,第一次 popup 就不再有閃黑。
    """

    _BG = QColor("#1f232a")  # = THEME["panel_alt"],跟 build_panel_qss default 一致
    _BORDER = QColor("#3a414d")  # = THEME["grid_strong"]

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WA_TranslucentBackground, False)
        self.setAttribute(Qt.WA_OpaquePaintEvent, True)
        self.setAutoFillBackground(True)
        pal = self.palette()
        pal.setColor(QPalette.Base, self._BG)
        pal.setColor(QPalette.Window, self._BG)
        self.setPalette(pal)
        # viewport 才是實際繪 item 的層,也要塗滿不透明,蓋掉父層任何穿透。
        vp = self.viewport()
        if vp is not None:
            vp.setAutoFillBackground(True)
            vp.setPalette(pal)

    def apply_to_popup_window(self) -> None:
        """對 popup window(view.window())套一份完整不透明配置。

        必須在 view 已 reparent 進 popup container 後呼叫(亦即 QComboBox
        `setView()` 之後)。重複呼叫安全。集中所有 stylesheet/palette/attr
        操作於此,避免在 showEvent 內首次呼叫造成「第一次黑」。
        """
        popup = self.window()
        if popup is None or popup is self:
            return
        popup.setAttribute(Qt.WA_TranslucentBackground, False)
        popup.setAutoFillBackground(True)
        wpal = popup.palette()
        wpal.setColor(QPalette.Base, self._BG)
        wpal.setColor(QPalette.Window, self._BG)
        popup.setPalette(wpal)
        # stylesheet 是最昂貴那一刀,提前在建構期吃掉,避免首次 popup 卡頓露黑底。
        popup.setStyleSheet(
            f"background:{self._BG.name()};border:1px solid {self._BORDER.name()};"
        )
        popup.ensurePolished()

    def showEvent(self, event):  # noqa: N802 - Qt override.
        # 防禦性補刀:`apply_to_popup_window` 應該已在建構期跑過,但若 Qt 之後
        # 重建/換 popup container(切 style、reparent 等),此處仍能把配置補回。
        # 不再做 setStyleSheet 等昂貴呼叫 — 那些首次代價已在建構期付掉。
        popup = self.window()
        if popup is not None and popup is not self:
            popup.setAttribute(Qt.WA_TranslucentBackground, False)
            popup.setAutoFillBackground(True)
        super().showEvent(event)


# --- _KeybindCaptureWidget --------------------------------------------------
# 從 piano_player.py 搬過來,粉爪「全自動 toggle 熱鍵」要用。

class KeybindCaptureWidget(QWidget):
    """擷取一個按鍵當熱鍵綁定,顯示目前綁定鍵名。

    輸出格式跟 nte_automation._heist_vk_code 接受的字串對齊:
    a-z / 0-9 單字元、f1-f12、space/shift/ctrl/alt/esc/tab/enter/backspace。
    空字串 = 停用此熱鍵。
    """

    key_changed = Signal(str)

    _SPECIAL_KEY_NAMES = {
        Qt.Key_Space: "space",
        Qt.Key_Shift: "shift",
        Qt.Key_Control: "ctrl",
        Qt.Key_Alt: "alt",
        Qt.Key_Escape: "esc",
        Qt.Key_Tab: "tab",
        Qt.Key_Backtab: "tab",
        Qt.Key_Return: "enter",
        Qt.Key_Enter: "enter",
        Qt.Key_Backspace: "backspace",
    }

    def __init__(self, parent: QWidget | None = None, initial: str = "") -> None:
        super().__init__(parent)
        self.setObjectName("keybindCaptureRow")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self._key_name: str = ""
        self._capturing: bool = False

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self._display = QLabel()
        self._display.setMinimumWidth(90)
        self._display.setFrameShape(QFrame.StyledPanel)
        self._display.setAlignment(Qt.AlignCenter)
        self._display.setObjectName("keybindDisplay")
        layout.addWidget(self._display, 1)

        self._capture_btn = QPushButton("擷取")
        self._capture_btn.setObjectName("settingsBtn")
        self._capture_btn.setToolTip("按一下後再按下要綁定的按鍵")
        self._capture_btn.clicked.connect(self._begin_capture)
        layout.addWidget(self._capture_btn)

        self._clear_btn = QPushButton("停用")
        self._clear_btn.setObjectName("settingsBtn")
        self._clear_btn.setToolTip("清除綁定,熱鍵不生效")
        self._clear_btn.clicked.connect(self._clear)
        layout.addWidget(self._clear_btn)

        self.set_key_name(initial)

    def key_name(self) -> str:
        return self._key_name

    def set_key_name(self, name: str) -> None:
        new_name = str(name or "").strip().lower()
        if new_name == self._key_name:
            self._refresh_display()
            return
        self._key_name = new_name
        self._refresh_display()
        self.key_changed.emit(self._key_name)

    def _refresh_display(self) -> None:
        if self._capturing:
            self._display.setText("請按下按鍵…")
        elif self._key_name:
            self._display.setText(self._key_name.upper())
        else:
            self._display.setText("(停用)")

    def _begin_capture(self) -> None:
        self._capturing = True
        self._refresh_display()
        self.setFocus(Qt.OtherFocusReason)
        self.grabKeyboard()

    def _end_capture(self) -> None:
        if self._capturing:
            self._capturing = False
            self.releaseKeyboard()
        self._refresh_display()

    def _clear(self) -> None:
        if self._capturing:
            self._end_capture()
        if self._key_name:
            self._key_name = ""
            self._refresh_display()
            self.key_changed.emit(self._key_name)
        else:
            self._refresh_display()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if not self._capturing:
            super().keyPressEvent(event)
            return
        name = self._resolve_key_name(event)
        if not name:
            event.accept()
            return
        self.set_key_name(name)
        self._end_capture()
        event.accept()

    def _resolve_key_name(self, event: QKeyEvent) -> str:
        key = event.key()
        if key in self._SPECIAL_KEY_NAMES:
            return self._SPECIAL_KEY_NAMES[key]
        if Qt.Key_F1 <= key <= Qt.Key_F12:
            return f"f{key - Qt.Key_F1 + 1}"
        if Qt.Key_A <= key <= Qt.Key_Z:
            return chr(ord("a") + (key - Qt.Key_A))
        if Qt.Key_0 <= key <= Qt.Key_9:
            return chr(ord("0") + (key - Qt.Key_0))
        text = event.text()
        if text and len(text) == 1 and text.isprintable() and not text.isspace():
            return text.lower()
        return ""


# --- LabeledSlider ----------------------------------------------------------
# 整數/小數共用的拉桿 + 數值顯示。內部一律用 int * scale 處理小數。

class LabeledSlider(QWidget):
    """左 label / 右數值,底部 horizontal slider。

    支援整數或小數。step 是 UI 上的最小變動單位;decimals 控制顯示位數。
    對外只有 value_changed(float) signal 與 value()/set_value(float)。
    """

    value_changed = Signal(float)

    def __init__(
        self,
        label: str,
        minimum: float,
        maximum: float,
        step: float = 1.0,
        decimals: int = 0,
        suffix: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("labeledSliderRow")
        # 自定 QWidget 子類預設不繪 QSS background/border;明確開啟才會吃到
        # build_panel_qss 內針對 #labeledSliderRow 的 transparent + border-bottom。
        self.setAttribute(Qt.WA_StyledBackground, True)
        if step <= 0:
            raise ValueError("step must be > 0")
        if maximum < minimum:
            raise ValueError("maximum must be >= minimum")

        self._min = float(minimum)
        self._max = float(maximum)
        self._step = float(step)
        self._decimals = int(decimals)
        self._suffix = str(suffix)
        # 把 [min,max] 映射成 int [0, n_steps]
        self._n_steps = int(round((self._max - self._min) / self._step))

        vbox = QVBoxLayout(self)
        vbox.setContentsMargins(0, 0, 0, 0)
        vbox.setSpacing(2)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(8)
        self._label = QLabel(label)
        self._label.setObjectName("settingsRowLabel")
        self._value_label = QLabel("")
        self._value_label.setObjectName("settingsRowValue")
        self._value_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        header.addWidget(self._label, 1)
        header.addWidget(self._value_label, 0)
        vbox.addLayout(header)

        self._slider = QSlider(Qt.Horizontal)
        self._slider.setRange(0, max(1, self._n_steps))
        self._slider.setSingleStep(1)
        self._slider.setPageStep(max(1, self._n_steps // 10))
        self._slider.setObjectName("settingsSlider")
        self._slider.valueChanged.connect(self._on_slider_changed)
        vbox.addWidget(self._slider)

        self.set_value(self._min)

    def value(self) -> float:
        return self._min + self._slider.value() * self._step

    def set_value(self, value: float) -> None:
        idx = int(round((float(value) - self._min) / self._step))
        idx = max(0, min(self._n_steps, idx))
        # blockSignals 避免外部 set 反過來觸發 value_changed
        self._slider.blockSignals(True)
        self._slider.setValue(idx)
        self._slider.blockSignals(False)
        self._update_value_label()

    def _on_slider_changed(self, _idx: int) -> None:
        self._update_value_label()
        self.value_changed.emit(self.value())

    def _update_value_label(self) -> None:
        v = self.value()
        if self._decimals <= 0:
            text = f"{int(round(v))}{self._suffix}"
        else:
            text = f"{v:.{self._decimals}f}{self._suffix}"
        self._value_label.setText(text)


# --- IOSToggleSwitch / ToggleSwitchRow --------------------------------------

class IOSToggleSwitch(QWidget):
    """iPhone 風格左右開關。

    對外 API 對齊 QPushButton(checkable):toggled(bool) signal、isChecked()、
    setChecked(bool),這樣 SettingsPanel._register / refresh_from_settings 可以
    照舊用。內部用 QPropertyAnimation 推一個 0.0~1.0 的進度值,paint 時把進度
    映射成滑塊 x 與軌道顏色混合。
    """

    toggled = Signal(bool)

    _TRACK_W = 44
    _TRACK_H = 24
    _KNOB_MARGIN = 2

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._checked = False
        self._progress = 0.0  # 0.0 = off / 左,1.0 = on / 右
        self._on_color = QColor("#4d8cff")
        self._off_color = QColor("#3a414d")
        self._knob_color = QColor("#ffffff")
        self.setObjectName("iosToggle")
        self.setFixedSize(self._TRACK_W, self._TRACK_H)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.StrongFocus)
        self._anim = QPropertyAnimation(self, b"progress", self)
        self._anim.setDuration(160)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)

    # ---- 動畫用 property(QPropertyAnimation 透過 setProgress 推進)----

    def _get_progress(self) -> float:
        return self._progress

    def _set_progress(self, value: float) -> None:
        self._progress = max(0.0, min(1.0, float(value)))
        self.update()

    progress = Property(float, _get_progress, _set_progress)

    # ---- 顏色設定(主視窗建好 panel 後可以套主題色)----

    def set_colors(self, on_color: QColor, off_color: QColor) -> None:
        self._on_color = QColor(on_color)
        self._off_color = QColor(off_color)
        self.update()

    # ---- QPushButton 相容 API ----

    def isChecked(self) -> bool:
        return self._checked

    def setChecked(self, value: bool) -> None:
        new_state = bool(value)
        if new_state == self._checked:
            return
        self._checked = new_state
        self._animate_to(1.0 if new_state else 0.0)

    def toggle(self) -> None:
        self._set_checked_emit(not self._checked)

    # ---- 內部 ----

    def _set_checked_emit(self, new_state: bool) -> None:
        if new_state == self._checked:
            return
        self._checked = new_state
        self._animate_to(1.0 if new_state else 0.0)
        self.toggled.emit(self._checked)

    def _animate_to(self, target: float) -> None:
        self._anim.stop()
        self._anim.setStartValue(self._progress)
        self._anim.setEndValue(float(target))
        self._anim.start()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self._set_checked_emit(not self._checked)
            event.accept()
            return
        super().mousePressEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in (Qt.Key_Space, Qt.Key_Return, Qt.Key_Enter):
            self._set_checked_emit(not self._checked)
            event.accept()
            return
        super().keyPressEvent(event)

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)

        # 軌道:在 off/on 之間線性混色
        t = self._progress
        bg = QColor(
            int(self._off_color.red() * (1 - t) + self._on_color.red() * t),
            int(self._off_color.green() * (1 - t) + self._on_color.green() * t),
            int(self._off_color.blue() * (1 - t) + self._on_color.blue() * t),
        )
        track_rect = QRectF(0, 0, self.width(), self.height())
        radius = self.height() / 2.0
        p.setPen(Qt.NoPen)
        p.setBrush(bg)
        p.drawRoundedRect(track_rect, radius, radius)

        # 滑塊
        knob_d = self.height() - 2 * self._KNOB_MARGIN
        x_min = self._KNOB_MARGIN
        x_max = self.width() - self._KNOB_MARGIN - knob_d
        knob_x = x_min + (x_max - x_min) * t
        knob_rect = QRectF(knob_x, self._KNOB_MARGIN, knob_d, knob_d)
        # 陰影(畫一圈半透明黑)
        shadow = QColor(0, 0, 0, 60)
        p.setBrush(shadow)
        p.drawEllipse(knob_rect.translated(0, 1))
        # 主體
        p.setBrush(self._knob_color)
        p.drawEllipse(knob_rect)

        # 聚焦框(鍵盤導覽)
        if self.hasFocus():
            pen = QPen(self._on_color)
            pen.setWidthF(1.4)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(track_rect.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)


class ToggleSwitchRow(QWidget):
    """「左 label / 右 iOS 開關」橫列容器。

    對外暴露跟 IOSToggleSwitch 同名的 toggled/isChecked/setChecked,讓
    SettingsPanel._register 的 getter/setter 寫起來跟原本 ToggleButton 一樣短。
    """

    toggled = Signal(bool)

    def __init__(self, label: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("settingsToggleRow")
        self.setAttribute(Qt.WA_StyledBackground, True)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(2, 4, 2, 4)
        layout.setSpacing(10)

        self._label = QLabel(label)
        self._label.setObjectName("settingsToggleLabel")
        self._label.setWordWrap(True)
        self._label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        layout.addWidget(self._label, 1)

        self._switch = IOSToggleSwitch(self)
        self._switch.toggled.connect(self.toggled.emit)
        layout.addWidget(self._switch, 0, Qt.AlignVCenter)

    def isChecked(self) -> bool:
        return self._switch.isChecked()

    def setChecked(self, value: bool) -> None:
        self._switch.setChecked(value)

    def switch(self) -> IOSToggleSwitch:
        return self._switch


# --- ColorPickerButton ------------------------------------------------------
# 點擊開 QColorDialog 的色塊。背景色 = 當前色,文字 = hex(對比色)。給「自訂
# 音符顏色」用,可日後給其他 panel 設定重用。

class ColorPickerButton(QPushButton):
    """色塊按鈕:點擊跳 QColorDialog,選色後 emit color_changed(hex)。"""

    color_changed = Signal(str)

    def __init__(self, initial_hex: str = "#ff7a59", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("colorPickerBtn")
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedHeight(28)
        self.setMinimumWidth(90)
        self._color = QColor(initial_hex if QColor(initial_hex).isValid() else "#ff7a59")
        self._apply_visual()
        self.clicked.connect(self._on_clicked)

    def color_hex(self) -> str:
        return self._color.name()

    def set_color_hex(self, hex_str: str) -> None:
        c = QColor(hex_str)
        if not c.isValid() or c.name() == self._color.name():
            return
        self._color = c
        self._apply_visual()

    def _apply_visual(self) -> None:
        # 文字色:依背景亮度決定黑或白,確保 hex 數字看得清。
        l = (self._color.red() * 299 + self._color.green() * 587 + self._color.blue() * 114) / 1000
        text_color = "#16181d" if l > 140 else "#f0f2f5"
        self.setText(self._color.name().upper())
        self.setStyleSheet(
            f"QPushButton#colorPickerBtn{{background:{self._color.name()};"
            f"color:{text_color};border:1px solid #3a414d;border-radius:6px;"
            f"padding:2px 8px;font-family:'Cascadia Mono';font-size:11px;}}"
            f"QPushButton#colorPickerBtn:hover{{border:1px solid #ff7a59;}}"
        )

    def _on_clicked(self) -> None:
        # 用 modal QColorDialog,簡單可靠。getColor 不修改 widget,要自己 set。
        chosen = QColorDialog.getColor(self._color, self, "選擇顏色")
        if chosen.isValid() and chosen.name() != self._color.name():
            self._color = chosen
            self._apply_visual()
            self.color_changed.emit(self._color.name())


# --- SettingsPanel ----------------------------------------------------------

# Settings accessor — 主視窗端傳進來的 (key) -> value 函式。面板用它讀初始值,
# 寫值統一透過 setting_changed signal,主視窗自己處理 settings.set 與 side-effect。
SettingsGetter = Callable[[str, Any], Any]


class SettingsPanel(QWidget):
    """右側 dock 的設定面板。

    對外:
      - signal setting_changed(key: str, value): 任何 widget 變動時 emit。
        主視窗收到後 settings.set + 視 key 做 controller / piano roll / task
        啟停同步。面板自己不做任何 side-effect。
      - refresh_from_settings(getter): 用 getter 重新拉所有 widget 的值。過程
        中 signal 被 block,不會反向觸發 setting_changed。

    NOTE: NTE Piano 既有 `THEME` / `NOTE_COLOR_STYLES` 由建構時傳入,避免
    panel 模組反向 import piano_player(會循環)。
    """

    setting_changed = Signal(str, object)

    # Combo / 選項清單 — 跟 nte_automation 後端接受的字串對齊。
    _HEIST_TRIGGER_OPTIONS = [
        ("f", "F"), ("e", "E"), ("g", "G"), ("r", "R"),
        ("space", "Space"), ("shift", "Shift"), ("ctrl", "Ctrl"), ("alt", "Alt"),
    ]
    _DODGE_KEY_OPTIONS = [
        ("shift", "Shift"), ("space", "Space"), ("ctrl", "Ctrl"),
    ]
    _FPS_OPTIONS = [(30, "30"), (60, "60"), (120, "120")]

    def __init__(
        self,
        get_setting: SettingsGetter,
        note_color_styles: dict,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        # 給 root 一個 objectName 讓 QSS 能定位它,避免主視窗全域 QWidget rule
        # 把它塗成 bg(黑色),跟 GroupBox 不一致。
        self.setObjectName("nteSettingsRoot")
        self._get = get_setting
        self._note_color_styles = note_color_styles
        self._suppress = False  # refresh 期間阻止 setting_changed 反向 emit

        # widget 註冊表:_widgets[key] = (widget, getter, setter)
        # 用於 refresh_from_settings 統一更新。
        self._widgets: dict[str, tuple[QWidget, Callable[[], Any], Callable[[Any], None]]] = {}

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        scroll = SmoothScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setObjectName("settingsScroll")
        scroll.set_smooth_enabled(bool(self._get("smooth_scroll_enabled", True)))
        outer.addWidget(scroll)
        self._scroll = scroll

        content = QWidget()
        content.setObjectName("settingsContent")
        scroll.setWidget(content)
        self._content_layout = QVBoxLayout(content)
        self._content_layout.setContentsMargins(10, 10, 10, 14)
        self._content_layout.setSpacing(10)

        self._build_groups()
        self._content_layout.addStretch(1)
        # QSS 對 QComboBox 的 popup 容器(外層 OS-level window)塗色不可靠,
        # 動畫期間 popup window 已顯示但 view 還沒繪完 → 露出 OS 預設黑底;
        # 動畫結束後 view 雖然吃到 panel_alt,但外框 frame 可能仍透明出殘影。
        # 解法:對每個 QComboBox 的 view + view.window() 強制 autoFillBackground
        # 並把 palette base/window 設成 panel_alt,讓底色從動畫第一幀就到位。
        self._harden_combobox_popups()

    # ----- 主結構 -----

    def _build_groups(self) -> None:
        self._build_playback_group()
        self._build_visual_group()
        self._build_focus_group()
        self._build_import_group()
        self._build_dialogs_group()
        self._build_dodge_group()
        self._build_rhythm_group()
        self._build_heist_group()

    def _add_group(self, title: str) -> QGridLayout:
        group = QGroupBox(title)
        group.setObjectName("settingsGroup")
        grid = QGridLayout(group)
        grid.setContentsMargins(10, 14, 10, 12)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(8)
        self._content_layout.addWidget(group)
        return grid

    # ----- group 們 -----

    def _build_playback_group(self) -> None:
        g = self._add_group("播放")

        speed = LabeledSlider("速度", 0.5, 2.0, 0.05, decimals=2, suffix="×")
        speed.set_value(float(self._get("playback_speed", 1.0)))
        speed.value_changed.connect(lambda v: self._emit("playback_speed", float(v)))
        g.addWidget(speed, 0, 0, 1, 2)
        self._register("playback_speed", speed, speed.value, speed.set_value)

        zoom = LabeledSlider("Piano Roll 縮放", 0.4, 3.0, 0.05, decimals=2, suffix="×")
        zoom.set_value(float(self._get("zoom_factor", 1.0)))
        zoom.value_changed.connect(lambda v: self._emit("zoom_factor", float(v)))
        g.addWidget(zoom, 1, 0, 1, 2)
        self._register("zoom_factor", zoom, zoom.value, zoom.set_value)

        countdown = LabeledSlider("倒數秒數", 0, 10, 1, decimals=0, suffix=" 秒")
        countdown.set_value(int(self._get("countdown_seconds", 0)))
        countdown.value_changed.connect(lambda v: self._emit("countdown_seconds", int(round(v))))
        g.addWidget(countdown, 2, 0, 1, 2)
        self._register(
            "countdown_seconds", countdown,
            lambda: int(round(countdown.value())), lambda v: countdown.set_value(float(v))
        )

    def _build_visual_group(self) -> None:
        g = self._add_group("視覺")

        # FPS 用 3 檔 slider snap
        fps = LabeledSlider("Piano Roll FPS", 0, 2, 1, decimals=0)
        # 3 檔顯示文字自訂
        cur_fps = int(self._get("roll_fps", 60))
        cur_idx = next((i for i, (v, _) in enumerate(self._FPS_OPTIONS) if v == cur_fps), 1)
        fps.set_value(cur_idx)

        def _fps_label_text(_i: float) -> None:
            i = int(round(fps.value()))
            opt = self._FPS_OPTIONS[max(0, min(2, i))]
            fps._value_label.setText(f"{opt[1]} FPS")

        fps.value_changed.connect(_fps_label_text)
        fps.value_changed.connect(lambda v: self._emit("roll_fps", int(self._FPS_OPTIONS[int(round(v))][0])))
        _fps_label_text(cur_idx)
        g.addWidget(fps, 0, 0, 1, 2)

        def _fps_set(val: Any) -> None:
            v = int(val)
            for i, (opt_v, _) in enumerate(self._FPS_OPTIONS):
                if opt_v == v:
                    fps.set_value(i)
                    _fps_label_text(i)
                    return

        self._register(
            "roll_fps", fps,
            lambda: int(self._FPS_OPTIONS[int(round(fps.value()))][0]),
            _fps_set,
        )

        # 音色 ComboBox
        g.addWidget(QLabel("音符配色"), 1, 0)
        color = QComboBox()
        for key, style in self._note_color_styles.items():
            color.addItem(str(style.get("label", key)), key)
        cur_color = str(self._get("note_color_style", "default"))
        self._set_combo_data(color, cur_color)
        color.currentIndexChanged.connect(
            lambda _i: self._emit("note_color_style", str(color.currentData() or "default"))
        )
        g.addWidget(color, 1, 1)
        self._register(
            "note_color_style", color,
            lambda: str(color.currentData() or "default"),
            lambda v: self._set_combo_data(color, str(v)),
        )

        # 自訂三色 picker(H/M/L)。只在 note_color_style == "custom" 時 enabled,
        # 其他 style 時灰掉以示意「目前 dropdown 已蓋掉這三格」。
        self._custom_color_pickers: dict[str, ColorPickerButton] = {}
        for row_idx, (octave_key, octave_label, default_hex, setting_key) in enumerate((
            ("H", "高音(H) 顏色", "#ff7a59", "custom_note_h_color"),
            ("M", "中音(M) 顏色", "#4dd0c2", "custom_note_m_color"),
            ("L", "低音(L) 顏色", "#8a7cff", "custom_note_l_color"),
        ), start=2):
            g.addWidget(QLabel(octave_label), row_idx, 0)
            picker = ColorPickerButton(str(self._get(setting_key, default_hex)))
            picker.color_changed.connect(
                lambda hex_str, k=setting_key: self._emit(k, hex_str)
            )
            g.addWidget(picker, row_idx, 1)
            self._custom_color_pickers[octave_key] = picker
            self._register(setting_key, picker, picker.color_hex, picker.set_color_hex)

        def _sync_pickers_enabled():
            enabled = (str(color.currentData() or "default") == "custom")
            for p in self._custom_color_pickers.values():
                p.setEnabled(enabled)
        color.currentIndexChanged.connect(lambda _i: _sync_pickers_enabled())
        _sync_pickers_enabled()

        # 三個 toggle
        pitch = ToggleSwitchRow("依音高排序顯示")
        pitch.setChecked(bool(self._get("pitch_sort_mode", False)))
        pitch.toggled.connect(lambda c: self._emit("pitch_sort_mode", bool(c)))
        g.addWidget(pitch, 5, 0, 1, 2)
        self._register("pitch_sort_mode", pitch, pitch.isChecked, pitch.setChecked)

        kbd = ToggleSwitchRow("顯示遊戲鍵盤")
        kbd.setChecked(bool(self._get("show_piano_keyboard", True)))
        kbd.toggled.connect(lambda c: self._emit("show_piano_keyboard", bool(c)))
        g.addWidget(kbd, 6, 0, 1, 2)
        self._register("show_piano_keyboard", kbd, kbd.isChecked, kbd.setChecked)

        anim = ToggleSwitchRow("動畫效果")
        anim.setChecked(bool(self._get("animations_enabled", True)))
        anim.toggled.connect(lambda c: self._emit("animations_enabled", bool(c)))
        g.addWidget(anim, 7, 0, 1, 2)
        self._register("animations_enabled", anim, anim.isChecked, anim.setChecked)

        # 平滑捲動 toggle:直接在面板內套用到自家 scroll area,不靠主視窗繞回來。
        smooth = ToggleSwitchRow("平滑捲動設定面板")
        smooth.setChecked(bool(self._get("smooth_scroll_enabled", True)))

        def _on_smooth_toggled(checked: bool) -> None:
            self._scroll.set_smooth_enabled(bool(checked))
            self._emit("smooth_scroll_enabled", bool(checked))

        smooth.toggled.connect(_on_smooth_toggled)
        g.addWidget(smooth, 8, 0, 1, 2)

        def _smooth_set(value: Any) -> None:
            smooth.setChecked(bool(value))
            self._scroll.set_smooth_enabled(bool(value))

        self._register(
            "smooth_scroll_enabled", smooth, smooth.isChecked, _smooth_set
        )

        # 音樂編輯區(piano roll)滾輪平滑捲動。獨立於上面設定面板那個 toggle。
        # 關閉後回到一格一格離散跳。
        smooth_roll = ToggleSwitchRow("平滑捲動音樂編輯區")
        smooth_roll.setChecked(bool(self._get("smooth_scroll_pianoroll", True)))
        smooth_roll.toggled.connect(
            lambda c: self._emit("smooth_scroll_pianoroll", bool(c))
        )
        g.addWidget(smooth_roll, 9, 0, 1, 2)
        self._register(
            "smooth_scroll_pianoroll",
            smooth_roll,
            smooth_roll.isChecked,
            smooth_roll.setChecked,
        )

        # 音樂編輯區 Ctrl+滾輪縮放平滑過渡。140ms OutCubic;關閉時直接生效無動畫。
        smooth_zoom = ToggleSwitchRow("平滑縮放音樂編輯區")
        smooth_zoom.setChecked(bool(self._get("smooth_zoom_pianoroll", True)))
        smooth_zoom.toggled.connect(
            lambda c: self._emit("smooth_zoom_pianoroll", bool(c))
        )
        g.addWidget(smooth_zoom, 10, 0, 1, 2)
        self._register(
            "smooth_zoom_pianoroll",
            smooth_zoom,
            smooth_zoom.isChecked,
            smooth_zoom.setChecked,
        )

    def _build_focus_group(self) -> None:
        g = self._add_group("遊戲視窗")

        focus = ToggleSwitchRow("播放時自動聚焦遊戲視窗")
        focus.setChecked(bool(self._get("focus_game_on_play", True)))
        focus.toggled.connect(lambda c: self._emit("focus_game_on_play", bool(c)))
        g.addWidget(focus, 0, 0, 1, 2)
        self._register("focus_game_on_play", focus, focus.isChecked, focus.setChecked)

        pause = ToggleSwitchRow("失焦時自動暫停")
        pause.setChecked(bool(self._get("auto_pause_on_focus_loss", False)))
        pause.toggled.connect(lambda c: self._emit("auto_pause_on_focus_loss", bool(c)))
        g.addWidget(pause, 1, 0, 1, 2)
        self._register("auto_pause_on_focus_loss", pause, pause.isChecked, pause.setChecked)

        mute = ToggleSwitchRow("失焦時自動靜音遊戲")
        mute.setChecked(bool(self._get("mute_on_focus_loss", False)))
        mute.toggled.connect(lambda c: self._emit("mute_on_focus_loss", bool(c)))
        g.addWidget(mute, 2, 0, 1, 2)
        self._register("mute_on_focus_loss", mute, mute.isChecked, mute.setChecked)

    def _build_import_group(self) -> None:
        g = self._add_group("匯入")

        trim = ToggleSwitchRow("自動跳過譜面開頭空白")
        trim.setChecked(bool(self._get("auto_trim_leading_silence", True)))
        trim.toggled.connect(lambda c: self._emit("auto_trim_leading_silence", bool(c)))
        g.addWidget(trim, 0, 0, 1, 2)
        self._register(
            "auto_trim_leading_silence", trim, trim.isChecked, trim.setChecked
        )

        tempo = ToggleSwitchRow("匯入時一起匯入變速 (@)")
        tempo.setChecked(bool(self._get("import_tempo_changes", False)))
        tempo.toggled.connect(lambda c: self._emit("import_tempo_changes", bool(c)))
        g.addWidget(tempo, 1, 0, 1, 2)
        self._register("import_tempo_changes", tempo, tempo.isChecked, tempo.setChecked)

    def _build_dialogs_group(self) -> None:
        g = self._add_group("對話框")

        # 切歌/開檔/匯入/關視窗時若譜面 dirty,跳「尚未儲存」確認。
        # 關掉等於「總是同意覆蓋」,換歌不會被打斷,但 dirty 改動可能被無聲丟棄。
        discard = ToggleSwitchRow("未存檔提示")
        discard.setChecked(bool(self._get("confirm_discard_unsaved", True)))
        discard.toggled.connect(
            lambda c: self._emit("confirm_discard_unsaved", bool(c))
        )
        g.addWidget(discard, 0, 0, 1, 2)
        self._register(
            "confirm_discard_unsaved", discard, discard.isChecked, discard.setChecked
        )

        # 按「刪除目前歌曲」前是否要再問一次。關掉後點到就直接刪檔。
        delete = ToggleSwitchRow("刪除歌曲提示")
        delete.setChecked(bool(self._get("confirm_delete_song", True)))
        delete.toggled.connect(
            lambda c: self._emit("confirm_delete_song", bool(c))
        )
        g.addWidget(delete, 1, 0, 1, 2)
        self._register(
            "confirm_delete_song", delete, delete.isChecked, delete.setChecked
        )

    def _build_dodge_group(self) -> None:
        g = self._add_group("自動閃避 (F10)")

        enable = ToggleSwitchRow("啟用")
        enable.setChecked(bool(self._get("dodge_active", False)))
        enable.toggled.connect(lambda c: self._emit("dodge_active", bool(c)))
        g.addWidget(enable, 0, 0, 1, 2)
        self._register("dodge_active", enable, enable.isChecked, enable.setChecked)

        thr = LabeledSlider("閃避閾值", 0.01, 1.0, 0.01, decimals=2)
        thr.set_value(float(self._get("dodge_threshold", 0.13)))
        thr.value_changed.connect(lambda v: self._emit("dodge_threshold", float(v)))
        g.addWidget(thr, 1, 0, 1, 2)
        self._register("dodge_threshold", thr, thr.value, thr.set_value)

        ctr = LabeledSlider("反擊閾值", 0.01, 1.0, 0.01, decimals=2)
        ctr.set_value(float(self._get("dodge_counter_threshold", 0.12)))
        ctr.value_changed.connect(lambda v: self._emit("dodge_counter_threshold", float(v)))
        g.addWidget(ctr, 2, 0, 1, 2)
        self._register("dodge_counter_threshold", ctr, ctr.value, ctr.set_value)

        g.addWidget(QLabel("閃避按鍵"), 3, 0)
        key = QComboBox()
        for v, label in self._DODGE_KEY_OPTIONS:
            key.addItem(label, v)
        self._set_combo_data(key, str(self._get("dodge_key", "shift")))
        key.currentIndexChanged.connect(
            lambda _i: self._emit("dodge_key", str(key.currentData() or "shift"))
        )
        g.addWidget(key, 3, 1)
        self._register(
            "dodge_key", key,
            lambda: str(key.currentData() or "shift"),
            lambda v: self._set_combo_data(key, str(v)),
        )

        mouse = ToggleSwitchRow("反擊用滑鼠左鍵")
        mouse.setChecked(bool(self._get("dodge_counter_use_mouse", True)))
        mouse.toggled.connect(lambda c: self._emit("dodge_counter_use_mouse", bool(c)))
        g.addWidget(mouse, 4, 0, 1, 2)
        self._register(
            "dodge_counter_use_mouse", mouse, mouse.isChecked, mouse.setChecked
        )

    def _build_rhythm_group(self) -> None:
        g = self._add_group("自動音遊 (F11)")

        enable = ToggleSwitchRow("啟用")
        enable.setChecked(bool(self._get("rhythm_active", False)))
        enable.toggled.connect(lambda c: self._emit("rhythm_active", bool(c)))
        g.addWidget(enable, 0, 0, 1, 2)
        self._register("rhythm_active", enable, enable.isChecked, enable.setChecked)

        loop = LabeledSlider("循環次數(0=無限)", 0, 50, 1, decimals=0, suffix=" 次")
        loop.set_value(int(self._get("rhythm_loop_count", 0)))
        loop.value_changed.connect(lambda v: self._emit("rhythm_loop_count", int(round(v))))
        g.addWidget(loop, 1, 0, 1, 2)
        self._register(
            "rhythm_loop_count", loop,
            lambda: int(round(loop.value())), lambda v: loop.set_value(float(v))
        )

        timeout = LabeledSlider("單曲超時", 30, 600, 10, decimals=0, suffix=" 秒")
        timeout.set_value(int(self._get("rhythm_timeout_seconds", 180)))
        timeout.value_changed.connect(
            lambda v: self._emit("rhythm_timeout_seconds", int(round(v)))
        )
        g.addWidget(timeout, 2, 0, 1, 2)
        self._register(
            "rhythm_timeout_seconds", timeout,
            lambda: int(round(timeout.value())), lambda v: timeout.set_value(float(v))
        )

        g.addWidget(QLabel("4 軌按鍵(逗號分隔)"), 3, 0)
        keys = QLineEdit()
        keys.setObjectName("settingsLineEdit")
        keys.setText(str(self._get("rhythm_track_keys", "d,f,j,k")))
        keys.editingFinished.connect(
            lambda: self._emit("rhythm_track_keys", keys.text().strip() or "d,f,j,k")
        )
        g.addWidget(keys, 3, 1)
        self._register(
            "rhythm_track_keys", keys,
            lambda: keys.text().strip() or "d,f,j,k",
            lambda v: keys.setText(str(v)),
        )

        delay = LabeledSlider("打擊延遲", -100, 200, 5, decimals=0, suffix=" ms")
        delay.set_value(int(self._get("rhythm_delay_ms", 0)))
        delay.value_changed.connect(lambda v: self._emit("rhythm_delay_ms", int(round(v))))
        g.addWidget(delay, 4, 0, 1, 2)
        self._register(
            "rhythm_delay_ms", delay,
            lambda: int(round(delay.value())), lambda v: delay.set_value(float(v))
        )

    def _build_heist_group(self) -> None:
        g = self._add_group("粉爪大劫案")

        enable = ToggleSwitchRow("啟用快速拾取")
        enable.setChecked(bool(self._get("heist_enabled", False)))
        enable.toggled.connect(lambda c: self._emit("heist_enabled", bool(c)))
        g.addWidget(enable, 0, 0, 1, 2)
        self._register("heist_enabled", enable, enable.isChecked, enable.setChecked)

        g.addWidget(QLabel("觸發鍵"), 1, 0)
        trig = QComboBox()
        for v, label in self._HEIST_TRIGGER_OPTIONS:
            trig.addItem(label, v)
        self._set_combo_data(trig, str(self._get("heist_trigger_key", "f")))
        trig.currentIndexChanged.connect(
            lambda _i: self._emit("heist_trigger_key", str(trig.currentData() or "f"))
        )
        g.addWidget(trig, 1, 1)
        self._register(
            "heist_trigger_key", trig,
            lambda: str(trig.currentData() or "f"),
            lambda v: self._set_combo_data(trig, str(v)),
        )

        auto = ToggleSwitchRow("全自動拾取(不必按住觸發鍵)")
        auto.setChecked(bool(self._get("heist_auto_mode", False)))
        auto.toggled.connect(lambda c: self._emit("heist_auto_mode", bool(c)))
        g.addWidget(auto, 2, 0, 1, 2)
        self._register("heist_auto_mode", auto, auto.isChecked, auto.setChecked)

        g.addWidget(QLabel("全自動 toggle 熱鍵"), 3, 0)
        hotkey = KeybindCaptureWidget(
            initial=str(self._get("heist_auto_mode_hotkey", "f8"))
        )
        hotkey.key_changed.connect(
            lambda name: self._emit("heist_auto_mode_hotkey", str(name))
        )
        g.addWidget(hotkey, 3, 1)
        self._register(
            "heist_auto_mode_hotkey", hotkey, hotkey.key_name, hotkey.set_key_name
        )

    # ----- 共用工具 -----

    def _harden_combobox_popups(self) -> None:
        """把所有 QComboBox 的 view 換成 OpaqueComboPopupView,徹底解掉
        popup 在 Windows 上「展開過程純黑、完成後穿透露桌面」的問題。

        根因:Qt 預設 popup container 帶 WA_TranslucentBackground 做系統動畫,
        QSS / setPalette 對外層 popup window 都打不到。OpaqueComboPopupView 把
        view 自身設成不透明、palette 推 panel_alt;再透過 `apply_to_popup_window`
        對 reparent 後的 popup window 同步處理。

        為什麼這裡呼叫 apply_to_popup_window 而不依賴 showEvent:`setStyleSheet`
        首次套用要重跑 QSS resolution + polish + layout,如果留到 showEvent 才做,
        popup window 已 show 但繪未繪完,系統補黑底。預先在建構期吃掉這一刀,
        再加 ensurePolished 暖 style cache,第一次點開就沒閃黑。
        """
        for combo in self.findChildren(QComboBox):
            view = OpaqueComboPopupView(combo)
            combo.setView(view)
            # setView 後 view 已 reparent 進 popup container,window() 拿得到。
            # 立刻把所有昂貴的 stylesheet/palette/attr 設定吃掉,並 polish。
            view.apply_to_popup_window()
            combo.ensurePolished()

    @staticmethod
    def _set_combo_data(combo: QComboBox, data_value: str) -> None:
        for i in range(combo.count()):
            if combo.itemData(i) == data_value:
                combo.blockSignals(True)
                combo.setCurrentIndex(i)
                combo.blockSignals(False)
                return

    def _register(
        self,
        key: str,
        widget: QWidget,
        getter: Callable[[], Any],
        setter: Callable[[Any], None],
    ) -> None:
        self._widgets[key] = (widget, getter, setter)

    def _emit(self, key: str, value: Any) -> None:
        if self._suppress:
            return
        self.setting_changed.emit(key, value)

    # ----- 對外 API -----

    def refresh_from_settings(self, get_setting: SettingsGetter | None = None) -> None:
        """從外部 settings 重新拉所有 widget 值,不會反向 emit setting_changed。

        傳新的 getter 進來可以一次切換 settings 來源;不傳就用建構時那個。
        """
        if get_setting is not None:
            self._get = get_setting
        self._suppress = True
        try:
            for key, (_, _, setter) in self._widgets.items():
                value = self._get(key, None)
                if value is not None:
                    try:
                        setter(value)
                    except Exception:  # noqa: BLE001
                        # 個別 widget set 失敗不要連帶炸掉整個 refresh
                        pass
        finally:
            self._suppress = False

    def widget_for(self, key: str) -> QWidget | None:
        entry = self._widgets.get(key)
        return entry[0] if entry else None


# --- QSS for panel widgets --------------------------------------------------

def build_panel_qss(theme: dict) -> str:
    """產生面板專用 QSS。沿用 NTE Piano THEME 顏色,套 randomChoice 圓角風格。

    要點:
      - 所有規則用 objectName 限定(settingsBtn / settingsToggle / settingsSlider
        / settingsGroup / settingsContent / settingsScroll / keybindDisplay /
        settingsLineEdit / settingsRowLabel / settingsRowValue / settingsStatusLabel)
      - 不汙染主視窗其他 QPushButton / QSlider 樣式
    """
    panel = theme.get("panel", "#1a1d23")
    panel_alt = theme.get("panel_alt", "#1f232a")
    fg = theme.get("fg", "#e6e8ec")
    fg_dim = theme.get("fg_dim", "#9aa1ad")
    fg_subtle = theme.get("fg_subtle", "#6b7280")
    accent = theme.get("accent", "#ff7a59")
    grid = theme.get("grid", "#262a33")
    grid_strong = theme.get("grid_strong", "#3a414d")
    play = theme.get("play", "#4d8cff")

    return f"""
/* 兜底:面板內所有子 QWidget 預設透明,免得主視窗的
   `QMainWindow, QWidget {{ background-color: bg }}` 規則漏進來,把
   GroupBox 內部塗成更暗的 bg(看起來像黑色色塊)。
   下面 #nteSettingsRoot / #settingsContent / GroupBox 再依需要覆寫顏色。 */
QWidget#nteSettingsRoot QWidget {{
    background: transparent;
}}
QWidget#nteSettingsRoot {{
    background: {panel};
}}
QWidget#settingsContent {{
    background: {panel};
}}
QScrollArea#settingsScroll {{
    background: {panel};
    border: none;
}}

QWidget#nteSettingsRoot QGroupBox#settingsGroup {{
    background: {panel_alt};
    border: 1px solid {grid_strong};
    border-radius: 10px;
    margin-top: 14px;
    padding: 14px 4px 4px 4px;
    font-weight: 700;
    color: {fg};
}}
QWidget#nteSettingsRoot QGroupBox#settingsGroup::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 12px;
    padding: 0 8px;
    color: {accent};
    font-size: 12px;
    font-weight: 700;
}}

/* GroupBox 內每個 row 容器:背景透明顯出 GroupBox 的 panel_alt,
   底部 1px 淡線拉開 row 間距。最後一列由 ::last-child 移除分隔,但 Qt QSS
   不支援 :last-child,所以採均勻分隔,視覺上仍清晰。 */
QWidget#labeledSliderRow,
QWidget#settingsToggleRow,
QWidget#keybindCaptureRow {{
    background: transparent;
    border-bottom: 1px solid {grid};
    padding-bottom: 4px;
}}

QLabel#settingsRowLabel {{ color: {fg}; font-size: 12px; background: transparent; }}
QLabel#settingsRowValue {{ color: {fg_dim}; font-size: 12px; font-weight: 600; background: transparent; }}
QLabel#settingsStatusLabel {{ color: {fg_dim}; font-size: 12px; background: transparent; }}
QLabel#settingsToggleLabel {{
    color: {fg};
    font-size: 13px;
    font-weight: 500;
    background: transparent;
}}
QLabel#keybindDisplay {{
    background: {panel};
    border: 1px solid {grid_strong};
    border-radius: 6px;
    padding: 4px 8px;
    color: {fg};
    font-weight: 600;
}}

QPushButton#settingsBtn {{
    background: {panel};
    color: {fg_dim};
    border: 1px solid {grid_strong};
    border-radius: 6px;
    padding: 6px 14px;
    font-size: 12px;
    font-weight: 600;
}}
QPushButton#settingsBtn:hover {{
    background: #2a2e36;
    border: 1px solid {accent};
    color: {fg};
}}
QPushButton#settingsBtn:pressed {{
    background: #16191f;
}}

QComboBox {{
    background: {panel};
    color: {fg};
    border: 1px solid {grid_strong};
    border-radius: 6px;
    padding: 4px 8px;
    min-height: 24px;
}}
QComboBox:hover {{ border: 1px solid {accent}; }}
QComboBox QAbstractItemView {{
    background: {panel_alt};
    color: {fg};
    border: 1px solid {grid_strong};
    selection-background-color: {accent};
    selection-color: #16181d;
    outline: 0;
}}
/* QListView 是 popup 內部 itemView 的具體型別,某些 style 只認這個 selector;
   另外把 popup 容器自身 (QFrame/QWidget) 塗上同底色,避免動畫期間 OS popup
   window 露出黑底、或動畫結束後外框看起來透明。 */
QComboBox QListView {{
    background: {panel_alt};
    color: {fg};
    border: none;
    outline: 0;
}}
QComboBox QFrame {{
    background: {panel_alt};
    border: 1px solid {grid_strong};
}}
QComboBox QAbstractItemView::item {{
    padding: 4px 8px;
    min-height: 22px;
}}
QComboBox QAbstractItemView::item:hover {{
    background: {grid_strong};
    color: {fg};
}}

QLineEdit#settingsLineEdit {{
    background: {panel};
    color: {fg};
    border: 1px solid {grid_strong};
    border-radius: 6px;
    padding: 4px 8px;
}}
QLineEdit#settingsLineEdit:focus {{ border: 1px solid {accent}; }}

QSlider#settingsSlider {{
    min-height: 22px;
}}
QSlider#settingsSlider::groove:horizontal {{
    height: 6px;
    border-radius: 3px;
    background: {grid};
}}
QSlider#settingsSlider::sub-page:horizontal {{
    height: 6px;
    border-radius: 3px;
    background: qlineargradient(
        x1:0, y1:0, x2:1, y2:0,
        stop:0 {accent}, stop:1 {play}
    );
}}
QSlider#settingsSlider::add-page:horizontal {{
    height: 6px;
    border-radius: 3px;
    background: {grid};
}}
QSlider#settingsSlider::handle:horizontal {{
    width: 14px;
    height: 14px;
    margin: -5px 0;
    border-radius: 8px;
    background: qradialgradient(
        cx:0.5, cy:0.5, radius:0.6, fx:0.4, fy:0.4,
        stop:0 #ffffff, stop:1 #d0d4dc
    );
    border: 1px solid {accent};
}}
QSlider#settingsSlider::handle:horizontal:hover {{
    border: 2px solid {accent};
}}
"""
