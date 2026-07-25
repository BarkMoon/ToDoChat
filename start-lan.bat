@echo off
rem ToDoChat LAN launcher. Binds the server to ALL network interfaces so a phone
rem or another PC on the SAME Wi-Fi / VPN can connect (http://<this-PC-IP>:8765/).
rem
rem SECURITY: there is no authentication yet, so anyone who can reach this port
rem can drive the CLI. Only use this on a trusted network (home Wi-Fi / VPN).
rem For local-only use, run the normal start.bat instead.
rem
rem NOTE: close any already-running ToDoChat window first -- otherwise this one
rem hits "port 8765 in use" and the old loopback-only server keeps running.
cd /d "%~dp0"

set "TODOCHAT_HOST=0.0.0.0"
python "app\server.py"

echo.
echo ============================================================
echo  ToDoChat server stopped (exit code %errorlevel%).
echo ============================================================
pause
