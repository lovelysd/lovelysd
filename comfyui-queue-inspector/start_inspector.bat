@echo off
setlocal
cd /d "%~dp0"

rem ------------------------------------------------------------------
rem  ComfyUI Queue Inspector launcher
rem
rem  PYEXE : python.exe to use. Leave empty to auto-detect, in this order:
rem          1. ComfyUI's own Python next to this folder
rem             (..\python_embeded, ..\..\python_embeded, ..\.venv, ..\venv)
rem          2. the "py" launcher   3. "python" on PATH (Store stub skipped)
rem  COMFY : ComfyUI address. Leave empty to auto-detect
rem          (127.0.0.1:8188 portable/manual, then 127.0.0.1:8000 Desktop app).
rem ------------------------------------------------------------------
set "PYEXE="
set "COMFY="

if defined PYEXE goto have_python
if exist "%~dp0..\python_embeded\python.exe" set "PYEXE=%~dp0..\python_embeded\python.exe"
if defined PYEXE goto have_python
if exist "%~dp0..\..\python_embeded\python.exe" set "PYEXE=%~dp0..\..\python_embeded\python.exe"
if defined PYEXE goto have_python
if exist "%~dp0..\.venv\Scripts\python.exe" set "PYEXE=%~dp0..\.venv\Scripts\python.exe"
if defined PYEXE goto have_python
if exist "%~dp0..\venv\Scripts\python.exe" set "PYEXE=%~dp0..\venv\Scripts\python.exe"
if defined PYEXE goto have_python
where py >nul 2>nul && goto use_py
rem "python" in WindowsApps is the Microsoft Store stub, not a real Python.
for /f "delims=" %%P in ('where python 2^>nul ^| findstr /v /i "WindowsApps"') do if not defined PYEXE set "PYEXE=%%P"
if defined PYEXE goto have_python

echo Python was not found.
echo Install Python 3 from python.org, or open this file in Notepad and set
echo PYEXE to your ComfyUI Python, e.g. ...\python_embeded\python.exe
pause
exit /b 1

:use_py
set "PYCMD=py -3"
goto run

:have_python
set PYCMD="%PYEXE%"

:run
echo Using Python: %PYCMD%
if defined COMFY goto run_fixed
%PYCMD% inspector.py %*
goto end

:run_fixed
%PYCMD% inspector.py --comfy %COMFY% %*

:end
pause
