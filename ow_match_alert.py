"""
Overwatch 2 Match Found Notifier
================================
Lightweight system-tray app that detects when a match is found in Overwatch 2
and sends a Windows toast notification + optionally Alt-Tabs into the game.

Detection method:
  - Captures the screen via DXGI Desktop Duplication (DXCam) — works with
    fullscreen exclusive, borderless, and windowed modes
  - Checks the top-center banner region for the "GAME FOUND!" indicator
  - Uses color analysis (green checkmark on pink/red banner) + template matching
  - Also detects post-found screens: map vote, hero ban, hero select, team assembly

Usage:
  python ow_notifier.py          # Run with system tray icon
  python ow_notifier.py --debug  # Run with debug logging + saves debug screenshots
"""

import ctypes
import ctypes.wintypes
import logging
import logging.handlers
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Optional, Deque, Any
from collections import deque


import cv2
import numpy as np
from PIL import Image

try:
    import windows_capture
    HAS_WGC = True
except ImportError:
    HAS_WGC = False

try:
    import mss
    HAS_MSS = True
except ImportError:
    HAS_MSS = False

# ---------------------------------------------------------------------------
# App Info
# ---------------------------------------------------------------------------

APP_NAME = "Overwatch Match Alert"
VERSION = "1.2.0-dev.1"

# ---------------------------------------------------------------------------
# Constants & Config
# ---------------------------------------------------------------------------
import configparser
import urllib.request
import urllib.parse
import uuid

if getattr(sys, 'frozen', False):
    # Running in a PyInstaller bundle
    SCRIPT_DIR = Path(sys._MEIPASS)
else:
    # Running in a normal Python environment
    SCRIPT_DIR = Path(__file__).parent

APP_DATA_DIR = Path.home() / ".ow_notifier"
TEMPLATES_DIR = SCRIPT_DIR / "masked_screenshots"
DEBUG_DIR = APP_DATA_DIR / "debug_screenshots"
CONFIG_FILE = APP_DATA_DIR / "config.ini"

def generate_default_config():
    return f"""[PhoneAlerts]
# Set enabled to true to receive phone notifications
enabled = false

# Your unique notification topic. Do not share this with others!
# On your phone, install the 'ntfy' app (Android/iOS) or go to ntfy.sh
# and subscribe to this exact topic string:
ntfy_topic = ow_alert_{uuid.uuid4().hex[:8]}
"""

config = configparser.ConfigParser()
if not CONFIG_FILE.exists():
    APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, 'w') as f:
        f.write(generate_default_config())

config.read(CONFIG_FILE)

# Ensure PhoneAlerts section exists in older configs
if 'PhoneAlerts' not in config:
    with open(CONFIG_FILE, 'a') as f:
        f.write("\n" + generate_default_config())
    config.read(CONFIG_FILE)

# Overwatch 2 window title (exact match)
OW_WINDOW_TITLE = "Overwatch"

# Detection intervals
SCAN_INTERVAL_SEARCHING = 0.33  # seconds between scans while searching
SCAN_INTERVAL_IDLE = 3.0        # seconds between scans when game not in search

# Template matching threshold (0-1, higher = stricter)
TEMPLATE_MATCH_THRESHOLD = 0.70

# Color detection: the green checkmark HSV ranges
# Green checkmark: bright green circle
GREEN_CHECK_HSV_LOW = np.array([35, 100, 100])
GREEN_CHECK_HSV_HIGH = np.array([85, 255, 255])

# Pink/Red banner background HSV ranges
PINK_BANNER_HSV_LOW = np.array([140, 50, 100])
PINK_BANNER_HSV_HIGH = np.array([175, 255, 255])

# Minimum green pixel ratio in the checkmark region to trigger detection
GREEN_PIXEL_RATIO_THRESHOLD = 0.02
# Minimum pink pixel ratio in the banner region
PINK_PIXEL_RATIO_THRESHOLD = 0.15

# Tray Icon Colors
ICON_COLOR_ACTIVE = (255, 140, 0)   # Orange (Scanning)
ICON_COLOR_PAUSED = (128, 128, 128) # Gray (Paused manually)
ICON_COLOR_FOUND = (0, 255, 0)      # Green (Match found / Dormant)

def create_circle_icon(color: tuple, debug: bool = False) -> Image.Image:
    """Create a tray icon from the base icon with a colored status dot overlay."""
    from PIL import ImageDraw
    icon_path = SCRIPT_DIR / "icon.png"
    if icon_path.exists():
        try:
            base = Image.open(icon_path).convert("RGBA")
            base = base.resize((64, 64), Image.Resampling.LANCZOS)
            d = ImageDraw.Draw(base)
            # Draw a large colored status circle in the bottom right corner with a white border
            # Size: 34x34 pixels to make it very visible
            d.ellipse((28, 28, 62, 62), fill=color, outline=(255, 255, 255, 255), width=3)
            
            # If debug mode is on, draw a prominent RED indicator in the top right corner
            if debug:
                d.ellipse((44, 2, 62, 20), fill=(255, 0, 0), outline=(255, 255, 255, 255), width=2)
                
            return base
        except Exception as e:
            logging.error(f"Failed to load icon.png: {e}")

    # Fallback to simple circular icon image if file is missing
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    pixels = img.load()
    cx, cy, r = 32, 32, 28
    for x in range(64):
        for y in range(64):
            dist = ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5
            if dist < r:
                pixels[x, y] = (*color, 255)
            elif dist < r + 1.5:
                alpha = int(255 * (1 - (dist - r) / 1.5))
                pixels[x, y] = (*color, alpha)
    return img


