@echo off
REM Mabujang server (Windows): .\run.bat  -> http://localhost:8080
cd /d %~dp0
where claude >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Claude Code CLI not found. Install: npm i -g @anthropic-ai/claude-code
  exit /b 1
)
if exist .env for /f "usebackq eol=# tokens=1,* delims==" %%a in (".env") do set "%%a=%%b"
if not exist .venv python -m venv .venv
call .venv\Scripts\activate.bat
python -m pip install -q -r requirements.txt
if "%PORT%"=="" set PORT=8080
python -m uvicorn mabujang.app:app --host 0.0.0.0 --port %PORT%
