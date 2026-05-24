@echo off
REM Launch NTE Piano with performance logging enabled.
REM Output goes to %USERPROFILE%\.nte_piano\logs\perf_YYYYMMDD_HHMMSS.log
set NTE_PERF=1
py -3.14 piano_player.py %*
