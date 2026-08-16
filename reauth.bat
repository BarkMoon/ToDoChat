@echo off
rem ToDoChat re-authentication helper. Run this when the chat shows
rem "Failed to authenticate" -- it means the claude CLI has been signed out
rem (its OAuth token in %USERPROFILE%\.claude\.credentials.json is empty).
rem This opens a browser to sign back in with the Claude (Pro) subscription,
rem which is exactly what ToDoChat reads to talk to the model.
rem
rem The headless CLI is installed at %USERPROFILE%\.local\bin and is NOT on
rem PATH, so we call it by full path (same as app\server.py). If that build
rem is missing we fall back to a "claude" on PATH.
rem
rem NOTE: this file is saved in cp932 (Shift-JIS) so the Japanese messages
rem below display correctly in the default Windows console. Do NOT re-save it
rem as UTF-8.
setlocal
cd /d "%~dp0"

set "CLI=%USERPROFILE%\.local\bin\claude.exe"
if not exist "%CLI%" set "CLI=claude"

echo ============================================================
echo  ToDoChat 再認証ツール
echo ============================================================
echo.
echo 使用する CLI: "%CLI%"
echo.
echo [1/3] 現在の認証状態を確認します...
"%CLI%" auth status
echo.

echo [2/3] ブラウザでサインインします（Claude サブスクリプション）。
echo       開いたページで承認してください。ブラウザが自動で開かない
echo       場合は、表示された URL を手動で開いてください。
echo.
"%CLI%" auth login --claudeai --email shunichiro.ikeno@gmail.com
echo.

echo [3/3] 再度、認証状態を確認します...
"%CLI%" auth status
echo.

echo ------------------------------------------------------------
echo  上に "loggedIn": true と表示されていれば成功です。
echo  この後 ToDoChat（start.bat）を再起動すれば
echo  「Failed to authenticate」は解消します。
echo.
echo  まだ false の場合は、ブラウザ認証の代わりに長期トークンを試せます:
echo    "%CLI%" setup-token
echo ------------------------------------------------------------
echo.
pause
endlocal
