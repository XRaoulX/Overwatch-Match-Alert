@echo off
setlocal enabledelayedexpansion

:: Check git branch
for /f "tokens=*" %%g in ('git rev-parse --abbrev-ref HEAD 2^>nul') do set BRANCH=%%g

:: Extract version from python script
for /f "tokens=3" %%a in ('findstr /C:"VERSION =" ow_match_alert.py') do set VER=%%a
set VER=%VER:"=%

:: Main branch always outputs static "Overwatch_Match_Alert.exe", other branches include version tag
if "%BRANCH%"=="main" (
    set EXE_NAME=Overwatch_Match_Alert
) else (
    set EXE_NAME=Overwatch_Match_Alert_v%VER%
)

echo Building %EXE_NAME%...

:: Clean up old builds
rmdir /S /Q build
:: Delete the specific new exe if it exists to ensure a clean write
del /Q dist_builds\%EXE_NAME%.exe 2>nul

:: Run PyInstaller
pyinstaller --noconfirm ^
    --onefile ^
    --windowed ^
    --icon=icon.ico ^
    --add-data "masked_screenshots;masked_screenshots" ^
    --add-data "icon.png;." ^
    --name "%EXE_NAME%" ^
    --distpath "dist_builds" ^
    ow_match_alert.py

echo.
echo Build complete! The new executable %EXE_NAME%.exe is located in the dist_builds folder.
