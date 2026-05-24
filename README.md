<p align="center">
  <img src="assets/icon.png" width="140" alt="NTE Piano">
</p>

<h1 align="center">🎹 NTE Piano</h1>

<p align="center">
  <strong>NTE 遊戲鋼琴介面的視覺化編輯與自動演奏桌面工具。</strong>
</p>

![NTE Piano 主介面：Overview 縮略圖、Piano Roll 卷簾與仿遊戲 36 鍵鋼琴](assets/ss1.png)

> **小聲說明**：作者的程式設計經驗還在累積中，本專案有相當比例的程式碼是與 AI 協同撰寫的，難免有不夠優雅或不夠嚴謹之處。看到任何問題、想法、改進建議，都非常歡迎透過 issue / PR 回報，會很感激 QQ

---

## ✨ 為什麼選 NTE Piano

- **視覺化編輯，不只是文字框**。Piano Roll 卷簾 + 仿遊戲 36 鍵鋼琴 + 縮略圖總覽。
- **多格式匯入**。自動移調、超出三排八度的音符自動摺回可彈範圍。
- **F6 / F7 全域熱鍵**。遊戲不用切到前景也能控制播放，失焦時可自動把遊戲音量靜音。
- **內建鋼琴音色 + 編輯模式**。點鍵盤即時試聽；編輯模式下 F6 不送任何按鍵，純粹由本機音色驗證整曲節奏，沒開遊戲也能跑。

---

## 📦 安裝

### 一般使用者

到 [Releases](../../releases) 頁面下載最新版的 `NTEPiano-Setup.exe`，雙擊安裝即可，**不需要裝 Python**。

### 從原始碼執行

需要 Windows 10 / 11 與 Python 3.14（從 [python.org](https://www.python.org/downloads/) 取得）。

```cmd
py -3.14 -m pip install -r requirements.txt
py -3.14 piano_player.py
```

### 額外需要的軟體

- **MuseScore 4**（[免費下載](https://musescore.org/zh-hant/download)） — 僅匯入 `.mscz` / `.mscx` 時需要，請用 4 以上版本（舊版 CLI 介面不一致）。已內嵌 `.mxl` 的 `.mscz` 則不需要 MuseScore 也能讀。

---

## 🚀 第一次使用

1. 啟動 NTE Piano，會看到上方工具列、中間 Piano Roll 預覽區、下方仿遊戲鋼琴鍵盤。
2. 點工具列「檔案 → 匯入」，挑你的 MIDI / MusicXML / MuseScore 檔。
3. Piano Roll 會跑出音符方塊，工具列中央顯示 BPM、音數、總長。需要微調時按 **Ctrl+E** 打開右側譜面編輯抽屜。
4. 切到 NTE 遊戲視窗、進入鋼琴介面，按 **F6** 開始演奏，**F7** 停止。

> **為什麼需要管理員？** NTE 遊戲只接受由管理員權限送出的按鍵事件，因此 NTE Piano 啟動時會要求 UAC 提權。

---

## ⌨️ 操作快捷鍵

| 鍵 | 動作 |
|---|---|
| **F6** / **F7** | 開始 / 停止播放（全域，不需切回 NTE Piano 視窗） |
| **F8** | 暫停 / 繼續 |
| **Ctrl+E** | 切換右側譜面編輯抽屜 |
| **Esc** | 關閉編輯抽屜 |
| **Ctrl+N** / **Ctrl+O** | 新增 / 開啟譜面 |
| **Ctrl+S** / **Ctrl+Shift+S** | 儲存 / 另存譜面 |
| **Ctrl+I** | 匯入 MusicXML |
| **Ctrl+Z** / **Ctrl+Y** | 撤銷 / 重做 |

「檔案」選單還有更多匯入選項與設定，例如：動畫效果、未存檔提醒、失焦自動暫停、匯入時一併匯入變速、音符配色等。

---

## 🎼 支援的譜面格式

| 格式 | 副檔名 | 需要額外軟體 |
|---|---|---|
| 內建 DSL（文字檔） | `.txt` | 無 |
| MusicXML | `.xml` / `.mxl` | 無 |
| MIDI | `.mid` / `.midi` | 無 |
| MuseScore | `.mscz` / `.mscx` | **MuseScore 4 或以上** |

### 🔍 哪裡找樂譜

推薦使用 [**LibreScore**](https://github.com/LibreScore/app-librescore) 取得樂譜：

1. 到 [LibreScore App](https://github.com/LibreScore/app-librescore) 的 Releases 頁面下載對應平台的安裝檔。
2. 直接在 LibreScore App 內搜尋。
3. 把下載的檔案從 NTE Piano「檔案 → 匯入」進去即可。

> 純命令列使用者也可以改用 NPM 上的 [`dl-librescore`](https://www.npmjs.com/package/dl-librescore) 直接貼網址下載。請尊重原作者與譜面上傳者的版權，僅用於個人非商業練習用途。

## 🎵 譜面庫

`songs/` 內附了一批作者個人練習用的轉錄譜面（DSL `.txt` 格式），啟動時會隨機挑一首載入。你也可以把自己整理好的 `.txt` 直接丟進這個資料夾，重啟後會出現在工具列的歌曲下拉選單。

> **版權聲明**：`songs/` 內所有譜面均為作者個人遊玩 NTE 鋼琴介面時的練習轉錄，**樂曲版權歸原作曲者 / 版權所有人所有**，本工具僅作為個人非營利的遊戲內練習用途，不販售也不另作商業散布。若您是原作者或版權所有人，認為此處有侵權內容並希望下架，請開 issue 通知，會儘速移除對應檔案。

---

## 🎁 額外功能

除了演奏，NTE Piano 還整合了幾個遊戲內小工具，從設定面板開關，不用時不佔資源：

- **聲音閃避反擊**（實驗性）：偵測遊戲音效，自動在閃避視窗內按反擊鍵。
- **自動音遊**（實驗性）：辨識畫面節奏條，自動點擊。
- **失焦自動靜音**：切走遊戲視窗時，自動把 NTE 的音量降到 0，回來時還原。
- **粉爪大劫案**：按住設定鍵即可連續拾取，消除手動點擊的卡頓感。

標註「實驗性」的項目目前還在打磨，可用但穩定不了。

---

## 📄 授權

GPL-3.0-or-later。詳見 `LICENSE`。

Copyright (C) 2026 Yulun。
