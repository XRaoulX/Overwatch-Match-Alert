# Overwatch 2 Match Found Notifier (v1.1.0)

A lightweight, portable Windows system-tray application that silently watches your Overwatch 2 game in the background and sends you a desktop notification (and optionally Alt-Tabs you back in) the moment your queue pops and a match is found!

## How It Works

The app utilizes the **Windows Graphics Capture (WGC)** API to capture frames from the Overwatch 2 game window in real-time, even when the game is completely obscured behind other windows (like a web browser or Discord). 

It uses **OpenCV Masked Template Matching** against perfectly isolated reference images to detect the exact pixel patterns of the "GAME FOUND" banner, the Hero Select screen, and Map Loading screens with 100% accuracy, without injecting anything into the game client (completely safe from anti-cheat).

---

## ⚠️ CRITICAL SETUP REQUIREMENT ⚠️

Because of how Windows 10/11 handles hardware-accelerated games, **you CANNOT fully minimize the game to the taskbar**. If you minimize the game, Windows completely pauses the game's rendering engine (Desktop Window Manager), and our scanner will only see a frozen frame. 

**To use this app while doing other things:**
1. Set Overwatch 2 to **Borderless Windowed** or **Windowed** mode in the Video Settings.
2. When you queue up, **Alt-Tab** out of the game or click on another window (like Google Chrome) to put it in the background.
3. As long as the game is *technically* open behind your other windows (not minimized to the taskbar icon), the scanner will see it perfectly and notify you!

---

## How to Use

### 1. Portable Executable (Recommended)
You do not need Python installed! 
1. Download **`Overwatch_Match_Alert.exe`** and place it anywhere on your PC (e.g., your Desktop).
2. Double-click the file to run it.
3. It will launch directly into your Windows System Tray (bottom right corner of your taskbar).

### 2. System Tray Controls
Right-click the app icon in your system tray to access controls:
* **Scanning:** Toggle the match scanner on or off. 
  * 🟠 **Orange Icon:** Actively scanning for a match.
  * 🟢 **Green Icon:** Match found! (Scanner goes dormant to save resources).
  * 🔘 **Gray Icon:** Manually paused.
* **Auto-focus game (Alt-Tab):** If checked, the app will automatically force Overwatch 2 to the front of your screen the exact second a match is found.
* **Select Target Window:** Explicitly lock the scanner to a specific Overwatch window (the app uses exact title matching to find the game automatically by default).
* **Phone Alerts (ntfy.sh):** Toggle push notifications to your phone on or off. You can easily copy your unique channel topic to your clipboard from the menu below it.
* **🔴 DEBUG MODE ON:** Instantly turns on aggressive logging and saves the last 10 screenshots to your disk if you need to troubleshoot why a match wasn't detected.

### 3. Config & Logs
The app automatically creates a folder in your user directory: `C:\Users\<YourUser>\.ow_notifier\`
* `config.ini` - Settings file for Phone Alerts.
* `ow_notifier.log` - Rotating log file (capped at 5MB).
* `debug_screenshots/` - (When debug is on) stores recent frames so you can see exactly what the scanner saw.

## For Developers

If you wish to edit the source code or tweak the templates:
1. Ensure Python 3.10+ is installed.
2. Install dependencies: `pip install -r requirements.txt`
3. Add or replace templates in the `masked_screenshots/` directory. They must be masked PNGs (pure black `[0,0,0]` is treated as fully transparent).
4. Run the raw python script: `python ow_match_alert.py`
5. **To Rebuild the .exe:** Simply double-click the included `build_exe.bat` script. It will automatically package everything via PyInstaller into the `dist_builds` folder.