# ---------------------------------------------------------------------------
# Win32 API helpers
# ---------------------------------------------------------------------------

user32 = ctypes.windll.user32

def get_window_title(hwnd: int) -> str:
    """Safely get window title using timeouts to prevent hangs."""
    if not hwnd:
        return ""
    SMTO_ABORTIFHUNG = 0x0002
    WM_GETTEXTLENGTH = 0x000E
    WM_GETTEXT = 0x000D
    
    # Configure argtypes for 64-bit compatibility
    user32.SendMessageTimeoutW.argtypes = [
        ctypes.wintypes.HWND, ctypes.wintypes.UINT, ctypes.wintypes.WPARAM, 
        ctypes.wintypes.LPARAM, ctypes.wintypes.UINT, ctypes.wintypes.UINT, 
        ctypes.POINTER(ctypes.c_size_t)
    ]
    user32.SendMessageTimeoutW.restype = ctypes.c_ssize_t

    res_len = ctypes.c_size_t()
    if user32.SendMessageTimeoutW(hwnd, WM_GETTEXTLENGTH, 0, 0, SMTO_ABORTIFHUNG, 50, ctypes.byref(res_len)):
        length = res_len.value
        if length > 0:
            buf = ctypes.create_unicode_buffer(length + 1)
            if user32.SendMessageTimeoutW(hwnd, WM_GETTEXT, length + 1, ctypes.addressof(buf), SMTO_ABORTIFHUNG, 50, ctypes.byref(res_len)):
                return buf.value
    return ""

def get_visible_windows() -> list[tuple[int, str]]:
    """Returns a list of (hwnd, title) for all visible top-level windows."""
    result = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
    def enum_callback(hwnd, lparam):
        if user32.IsWindowVisible(hwnd):
            title = get_window_title(hwnd)
            if title:
                result.append((hwnd, title))
        return True

    user32.EnumWindows(enum_callback, 0)
    return result


def find_target_window(state: 'AppState') -> Optional[int]:
    """Finds the Overwatch window or the user-selected target window."""
    # If the user selected a specific window, verify it still exists
    if getattr(state, "target_hwnd", None) is not None:
        if user32.IsWindow(state.target_hwnd):
            return state.target_hwnd
        else:
            logging.info("Target window closed, reverting to auto-detect")
            state.target_hwnd = None
            state.target_title = "Auto-detect (Overwatch)"

    # Auto-detect mode: match exactly "Overwatch"
    matches = [(hwnd, title) for hwnd, title in get_visible_windows()
               if title == OW_WINDOW_TITLE]
    if getattr(state, 'debug_mode', False) and matches:
        if len(matches) > 1:
            logging.debug(f"find_target_window: Multiple OW windows found: {matches}")
        logging.debug(f"find_target_window: Returning hwnd={matches[0][0]}, title='{matches[0][1]}'")
    if matches:
        return matches[0][0]

    return None


def get_window_rect(hwnd: int) -> Optional[tuple]:
    """Get the window's screen coordinates as (left, top, right, bottom)."""
    rect = ctypes.wintypes.RECT()
    if user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return (rect.left, rect.top, rect.right, rect.bottom)
    return None


def is_window_minimized(hwnd: int) -> bool:
    """Check if a window is minimized."""
    return bool(user32.IsIconic(hwnd))


def bring_window_to_front(hwnd: int):
    """Alt-Tab equivalent: bring the window to foreground."""
    SW_RESTORE = 9
    SW_SHOW = 5

    if is_window_minimized(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)

    # Use SetForegroundWindow
    user32.SetForegroundWindow(hwnd)

    # Also simulate Alt press to allow SetForegroundWindow to work
    # (Windows restricts SetForegroundWindow unless the calling process
    #  is in the foreground or the user pressed Alt)
    VK_MENU = 0x12
    KEYEVENTF_EXTENDEDKEY = 0x0001
    KEYEVENTF_KEYUP = 0x0002
    user32.keybd_event(VK_MENU, 0, KEYEVENTF_EXTENDEDKEY, 0)
    user32.keybd_event(VK_MENU, 0, KEYEVENTF_EXTENDEDKEY | KEYEVENTF_KEYUP, 0)
    user32.SetForegroundWindow(hwnd)


# ---------------------------------------------------------------------------
# Screen capture (WGC for window + mss fallback)
# ---------------------------------------------------------------------------

