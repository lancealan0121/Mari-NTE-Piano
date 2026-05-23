@echo off
setlocal EnableExtensions

set "ROOT=%~dp0"
set "APP_FILE=%ROOT%piano_player.py"
set "LOG_DIR=%ROOT%logs"
set "LOG_FILE=%LOG_DIR%\last_run.log"

pushd "%ROOT%" >nul 2>nul
if errorlevel 1 (
    echo Cannot enter project directory:
    echo %ROOT%
    pause
    exit /b 1
)

fltmc >nul 2>nul
if errorlevel 1 (
    powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -WorkingDirectory '%ROOT%' -Verb RunAs"
    popd
    exit /b
)

py -3.14 --version >nul 2>nul
if errorlevel 1 (
    echo Python 3.14 was not found via the py launcher.
    echo.
    echo Please install Python 3.14 from https://www.python.org/downloads/
    echo and then run:
    echo     py -3.14 -m pip install -r requirements.txt
    pause
    popd
    exit /b 1
)

if not exist "%APP_FILE%" (
    echo App file was not found:
    echo %APP_FILE%
    pause
    popd
    exit /b 1
)

if not exist "%LOG_DIR%" mkdir "%LOG_DIR%" >nul 2>nul
if not exist "%LOG_DIR%" (
    echo Cannot create log directory:
    echo %LOG_DIR%
    pause
    popd
    exit /b 1
)

py -3.14 "%APP_FILE%" > "%LOG_FILE%" 2>&1
set "APP_RC=%errorlevel%"

if not "%APP_RC%"=="0" (
    echo.
    echo Program exited with an error. Exit code: %APP_RC%
    echo.
    if exist "%LOG_FILE%" (
        type "%LOG_FILE%"
    ) else (
        echo Log file was not created:
        echo %LOG_FILE%
    )
    echo.
    pause
)

popd
exit /b %APP_RC%
