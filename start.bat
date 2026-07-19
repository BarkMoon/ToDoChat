@echo off
rem ToDoChat launcher (prototype). Opens the local web app in the browser.
cd /d "%~dp0"

rem Log everything the server prints to a file too, so errors are still
rem readable even if this window closes. The window is also kept open with
rem "pause" below so a startup crash / traceback stays on screen.
python "app\server.py"

echo.
echo ============================================================
echo  ToDoChat server stopped (exit code %errorlevel%).
echo  If this was unexpected, the error is shown above and also
echo  saved to: "%~dp0server_error.log"
echo ============================================================
pause
