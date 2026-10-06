@echo off
setlocal
cd /d "%~dp0"

rem ------------------------------------------------------------------
rem  ComfyUI Queue Inspector launcher
rem
rem  PYEXE : python.exe to use. Leave empty to auto-detect (py / python).
rem          For ComfyUI portable you can point to its embedded Python, e.g.
rem          set "PYEXE=C:\ComfyUI_windows_portable\python_embeded\python.exe"
rem  COMFY : ComfyUI address.
rem ------------------------------------------------------------------
set "PYEXE="
set "COMFY=http://127.0.0.1:8188"

if defined PYEXE goto run_custom
where py >nul 2>nul && goto run_py
where python >nul 2>nul && goto run_python

echo Python was not found.
echo Install Python 3 from python.org, or set PYEXE in this file to
echo ComfyUI portable's python_embeded\python.exe
pause
exit /b 1

:run_custom
"%PYEXE%" inspector.py --comfy %COMFY% %*
goto end

:run_py
py -3 inspector.py --comfy %COMFY% %*
goto end

:run_python
python inspector.py --comfy %COMFY% %*

:end
pause
