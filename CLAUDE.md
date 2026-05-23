# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 專案定位

針對 NTE 遊戲（程序名 `HTGame.exe`，視窗標題含 `NTE`）內建鋼琴介面的自動演奏工具，Windows-only PySide6 桌面 GUI。專案採模組化架構，主檔 `piano_player.py` 只負責 GUI 組裝與控制流程，邏輯依關注點拆到下列模組（新增功能時優先延伸對應模組，不要把無關邏輯倒回 `piano_player.py`）：

- `nte_dsl.py`：譜面 DSL 解析、`Sheet` / `NoteEvent` / `KeyStroke` 資料模型、`TRACK_ORDER` / `BASE_KEYS` 等共用常數。**沒有 GUI / 後端依賴**。
- `nte_importers.py`：`MusicXMLImporter` / `MidiImporter` / `MsczImporter`，輸出 DSL 文字。MuseScore 透過 CLI 子程序轉 MusicXML 後共用 MusicXML 路徑。
- `nte_playback.py`：Win32 視窗工具（`find_game_window` / `focus_window` / `is_target_foreground`）、按鍵後端（`KeyBackend` / `PyDirectInputBackend` / `PynputBackend` + `create_backend_with_fallback`）、`PlaybackWorker`（QThread 上跑的排程執行層）、`HotkeyBridge` / `GlobalHotkeys`。
- `nte_automation.py`：遊戲內自動化任務（`FishingTask` 自動釣魚、`SoundCombatTask` 聲音閃避反擊、`RhythmTask` 自動音遊）。所有 worker 繼承 `threading.Thread`（**不是** QThread/QObject），透過 `AutomationProxy`（main-thread QObject）轉發 signal 給 GUI。設計理由是避免 PySide6 QThread 帶來的 OleInitialize / 跨執行緒 QTimer 衝突；新增任務請延用此模式。
- `nte_checker.py`：輕量視窗探測 widget，1Hz 偵測 NTE 視窗存在/前景狀態並顯示燈號。

CLI 輔助腳本放在 `tools/`。

GUI 由上而下：頂部工具列（檔案選單 / 播放 / 停止 / 曲目下拉 / 編輯按鈕）、`OverviewBar` 縮略圖、`PianoRollView` 橫向卷簾（佔主要視覺權重）、`PianoKeyboardWidget` 仿遊戲內 21 鍵簡譜鍵盤。譜面編輯器收進右側 `QDockWidget` 抽屜，預設隱藏，Ctrl+E 切換。深色主題，色彩從 `THEME` dict 派生。

## 常用指令

首次設定（PowerShell / cmd，使用全域 Python 3.14）：

```cmd
py -3.14 -m pip install -r requirements.txt
```

不使用 venv；直接打到全域 site-packages。若主機沒裝 Python 3.14 請從 https://www.python.org/downloads/ 安裝後再執行上述指令。

啟動方式（依使用情境擇一）：

- `run.bat`：自動 UAC 提權後執行，把 stdout/stderr 導入 `logs\last_run.log`，遊戲若不接受非提權輸入時用這個。
- 直接執行：`py -3.14 piano_player.py [score.txt]`，第二參數為要預載的譜面檔；除錯時用這條（不會提權，方便看 traceback）。

執行階段控制鍵：

- F6 播放、F7 停止：`pynput` 全域 hotkey + 視窗內 `QAction` 雙重綁定，視窗失焦也會作用，因此在編輯器中切換譜面時要小心誤觸。
- Ctrl+E 切換編輯抽屜、Esc 關閉抽屜；Ctrl+N / Ctrl+O / Ctrl+S / Ctrl+Shift+S 是新增/開啟/儲存/另存；Ctrl+I 匯入 MusicXML。
- 「檔案」選單還有 MIDI、MuseScore (MSCZ)、MSCZ 最佳化匯入，以及一系列 checkable settings（動畫效果、未存檔提醒、失焦自動暫停、啟動時詢問回復未儲存、匯入時一起匯入變速、`note_color_style` 子選單）。
- 工具列正中央顯示 BPM／音數／長度（`now_label`），由 `_refresh_sheet_from_text` 推進。

