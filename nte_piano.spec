# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for NTE Piano (onedir / windowed / uac_admin)
# 因為 nte_automation 內的 mss/cv2/librosa/scipy/sklearn/soundcard/pycaw/comtypes
# 都是 function-scope 的 lazy import，PyInstaller 預設找不到，必須在這裡列出。

from PyInstaller.utils.hooks import collect_submodules, collect_data_files


def _safe_collect_submodules(name: str) -> list[str]:
    try:
        return collect_submodules(name)
    except Exception:
        return []


def _safe_collect_data_files(name: str) -> list[tuple[str, str]]:
    try:
        return collect_data_files(name)
    except Exception:
        return []


hiddenimports: list[str] = [
    "pydirectinput",
    "pynput",
    "pynput.keyboard",
    "pynput.mouse",
    "mss",
    "mss.windows",
    "cv2",
    "numpy",
    "scipy",
    "scipy.signal",
    "sklearn",
    "sklearn.preprocessing",
    "soundcard",
    "pycaw",
    "pycaw.pycaw",
    "comtypes",
    "comtypes.client",
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
]
for _pkg in ("librosa", "pycaw", "comtypes", "soundcard", "pydirectinput"):
    hiddenimports += _safe_collect_submodules(_pkg)

# 去重
hiddenimports = sorted(set(hiddenimports))

datas: list[tuple[str, str]] = [
    ("assets", "assets"),
    # songs/ 在 dev 是使用者資料夾；frozen 時當 bundled examples,
    # 由 _seed_default_songs() copy 到 exe 旁的 songs/。
    ("songs", "examples"),
]
datas += _safe_collect_data_files("librosa")
datas += _safe_collect_data_files("soundcard")

a = Analysis(
    ["piano_player.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="NTEPiano",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="assets/icon.ico",
    uac_admin=True,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="NTEPiano",
)
