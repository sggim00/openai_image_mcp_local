@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>nul && (set PY=py -3) || (set PY=python)

if not exist .venv (
    echo [1/3] Creating virtual environment...
    %PY% -m venv .venv || goto :fail
)

echo [2/3] Installing packages...
.venv\Scripts\python.exe -m pip install -q --upgrade pip
.venv\Scripts\python.exe -m pip install -q -r requirements.txt || goto :fail

if not exist .env copy .env.example .env >nul

echo [3/3] Done.
echo.
echo  1) Put your OpenAI key in: %CD%\.env
echo  2) Add this to %%APPDATA%%\Claude\claude_desktop_config.json  ("mcpServers" section):
echo.
set "P=%CD:\=\\%"
echo     "openai-image-local": {
echo       "command": "%P%\\.venv\\Scripts\\python.exe",
echo       "args": ["%P%\\server.py"]
echo     }
echo.
echo  3) Or for Claude Code:
echo     claude mcp add openai-image-local -s user -- "%CD%\.venv\Scripts\python.exe" "%CD%\server.py"
echo.
echo  Then fully quit and restart Claude Desktop.
echo.
echo  4) To use it from your phone while this PC is on: run setup_mobile.bat
pause
exit /b 0

:fail
echo Install failed. Python 3.10+ is required.
pause
exit /b 1