class ScreenCapture:
    """
    Captures the screen using Windows Graphics Capture (via windows_capture).
    This works with fullscreen exclusive DirectX games, borderless windowed,
    and regular windowed modes, even when minimized or obscured.

    Falls back to mss if WGC is unavailable.
    """

    def __init__(self, target_fps: int = 1):
        self._backend = None  # "wgc" or "mss"
        self._lock = threading.Lock()
        self._latest_frame = None
        self._capture_control = None
        self._hwnd = None

    @property
    def backend_name(self) -> str:
        return self._backend or "none"

    def _start_wgc(self, hwnd: int) -> bool:
        """Initialize the WGC capture backend for a specific window."""
        if not HAS_WGC:
            return False

        def _setup_cap(draw_border: bool):
            cap = windows_capture.WindowsCapture(
                window_hwnd=hwnd,
                draw_border=draw_border,
                cursor_capture=False,
            )

            @cap.event
            def on_frame_arrived(frame, capture_control):
                with self._lock:
                    self._latest_frame = frame.frame_buffer.copy()

            @cap.event
            def on_closed():
                with self._lock:
                    self._latest_frame = None
                logging.info("WGC session closed by Windows.")
            
            return cap

        try:
            try:
                # Try without the border first (Requires Windows 11 or newer Win 10 builds)
                cap = _setup_cap(draw_border=False)
                # DO NOT hold lock during start_free_threaded!
                ctrl = cap.start_free_threaded()
            except Exception as e:
                if "border is not supported" in str(e).lower():
                    logging.info("Hiding capture border unsupported on this OS. Retrying with border enabled.")
                    cap = _setup_cap(draw_border=True)
                    ctrl = cap.start_free_threaded()
                else:
                    raise
            
            with self._lock:
                self._capture_control = ctrl
                self._hwnd = hwnd
                self._backend = "wgc"
                
            logging.info(f"Screen capture: Windows Graphics Capture initialized for HWND {hwnd}")
            return True
        except Exception as e:
            logging.warning(f"WGC init failed: {e}")
            with self._lock:
                self._capture_control = None
                self._hwnd = None
            return False

    def grab(self, hwnd: int) -> Optional[np.ndarray]:
        """
        Capture the window.

        Args:
            hwnd: Window handle to capture.

        Returns:
            BGR numpy array, or None on failure.
        """
        ctrl_to_stop = None
        need_start = False
        
        with self._lock:
            # Check if we need to start or restart the capture session
            if self._capture_control is None or self._hwnd != hwnd:
                ctrl_to_stop = self._capture_control
                self._capture_control = None
                self._latest_frame = None
                self._hwnd = None
                need_start = True

        if ctrl_to_stop is not None:
            try:
                # Stop old session outside the lock!
                def _do_stop():
                    try:
                        ctrl_to_stop.stop()
                    except:
                        pass
                t = threading.Thread(target=_do_stop, daemon=True)
                t.start()
                t.join(timeout=1.0)
            except:
                pass
            
        if need_start:
            # Start new stream outside the lock!
            if not self._start_wgc(hwnd):
                # Fallback to MSS if WGC fails
                if HAS_MSS:
                    with self._lock:
                        self._backend = "mss"
                        self._hwnd = hwnd
                else:
                    return None

        # Determine backend and fetch frame securely under lock
        with self._lock:
            backend = self._backend
            frame = self._latest_frame

        # WGC Capture Path
        if backend == "wgc":
            if frame is not None:
                img = frame
                # WGC returns BGRA, convert to BGR (outside lock for speed)
                if img.shape[2] == 4:
                    img = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
                return img
            return None
        
        # MSS Fallback Path (doesn't support minimized capture well)
        if backend == "mss":
            try:
                rect = get_window_rect(hwnd)
                if not rect:
                    return None
                
                left, top, right, bottom = rect
                monitor = {
                    "left": left,
                    "top": top,
                    "width": right - left,
                    "height": bottom - top,
                }
                with mss.mss() as sct:
                    screenshot = sct.grab(monitor)
                    img = np.array(screenshot)
                    img = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
                    return img
            except Exception as e:
                logging.debug(f"mss grab failed: {e}")
                return None

        return None

    def release(self):
        """Release capture resources (used when going dormant)."""
        ctrl = None
        with self._lock:
            if self._capture_control is not None:
                ctrl = self._capture_control
                self._capture_control = None
            
            self._latest_frame = None
            self._hwnd = None
            self._backend = None
            
        if ctrl is not None:
            try:
                def _do_stop():
                    try:
                        ctrl.stop()
                    except:
                        pass
                t = threading.Thread(target=_do_stop, daemon=True)
                t.start()
                t.join(timeout=1.0)
            except Exception:
                pass
                
        logging.info("Screen capture released")

    def __del__(self):
        self.release()


# ---------------------------------------------------------------------------
# Detection logic
# ---------------------------------------------------------------------------

class GameState(Enum):
    UNKNOWN = auto()
    SEARCHING = auto()
    GAME_FOUND = auto()
    IN_LOBBY = auto()    # map vote / hero ban / hero select / assemble
    IN_MATCH = auto()
    IDLE = auto()        # main menu, not searching


@dataclass
class DetectionResult:
    state: GameState
    confidence: float = 0.0
    method: str = ""


