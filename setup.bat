@echo off
echo ============================================
echo  PhotoFinder - Setup
echo ============================================

:: Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found. Install Python 3.10+ from python.org
    pause & exit /b 1
)

:: Create virtual environment if not exists
if not exist "venv\" (
    echo Creating virtual environment...
    python -m venv venv
)

echo Activating virtual environment...
call venv\Scripts\activate.bat

echo Installing dependencies (this may take a few minutes)...
pip install --upgrade pip
pip install -r requirements.txt

echo.
echo ============================================
echo  Setup complete!
echo  Next steps:
echo   1. Edit config.py with your email credentials
echo   2. Run:  run.bat
echo   3. Open: http://localhost:5000
echo   4. Default login: admin / admin123
echo ============================================
pause
