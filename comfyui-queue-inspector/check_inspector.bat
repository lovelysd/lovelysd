@echo off
rem Checks this installation (files, Python, ComfyUI, port, tests). Changes nothing.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\check_windows.ps1"
pause
