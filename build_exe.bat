@echo off
echo Building Overwatch Match Alert...

:: Clean up old builds
rmdir /S /Q build
rmdir /S /Q dist_builds\Overwatch_Match_Alert.exe

:: Run PyInstaller
pyinstaller --noconfirm ^
    --onefile ^
    --windowed ^
    --icon=icon.ico ^
    --add-data "masked_screenshots;masked_screenshots" ^
    --add-data "icon.png;." ^
    --name "Overwatch_Match_Alert" ^
    --distpath "dist_builds" ^
    ow_match_alert.py

echo.
echo Build complete! The new executable is located in the dist_builds folder.
