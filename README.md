# Overwatch 2 Match Found Notifier (v1.3.0)

A lightweight, portable Windows system-tray application that silently watches your Overwatch 2 game in the background and sends you a desktop notification (and optionally Alt-Tabs you back in) the moment your queue pops and a match is found!

> 📖 **Official GitHub Wiki:** Visit the [Overwatch Match Alert Wiki](https://github.com/XRaoulX/Overwatch-Match-Alert/wiki) for the full user guide, step-by-step mobile setup with screenshots, custom template masking tutorials, and troubleshooting tips!

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
  * ⚪ **Gray Icon:** Manually paused.
* **Auto-focus game (Alt-Tab):** If checked, the app will automatically force Overwatch 2 to the front of your screen the exact second a match is found. *(Persistent setting)*
  * └─ **Pause Media when auto-focusing:** If checked, the app will instantly pause any music or videos playing on your PC right before Alt-Tabbing you back into the game so you can hear voice chat.
* **Select Target Window:** Explicitly lock the scanner to a specific Overwatch window.
* **Phone Alerts (ntfy.sh):** Toggle push notifications to your phone on or off. Notifications automatically feature the crisp flat Overwatch logo!
  * └─ **Generate Android Backup...:** Automatically generates a perfect backup `.json` file for the Android `ntfy` app so you don't even have to type in your topic manually!
* **Open App Data Folder...:** Opens your `C:\Users\<YourUser>\.ow_notifier\` folder in Windows Explorer to access your logs, debug screenshots, and configuration file.
* **🐛 DEBUG MODE ON:** Instantly turns on aggressive logging and saves the last 10 screenshots to your disk.

### 3. Config, Logs & Custom Templates
The app automatically creates a folder in your user directory: `C:\Users\<YourUser>\.ow_notifier\`
* `config.ini` - Settings file for Phone Alerts, Tray toggles, and Custom Templates.
* `ow_notifier.log` - Rotating log file (capped at 5MB).
* `debug_screenshots/` - (When debug is on) stores recent frames so you can see exactly what the scanner saw.

**Custom Templates (New in v1.3.0)**
You can now add your own match detection images without recompiling the executable! Open the `config.ini` (via *Edit config...*) and set `enabled = true` under `[CustomTemplates]`. Then, place your masked `.png` images in the `custom_templates` folder next to the `.exe`. 
- Images with `searching` in the filename will be used to detect the queue timer.
- All other images will trigger the "Match Found" alert!

## For Developers

If you wish to edit the source code or tweak the templates:
1. Ensure Python 3.10+ is installed.
2. Install dependencies: `pip install -r requirements.txt`
3. Add or replace templates in the `masked_screenshots/` directory. They must be masked PNGs (pure black `[0,0,0]` is treated as fully transparent).
4. Run the raw python script: `python ow_match_alert.py`
5. **To Rebuild the .exe:** Simply double-click the included `build_exe.bat` script. It will automatically package everything via PyInstaller into the `dist_builds` folder.
