@echo off
echo ============================================
echo  PhotoFinder - Cleanup
echo ============================================
echo.
echo Stopping any running Python processes...
taskkill /F /IM python.exe /FI "WINDOWTITLE eq flask*" >nul 2>&1
taskkill /F /IM python.exe >nul 2>&1
timeout /t 2 /nobreak

echo Removing old virtual environment...
if exist "venv\" (
    rd /s /q venv
    echo [OK] venv removed
) else (
    echo [INFO] venv not found
)

echo.
echo Cleanup complete! You can now run:
echo   setup.bat
echo.
pause
