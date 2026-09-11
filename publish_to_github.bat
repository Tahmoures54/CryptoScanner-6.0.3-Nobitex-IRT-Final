```bat
@echo off
setlocal EnableExtensions EnableDelayedExpansion

title CryptoScanner 6.0.3 - GitHub Publisher

REM ============================================================
REM CryptoScanner 6.0.3
REM GitHub Publisher
REM ============================================================

cd /d "%~dp0"

set "PROJECT_DIR=%CD%"
set "BRANCH=main"
set "REPO=https://github.com/Tahmoures54/CryptoScanner-6.0.3-Nobitex-IRT-Final-trading.git"
set "COMMIT_MESSAGE=Publish CryptoScanner 6.0.3 Nobitex IRT"

echo.
echo ============================================================
echo   CryptoScanner 6.0.3
echo   GitHub Publisher
echo ============================================================
echo.
echo Project:
echo %PROJECT_DIR%
echo.
echo GitHub:
echo %REPO%
echo.
echo ============================================================
echo.

REM ============================================================
REM CHECK GIT
REM ============================================================

echo [1/10] Checking Git...

git --version

if errorlevel 1 (
    echo.
    echo ERROR: Git is not available.
    pause
    exit /b 1
)

echo.
echo Git OK.
echo.

REM ============================================================
REM INITIALIZE REPOSITORY
REM ============================================================

echo [2/10] Initializing local repository...

if not exist ".git" (
    git init

    if errorlevel 1 (
        echo ERROR: git init failed.
        pause
        exit /b 1
    )
) else (
    echo Local Git repository already exists.
)

git branch -M %BRANCH%

echo.
echo Local repository ready.
echo.

REM ============================================================
REM CREATE / VERIFY .gitignore
REM ============================================================

echo [3/10] Checking .gitignore...

if not exist ".gitignore" (
    echo.
    echo ERROR: .gitignore does not exist.
    echo.
    echo Create .gitignore before publishing.
    echo This is required to protect secrets.
    pause
    exit /b 1
)

echo .gitignore found.
echo.

REM ============================================================
REM REMOVE DANGEROUS FILES FROM INDEX
REM ============================================================

echo [4/10] Removing protected files from Git index if necessary...

git rm -r --cached --ignore-unmatch secrets >nul 2>&1
git rm --cached --ignore-unmatch .env >nul 2>&1
git rm --cached --ignore-unmatch secrets\api_key.txt >nul 2>&1
git rm --cached --ignore-unmatch secrets\config.ini >nul 2>&1
git rm --cached --ignore-unmatch secrets\license_key.key >nul 2>&1

echo Protected files checked.
echo.

REM ============================================================
REM CHECK DANGEROUS FILES
REM ============================================================

echo [5/10] Security check...

echo.
echo Checking for known sensitive files...
echo.

if exist "secrets" (
    echo [PROTECTED] secrets\ directory exists locally.
    echo It will NOT be uploaded.
)

if exist ".env" (
    echo [PROTECTED] .env exists locally.
    echo It will NOT be uploaded.
)

if exist "te.py" (
    echo [WARNING] te.py exists.
    echo.
    echo IMPORTANT:
    echo If te.py contains real API credentials,
    echo delete it before publishing.
    echo.
    echo Publishing will continue only if .gitignore excludes it.
)

echo.

REM ============================================================
REM SEARCH FOR HARD-CODED CREDENTIALS
REM ============================================================

echo Running basic source-code secret scan...
echo.

set "SECRET_FOUND=0"

findstr /S /I /N ^
 /C:"api_key =" ^
 /C:"api_secret =" ^
 /C:"private_key =" ^
 /C:"API_KEY =" ^
 /C:"API_SECRET =" ^
 /C:"PASSWORD =" ^
 /C:"TOKEN =" ^
 *.py trading\*.py gui\*.py core\*.py api\*.py 2>nul

if not errorlevel 1 (
    echo.
    echo [WARNING]
    echo Potential credential-related code was found.
    echo This may be normal configuration code.
    echo Review it before publishing.
    echo.
)

REM ============================================================
REM STAGE FILES
REM ============================================================

echo [6/10] Staging project files...

git add .

if errorlevel 1 (
    echo.
    echo ERROR: git add failed.
    pause
    exit /b 1
)

echo.
echo Files staged.
echo.

REM ============================================================
REM FINAL PROTECTED-FILE CHECK
REM ============================================================

echo [7/10] Final Git security check...
echo.

set "BAD_FILES=0"

for /f "delims=" %%F in ('git diff --cached --name-only') do (

    set "FILE=%%F"

    echo !FILE! | findstr /I /B /C:"secrets/" >nul
    if not errorlevel 1 (
        echo ERROR: secrets file staged: !FILE!
        set "BAD_FILES=1"
    )

    if /I "!FILE!"==".env" (
        echo ERROR: .env staged.
        set "BAD_FILES=1"
    )

    echo !FILE! | findstr /I /R "\.key$ \.pem$ \.p12$ \.pfx$" >nul
    if not errorlevel 1 (
        echo ERROR: private key file staged: !FILE!
        set "BAD_FILES=1"
    )

    if /I "!FILE!"=="te.py" (
        echo ERROR: te.py is staged.
        echo Remove te.py before publishing.
        set "BAD_FILES=1"
    )
)

if "%BAD_FILES%"=="1" (
    echo.
    echo ============================================================
    echo SECURITY STOP
    echo ============================================================
    echo.
    echo A protected file is staged.
    echo Publishing has been stopped.
    echo.
    git reset
    pause
    exit /b 1
)

echo.
echo Security check passed.
echo.

REM ============================================================
REM SHOW STAGED FILES
REM ============================================================

echo ============================================================
echo Files that will be committed
echo ============================================================
echo.

git diff --cached --name-only

echo.
echo ============================================================
echo.

REM ============================================================
REM COMMIT
REM ============================================================

echo [8/10] Creating Git commit...

git diff --cached --quiet

if not errorlevel 1 (
    echo No changes require a new commit.
) else (
    git commit -m "%COMMIT_MESSAGE%"

    if errorlevel 1 (
        echo.
        echo ERROR: Git commit failed.
        pause
        exit /b 1
    )

    echo Commit created successfully.
)

echo.

REM ============================================================
REM CONFIGURE REMOTE
REM ============================================================

echo [9/10] Configuring GitHub remote...

git remote get-url origin >nul 2>&1

if errorlevel 1 (

    echo Adding GitHub remote...

    git remote add origin "%REPO%"

    if errorlevel 1 (
        echo.
        echo ERROR: Could not add GitHub remote.
        pause
        exit /b 1
    )

) else (

    echo Existing origin found.
    echo Updating origin...

    git remote set-url origin "%REPO%"

    if errorlevel 1 (
        echo.
        echo ERROR: Could not update GitHub remote.
        pause
        exit /b 1
    )
)

echo.
echo Remote:
git remote -v
echo.

REM ============================================================
REM PUSH
REM ============================================================

echo [10/10] Publishing to GitHub...
echo.
echo Repository:
echo %REPO%
echo.
echo Branch:
echo %BRANCH%
echo.
echo ============================================================
echo.

git push -u origin %BRANCH%

if errorlevel 1 (
    echo.
    echo ============================================================
    echo PUSH FAILED
    echo ============================================================
    echo.
    echo The local commit was created successfully,
    echo but GitHub rejected the push.
    echo.
    echo Possible causes:
    echo.
    echo 1. GitHub authentication is required.
    echo 2. GitHub repository URL is incorrect.
    echo 3. Repository permissions are insufficient.
    echo 4. GitHub has an existing unrelated commit.
    echo.
    echo Check:
    echo.
    git remote -v
    echo.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo   PUBLISH SUCCESSFUL
echo ============================================================
echo.
echo Repository:
echo %REPO%
echo.
echo Branch:
echo %BRANCH%
echo.
echo Latest commit:
git log -1 --oneline
echo.
echo ============================================================
echo.
echo CryptoScanner has been published successfully.
echo.
echo Open:
echo https://github.com/Tahmoures54/CryptoScanner-6.0.3-Nobitex-IRT-Final-trading
echo.
pause

endlocal
```