class MatchDetector:
    """Detects Overwatch 2 match state from screen captures using masked templates."""

    def __init__(self, templates_dir: Path = TEMPLATES_DIR):
        self.templates_dir = templates_dir
        self.raw_templates = {}
        
        if templates_dir.exists():
            for template_path in templates_dir.glob("*.png"):
                img = cv2.imread(str(template_path), cv2.IMREAD_COLOR)
                if img is not None:
                    self.raw_templates[template_path.stem] = img
                    logging.debug(f"Loaded masked template: {template_path.stem}")
        else:
            logging.warning(f"Templates directory not found: {templates_dir}")

        logging.info(f"Loaded {len(self.raw_templates)} masked templates")
        
        self._cached_w = 0
        self._cached_h = 0
        self._scaled_templates = {}
        self.found_templates = {}
        self.search_templates = {}

    def _build_resolution_cache(self, w: int, h: int):
        """Pre-computes scaled templates, masks, and bounding boxes for the current resolution."""
        self._cached_w = w
        self._cached_h = h
        self._scaled_templates = {}
        
        REFERENCE_W, REFERENCE_H = 2560, 1440
        
        for name, img in self.raw_templates.items():
            if w != REFERENCE_W or h != REFERENCE_H:
                # Resize original image to match new resolution
                scaled = cv2.resize(img, (w, h), interpolation=cv2.INTER_AREA)
            else:
                scaled = img
                
            # Generate mask (all non-black pixels are UI)
            gray = cv2.cvtColor(scaled, cv2.COLOR_BGR2GRAY)
            _, mask = cv2.threshold(gray, 5, 255, cv2.THRESH_BINARY)
            
            coords = cv2.findNonZero(mask)
            if coords is not None:
                x, y, bw, bh = cv2.boundingRect(coords)
                
                if bw > 0 and bh > 0:
                    template_crop = scaled[y:y+bh, x:x+bw]
                    mask_crop = mask[y:y+bh, x:x+bw]
                    
                    # Search region (add a margin of 10 pixels to allow slight UI shifts)
                    margin = 10
                    sx = max(0, x - margin)
                    sy = max(0, y - margin)
                    sw = min(w - sx, bw + margin * 2)
                    sh = min(h - sy, bh + margin * 2)
                    
                    # Count non-zero mask pixels for confidence calculation
                    valid_pixels = np.count_nonzero(mask_crop)
                    
                    self._scaled_templates[name] = {
                        'template': template_crop,
                        'mask': mask_crop,
                        'sx': sx, 'sy': sy, 'sw': sw, 'sh': sh,
                        'valid_pixels': valid_pixels
                    }
        
        # Cache the separated dictionaries for faster lookup in detect()
        self.found_templates = {k: v for k, v in self._scaled_templates.items() if "searching" not in k.lower()}
        self.search_templates = {k: v for k, v in self._scaled_templates.items() if "searching" in k.lower()}

    def detect(self, image: np.ndarray) -> DetectionResult:
        """
        Analyze a full-window screenshot and determine the game state using masked templates.
        """
        h, w = image.shape[:2]
        
        if w != self._cached_w or h != self._cached_h:
            self._build_resolution_cache(w, h)
            
        import math
        THRESHOLD = 0.90 # Super strict match needed since we use masked RMSE

        # Helper to compute confidence
        def compute_conf(tinfo):
            sx, sy, sw, sh = tinfo['sx'], tinfo['sy'], tinfo['sw'], tinfo['sh']
            search_region = image[sy:sy+sh, sx:sx+sw]
            
            template = tinfo['template']
            mask = tinfo['mask']
            valid_pixels = tinfo['valid_pixels']
            
            if search_region.shape[0] < template.shape[0] or search_region.shape[1] < template.shape[1]:
                return 0.0
                
            res = cv2.matchTemplate(search_region, template, cv2.TM_SQDIFF, mask=mask)
            min_val, _, _, _ = cv2.minMaxLoc(res)
            
            if valid_pixels > 0:
                # Calculate Root Mean Square Error (RMSE) per channel per masked pixel
                rmse = math.sqrt(max(0.0, min_val) / (valid_pixels * 3))
                # Map RMSE to confidence (0 RMSE = 1.0 conf, 255 RMSE = 0.0 conf)
                return max(0.0, 1.0 - (rmse / 255.0))
            return 0.0

        # 1. Check for GAME_FOUND (Early exit)
        best_found_conf = 0.0
        best_found_method = ""
        for name, tinfo in self.found_templates.items():
            conf = compute_conf(tinfo)
            if conf > best_found_conf:
                best_found_conf = conf
                best_found_method = name
            if conf > THRESHOLD:
                return DetectionResult(GameState.GAME_FOUND, conf, name)

        # 2. Check for SEARCHING (Early exit)
        best_search_conf = 0.0
        best_search_method = ""
        for name, tinfo in self.search_templates.items():
            conf = compute_conf(tinfo)
            if conf > best_search_conf:
                best_search_conf = conf
                best_search_method = name
            if conf > THRESHOLD:
                return DetectionResult(GameState.SEARCHING, conf, name)
                
        return DetectionResult(GameState.UNKNOWN, 0.0, "none")


# ---------------------------------------------------------------------------
# Notification
# ---------------------------------------------------------------------------

def send_notification(title: str, message: str, icon_path: Optional[str] = None):
    """Send a Windows toast notification."""
    try:
        from winotify import Notification, audio
        toast = Notification(
            app_id="Overwatch Match Finder",
            title=title,
            msg=message,
            duration="short",
        )
        toast.set_audio(audio.Default, loop=False)
        if icon_path and os.path.exists(icon_path):
            toast.icon = icon_path
        toast.show()
    except Exception as e:
        logging.error(f"Notification failed: {e}")
        # Fallback: use Windows MessageBeep
        try:
            import winsound
            winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
        except Exception:
            pass


def play_alert_sound():
    """Play an alert sound."""
    try:
        import winsound
        # Play the system "Exclamation" sound
        winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
        # Also play a sequence of beeps for attention
        for freq in [800, 1000, 1200]:
            winsound.Beep(freq, 150)
            time.sleep(0.05)
    except Exception as e:
        logging.debug(f"Sound failed: {e}")

def send_ntfy_alert(title: str, message: str):
    """Send a push notification via ntfy.sh if enabled in config."""
    try:
        # Reload config in case user edited it via the Setup menu
        config.read(CONFIG_FILE)
        
        enabled = config.getboolean('PhoneAlerts', 'enabled', fallback=False)
        if not enabled:
            return
            
        topic = config.get('PhoneAlerts', 'ntfy_topic', fallback='')
        if not topic:
            return
            
        def _post():
            try:
                url = f"https://ntfy.sh/{topic}"
                data = message.encode('utf-8')
                
                req = urllib.request.Request(url, data=data, method='POST')
                req.add_header('Title', title.encode('utf-8'))
                req.add_header('Tags', 'video_game,loudspeaker')
                req.add_header('Priority', 'urgent')
                
                with urllib.request.urlopen(req, timeout=5) as response:
                    if response.status == 200:
                        logging.info(f"Phone alert sent to ntfy.sh/{topic}")
                    else:
                        logging.warning(f"Phone alert failed with status: {response.status}")
            except Exception as e:
                logging.error(f"Failed to send phone alert: {e}")
                
        threading.Thread(target=_post, daemon=True).start()
    except Exception as e:
        logging.error(f"Failed to process phone alert request: {e}")



