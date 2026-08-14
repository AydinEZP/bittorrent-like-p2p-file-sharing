@echo off
cd /d "%~dp0\.."
py -m tracker.tracker_server --host 127.0.0.1 --port 8000 --log demo_workspace\logs\tracker\tracker.log
pause
