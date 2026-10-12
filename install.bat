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

if not exist .env if exist .env.example copy .env.example .env >nul

echo [3/3] Registering in Claude Desktop and testing the server...
.venv\Scripts\python.exe register_claude.py
echo.
echo  OpenAI key: %CD%\.env  (OPENAI_API_KEY_FILE or OPENAI_API_KEY)
echo  Claude Code: claude mcp add openai-image-local -s user -- "%CD%\.venv\Scripts\python.exe" "%CD%\server.py"
echo  Phone use while this PC is on: run setup_mobile.bat
pause
exit /b 0

:fail
echo Install failed. Python 3.10+ is required.
pause
exit /b 1
