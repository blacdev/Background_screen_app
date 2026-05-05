@echo off
setlocal

cd /d "%~dp0"

where py >nul 2>&1
if errorlevel 1 (
    echo Python launcher ^(`py`^) was not found.
    echo Install Python for Windows first, then run this file again.
    exit /b 1
)

py -3 -m pip show pyinstaller >nul 2>&1
if errorlevel 1 (
    echo Installing PyInstaller...
    py -3 -m pip install pyinstaller
    if errorlevel 1 (
        echo Failed to install PyInstaller.
        exit /b 1
    )
)

for /f "usebackq delims=" %%I in (`py -3 release_tools.py print-version`) do set "APP_VERSION=%%I"
if not defined APP_VERSION (
    echo Failed to read app version from VERSION.
    exit /b 1
)

py -3 release_tools.py prepare-build >nul
if errorlevel 1 (
    echo Failed to generate build metadata.
    exit /b 1
)

echo Building single-file executable for version %APP_VERSION%...
py -3 -m PyInstaller --noconfirm --clean "background_screen_controller.spec"
if errorlevel 1 (
    echo Build failed.
    exit /b 1
)

echo.
echo Build complete:
echo %cd%\dist\Background Screen Controller.exe
echo Version: %APP_VERSION%

endlocal
