@echo off
setlocal EnableDelayedExpansion
title XenForo Scraper - Prerequisites Installer
color 0A

echo ================================================
echo  XenForo Scraper - Prerequisites Installer
echo ================================================
echo.

:: -----------------------------------------------
:: CHECK: Python
:: -----------------------------------------------
echo [1/4] Checking for Python...
python --version >nul 2>&1
if %errorlevel% == 0 (
    for /f "tokens=*" %%i in ('python --version 2^>^&1') do echo       Found: %%i
) else (
    echo       Python not found. Downloading installer...
    echo       A browser window will open - download and install Python 3.x
    echo       IMPORTANT: Check "Add Python to PATH" during install!
    echo.
    start https://www.python.org/downloads/
    echo       After installing Python, re-run this script.
    pause
    exit /b 1
)
echo.

:: -----------------------------------------------
:: CHECK: pip
:: -----------------------------------------------
echo [2/4] Checking for pip...
pip --version >nul 2>&1
if %errorlevel% == 0 (
    for /f "tokens=*" %%i in ('pip --version 2^>^&1') do echo       Found: %%i
) else (
    echo       pip not found - attempting to install...
    python -m ensurepip --upgrade
    if %errorlevel% neq 0 (
        echo       ERROR: Could not install pip. Please reinstall Python with pip enabled.
        pause
        exit /b 1
    )
)
echo.

:: -----------------------------------------------
:: INSTALL: Python packages
:: -----------------------------------------------
echo [3/4] Installing Python packages...
echo.

echo       Installing requests...
pip install requests --quiet
if %errorlevel% neq 0 ( echo       WARNING: requests install may have failed. )

echo       Installing beautifulsoup4...
pip install beautifulsoup4 --quiet
if %errorlevel% neq 0 ( echo       WARNING: beautifulsoup4 install may have failed. )

echo       Installing lxml (faster HTML parser)...
pip install lxml --quiet
if %errorlevel% neq 0 ( echo       WARNING: lxml install may have failed - script will fall back to html.parser. )

echo       Installing tqdm (progress bars)...
pip install tqdm --quiet
if %errorlevel% neq 0 ( echo       WARNING: tqdm install may have failed. )

echo       Installing playwright...
pip install playwright --quiet
if %errorlevel% neq 0 (
    echo       ERROR: playwright install failed.
    pause
    exit /b 1
)
echo.

:: -----------------------------------------------
:: INSTALL: Playwright Chromium browser
:: -----------------------------------------------
echo [4/4] Installing Playwright Chromium browser...
echo       (This may take a few minutes - it downloads a full browser)
echo.
playwright install chromium
if %errorlevel% neq 0 (
    echo.
    echo       ERROR: Chromium install failed.
    echo       Try running manually: playwright install chromium
    pause
    exit /b 1
)
echo.

:: -----------------------------------------------
:: DONE
:: -----------------------------------------------
echo ================================================
echo  All prerequisites installed successfully!
echo ================================================
echo.
echo  You can now run the scraper with:
echo  python xenforo_scraper.py ^<thread_url^>
echo.
pause
