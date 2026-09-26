@echo off
setlocal enabledelayedexpansion

:: Extract version from python script
for /f "delims=" %%i in ('python -c "import re; print(re.search(r'VERSION\s*=\s*[\"']([^\"']+)[\"']', open('ow_match_alert.py', encoding='utf-8').read()).group(1))"') do set VERSION=%%i

set EXE_NAME=Overwatch_Match_Alert_v%VERSION%

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
