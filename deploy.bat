@echo off
echo ============================================
echo  PhotoFinder - Deploy to Railway
echo ============================================

:: Check git
git --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Git not installed. Get it from: https://git-scm.com
    pause & exit /b 1
)

:: Check railway CLI
railway --version >nul 2>&1
if errorlevel 1 (
    echo Installing Railway CLI...
    npm install -g @railway/cli
)

echo.
echo Step 1: Initializing Git repo...
git init
git add .
git commit -m "Initial PhotoFinder deploy"

echo.
echo Step 2: Logging in to Railway...
echo (A browser window will open — log in with GitHub or email)
railway login

echo.
echo Step 3: Creating Railway project...
railway init

echo.
echo Step 4: Setting environment variables...
echo Go to railway.app dashboard and set these in Variables tab:
echo   SECRET_KEY     = (any random string)
echo   ADMIN_USERNAME = admin
echo   ADMIN_PASSWORD = (your password)
echo   EMAIL_SENDER   = your-gmail@gmail.com
echo   EMAIL_PASSWORD = (16-char app password)
echo.

echo Step 5: Deploying...
railway up

echo.
echo ============================================
echo  DONE! Your app is live.
echo  Run: railway open   (to see your public URL)
echo ============================================
pause
