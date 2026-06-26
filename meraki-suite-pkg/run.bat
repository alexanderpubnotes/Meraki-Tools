@echo off
REM Launch the Meraki Suite GUI. Double-click this file to start.
cd /d "%~dp0"
python suite_gui.py
if errorlevel 1 (
    echo.
    echo The app exited with an error. Read the message above.
    echo Common fixes: install Python ^(python.org, "Add to PATH"^), then run: pip install meraki
    pause
)
