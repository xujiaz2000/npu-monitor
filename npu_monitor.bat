@echo off
rem ============================================================
rem  npu_monitor.bat
rem  双击启动 NPU 占用监控看板 (pythonw 无黑窗口)
rem  配置文件: npu_monitor.json (同目录, 可手工编辑)
rem ============================================================
cd /d "%~dp0"
where pythonw >nul 2>nul
if %errorlevel%==0 (
  start "" pythonw.exe npu_monitor.py
) else (
  start "" python.exe npu_monitor.py
)
