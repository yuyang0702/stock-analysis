@echo off
setlocal
chcp 65001 >nul
title 一键上传严格历史数据
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\strict_history_upload.ps1" -PackagePath "%~1"
set "UPLOAD_EXIT=%ERRORLEVEL%"
echo.
if "%UPLOAD_EXIT%"=="0" (
  echo 操作结束：成功。
) else (
  echo 操作结束：没有导入，请查看上面的红色提示。
)
echo.
pause
exit /b %UPLOAD_EXIT%
