@echo off
cd /d "%~dp0\.."
py -m tools.prepare_demo --workspace demo_workspace --tracker http://127.0.0.1:8000/announce --reset
pause
