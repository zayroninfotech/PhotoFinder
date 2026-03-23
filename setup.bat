@echo off
setlocal enabledelayedexpansion
echo ============================================
echo  PhotoFinder - Setup
echo ============================================

:: Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found. Install Python 3.10+ from python.org
    pause & exit /b 1
)

:: Kill Python processes
echo Stopping any running Python processes...
taskkill /F /IM python.exe >nul 2>&1
timeout /t 2 /nobreak

:: Remove old virtual environment for clean install
if exist "venv\" (
    echo Removing old virtual environment...
    rd /s /q venv
    timeout /t 1 /nobreak
)

echo Creating virtual environment...
python -m venv venv

echo Activating virtual environment...
call venv\Scripts\activate.bat

echo.
echo Installing dependencies (this may take 10-15 minutes)...
echo Step 1: Upgrading pip...
python -m pip install --upgrade pip

echo Step 2: Installing requirements...
python -m pip install -r requirements.txt

echo Step 3: Installing tf-keras for DeepFace...
python -m pip install tf-keras

echo Step 4: Verifying installation...
python -c "import flask, deepface, pymongo, PIL, qrcode; print('[OK] All packages ready')" >nul 2>&1
if errorlevel 1 (
    echo [WARNING] Retrying with clean cache...
    python -m pip install --no-cache-dir -r requirements.txt
)

echo.
echo ============================================
echo  Setup complete!
echo  Next steps:
echo   1. Edit config.py with your email credentials
echo   2. Run:  run.bat
echo   3. Open: http://localhost:5000
echo   4. Default login: vamsi / Zayron@2026
echo ============================================
pause
