@echo off
REM ============================================================================
REM NTE Piano - build script
REM
REM Steps:
REM   [0/3] Preflight checks (Python 3.14, PyInstaller, spec, icon)
REM   [1/3] Clean previous build (build\, dist\, __pycache__)
REM   [2/3] PyInstaller -> dist\NTEPiano\
REM   [3/3] Inno Setup installer (optional, skipped if iscc not on PATH)
REM
REM Pauses on success and on failure so the user can read messages
REM when launched by double-click. Preflight log: logs\last_build.log
REM
REM Usage:
REM   build.bat           full clean + PyInstaller + installer
REM   build.bat noclean   keep prior build cache (faster incremental rebuild)
REM
REM NOTE: This file is ASCII-only on purpose. Putting non-ASCII characters in
REM REM/echo lines breaks cmd.exe's parser before chcp 65001 takes effect.
REM ============================================================================
chcp 65001 >nul
setlocal EnableExtensions

pushd "%~dp0"

set "BUILD_LOG_DIR=%CD%\logs"
set "BUILD_LOG=%BUILD_LOG_DIR%\last_build.log"
if not exist "%BUILD_LOG_DIR%" mkdir "%BUILD_LOG_DIR%" >nul 2>nul

>"%BUILD_LOG%" echo === NTE Piano build started at %DATE% %TIME% ===

REM ---------------------------------------------------------------------------
REM [0/3] Preflight
REM ---------------------------------------------------------------------------
echo.
echo === [0/3] Preflight checks ===

py -3.14 --version >>"%BUILD_LOG%" 2>&1
if errorlevel 1 (
    echo [ERROR] Python 3.14 not available via 'py -3.14'.
    echo         Install from https://www.python.org/downloads/ then re-run.
    goto :fail
)

py -3.14 -m PyInstaller --version >>"%BUILD_LOG%" 2>&1
if errorlevel 1 (
    echo [ERROR] PyInstaller not installed for Python 3.14.
    echo         Run: py -3.14 -m pip install pyinstaller
    goto :fail
)

if not exist "piano_player.py" (
    echo [ERROR] piano_player.py not found in %CD%.
    goto :fail
)
if not exist "nte_piano.spec" (
    echo [ERROR] nte_piano.spec not found. Cannot build without spec.
    goto :fail
)
if not exist "assets\icon.ico" (
    echo [ERROR] assets\icon.ico not found; spec references it for the EXE icon.
    goto :fail
)

REM ---------------------------------------------------------------------------
REM [1/3] Clean
REM ---------------------------------------------------------------------------
echo.
echo === [1/3] Clean previous build ===

if /I "%~1"=="noclean" (
    echo [INFO] noclean mode, skipping build\ and dist\ removal.
) else (
    if exist "build" rmdir /S /Q "build"
    if exist "dist"  rmdir /S /Q "dist"
    if exist "dist" (
        echo [ERROR] Failed to remove dist\. Is the old NTEPiano.exe still running?
        goto :fail
    )
)

REM Wipe __pycache__ so stale .pyc files do not get picked up by PyInstaller
for /d /r "%CD%" %%d in (__pycache__) do (
    if exist "%%d" rmdir /S /Q "%%d" 2>nul
)

REM ---------------------------------------------------------------------------
REM [2/3] PyInstaller
REM ---------------------------------------------------------------------------
echo.
echo === [2/3] Run PyInstaller (onedir, windowed) ===

py -3.14 -m PyInstaller --noconfirm --clean --log-level=WARN nte_piano.spec
if errorlevel 1 (
    echo.
    echo [ERROR] PyInstaller failed. Re-run with --log-level=INFO for details:
    echo         py -3.14 -m PyInstaller --noconfirm --clean nte_piano.spec
    goto :fail
)
if not exist "dist\NTEPiano\NTEPiano.exe" (
    echo [ERROR] PyInstaller reported success but dist\NTEPiano\NTEPiano.exe is missing.
    goto :fail
)
echo [OK] dist\NTEPiano\NTEPiano.exe built.

REM ---------------------------------------------------------------------------
REM [3/3] Inno Setup
REM ---------------------------------------------------------------------------
echo.
echo === [3/3] Build installer ===

set "ISCC_EXE="
where iscc >nul 2>nul
if not errorlevel 1 set "ISCC_EXE=iscc"
if not defined ISCC_EXE if exist "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" set "ISCC_EXE=C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
if not defined ISCC_EXE if exist "C:\Program Files\Inno Setup 6\ISCC.exe" set "ISCC_EXE=C:\Program Files\Inno Setup 6\ISCC.exe"

if not defined ISCC_EXE (
    echo [INFO] Inno Setup 6 not found, skipping installer step.
    echo        onedir output: %CD%\dist\NTEPiano\
    echo        Install Inno Setup 6 from https://jrsoftware.org/isdl.php then re-run.
    goto :done
)

echo Using ISCC: %ISCC_EXE%
"%ISCC_EXE%" installer.iss
if errorlevel 1 (
    echo [ERROR] Inno Setup compile failed.
    goto :fail
)
if exist "dist\NTEPiano-Setup.exe" (
    echo [OK] Installer: %CD%\dist\NTEPiano-Setup.exe
) else (
    echo [WARN] ISCC returned success but dist\NTEPiano-Setup.exe missing.
)

:done
echo.
echo ============================================================
echo Build finished successfully at %DATE% %TIME%
echo ============================================================
>>"%BUILD_LOG%" echo === Build finished successfully at %DATE% %TIME% ===
popd
endlocal
echo.
echo Press any key to close...
pause >nul
exit /b 0

:fail
echo.
echo ============================================================
echo Build FAILED at %DATE% %TIME%
echo Preflight log: %BUILD_LOG%
echo ============================================================
>>"%BUILD_LOG%" echo === Build FAILED at %DATE% %TIME% ===
popd
endlocal
echo.
echo Press any key to close...
pause >nul
exit /b 1
