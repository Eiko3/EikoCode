@echo off
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\setup.ps1"
if errorlevel 1 (
    echo.
    echo === 配置未完成，请查看上面的错误信息 ===
) else (
    echo.
    echo === 配置已保存 ===
)
echo.
pause
