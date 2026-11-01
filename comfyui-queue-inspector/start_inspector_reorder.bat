@echo off
rem Same as start_inspector.bat, with changing the order of pending jobs enabled.
rem Jobs are re-queued only when you press Confirm; backups go to .\backups
call "%~dp0start_inspector.bat" --allow-reorder %*
