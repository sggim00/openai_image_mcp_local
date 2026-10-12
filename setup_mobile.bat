@echo off
rem openai_image_mcp_local - mobile worker setup (run once, after install.bat)
setlocal
cd /d "%~dp0"
set PYX=.venv\Scripts\python.exe
if not exist %PYX% (
    echo Run install.bat first.
    pause
    exit /b 1
)

if exist .gdrive_token.json goto :check
echo [1/3] Google sign-in (opens the browser)...
echo       Put the OAuth client JSON (client_secret_....json) in this folder first.
%PYX% get_drive_token.py || goto :fail

:check
echo.
echo [2/3] Checking Google Drive and the OpenAI key...
%PYX% drive_worker.py --check || goto :fail

echo.
echo [3/3] Starting the worker and enabling autostart at login...
%PYX% drive_worker.py --install-autostart
%PYX% drive_worker.py --start
echo.
echo Done. While this PC is on, Claude on your phone can make images.
echo   status: %PYX% drive_worker.py --status
echo   stop:   %PYX% drive_worker.py --stop
pause
exit /b 0

:fail
echo.
echo Setup stopped. Fix the message above and run setup_mobile.bat again.
pause
exit /b 1