# ---------------------------------------------------------------------------
# Main scanner loop
# ---------------------------------------------------------------------------

@dataclass
class AppState:
    """Shared application state."""
    running: bool = True
    scanning: bool = True
    auto_focus: bool = False
    last_state: GameState = GameState.UNKNOWN
    scan_count: int = 0
    match_found_count: int = 0
    debug_mode: bool = False
    status_text: str = "Starting..."
    # Automated arming state
    auto_dormant: bool = False
    current_mode: str = "INIT"
    # Track if we already notified for this search session
    notified_this_session: bool = False
    
    # Target window selection (None = Auto-detect)
    target_hwnd: Optional[int] = None
    target_title: str = "Auto-detect (Overwatch)"
    
    # Reference to the pystray icon to allow color changes
    tray_icon: Any = None
    
    # Buffer of recent scans for debugging (max 10)
    recent_scans: Deque[np.ndarray] = field(default_factory=lambda: deque(maxlen=10))
    # Lock for thread safety
    _lock: threading.Lock = field(default_factory=threading.Lock)
    
    _current_icon_color: tuple = ICON_COLOR_PAUSED

    def update_status(self, text: str):
        with self._lock:
            if self.status_text == text:
                return
            self.status_text = text
            
        if self.tray_icon:
            try:
                self.tray_icon.update_menu()
            except Exception:
                pass

    def set_icon_color(self, color: tuple = None):
        """Update the system tray icon color dynamically."""
        if color is not None:
            self._current_icon_color = color
        else:
            color = self._current_icon_color
            
        if self.tray_icon:
            # pystray allows setting the icon property directly from any thread
            self.tray_icon.icon = create_circle_icon(color, self.debug_mode)


