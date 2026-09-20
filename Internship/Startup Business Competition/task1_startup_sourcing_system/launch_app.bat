@echo off
setlocal

cd /d "%~dp0"

set "PORT=8501"

REM Prefer the py launcher; fall back to python on PATH.
set "PY=python"
where py >nul 2>&1
if %errorlevel%==0 set "PY=py"

%PY% -c "import streamlit" >nul 2>&1
if errorlevel 1 (
    echo Streamlit is not installed in this environment.
    echo Run: %PY% -m pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

echo Starting University Startup Sourcing Agent on port %PORT%...
echo.

%PY% -m streamlit run app.py --server.port %PORT% --server.headless false
