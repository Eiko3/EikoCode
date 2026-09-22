@echo off
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start.ps1"
if errorlevel 1 (
    echo.
    echo === EikoCode 异常退出，请查看上面的错误信息 ===
    echo.
    pause
)
