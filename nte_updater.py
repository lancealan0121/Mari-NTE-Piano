"""GitHub Releases 拉式自動更新。

設計沿用 nte_automation.py 內 `AutomationProxy` + `AutomationTask` 模式:
- Proxy 是 main-thread QObject,只負責跨執行緒 emit signal 給 GUI。
- Task 是 threading.Thread (daemon=True),用 _stop_event 取消。
- 刻意不用 QThread,理由見 CLAUDE.md「Threading pattern」段。

零第三方依賴:urllib.request + ssl + hashlib + json 都是 stdlib,避免引入
requests/certifi/urllib3 等四個 transitive deps(與主人「擔心 GitHub
binary 供應鏈安全」的初衷矛盾)。

SHA256 驗證:GitHub REST API 2024 起在 assets[] 自動帶 `digest` 欄位
(格式 "sha256:abc123..."),下載完整檔後比對 hash;不符即視為失敗、刪
.part 檔。若 release 沒帶 digest,GUI 端會先警告再讓主人決定是否安裝。
"""

from __future__ import annotations

import hashlib
import json
import ssl
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from nte_version import APP_VERSION, GITHUB_RELEASES_LATEST_URL, GITHUB_REPO_URL

USER_AGENT = f"NTEPiano-Updater/{APP_VERSION} (+{GITHUB_REPO_URL})"
HTTP_CHECK_TIMEOUT = 5.0
HTTP_DOWNLOAD_READ_TIMEOUT = 30.0
DOWNLOAD_CHUNK_SIZE = 64 * 1024


@dataclass(frozen=True)
class UpdateInfo:
    latest_version: str
    current_version: str
    download_url: str
    asset_name: str
    asset_size: int
    digest: str | None
    release_notes_url: str
    published_at: str


def _parse_semver(s: str) -> tuple[int, ...]:
    # 支援 "v1.0.0" / "1.0.0" / "1.0.0-rc1" 等;-rc 之後的 pre-release suffix 直接丟掉,
    # 視為與 "1.0.0" 同階(parse 結果一致)。對本專案夠用,不引入 packaging。
    core = s.lstrip("vV").split("-", 1)[0].split("+", 1)[0]
    parts: list[int] = []
    for token in core.split("."):
        try:
            parts.append(int(token))
        except ValueError:
            parts.append(0)
    return tuple(parts) if parts else (0,)


def is_newer(latest: str, current: str) -> bool:
    return _parse_semver(latest) > _parse_semver(current)


class UpdaterProxy(QObject):
    check_finished = Signal(object)           # UpdateInfo | None
    download_progress = Signal(int, int)      # bytes_done, bytes_total
    download_finished = Signal(str)           # local installer path
    failed = Signal(str)


def _build_ssl_context() -> ssl.SSLContext:
    return ssl.create_default_context()


class CheckUpdateTask(threading.Thread):
    """背景查詢 GitHub latest release。失敗一律 emit check_finished(None),
    讓 GUI 端依 manual flag 決定是否顯示錯誤訊息。"""

    def __init__(self, proxy: UpdaterProxy) -> None:
        super().__init__(daemon=True, name="NTEPianoUpdateCheck")
        self._proxy = proxy
        self._stop_event = threading.Event()

    def request_stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        try:
            req = urllib.request.Request(
                GITHUB_RELEASES_LATEST_URL,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "application/vnd.github+json",
                },
            )
            with urllib.request.urlopen(
                req, timeout=HTTP_CHECK_TIMEOUT, context=_build_ssl_context()
            ) as r:
                raw = r.read()
            if self._stop_event.is_set():
                return
            payload = json.loads(raw.decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError):
            self._proxy.check_finished.emit(None)
            return
        except Exception:  # noqa: BLE001
            # 把任何未預期錯誤吞掉再回 None,自動檢查不應該因為網路問題炸 GUI
            self._proxy.check_finished.emit(None)
            return

        tag = str(payload.get("tag_name") or "").strip()
        if not tag or not is_newer(tag, APP_VERSION):
            self._proxy.check_finished.emit(None)
            return

        # 找第一個 .exe asset(Inno installer);找不到視同沒有可下載的 release。
        asset = None
        for a in payload.get("assets", []) or []:
            name = str(a.get("name", "")).lower()
            if name.endswith(".exe"):
                asset = a
                break
        if not asset:
            self._proxy.check_finished.emit(None)
            return

        digest_raw = asset.get("digest")
        digest: str | None = str(digest_raw) if digest_raw else None

        info = UpdateInfo(
            latest_version=tag.lstrip("vV"),
            current_version=APP_VERSION,
            download_url=str(asset.get("browser_download_url") or ""),
            asset_name=str(asset.get("name") or ""),
            asset_size=int(asset.get("size") or 0),
            digest=digest,
            release_notes_url=str(payload.get("html_url") or ""),
            published_at=str(payload.get("published_at") or ""),
        )
        if not info.download_url:
            self._proxy.check_finished.emit(None)
            return
        self._proxy.check_finished.emit(info)


