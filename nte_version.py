"""NTE Piano 版本資訊單一真相。

主人改版時只動 APP_VERSION,build.bat 會用 `py -c "import nte_version;
print(nte_version.APP_VERSION)"` 讀進來注入 installer.iss,piano_player.py
則在 runtime 顯示在視窗標題與「關於」對話框。

刻意零依賴 — build.bat preflight 階段就會 import,此時 PySide6 / 第三方
套件尚未驗證可用。
"""

APP_VERSION = "1.0.0"

GITHUB_OWNER = "lancealan0121"
GITHUB_REPO = "Mari-NTE-Piano"
GITHUB_RELEASES_LATEST_URL = (
    f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"
)
GITHUB_REPO_URL = f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}"