def scanner_loop(state: AppState, detector: MatchDetector,
                 screen_capture: ScreenCapture):
    """Main scanning loop. Runs in a background thread.

    After a match is found, the scanner goes dormant (pauses itself and
    releases capture resources) to save resources. The user must manually
    re-enable scanning from the tray icon or console to resume.
    """
    logging.info(f"Scanner started (capture backend: {screen_capture.backend_name})")
    state.update_status("Scanner running - looking for Overwatch...")
    _last_focus_debug_time = 0.0  # throttle focus debug logs

    while state.running:
        try:
            if not state.scanning:
                if state.current_mode != "GRAY":
                    state.current_mode = "GRAY"
                    state.set_icon_color(ICON_COLOR_PAUSED)
                    if state.debug_mode:
                        logging.debug("Mode changed to GRAY (Manual Pause)")
                if "paused" not in state.status_text.lower():
                    state.update_status("Paused manually")
                time.sleep(1.0)
                continue

            # Find target window (used to confirm OW is running + for auto-focus + capture)
            hwnd = find_target_window(state)
            if not hwnd:
                state.update_status("Target window not found - waiting...")
                time.sleep(SCAN_INTERVAL_IDLE)
                continue

            # Determine if Overwatch window is the foreground window
            is_foreground = (hwnd == user32.GetForegroundWindow())
            # Debug logging for focus detection (throttled to every 5s)
            if state.debug_mode and (time.time() - _last_focus_debug_time >= 5.0):
                _last_focus_debug_time = time.time()
                fg_hwnd = user32.GetForegroundWindow()
                try:
                    ow_title = get_window_title(hwnd) if hwnd else "N/A"
                    fg_title = get_window_title(fg_hwnd) if fg_hwnd else "N/A"
                except Exception as e:
                    ow_title = "error"
                    fg_title = "error"
                    logging.debug(f"Focus debug exception: {e}")
                logging.debug(
                    f"Focus Debug - OW_hwnd={hwnd}, OW_title='{ow_title}', "
                    f"FG_hwnd={fg_hwnd}, FG_title='{fg_title}', "
                    f"is_foreground={is_foreground}, current_mode={state.current_mode}, "
                    f"auto_dormant={state.auto_dormant}, notified={state.notified_this_session}"
                )

            # If Overwatch is in foreground, we enter GREEN mode (In-Game / Dormant)
            if is_foreground:
                state.auto_dormant = False  # Clear auto-dormant since user is physically in the game
                # Always reset session tracking when user is in the game,
                # so that the next time they alt-tab out we start fresh
                state.notified_this_session = False
                if state.current_mode != "GREEN":
                    state.current_mode = "GREEN"
                    state.set_icon_color(ICON_COLOR_FOUND)
                    state.update_status("In-game (Active Window)")
                    if state.debug_mode:
                        logging.debug("Mode changed to GREEN (Game in foreground)")
                    # Release capture resources only on transition to save CPU
                    screen_capture.release()
                
                time.sleep(1.0)
                continue

            # If we have auto_dormant set (match recently found, user hasn't tabbed in yet)
            if state.auto_dormant:
                if state.current_mode != "GREEN":
                    state.current_mode = "GREEN"
                    state.set_icon_color(ICON_COLOR_FOUND)
                    state.update_status(f"Dormant (match #{state.match_found_count} found)")
                    if state.debug_mode:
                        logging.debug("Mode changed to GREEN (Match Found auto-dormant)")
                    # Release capture resources only on transition
                    screen_capture.release()
                
                time.sleep(1.0)
                continue

            # Otherwise, game is backgrounded and not auto_dormant. Enter ORANGE mode (Scanning)
            if state.current_mode != "ORANGE":
                state.current_mode = "ORANGE"
                state.auto_dormant = False
                state.notified_this_session = False
                state.set_icon_color(ICON_COLOR_ACTIVE)
                state.update_status("Scanner running - looking for match...")
                if state.debug_mode:
                    logging.debug("Mode changed to ORANGE (Game in background, scanning)")

            # Capture the window using WGC
            image = screen_capture.grab(hwnd)
            if image is None:
                state.update_status("Screenshot failed - retrying...")
                time.sleep(SCAN_INTERVAL_SEARCHING)
                continue

            state.scan_count += 1

            # Run detection
            result = detector.detect(image)

            # Unified debug saving logic
            if state.debug_mode:
                DEBUG_DIR.mkdir(exist_ok=True)
                state_name = result.state.name
                
                # Format differently based on the state
                if result.state == GameState.GAME_FOUND:
                    # We use +1 because match_found_count increments later in the loop
                    next_found_id = state.match_found_count + (1 if not state.notified_this_session else 0)
                    file_name = f"FOUND_{next_found_id:04d}.png"
                    pattern = "FOUND_*.png"
                else:
                    file_name = f"scan_{state.scan_count:06d}_{state_name}.png"
                    pattern = f"scan_*_{state_name}.png"
                    
                dump_path = DEBUG_DIR / file_name
                
                # Save the new screenshot
                cv2.imwrite(str(dump_path), image)
                
                # Keep only the 10 most recent images for this state category
                existing_files = sorted(DEBUG_DIR.glob(pattern))
                while len(existing_files) > 10:
                    oldest = existing_files.pop(0)
                    try:
                        oldest.unlink()
                    except Exception as e:
                        logging.debug(f"Failed to delete old debug file: {e}")

            # Handle state transitions
            prev_state = state.last_state

            if result.state == GameState.GAME_FOUND:
                state.update_status(
                    f"GAME FOUND! (conf={result.confidence:.2f}, method={result.method})"
                )
                logging.info(
                    f"GAME FOUND! confidence={result.confidence:.2f} method={result.method}"
                )

                if not state.notified_this_session:
                    state.notified_this_session = True
                    state.match_found_count += 1

                    # Send notification
                    send_notification(
                        "Overwatch - Game Found!",
                        "Your match is ready!",
                        str(SCRIPT_DIR / "icon.png")
                    )

                    # Send push notification
                    send_ntfy_alert(
                        "Overwatch Match Found!",
                        "Match Found."
                    )

                    # Play alert sound
                    play_alert_sound()

                    # Auto-focus if enabled
                    if state.auto_focus:
                        logging.info("Auto-focusing Overwatch window")
                        bring_window_to_front(hwnd)

                    # Trigger automated dormancy
                    logging.info("Match found - enabling auto_dormant mode")
                    state.auto_dormant = True

            elif result.state == GameState.IN_LOBBY:
                state.update_status(f"In lobby (conf={result.confidence:.2f})")

                # If we transitioned from searching or unknown to lobby, it means we missed
                # the "GAME FOUND!" banner - still notify
                if prev_state in (GameState.SEARCHING, GameState.UNKNOWN) and not state.notified_this_session:
                    state.notified_this_session = True
                    state.match_found_count += 1

                    send_notification(
                        "Overwatch - In Lobby!",
                        "Match started - you're in the lobby!"
                    )
                    send_ntfy_alert(
                        "Overwatch Match Found!",
                        "Match Found."
                    )
                    play_alert_sound()

                    if state.auto_focus:
                        bring_window_to_front(hwnd)

                    # Trigger automated dormancy
                    logging.info("Lobby found - enabling auto_dormant mode")
                    state.auto_dormant = True

            elif result.state == GameState.SEARCHING:
                state.update_status("Searching for game...")
                state.notified_this_session = False  # Reset for next match

            elif result.state == GameState.UNKNOWN:
                state.update_status("Monitoring...")

            state.last_state = result.state

            # Adjust scan interval based on state
            if result.state == GameState.SEARCHING:
                time.sleep(SCAN_INTERVAL_SEARCHING)
            else:
                time.sleep(SCAN_INTERVAL_IDLE)

        except Exception as e:
            logging.error(f"Scanner loop exception: {e}", exc_info=True)
            time.sleep(2.0)

    # Cleanup
    screen_capture.release()
    logging.info("Scanner stopped")


# ---------------------------------------------------------------------------
# System tray
# ---------------------------------------------------------------------------

