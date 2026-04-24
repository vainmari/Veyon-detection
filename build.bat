@echo off
REM ─────────────────────────────────────────────────────────────────────────
REM  Veyon AI Monitor — build script
REM  Run from repo root:  build.bat
REM ─────────────────────────────────────────────────────────────────────────

echo.
echo [1/4] Activating virtual environment...

if not exist ".venv\Scripts\activate.bat" (
    echo ERROR: .venv not found.
    echo Create it first:
    echo   python -m venv .venv
    echo   .venv\Scripts\Activate.ps1
    echo   pip install -e ".[dev,build]"
    pause
    exit /b 1
)

call .venv\Scripts\activate.bat
echo    venv active: %VIRTUAL_ENV%

echo.
echo [2/4] Checking build tools...

python -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
    echo PyInstaller not found in venv. Installing...
    pip install pyinstaller>=6.0
    if errorlevel 1 (
        echo ERROR: Failed to install PyInstaller.
        pause
        exit /b 1
    )
)

python -c "import nicegui" >nul 2>&1
if errorlevel 1 (
    echo ERROR: nicegui not found in venv. Run:
    echo   pip install -e ".[dev,build]"
    pause
    exit /b 1
)

echo    PyInstaller: OK
echo    nicegui: OK

echo.
echo [3/4] Running build.py...
python build.py
if errorlevel 1 (
    echo.
    echo Build FAILED. See errors above.
    pause
    exit /b 1
)

echo.
echo [4/4] Done!
echo.
echo   Distribute the folder:  dist\
echo   Users run:               dist\VeyonAIMonitor.exe
echo.
pause
