@echo off
rem ToDoChat launcher. Opens the local web app. By default the server also binds
rem to ALL network interfaces so a phone or another PC on the SAME Wi-Fi / VPN can
rem connect (http://<this-PC-IP>:8765/). This is now controlled in-app: open the
rem settings window (gear icon) and switch the connection mode between LAN and
rem local-only (the choice is saved and applied on the next launch).
rem
rem SECURITY: there is no authentication yet, so anyone who can reach this port
rem can drive the CLI. Only use LAN mode on a trusted network (home Wi-Fi / VPN).
rem
rem ADVANCED: uncommenting the line below pins the listen address via env var and
rem OVERRIDES the in-app connection-mode setting (0.0.0.0 = LAN, 127.0.0.1 = local).
rem   set "TODOCHAT_HOST=0.0.0.0"
rem
rem NOTE: close any already-running ToDoChat window first -- otherwise this one
rem hits "port 8765 in use" and the old server keeps running.
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
