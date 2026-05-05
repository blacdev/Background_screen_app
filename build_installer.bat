@echo off
setlocal

cd /d "%~dp0"

for /f "usebackq delims=" %%I in (`py -3 release_tools.py print-version`) do set "APP_VERSION=%%I"
if not defined APP_VERSION (
    echo Failed to read app version from VERSION.
    exit /b 1
)

call build_exe.bat
if errorlevel 1 (
    echo EXE build failed.
    exit /b 1
)

set "ISCC_PATH="

where ISCC.exe >nul 2>&1
if not errorlevel 1 (
    for /f "delims=" %%I in ('where ISCC.exe') do set "ISCC_PATH=%%I"
)

if not defined ISCC_PATH if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC_PATH=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not defined ISCC_PATH if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set "ISCC_PATH=%ProgramFiles%\Inno Setup 6\ISCC.exe"

if not defined ISCC_PATH (
    echo Inno Setup Compiler ^(ISCC.exe^) was not found.
    echo Install Inno Setup 6 and run this file again.
    exit /b 1
)

echo Building Windows installer for version %APP_VERSION%...
"%ISCC_PATH%" /DAppVersion=%APP_VERSION% "background_screen_controller_installer.iss"
if errorlevel 1 (
    echo Installer build failed.
    exit /b 1
)

py -3 release_tools.py write-update-manifest --installer "installer-dist\BackgroundScreenControllerSetup-%APP_VERSION%.exe" --output "installer-dist\latest.json" >nul
if errorlevel 1 (
    echo Failed to write update manifest.
    exit /b 1
)

echo.
echo Installer build complete:
echo %cd%\installer-dist\BackgroundScreenControllerSetup-%APP_VERSION%.exe
echo Update manifest:
echo %cd%\installer-dist\latest.json

endlocal