def create_tray_icon(state: AppState, screen_capture: ScreenCapture):
    """Create and run the system tray icon."""
    import pystray

    def on_select_window(hwnd, title):
        def handler(icon, item):
            if hwnd == 0:
                state.target_hwnd = None
                state.target_title = "Auto-detect (Overwatch)"
                logging.info("Window selection reset to Auto-detect")
            else:
                state.target_hwnd = hwnd
                state.target_title = title
                logging.info(f"Target window explicitly set to: {title}")
            # Release screen capture so it rebinds to the newly selected window
            screen_capture.release()
        return handler

    def window_submenu():
        windows = get_visible_windows()
        # Sort so exact Overwatch window is at the top, then alphabetically
        windows.sort(key=lambda x: (x[1] != OW_WINDOW_TITLE, x[1].lower()))
        
        items = [
            pystray.MenuItem(
                "Auto-detect (Overwatch)",
                on_select_window(0, "Auto-detect (Overwatch)"),
                checked=lambda item: state.target_hwnd is None,
                radio=True
            ),
            pystray.Menu.SEPARATOR
        ]
        
        for hwnd, title in windows:
            # Limit title length for menu
            display_title = title[:40] + ("..." if len(title) > 40 else "")
            items.append(
                pystray.MenuItem(
                    display_title,
                    on_select_window(hwnd, title),
                    checked=lambda item, h=hwnd: state.target_hwnd == h,
                    radio=True
                )
            )
        return items

    def on_toggle_scanning(icon, item):
        state.scanning = not state.scanning
        if state.scanning:
            state.notified_this_session = False
            state.auto_dormant = False
            state.set_icon_color(ICON_COLOR_ACTIVE)
            state.update_status("Scanning resumed...")
            logging.info("Scanning resumed (Auto-mode re-armed)")
        else:
            state.set_icon_color(ICON_COLOR_PAUSED)
            state.update_status("Scanning paused")
            logging.info("Scanning paused")

    def on_toggle_auto_focus(icon, item):
        state.auto_focus = not state.auto_focus
        logging.info(f"Auto-focus {'enabled' if state.auto_focus else 'disabled'}")

    def on_test_notification(icon, item):
        send_notification("Test Notification", "Match finder is working!")
        play_alert_sound()
        
    def on_phone_alert_setup(icon, item):
        import subprocess
        # Open the config file in notepad
        try:
            subprocess.Popen(['notepad.exe', str(CONFIG_FILE)])
        except Exception as e:
            logging.error(f"Could not open config file: {e}")
            
    def on_test_phone_alert(icon, item):
        send_ntfy_alert("Test Alert", "Phone notification is working!")

    def on_toggle_phone_alerts(icon, item):
        config.read(CONFIG_FILE)
        current = config.getboolean('PhoneAlerts', 'enabled', fallback=False)
        new_state = not current
        
        try:
            import re
            with open(CONFIG_FILE, 'r') as f:
                content = f.read()
            content = re.sub(r'^(enabled\s*=\s*)(true|false)', rf'\g<1>{str(new_state).lower()}', content, flags=re.MULTILINE|re.IGNORECASE)
            with open(CONFIG_FILE, 'w') as f:
                f.write(content)
            config.read(CONFIG_FILE)
        except Exception as e:
            logging.error(f"Failed to update config file text: {e}")
        
        # Log and force a UI redraw to update the checkmark immediately
        status = "enabled" if new_state else "disabled"
        logging.info(f"Phone alerts {status}")
        state.set_icon_color()

    def phone_alerts_checked(item):
        return config.getboolean('PhoneAlerts', 'enabled', fallback=False)

    def on_copy_topic(icon, item):
        topic = config.get('PhoneAlerts', 'ntfy_topic', fallback='')
        if topic:
            import subprocess
            subprocess.run(['clip.exe'], input=topic.encode('utf-16le'), check=False)
            send_notification("Copied to Clipboard", f"ntfy Topic: {topic}", str(SCRIPT_DIR / "icon.png"))

    def on_quit(icon, item):
        state.running = False
        icon.stop()

    def scanning_checked(item):
        return state.scanning

    def auto_focus_checked(item):
        return state.auto_focus

    def on_toggle_debug(icon, item):
        state.debug_mode = not state.debug_mode
        new_level = logging.DEBUG if state.debug_mode else logging.INFO
        logging.getLogger().setLevel(new_level)
        logging.info(f"Debug mode {'enabled' if state.debug_mode else 'disabled'} (Logging Level: {logging.getLevelName(new_level)})")
        # Force the tray icon to redraw with the new debug indicator
        state.set_icon_color()

    def debug_checked(item):
        return state.debug_mode

    # Start with active or paused color based on initial state
    initial_color = ICON_COLOR_ACTIVE if state.scanning else ICON_COLOR_PAUSED
    icon = pystray.Icon(
        "ow_match_alert",
        icon=create_circle_icon(initial_color),
        title=f"{APP_NAME} v{VERSION}",
        menu=pystray.Menu(
            pystray.MenuItem(
                f"Version: {VERSION}",
                None,
                enabled=False,
            ),
            pystray.MenuItem(
                lambda text: f"Status: {state.status_text[:50]}",
                None,
                enabled=False,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Scanning",
                on_toggle_scanning,
                checked=scanning_checked,
                default=True,
            ),
            pystray.MenuItem(
                "Auto-focus game (Alt-Tab)",
                on_toggle_auto_focus,
                checked=auto_focus_checked,
            ),
            pystray.MenuItem(
                lambda text: "🔴 DEBUG MODE ON (Saving Images)" if state.debug_mode else "Debug Mode (Save Logs/Images)",
                on_toggle_debug,
                checked=debug_checked,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Select Target Window...",
                pystray.Menu(window_submenu)
            ),
            pystray.MenuItem(
                lambda text: f"Target: {state.target_title[:40]}",
                None,
                enabled=False,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                lambda text: f"Matches found: {state.match_found_count}",
                None,
                enabled=False,
            ),
            pystray.MenuItem(
                lambda text: f"Scans: {state.scan_count}",
                None,
                enabled=False,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Test Notification (Desktop)", on_test_notification),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Phone Alerts (ntfy.sh)",
                on_toggle_phone_alerts,
                checked=phone_alerts_checked,
            ),
            pystray.MenuItem(
                lambda text: f"Copy Topic: {config.get('PhoneAlerts', 'ntfy_topic', fallback='')}",
                on_copy_topic,
            ),
            pystray.MenuItem("Test Phone Alert", on_test_phone_alert),
            pystray.MenuItem("Advanced Phone Setup...", on_phone_alert_setup),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", on_quit),
        ),
    )

    state.tray_icon = icon
    icon.run()


