@echo off
rem ============================================================
rem  check_npu.bat
rem  Check NPU usage on 3 remote A3 servers via SSH (no password)
rem  Edit check_npu.ps1 to change servers / user / command.
rem ============================================================
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0check_npu.ps1"
echo.
pause