class DownloadUpdateTask(threading.Thread):
    """串流下載 installer 到 dest_dir,邊下載邊算 SHA256。

    完成才把 .part 改名到正式路徑;中斷或 hash 不符會刪 .part,不留垃圾。
    GUI 端透過 request_stop() 取消。
    """

    def __init__(
        self, proxy: UpdaterProxy, info: UpdateInfo, dest_dir: Path
    ) -> None:
        super().__init__(daemon=True, name="NTEPianoUpdateDownload")
        self._proxy = proxy
        self._info = info
        safe_name = f"NTEPiano-Setup-{info.latest_version}.exe"
        self._dest = dest_dir / safe_name
        self._tmp = self._dest.with_suffix(".exe.part")
        self._stop_event = threading.Event()

    def request_stop(self) -> None:
        self._stop_event.set()

    @property
    def info(self) -> UpdateInfo:
        return self._info

    def run(self) -> None:
        try:
            self._dest.parent.mkdir(parents=True, exist_ok=True)
            # 若先前留有 .part 殘骸,先清掉。
            self._tmp.unlink(missing_ok=True)

            req = urllib.request.Request(
                self._info.download_url,
                headers={"User-Agent": USER_AGENT},
            )
            sha = hashlib.sha256()
            done = 0
            with urllib.request.urlopen(
                req,
                timeout=HTTP_DOWNLOAD_READ_TIMEOUT,
                context=_build_ssl_context(),
            ) as r, open(self._tmp, "wb") as f:
                header_len = r.headers.get("Content-Length")
                try:
                    total = int(header_len) if header_len else self._info.asset_size
                except ValueError:
                    total = self._info.asset_size
                # 先發一次 0/total,GUI 能立刻顯示進度條框架。
                self._proxy.download_progress.emit(0, total)
                while True:
                    if self._stop_event.is_set():
                        break
                    chunk = r.read(DOWNLOAD_CHUNK_SIZE)
                    if not chunk:
                        break
                    f.write(chunk)
                    sha.update(chunk)
                    done += len(chunk)
                    self._proxy.download_progress.emit(done, total)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            self._tmp.unlink(missing_ok=True)
            self._proxy.failed.emit(f"下載失敗:{e}")
            return
        except Exception as e:  # noqa: BLE001
            self._tmp.unlink(missing_ok=True)
            self._proxy.failed.emit(f"下載失敗:{e}")
            return

        if self._stop_event.is_set():
            self._tmp.unlink(missing_ok=True)
            return

        if self._info.digest:
            want_raw = self._info.digest.split(":", 1)[-1].strip().lower()
            got = sha.hexdigest().lower()
            if got != want_raw:
                self._tmp.unlink(missing_ok=True)
                self._proxy.failed.emit(
                    "SHA256 校驗不符,檔案可能損毀或遭竄改。\n"
                    f"預期:{want_raw}\n實際:{got}"
                )
                return

        try:
            self._tmp.replace(self._dest)
        except OSError as e:
            self._tmp.unlink(missing_ok=True)
            self._proxy.failed.emit(f"無法寫入安裝檔:{e}")
            return

        self._proxy.download_finished.emit(str(self._dest))