# ---------------------------------------------------------------------------
# Console-only mode (no tray)
# ---------------------------------------------------------------------------

def console_loop(state: AppState, detector: MatchDetector,
                 screen_capture: ScreenCapture):
    """Run in console mode without system tray."""
    scanner_thread = threading.Thread(
        target=scanner_loop, args=(state, detector, screen_capture), daemon=True
    )
    scanner_thread.start()

    print("\n" + "=" * 60)
    print("  Overwatch 2 Match Notifier - Console Mode")
    print(f"  Capture: {screen_capture.backend_name}")
    print("=" * 60)
    print("\nCommands:")
    print("  p  - Pause/Resume scanning")
    print("  f  - Toggle auto-focus")
    print("  w  - Select target window")
    print("  d  - Toggle debug mode")
    print("  t  - Test notification")
    print("  s  - Show status")
    print("  q  - Quit")
    print()

    while state.running:
        try:
            cmd = input("> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            break
        except RuntimeError as e:
            if "sys.stdin" in str(e):
                logging.warning("Console input not available (running in windowed mode). Running in background...")
                # Sleep in a loop so the scanner thread can keep running in the background
                while state.running:
                    time.sleep(1)
                break
            else:
                raise

        if cmd == "q":
            state.running = False
        elif cmd == "p":
            state.scanning = not state.scanning
            if state.scanning:
                state.notified_this_session = False
            print(f"Scanning: {'ON' if state.scanning else 'PAUSED'}")
        elif cmd == "f":
            state.auto_focus = not state.auto_focus
            print(f"Auto-focus: {'ON' if state.auto_focus else 'OFF'}")
        elif cmd == "w":
            windows = get_visible_windows()
            print("\nAvailable windows:")
            print("  0: Auto-detect (Overwatch)")
            for i, (hwnd, title) in enumerate(windows, 1):
                print(f"  {i}: {title[:60]}")
            try:
                sel = int(input("Select window number: "))
                if sel == 0:
                    state.target_hwnd = None
                    state.target_title = "Auto-detect (Overwatch)"
                    print("Window selection reset to Auto-detect")
                elif 1 <= sel <= len(windows):
                    hwnd, title = windows[sel-1]
                    state.target_hwnd = hwnd
                    state.target_title = title
                    print(f"Target window set to: {title}")
                else:
                    print("Invalid selection.")
                screen_capture.release()
            except ValueError:
                print("Invalid input.")
        elif cmd == "d":
            state.debug_mode = not state.debug_mode
            new_level = logging.DEBUG if state.debug_mode else logging.INFO
            logging.getLogger().setLevel(new_level)
            print(f"Debug mode: {'ON' if state.debug_mode else 'OFF'} (Logging Level: {logging.getLevelName(new_level)})")
        elif cmd == "t":
            send_notification("Test Notification", "Match finder is working!")
            play_alert_sound()
        elif cmd == "s":
            print(f"  Capture:  {screen_capture.backend_name}")
            print(f"  Status:   {state.status_text}")
            print(f"  State:    {state.last_state.name}")
            print(f"  Scans:    {state.scan_count}")
            print(f"  Found:    {state.match_found_count}")
            print(f"  Scanning: {'ON' if state.scanning else 'PAUSED'}")
            print(f"  Focus:    {'ON' if state.auto_focus else 'OFF'}")
            print(f"  Debug:    {'ON' if state.debug_mode else 'OFF'}")

    state.running = False
    scanner_thread.join(timeout=5)
    print("Goodbye!")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    debug = "--debug" in sys.argv
    console = "--console" in sys.argv

    # Setup logging
    log_level = logging.DEBUG if debug else logging.INFO
    log_formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)
    
    # Remove any existing handlers
    if root_logger.hasHandlers():
        root_logger.handlers.clear()
        
    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(log_formatter)
    root_logger.addHandler(console_handler)
    
    # Rotating file handler (max 5MB, keep 1 backup)
    APP_DATA_DIR.mkdir(exist_ok=True)
    log_file_path = APP_DATA_DIR / "ow_notifier.log"
    file_handler = logging.handlers.RotatingFileHandler(
        log_file_path, maxBytes=5 * 1024 * 1024, backupCount=1
    )
    file_handler.setFormatter(log_formatter)
    root_logger.addHandler(file_handler)

    logging.info(f"{APP_NAME} v{VERSION} starting...")

    # Create detector
    detector = MatchDetector()
    if not detector.raw_templates:
        logging.error("No templates loaded!")
        print("\nERROR: No masked templates found.")
        print("  Please add your masked PNGs to the 'masked_screenshots' folder.")
        sys.exit(1)

    # Create screen capture (DXCam primary, mss fallback)
    try:
        screen_capture = ScreenCapture()
    except RuntimeError as e:
        logging.error(str(e))
        print(f"\nERROR: {e}")
        sys.exit(1)

    # Create shared state
    state = AppState(debug_mode=debug)

    if console:
        # Console-only mode
        console_loop(state, detector, screen_capture)
    else:
        # System tray mode
        scanner_thread = threading.Thread(
            target=scanner_loop,
            args=(state, detector, screen_capture),
            daemon=True,
        )
        scanner_thread.start()

        logging.info("Starting system tray icon...")
        try:
            create_tray_icon(state, screen_capture)
        except KeyboardInterrupt:
            pass
        finally:
            state.running = False
            scanner_thread.join(timeout=5)

    logging.info("Exiting.")


if __name__ == "__main__":
    main()
