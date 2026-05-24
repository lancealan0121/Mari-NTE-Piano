<p align="center">
  <img src="assets/icon.png" width="140" alt="NTE Piano">
</p>

<h1 align="center">🎹 NTE Piano</h1>

<p align="center">
  <strong>NTE 游戏钢琴界面的可视化编辑与自动演奏桌面工具。</strong>
</p>

<p align="center">
  <a href="README.md">繁體中文</a> · <strong>简体中文</strong>
</p>

![NTE Piano 主界面：Overview 缩略图、Piano Roll 卷帘与仿游戏 36 键钢琴](assets/ss1.png)

> **小声说明**：作者的编程经验还在积累中，本项目有相当比例的代码是与 AI 协同编写的，难免有不够优雅或不够严谨之处。看到任何问题、想法、改进建议，都非常欢迎通过 issue / PR 反馈，会很感激 QQ

---

## ✨ 为什么选 NTE Piano

- **可视化编辑，不只是文本框**。Piano Roll 卷帘 + 仿游戏 36 键钢琴 + 缩略图总览。
- **多格式导入**。自动移调、超出三排八度的音符自动折回可弹范围。
- **F6 / F7 全局热键**。游戏不用切到前台也能控制播放，失焦时可自动把游戏音量静音。
- **内置钢琴音色 + 编辑模式**。点键盘即时试听；编辑模式下 F6 不发送任何按键，纯粹由本机音色验证整曲节奏，没开游戏也能跑。

---

## 📦 安装

### 普通用户

到 [Releases](../../releases) 页面下载最新版的 `NTEPiano-Setup.exe`，双击安装即可，**不需要装 Python**。

### 从源代码运行

需要 Windows 10 / 11 与 Python 3.14（从 [python.org](https://www.python.org/downloads/) 获取）。

```cmd
py -3.14 -m pip install -r requirements.txt
py -3.14 piano_player.py
```

### 额外需要的软件

- **MuseScore 4**（[免费下载](https://musescore.org/zh-hans/download)） — 仅导入 `.mscz` / `.mscx` 时需要，请用 4 以上版本（旧版 CLI 接口不一致）。已内嵌 `.mxl` 的 `.mscz` 则不需要 MuseScore 也能读。

---

## 🚀 第一次使用

1. 启动 NTE Piano，会看到上方工具栏、中间 Piano Roll 预览区、下方仿游戏钢琴键盘。
2. 点工具栏「文件 → 导入」，挑你的 MIDI / MusicXML / MuseScore 文件。
3. Piano Roll 会跑出音符方块，工具栏中央显示 BPM、音数、总长。需要微调时按 **Ctrl+E** 打开右侧谱面编辑抽屉。
4. 切到 NTE 游戏窗口、进入钢琴界面，按 **F6** 开始演奏，**F7** 停止。

> **为什么需要管理员？** NTE 游戏只接受由管理员权限发送的按键事件，因此 NTE Piano 启动时会要求 UAC 提权。

---

## ⌨️ 操作快捷键

| 键 | 动作 |
|---|---|
| **F6** / **F7** | 开始 / 停止播放（全局，不需切回 NTE Piano 窗口） |
| **F8** | 暂停 / 继续 |
| **Ctrl+E** | 切换右侧谱面编辑抽屉 |
| **Esc** | 关闭编辑抽屉 |
| **Ctrl+N** / **Ctrl+O** | 新建 / 打开谱面 |
| **Ctrl+S** / **Ctrl+Shift+S** | 保存 / 另存谱面 |
| **Ctrl+I** | 导入 MusicXML |
| **Ctrl+Z** / **Ctrl+Y** | 撤销 / 重做 |

「文件」菜单还有更多导入选项与设置，例如：动画效果、未保存提醒、失焦自动暂停、导入时一并导入变速、音符配色等。

---

## 🎼 支持的谱面格式

| 格式 | 扩展名 | 需要额外软件 |
|---|---|---|
| 内置 DSL（文本文件） | `.txt` | 无 |
| MusicXML | `.xml` / `.mxl` | 无 |
| MIDI | `.mid` / `.midi` | 无 |
| MuseScore | `.mscz` / `.mscx` | **MuseScore 4 或以上** |

### 🔍 哪里找乐谱

推荐使用 [**LibreScore**](https://github.com/LibreScore/app-librescore) 获取乐谱：

1. 到 [LibreScore App](https://github.com/LibreScore/app-librescore) 的 Releases 页面下载对应平台的安装包。
2. 直接在 LibreScore App 内搜索。
3. 把下载的文件从 NTE Piano「文件 → 导入」进去即可。

> 纯命令行用户也可以改用 NPM 上的 [`dl-librescore`](https://www.npmjs.com/package/dl-librescore) 直接粘贴网址下载。请尊重原作者与谱面上传者的版权，仅用于个人非商业练习用途。

## 🎵 谱面库

`songs/` 内附了一批作者个人练习用的转录谱面（DSL `.txt` 格式），启动时会随机挑一首加载。你也可以把自己整理好的 `.txt` 直接丢进这个文件夹，重启后会出现在工具栏的歌曲下拉菜单。

> **版权声明**：`songs/` 内所有谱面均为作者个人游玩 NTE 钢琴界面时的练习转录，**乐曲版权归原作曲者 / 版权所有人所有**，本工具仅作为个人非营利的游戏内练习用途，不贩售也不另作商业散布。若您是原作者或版权所有人，认为此处有侵权内容并希望下架，请开 issue 通知，会尽快移除对应文件。

---

## 🎁 额外功能

除了演奏，NTE Piano 还整合了几个游戏内小工具，从设置面板开关，不用时不占资源：

- **声音闪避反击**（实验性）：检测游戏音效，自动在闪避窗口内按反击键。
- **自动音游**（实验性）：识别画面节奏条，自动点击。
- **失焦自动静音**：切走游戏窗口时，自动把 NTE 的音量降到 0，回来时还原。
- **粉爪大劫案**：按住设置键即可连续拾取，消除手动点击的卡顿感。

标注「实验性」的项目目前还在打磨，可用但稳定不了。

---

## 📄 许可证

GPL-3.0-or-later。详见 `LICENSE`。

Copyright (C) 2026 Yulun。
