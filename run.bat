@echo off
echo ============================================
echo  PhotoFinder - Starting Server
echo ============================================

if exist "venv\Scripts\activate.bat" (
    call venv\Scripts\activate.bat
)

echo Starting Flask server on http://localhost:5000
echo Press Ctrl+C to stop.
echo.
python app.py
pause