本專案沒有 lint / format / test 設定；不要主動引入 ruff / black / pytest 之類的工具或 CI 設定，除非主人明確要求。語法檢查可用 `py -3.14 -m py_compile piano_player.py nte_dsl.py nte_importers.py nte_playback.py nte_automation.py nte_checker.py`。

## 設定與自動存檔

- `SettingsManager` 寫到 `~/.nte_piano/settings.json`（atomic write：寫到 `.tmp` 再 `os.replace`）。`_DEFAULTS` 是 schema 唯一真相，新增 key 必須同步更新。
- 設定檔有版本欄位 `SETTINGS_VERSION`，目前 v2；`_migrate` 處理升版（例：v1→v2 把 `auto_pause_on_focus_loss` 預設改回 `False`，因為 `SetForegroundWindow` 被 Windows 拒絕時會誤觸發暫停，把播放切碎）。新增遷移時加 `if version < N:` 分支即可。
- 載入失敗（JSON 損壞、版本超前）會把壞檔備份成 `settings.json.bad-{timestamp}` 並退回預設值，不拋例外。
- `PianoPlayerWindow.AUTOSAVE_PATH` 指向 `.tmp/autosave.txt`，編輯器內容變更後 `AUTOSAVE_DEBOUNCE_MS=5000ms` debounce 寫入；下次啟動若 `autosave_restore_prompt=True` 會問是否回復。

## 工具腳本（`tools/`）

兩支腳本都是 `nte_importers.py` 內 importer 類別的 CLI 薄殼，GUI「匯入」走完全相同的程式路徑，行為一致：

- `tools/mxl_to_dsl.py`：MusicXML（`.mxl` / `.xml`）→ DSL，背後是 `MusicXMLImporter`。預設 `--transpose -5`（F→C），可改用 `--auto-transpose` 由 fifths 自動推算。`--right-prefer` / `--left-prefer` 支援 `H/MH/M/ML/L/auto/none`，`--melody-mode` 有 `full/skeleton/melody_only/dense`。
- `tools/midi_to_dsl.py`：`.mid` / `.midi` → DSL，背後是 `MidiImporter`。預設 `--transpose 0`，可改用 `--auto-transpose`（由 KeySignature 推算）。

```cmd
py -3.14 tools\mxl_to_dsl.py "input.mxl" "examples\output.txt"
py -3.14 tools\midi_to_dsl.py "input.mid" "examples\output.txt" --auto-transpose
```

關鍵共通行為（細節在 `nte_importers.py` 內 `MusicXMLImporter` / `MidiImporter`）：

- 音域固定折疊到 H/M/L 三排（MIDI 60–95），超出範圍自動升降八度；右手預設偏高、左手預設偏低。
- 自動寫入 `tempo`、`beat`、`gap`、`hold`、`modifier_delay` 全域命令，以及 `track right` / `track left` 兩軌。
- 同 onset 同手的重疊音會合併成和弦或 skip（輸出尾端印 `skipped overlap` 統計）。
- 改 DSL 全域命令名稱或範圍時，這兩條 importer 都要同步更新。

MuseScore `.mscz` / `.mscx` 沒有 CLI 腳本，只能從 GUI 匯入：`MsczImporter` 全部走 MusicXML 流程，`.mscz` 內若內嵌 `.mxl`/`.musicxml` 直接解出，否則呼叫 MuseScore CLI（搜尋 `MuseScore4.exe` / `MuseScore3.exe` / `mscore` 等名稱）轉成暫存 `.musicxml` 再交給 `MusicXMLImporter`。找不到可執行檔直接 `RuntimeError`。

## 譜面 DSL（內建解析器）

`SheetParser` 解析的是專案自定義記譜法，沒有外部規格文件。新增格式前先讀 `SheetParser.parse` 與 `_parse_event`：

