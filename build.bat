@echo off
REM ===================================================================
REM  FIT Forge - build script
REM  Produces a single-file Windows executable: dist\FitForge.exe
REM  Run this from the folder containing fit_app.py
REM ===================================================================

echo.
echo ==========================================
echo   FIT Forge - build
echo ==========================================
echo.

REM --- sanity check -------------------------------------------------
if not exist fit_app.py (
    echo [ERROR] fit_app.py not found.
    echo         Run this script from the FIT Forge project folder.
    pause
    exit /b 1
)
if not exist fit_forge.html (
    echo [ERROR] fit_forge.html not found in this folder.
    pause
    exit /b 1
)

REM --- dependencies -------------------------------------------------
echo [1/3] Installing/updating dependencies...
python -m pip install --upgrade pip >nul
python -m pip install --upgrade pyinstaller pywebview garmin-fit-sdk
if errorlevel 1 (
    echo [ERROR] Dependency install failed.
    pause
    exit /b 1
)

REM --- clean previous build ----------------------------------------
echo [2/3] Cleaning previous build...
if exist build rmdir /s /q build
if exist dist  rmdir /s /q dist

REM --- build --------------------------------------------------------
echo [3/3] Building executable (this takes 1-3 minutes)...
pyinstaller FitForge.spec --noconfirm
if errorlevel 1 (
    echo.
    echo [ERROR] Build failed. See the messages above.
    pause
    exit /b 1
)

echo.
echo ==========================================
echo   DONE
echo ==========================================
echo   Your app:  dist\FitForge.exe
echo   Double-click it to run. No install needed.
echo ==========================================
echo.
pause
