@echo off
setlocal
title StrategySnapshotOneClick
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\strategy_snapshot_download.ps1"
set "SNAPSHOT_EXIT=%ERRORLEVEL%"
echo.
pause
exit /b %SNAPSHOT_EXIT%