- 八度前綴：`H`（高音 QWERTY 排）、`M`（中音 ASDFGH 排）、`L`（低音 ZXCVBN 排），對映 `BASE_KEYS`。
- 音級：`1`–`7`（簡譜）；升降號 `#` 與 `b` 可放音級前後（`M#1` 與 `M1#` 等價）。`#` → Shift 修飾鍵、`b` → Ctrl，由 `make_stroke` 決定。
- 時值倍率：`M1*2` 表示兩個 beat；單位以 `beat` 命令設定（拍對 `tempo` 的乘數）。
- 和弦：方括號加 `+`，如 `[M1+M3+M5]*2`；同 token 內的音同時按下，於 `_parse_event` 處理。
- 休止符：`0` / `-` / `.` / `R` / `REST`，但不能出現在和弦內。
- 多軌：`right:` / `left:` / `chord:` 等標籤建立獨立 track，每軌有自己的時間 cursor，事件最終依 `(start_beats, line, track)` 全域排序。亦可用 `track <name>` 切換預設軌。
- 全域命令：`tempo`、`beat`、`gap`、`hold`、`modifier_delay`（或 `mod_delay`）。範圍校驗在 `_apply_command`，動到時間相關欄位記得同步檢查。
- 變速：`tempo @ <beats> = <bpm>` 在 `Sheet.tempo_changes` 累積，由匯入器寫入；`import_tempo_changes` 設定關閉時匯入會略過。
- 註解：`#` 開頭整行；行中需前後皆空白才視為註解，否則 `M#1` 會被誤判（`_strip_comment`）。

範例譜面位於 `examples/`：`laputa.txt`（〈天空之城 / 君をのせて〉，啟動預設載入）、`gurenge.txt`、`kick_back.txt`、`mary.txt`、`ode_to_joy.txt`、`twinkle.txt`、`yoasobi.txt`、`yoru_ni_kakeru.txt`、`千本櫻.txt`。啟動時若沒指定檔案，會嘗試載入 `laputa.txt`，找不到就放空白編輯器（`_load_startup_score`）。新增 example 優先用 importer 從 MusicXML / MIDI / MSCZ 轉出後再人工微調，不要直接手寫長譜。

## 高層架構

播放路徑（編輯器 → 鍵盤事件）四層分工，跨 thread 處理時請維持：

1. **解析層**：`SheetParser` 把譜面文字轉為 `Sheet`（`tempo`/`beat`/`gap`/`hold`/`modifier_delay` + `tempo_changes` + `NoteEvent` 列表）。`NoteEvent` 是不可變 dataclass：`start_beats`、`duration_beats`、`strokes`（`KeyStroke` tuple）、`source`、`line`、`track`。
2. **排程層**：`PlaybackWorker._build_schedule` 把每個 `NoteEvent` 展開為 `ScheduledAction`（progress/down/up），統一以絕對秒數排序；`hold` 與 `gap` 控制 down 與 up 之間的距離（`hold` 為比率，`gap` 為下限）。
3. **執行層**：`PlaybackWorker.run` 在 `QThread` 中以 `time.perf_counter()` 為基準等到目標時間後送鍵；`request_stop()` 透過 `threading.Event` 中斷等待。`finally` 強制 `_release_all` 釋放所有按下中的鍵與修飾鍵，**避免修改這段時把例外吞掉**，否則異常終止會留住按鍵。Worker 在 `wait(start_delay)` 結束後 emit `started` signal，GUI 端用它驅動 `PianoRollView` 的 `QElapsedTimer` 起算，這就是 piano roll 與實際送鍵能對齊的關鍵；改 worker 啟動流程時請保留這個 signal 的時序。
4. **後端層**：抽象基底 `KeyBackend` 兩個實作 `PyDirectInputBackend`（預設，多數遊戲只接受 DirectInput）、`PynputBackend`（備援）。`create_backend_with_fallback` 會優先試 `pydirectinput`，匯入失敗自動退回 `pynput`，所以 GUI 不需選後端。後端在背景執行緒內建立，不要在主 GUI 執行緒呼叫；錯誤透過 `failed` signal 回主執行緒。

按鍵狀態管理（容易踩雷之處）：

