@echo off
echo Building Overwatch 2 Match Notifier...

:: Clean up old builds
rmdir /S /Q build
rmdir /S /Q dist_builds\OW2_Match_Notifier.exe

:: Run PyInstaller
pyinstaller --noconfirm ^
    --onefile ^
    --windowed ^
    --icon=icon.ico ^
    --add-data "masked_screenshots;masked_screenshots" ^
    --add-data "icon.png;." ^
    --name "OW2_Match_Notifier" ^
    --distpath "dist_builds" ^
    ow_notifier.py

echo.
echo Build complete! The new executable is located in the dist_builds folder.
