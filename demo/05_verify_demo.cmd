@echo off
cd /d "%~dp0\.."
py -m tools.verify_demo --workspace demo_workspace
pause