- `_press_active` / `_release_active` 用 reference counting，多軌同時持有同一個鍵（旋律與和弦重疊）時不會被任一軌的 up 提早釋放。
- 修飾鍵（Shift / Ctrl）在每個音符按下後**立即釋放**（見 `_stroke_down` 結尾的 `for modifier in reversed(...)`）；這是為了避免後續自然音被誤升降。修改修飾鍵時序時要保留這個語意。
- `_active_label_counts` 是顯示用的標籤計數，**不能拿來判斷實際按鍵狀態**，按鍵狀態以 `_active_counts` 為準。

GUI 與工作執行緒之間：

- `PlaybackWorker` 為 `QObject`，移到自建 `QThread` 上跑；通訊只透過五個 signal：`progress`、`active_notes`、`started`、`failed`、`finished`，所有 UI 更新只在 main thread 對應 slot 內執行（`Qt.QueuedConnection`）。新增播放期事件請延用同一機制，不要從 worker 直接碰 UI。
- `HotkeyBridge` 把 pynput 非 Qt 執行緒回呼跨進 Qt event loop（`play_requested` / `stop_requested` 兩個 signal），同樣以 `Qt.QueuedConnection` 連接到 `PianoPlayerWindow`。

視覺化 widget：

- `PianoRollView`：`QWidget` 自繪，36 軌（`TRACK_ORDER` / `TRACK_INDEX`，由 H/M/L 三排各 12 個 chromatic 條目組成），時間從左流向右；播放遊標固定在 `LOOK_BEHIND_SECONDS / (LOOK_BEHIND_SECONDS + LOOK_AHEAD_SECONDS)` 的水平比例（預設 1.5/6 = 25%）。內建 `QTimer` 30FPS 重繪，時間軸用 `QElapsedTimer.elapsed()`，跟 worker 用同一個 `started_at` 起點所以自然對齊。對外 slot：`set_sheet` / `set_active_labels` / `start_playing` / `stop_playing`。
- `PianoKeyboardWidget`：`QWidget` 自繪，3 排 × 12 圓形按鈕對應 `BASE_KEYS` × `CHROMATIC_LAYOUT`，仿遊戲內鋼琴介面。對外只有 `set_active_labels`，由 worker 的 `active_notes` 推進；不接受滑鼠/鍵盤輸入。
- `OverviewBar`：全曲縮略圖；新增軌道相關功能時注意它跟 `PianoRollView` 共用 `TRACK_ORDER` / `TRACK_INDEX`。

軌道字典 `TRACK_ORDER` / `TRACK_INDEX` 是上述 widget 共用的，`KeyStroke.label` 是它們的 key（格式 `{octave}{accidental}{degree}`，如 `M#1`、`Lb7`）。

編輯器 undo/redo 策略：

- **以 `QPlainTextEdit` 內建 undo 為唯一真相**，不另建 stack。所有非文字操作（拖曳、刪除、貼上、調色）都序列化成 DSL 文字後 `editor.setPlainText(new_text)`，自動進編輯器 undo stack；按一次 Ctrl+Z 還原一個動作。
- 焦點不在編輯器時用 `QShortcut(context=Qt.ApplicationShortcut)` 把 Ctrl+Z / Ctrl+Y forward 給 `editor.undo() / redo()`。
- `setPlainText` 會觸發 `textChanged`，用 `_loading_text: bool` flag 防止 `_refresh_sheet_from_text` 再寫回造成無限迴圈。

Win32 整合：

- `_configure_winapi()` 集中設定 `user32` / `kernel32` / `shell32` 函式簽名，惰性首次呼叫；新增 Win32 呼叫把 `argtypes` / `restype` 加在這裡，不要散落各處。
- 視窗鎖定簡化為「按下 F6 時呼叫一次 `find_game_window()`，找到就 `focus_window` 嘗試聚焦並把 hwnd 傳給 worker；找不到也照樣播放，把鍵送到當前焦點視窗（狀態列會提示）」。沒有舊版 `auto_focus` / `foreground_only` / `off` 三種模式。`SetForegroundWindow` 偶爾被 Windows 安全機制拒絕，這是系統行為不是 bug。
- `auto_pause_on_focus_loss` 預設關，原因見「設定與自動存檔」。
- 「管理員權限」是顯示在狀態列的提示，不是執行條件；`run.bat` 自動提權是因為某些遊戲不接受非提權程序的輸入。

