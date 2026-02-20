import time
import random, math
import datetime
import threading
import json
import os
import spidev
import subprocess
import requests
import re
import sys, logging
import shlex
import textwrap
import tempfile
from logging.handlers import RotatingFileHandler
from functools import lru_cache
from pathlib import Path
from typing import Optional, List
try:
    import yaml  # Optional; used for resorts_meta.yaml parsing
except Exception:
    yaml = None
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from packaging import version
from PIL import Image, ImageDraw, ImageFont, ImageEnhance, ImageFilter
from luma.core.interface.serial import spi
from luma.lcd.device import ili9341
try:
    from debug_hud import draw_cpu_badge, draw_wifi_bars_badge
    _HAS_DEBUG_HUD = True
except Exception as e:
    _HAS_DEBUG_HUD = False

    def draw_cpu_badge(*args, **kwargs):
        return None

    def draw_wifi_bars_badge(*args, **kwargs):
        return None

    print(f"[HUD] debug_hud unavailable ({e}); using no-op badges.")
try:
    from snowfall_overlay import SnowfallOverlay
    _SNOWFALL_OVERLAY_AVAILABLE = True
except Exception as e:
    _SNOWFALL_OVERLAY_AVAILABLE = False

    class SnowfallOverlay:
        # No-op fallback if snowfall_overlay (or psutil inside it) is missing.
        def __init__(self, *args, **kwargs):
            self.error = e

        def update_base(self, *args, **kwargs):
            pass

        def trigger(self, *args, **kwargs):
            pass

        def stop(self, *args, **kwargs):
            pass

        def on_enter(self, *args, **kwargs):
            pass

        def on_exit(self, *args, **kwargs):
            pass

    print(f"[Overlay] snowfall_overlay unavailable ({e}); using no-op overlay.")

# ----------------------------
# Constants & Config
# ----------------------------
REPO_URL = "https://github.com/spellbin/snowscraper.git"
LOCAL_REPO_PATH = "/home/pi/snowscraper"  # Replace with your local path
# Systemd integration
SERVICE_NAME = "snowscraper.service"     # change if your unit is named differently
UPDATER_UNIT = "snowgui-updater"     # transient unit name used for updates
VERSION_FILE = os.path.join(LOCAL_REPO_PATH, "VERSION")  # Path to version file
MAX_RETRIES = 3
RETRY_DELAY = 5
VERBOSE = False # set True for extra console logging ie. each touch read
GITHUB_TOKEN = None  # Optional GitHub token for private repos
CALIBRATION_FILE = "/home/pi/snowscraper/conf/touch_calibration.json"
HEARTBEAT_FILE = "/home/pi/snowscraper/heartbeat.txt"
HEARTBEAT_RAM_FILE = "/run/heartbeat.txt"
ALARM_CONF_FILE = "/home/pi/snowscraper/conf/alarm.conf"
HEARTBEAT_INTERVAL = 10  # seconds
DEV_MODE = False  # set True to avoid hitting live scrapers
SNOW_LOG_FILE = "/home/pi/snowscraper/logs/snow_log.json"
# Journald drop-in to force volatile storage (RAM) and reduce disk writes
JOURNALD_DROPIN_DIR = "/etc/systemd/journald.conf.d"
JOURNALD_VOLATILE_CONF = os.path.join(JOURNALD_DROPIN_DIR, "volatile.conf")
JOURNALD_VOLATILE_CONTENT = """[Journal]
Storage=volatile
RuntimeMaxUse=50M
RuntimeKeepFree=10M
RuntimeMaxFileSize=10M
"""

# Brightness profiles: shared by LCD dim overlay and LED scaling
BRIGHTNESS_CONF_FILE = "/home/pi/snowscraper/conf/brightness.conf"
BRIGHTNESS_LEVELS = [
    {"name": "Full", "scale": 1.0, "menu_bg": "images/mainmenu_night.png"},
    {"name": "Dim",  "scale": 0.35, "menu_bg": "images/mainmenu_day.png"},
]

# Shared resort metadata used across the UI.
RESORT_META_FILE = "conf/resorts_meta.yaml"
COUNTRY_CONF_FILE = "conf/country.conf"
REGION_CONF_FILE = "conf/region.conf"
ALL_COUNTRIES_LABEL = "All Countries"
ALL_REGIONS_LABEL = "All Regions"
ALL_RESORTS_LABEL = "All Resorts"  # legacy alias accepted in region.conf
OTHER_COUNTRY_LABEL = "Other"
OTHER_REGION_LABEL = "Other"
AVY_POINT_URL = "https://api.avalanche.ca/forecasts/en/products/point"
AVY_HEADERS = {
    "User-Agent": "SnowGUI-Avy/0.1 (+https://www.snowscraper.ca)",
    "Accept": "application/json",
}


# Alarm config cache (avoid disk IO every heartbeat iteration)
_alarm_cfg_cache = None
_alarm_cfg_lock = threading.RLock()

print(f"[BOOT] DEV_MODE = {DEV_MODE}")


def _atomic_write_text(content: str, path: str) -> None:
    """Write text atomically by replacing the target file in one move."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(prefix=f"{target.name}.", dir=target.parent)
        with os.fdopen(fd, "w", encoding="utf-8") as tmp_file:
            tmp_file.write(content)
            tmp_file.flush()
            os.fsync(tmp_file.fileno())
        os.replace(tmp_path, target)
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass


def _atomic_write_json(payload, path: str, *, indent=None) -> None:
    _atomic_write_text(json.dumps(payload, indent=indent), path)


# ----------------------------
# Brightness state (LCD dim overlay + LED scaling)
# ----------------------------
def _read_brightness_index(path=BRIGHTNESS_CONF_FILE, default=0) -> int:
    try:
        with open(path, "r") as f:
            raw = f.read().strip()
        idx = int(raw)
        return max(0, min(idx, len(BRIGHTNESS_LEVELS) - 1))
    except Exception:
        return default


def _write_brightness_index(index: int, path=BRIGHTNESS_CONF_FILE) -> bool:
    try:
        index = max(0, min(index, len(BRIGHTNESS_LEVELS) - 1))
        _atomic_write_text(str(index), path)
        return True
    except Exception as e:
        print(f"[Brightness] Failed to write {path}: {e}")
        return False


class BrightnessState:
    def __init__(self):
        self.levels = list(BRIGHTNESS_LEVELS)
        self.index = _read_brightness_index()
        self._apply_index(self.index)

    def _apply_index(self, idx: int):
        if not self.levels:
            self.levels = [{"name": "Full", "scale": 1.0, "menu_bg": "images/mainmenu.png"}]
        self.index = max(0, min(idx, len(self.levels) - 1))
        level = self.levels[self.index]
        self.name = level.get("name", "")
        self.scale = float(level.get("scale", 1.0))
        self.menu_bg = level.get("menu_bg", "images/mainmenu.png")

    def cycle(self):
        """Advance to the next brightness profile and persist."""
        next_idx = (self.index + 1) % len(self.levels)
        self._apply_index(next_idx)
        _write_brightness_index(self.index)

    def set_index(self, idx: int):
        self._apply_index(idx)
        _write_brightness_index(self.index)

    def is_dim(self) -> bool:
        return self.scale < 0.99


# Singleton brightness controller
brightness_state = BrightnessState()

# ---- Global hill singleton ---------------------------------
hill = None  # skiHill instance; refreshed when skihill.conf changes

def _is_systemd() -> bool:
    try:
        return os.path.isdir("/run/systemd/system")
    except Exception:
        return False

def _is_root() -> bool:
    try:
        return hasattr(os, "geteuid") and os.geteuid() == 0
    except Exception:
        return False

def _read_effective_journald_storage() -> Optional[str]:
    """
    Returns the effective Storage= mode for journald, or None if unknown.
    Prefers systemd-analyze to read the merged config; falls back to dir heuristics.
    """
    # Preferred: merged config view
    try:
        res = subprocess.run(
            ["systemd-analyze", "cat-config", "systemd/journald.conf"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        if res.stdout:
            for line in res.stdout.splitlines():
                m = re.match(r"^\s*Storage\s*=\s*(\w+)", line)
                if m:
                    return m.group(1).strip().lower()
    except Exception as e:
        print(f"[Journald] systemd-analyze probe failed: {e}")

    # Heuristic: presence of volatile vs persistent log dirs
    try:
        if os.path.isdir("/run/log/journal") and not os.path.isdir("/var/log/journal"):
            return "volatile"
        if os.path.isdir("/var/log/journal"):
            return "persistent"
    except Exception:
        pass
    return None

def _write_journald_volatile_dropin() -> bool:
    """
    Writes the drop-in that forces volatile journald storage.
    Returns True on success.
    """
    try:
        os.makedirs(JOURNALD_DROPIN_DIR, exist_ok=True)
        # Write atomically to avoid partial configs
        tmp_path = JOURNALD_VOLATILE_CONF + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(JOURNALD_VOLATILE_CONTENT)
        os.replace(tmp_path, JOURNALD_VOLATILE_CONF)
        return True
    except Exception as e:
        print(f"[Journald] Failed to write drop-in: {e}")
        return False

def ensure_journald_volatile():
    """
    Ensures journald writes only to RAM (Storage=volatile). Safe no-op if already set.
    """
    if not _is_systemd():
        print("[Journald] Not running under systemd; skipping journald configuration check.")
        return

    current = _read_effective_journald_storage()
    if current == "volatile":
        print("[Journald] Storage already volatile; no action needed.")
        return

    if not _is_root():
        print("[Journald] WARNING: need root to enforce volatile journald storage.")
        return

    if not _write_journald_volatile_dropin():
        print("[Journald] ERROR: could not write volatile drop-in.")
        return

    try:
        subprocess.run(
            ["systemctl", "restart", "systemd-journald.service"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        print("[Journald] Requested systemd-journald restart to apply volatile storage.")
    except Exception as e:
        print(f"[Journald] WARNING: failed to restart journald: {e}")

    # Re-check effective config to confirm
    post = _read_effective_journald_storage()
    if post != "volatile":
        print(f"[Journald] WARNING: expected volatile storage, detected '{post}'.")
    else:
        print("[Journald] Volatile storage confirmed.")

def _update_inline_git_checkout(version_str: str) -> bool:
    """
    Original inline update (used when systemd is not available).
    """
    if not version_str:
        return False
    try:
        _ensure_git_safe_dir(LOCAL_REPO_PATH)

        if not os.path.exists(os.path.join(LOCAL_REPO_PATH, ".git")):
            print("Repository not found, cloning...")
            subprocess.run(
                ["git", "clone", REPO_URL, LOCAL_REPO_PATH],
                check=True, capture_output=True, text=True
            )

        subprocess.run(
            ["git", "fetch", "--all", "--tags"],
            cwd=LOCAL_REPO_PATH, check=True, capture_output=True, text=True
        )
        subprocess.run(
            ["git", "checkout", f"tags/{version_str}", "-f"],
            cwd=LOCAL_REPO_PATH, check=True, capture_output=True, text=True
        )

        with open(VERSION_FILE, "w") as f:
            f.write(version_str)

        return True

    except subprocess.CalledProcessError as e:
        print(f"[Update] Inline update failed: {e.stderr}")
        return False
    except Exception as e:
        print(f"[Update] Inline update error: {e}")
        return False

def _systemd_run_update(version_str: str) -> bool:
    """
    Launch the updater as a transient systemd unit (older systemd compatible).
    - Avoids --replace (not present on some Raspberry Pi builds).
    - Uses a unique unit name to prevent collisions.
    - Probes for --collect support.
    - Requires running as root (system scope).
    Returns True if the transient unit was started successfully.
    """
    import os, time, textwrap, subprocess

    # Must be root to create a *system* transient unit.
    if os.geteuid() != 0:
        print("[Update] Not running as root: cannot create a system transient unit.")
        return False

    # Probe systemd-run flags on this OS
    try:
        help_txt = subprocess.run(
            ["systemd-run", "--help"], capture_output=True, text=True
        ).stdout
    except Exception as e:
        print(f"[Update] systemd-run unavailable: {e}")
        return False

    def _has(flag: str) -> bool:
        # simple string probe is sufficient for our needs
        return flag in help_txt

    # Unique unit name so we don't need --replace
    unit_name = f"snowgui-updater-{int(time.time())}"

    # The payload script the unit will execute
    script = textwrap.dedent(f"""\
        set -euo pipefail

        REPO=/home/pi/snowscraper
        TAG="{version_str}"

        # --- find runuser (path can vary on some images) ---------------------
        RUNUSER="$(command -v runuser || true)"
        if [ -z "$RUNUSER" ]; then
          for CAND in /sbin/runuser /usr/sbin/runuser /bin/runuser /usr/bin/runuser; do
            [ -x "$CAND" ] && RUNUSER="$CAND" && break
          done
        fi
        if [ -z "$RUNUSER" ]; then
          echo "[Updater] ERROR: runuser not found."
          exit 127
        fi

        # --- detect service to stop/start (non-fatal if missing) ------------
        detect_service() {{
          local candidates="snowscraper.service snowgui.service"
          local picked=""
          for S in $candidates; do
            systemctl list-unit-files | awk '{{print $1}}' | grep -xq "$S" && {{ picked="$S"; break; }}
            systemctl status "$S" >/dev/null 2>&1 && {{ picked="$S"; break; }}
          done
          echo "$picked"
        }}

        SVC="$(detect_service || true)"
        if [ -n "$SVC" ]; then
          echo "[Updater] Using service: $SVC"
          echo "[Updater] Stopping $SVC"
          systemctl stop "$SVC" || echo "[Updater] WARNING: stop failed; continuing."
        else
          echo "[Updater] WARNING: No matching service found; proceeding without stop/start."
        fi

        echo "[Updater] Ensuring repo exists: $REPO"
        if [ ! -d "$REPO/.git" ]; then
          echo "[Updater] ERROR: $REPO is not a git repo"
          [ -n "$SVC" ] && systemctl start "$SVC" || true
          exit 128
        fi

        echo "[Updater] Fetching tags (as pi)"
        "$RUNUSER" -u pi -- git -c safe.directory="$REPO" -C "$REPO" fetch --all --prune --tags

        echo "[Updater] Verifying tag $TAG exists"
        "$RUNUSER" -u pi -- git -c safe.directory="$REPO" -C "$REPO" rev-parse "refs/tags/$TAG" >/dev/null

        echo "[Updater] Checking out tag $TAG (force)"
        "$RUNUSER" -u pi -- git -c safe.directory="$REPO" -C "$REPO" checkout -f "tags/$TAG"

        echo "[Updater] Writing VERSION file"
        printf "%s" "$TAG" | "$RUNUSER" -u pi -- tee "$REPO/VERSION" >/dev/null

        if [ -n "$SVC" ]; then
          echo "[Updater] Starting $SVC"
          systemctl start "$SVC" || echo "[Updater] WARNING: start failed."
        fi

        echo "[Updater] Done."
    """)

    # Build the systemd-run command with only flags supported on this box
    cmd = [
        "systemd-run",
        "--quiet",
        "--unit", unit_name,
        "--property=Type=oneshot",
        "--property=RemainAfterExit=no",
    ]
    if _has("--collect"):
        cmd.append("--collect")

    # Environment (harmless even if the script doesn't use VER directly)
    cmd += [
        "--setenv", "GIT_TERMINAL_PROMPT=0",
        "--setenv", "HOME=/home/pi",
        "--setenv", f"VER={version_str}",
        "/bin/bash", "-lc", script,
    ]

    try:
        res = subprocess.run(cmd, check=True, capture_output=True, text=True)
        out = res.stdout.strip()
        if out:
            print(f"[Update] systemd-run started: {out}")
        else:
            print(f"[Update] systemd-run started unit {unit_name}")
        return True
    except subprocess.CalledProcessError as e:
        print(f"[Update] systemd-run failed (rc={e.returncode})")
        if e.stdout:
            print(f"[Update] stdout:\n{e.stdout}")
        if e.stderr:
            print(f"[Update] stderr:\n{e.stderr}")
        return False

# --- Logging bootstrap (keep prints working, also log to file) ---
# Log file lives next to this script: ./logs/snowgui.log
_HERE = Path(__file__).resolve().parent
_LOG_DIR = _HERE / "logs"
try:
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
except Exception as e:
    # If we cannot create the log directory, continue with console-only logging.
    try:
        sys.__stderr__.write(f"[Logging] Could not ensure log dir {_LOG_DIR}: {e}\n")
    except Exception:
        pass
_LOG_PATH = _LOG_DIR / "snowgui.log"

logger = logging.getLogger("snowgui")
logger.setLevel(logging.INFO)

_fmt = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")


class _FailSafeRotatingFileHandler(RotatingFileHandler):
    """
    Rotating file handler that disables itself on the first OSError so logging
    continues via the console handler.
    """
    def __init__(self, *args, logger_ref=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._logger_ref = logger_ref
        self._failed = False

    def emit(self, record):
        if self._failed:
            return
        try:
            super().emit(record)
        except OSError as exc:
            self._failed = True
            try:
                self.close()
            except Exception:
                pass
            # Remove the handler so we fall back to console-only logging.
            if self._logger_ref:
                try:
                    self._logger_ref.removeHandler(self)
                except Exception:
                    pass
            try:
                sys.__stderr__.write(
                    f"[Logging] Disabling file logging ({exc}); console only from now on.\n"
                )
            except Exception:
                pass

# File handler (rotates at ~512 KB, keeps 3 backups); disabled if IO fails.
_fh = None
try:
    _fh = _FailSafeRotatingFileHandler(
        _LOG_PATH,
        maxBytes=512 * 1024,
        backupCount=3,
        logger_ref=logger,
    )
    _fh.setFormatter(_fmt)
    _fh.setLevel(logging.INFO)
except Exception as e:
    try:
        sys.__stderr__.write(f"[Logging] File handler unavailable ({e}); using console only.\n")
    except Exception:
        pass

# Console handler (so you still see output when running interactively)
_sh = logging.StreamHandler(sys.__stdout__)
_sh.setFormatter(_fmt)
_sh.setLevel(logging.INFO)

# Avoid duplicate handlers if the module is reloaded
if not logger.handlers:
    logger.addHandler(_sh)
    if _fh:
        logger.addHandler(_fh)

# Pipe Python warnings (e.g., RuntimeWarning from GPIO/luma) into logging
logging.captureWarnings(True)

# Redirect print() to logging so you don't have to change your code
class _PrintToLog:
    def __init__(self, level=logging.INFO):
        self.level = level
        self._buf = ""
    def write(self, msg):
        # accumulate and emit one line at a time
        self._buf += str(msg)
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line:
                logger.log(self.level, line)
    def flush(self):
        if self._buf:
            logger.log(self.level, self._buf)
            self._buf = ""

# Send normal prints to INFO, errors/tracebacks to ERROR
sys.stdout = _PrintToLog(logging.INFO)
sys.stderr = _PrintToLog(logging.ERROR)

# ---- Dynamic text fit helpers ----
@lru_cache(maxsize=64)
def _font_cached(path: str, size: int):
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        return ImageFont.load_default()

def _measure(draw: ImageDraw.ImageDraw, text: str, font):
    # Returns (w, h) for the rendered text
    # textbbox is precise; falls back to textsize if needed
    try:
        l, t, r, b = draw.textbbox((0, 0), text, font=font)
        return (r - l, b - t)
    except Exception:
        return draw.textsize(text, font=font)

def _shrink_to_fit(draw, text: str, box_w: int, box_h: int,
                   font_path: str, min_sz: int = 10, max_sz: int = 40):
    # Binary-search the largest size that fits
    lo, hi = min_sz, max_sz
    best_font, best_size = None, None
    while lo <= hi:
        mid = (lo + hi) // 2
        f = _font_cached(font_path, mid)
        w, h = _measure(draw, text, f)
        if w <= box_w and h <= box_h:
            best_font, best_size = f, mid
            lo = mid + 1
        else:
            hi = mid - 1

    if best_font:
        return best_font, text

    # If even min size wonÃ¢â‚¬â„¢t fit, ellipsize
    f = _font_cached(font_path, min_sz)
    s = text
    while s and _measure(draw, s + "Ã¢â‚¬Â¦", f)[0] > box_w:
        s = s[:-1]
    return f, (s + "Ã¢â‚¬Â¦") if s else "Ã¢â‚¬Â¦"

def draw_text_in_box(img, text: str, box_xywh, font_path: str,
                     color="white", min_sz=10, max_sz=40, align="center"):
    x, y, w, h = box_xywh
    draw = ImageDraw.Draw(img)
    font, txt = _shrink_to_fit(draw, text, w, h, font_path, min_sz, max_sz)
    tw, th = _measure(draw, txt, font)

    if align == "center":
        tx = x + (w - tw) // 2
    elif align == "right":
        tx = x + (w - tw)
    else:
        tx = x
    ty = y + (h - th) // 2
    draw.text((tx, ty), txt, fill=color, font=font)

# Also catch totally unhandled exceptions and log stack traces
def _excepthook(exctype, value, tb):
    logger.exception("Unhandled exception", exc_info=(exctype, value, tb))
sys.excepthook = _excepthook
# --- end logging bootstrap ---

# ----------------------------
# WS2812 LED (Neopixel) integration for Pi Zero 2 W
# - Pin: GPIO13 (PWM1 / Channel 1) -> avoids buzzer on GPIO18 (PWM0)
# - Python 3.9
# - Effects: snow color map, delta-based breathing, >15cm sparkles, splash rainbow
# ----------------------------

try:
    from rpi_ws281x import PixelStrip, Color, ws
    _HAS_PIXELS = True
except Exception:
    _HAS_PIXELS = False

LED_PIN = 13                 # <<< GPIO13 (PWM1)
LED_CHANNEL = 1              # <<< PWM1 channel
LED_COUNT = 7
LED_FREQ_HZ = 800_000
LED_DMA = 10
LED_INVERT = False
LED_BRIGHTNESS_MAX = 255     # driver max; we do our own scaling
LED_STRIP_TYPE = ws.WS2811_STRIP_GRB  # most WS2812 rings are GRB

class SnowLEDs:
    def __init__(self):
        self.strip = None
        self._lock = threading.Lock()
        # worker that runs only when breathing is enabled (value changed)
        self._breath_thread = None
        self._breath_stop = threading.Event()
        # lightweight sparkle worker for >15cm
        self._sparkle_thread = None
        self._sparkle_stop = threading.Event()

        # state
        self._base_color = (0, 0, 0)
        self._steady_brightness = 0.35   # used when NOT breathing
        self._current_cm = 0
        self._prev_cm = 0
        self._global_scale = getattr(brightness_state, "scale", 1.0)

        if _HAS_PIXELS:
            try:
                self.strip = PixelStrip(
                    LED_COUNT,
                    LED_PIN,
                    LED_FREQ_HZ,
                    LED_DMA,
                    LED_INVERT,
                    LED_BRIGHTNESS_MAX,
                    LED_CHANNEL,
                    strip_type=LED_STRIP_TYPE
                )
                self.strip.begin()
                print("[LED] WS2812 initialized on GPIO13 (PWM1/channel 1).")
            except Exception as e:
                print(f"[LED] Init failed: {e}")
                self._make_dummy()
        else:
            self._make_dummy()

    def _make_dummy(self):
        class _Dummy:
            def setPixelColor(self, i, c): pass
            def show(self): pass
            def numPixels(self): return LED_COUNT
        self.strip = _Dummy()

    # ---------- public API ----------
    def set_snow_value(self, cm_now: int, cm_prev: int):
        """
        Set visual state for current snow.

        - Uses REAL cm values for change detection, breathing, and sparkle logic.
        - Clamps ONLY for color mapping (0Ã¢â‚¬â€œ20 cm visual scale).
        """
        # Raw values (can be >20)
        raw_now = int(cm_now or 0)
        raw_prev = int(cm_prev or 0)

        # Clamped value for color mapping only
        color_cm = max(0, min(20, raw_now))

        print(f"[LED] Set Snow value now: {raw_now} prev: {raw_prev}")

        with self._lock:
            # Preserve real values for internal logic
            self._current_cm = raw_now
            self._prev_cm = raw_prev

            # Base color uses clamped visual range
            self._base_color = self._color_for_cm(color_cm) if raw_now > 0 else (0, 0, 0)

        # Sparkle on heavy snowfall using REAL value
        if raw_now > 20:
            self._start_sparkle()
        else:
            self._stop_sparkle()

        # No snow -> off
        if raw_now <= 0:
            self._stop_breathe()
            self._paint_solid((0, 0, 0), 0.0)
            return

        # Value changed -> breathing based on REAL delta
        if raw_now != raw_prev:
            delta = abs(raw_now - raw_prev)
            period = self._breath_period_for_delta(delta)
            self._start_breathe(period_sec=period)
        else:
            # Unchanged -> steady, no breathing
            self._stop_breathe()
            self._paint_solid(self._base_color, self._steady_brightness)

    def rainbow_fade_in(self, duration_sec=5.0):
        """Strandtest-style rainbow that fades in over the splash duration, then turns off."""
        t0 = time.time()
        random.seed(int(t0) ^ os.getpid())
        while True:
            t = time.time() - t0
            if t >= duration_sec:
                break
            # smooth fade 0->1
            u = max(0.0, min(1.0, t / duration_sec))
            fade = u * u * (3 - 2 * u)  # smoothstep
            wheel_base = int((t * 256 / 5.0))  # ~one full wheel per ~5s
            for i in range(self.strip.numPixels()):
                r, g, b = self._wheel((wheel_base + int(i * (256 / max(1, self.strip.numPixels())))) & 255)
                self._set_pixel(i, (int(r * fade), int(g * fade), int(b * fade)))
            self.strip.show()
            time.sleep(0.02)
        self.clear()  # off when splash ends

    def clear(self):
        self._stop_breathe()
        self._stop_sparkle()
        for i in range(self.strip.numPixels()):
            self.strip.setPixelColor(i, Color(0, 0, 0))
        self.strip.show()

    # ---------- internals ----------
    def _paint_solid(self, rgb, brightness):
        with self._lock:
            r, g, b = rgb
            brightness = max(0.0, min(1.0, brightness * self._global_scale))
            r = int(r * brightness); g = int(g * brightness); b = int(b * brightness)
            for i in range(self.strip.numPixels()):
                self._set_pixel(i, (r, g, b))
            self.strip.show()

    def _set_pixel(self, i, rgb):
        r, g, b = rgb
        # Color() takes RGB; GRB packing is handled by strip_type
        self.strip.setPixelColor(i, Color(r, g, b))

    # ----- breathing worker -----
    def _start_breathe(self, period_sec=6.0):
        # restart with new period
        self._stop_breathe()
        self._breath_stop.clear()
        self._breath_thread = threading.Thread(
            target=self._breathe_loop, args=(period_sec,), daemon=True
        )
        self._breath_thread.start()

    def _stop_breathe(self):
        self._breath_stop.set()
        t = self._breath_thread
        if t and t.is_alive():
            t.join(timeout=0.6)
        self._breath_thread = None

    def _breathe_loop(self, period_sec):
        base = self._base_color
        low, high = 0.18, 0.85
        t0 = time.time()
        while not self._breath_stop.is_set():
            # cosine wave 0..1
            phase = ((time.time() - t0) % period_sec) / period_sec
            amp = 0.5 - 0.5 * math.cos(2 * math.pi * phase)
            brightness = low + (high - low) * amp
            self._paint_solid(base, brightness)
            time.sleep(0.02)  # ~50 FPS

    def _breath_period_for_delta(self, delta):
        # delta 1 -> slow (~8s), delta Ã¢â€°Â¥10 -> fast (~1.5s)
        delta = max(1, min(10, int(delta)))
        return max(1.5, 8.0 - (delta - 1) * 0.73)

    # ----- sparkle worker (>20 cm) -----
    def _start_sparkle(self):
        if self._sparkle_thread and self._sparkle_thread.is_alive():
            return
        self._sparkle_stop.clear()
        self._sparkle_thread = threading.Thread(target=self._sparkle_loop, daemon=True)
        self._sparkle_thread.start()

    def _stop_sparkle(self):
        self._sparkle_stop.set()
        t = self._sparkle_thread
        if t and t.is_alive():
            t.join(timeout=0.6)
        self._sparkle_thread = None

    def _sparkle_loop(self):
        """Overlay brief white sparkles; respects steady/breathing repaints."""
        rng = random.Random()
        while not self._sparkle_stop.is_set():
            cm = self._current_cm
            # spawn rate grows with 16..20cm
            spawn_prob = 0.10 + 0.15 * max(0.0, min(1.0, (cm - 15) / 5.0))
            # draw base (if breathing is off, keep solid visible)
            base = self._base_color
            self._paint_solid(base, self._steady_brightness if self._breath_thread is None else 0.50)
            # choose a few pixels to flash
            for i in range(self.strip.numPixels()):
                if rng.random() < spawn_prob:
                    self._set_pixel(i, (255, 255, 255))
            self.strip.show()
            time.sleep(0.08)

    def set_global_brightness(self, scale: float):
        """Apply a global brightness scalar (shared dimmer). Repaint immediately."""
        try:
            scale = float(scale)
        except Exception:
            scale = 1.0
        scale = max(0.05, min(1.0, scale))
        self._global_scale = scale
        # repaint current state so dimmer takes effect right away
        base = self._base_color
        brightness = self._steady_brightness if self._breath_thread is None else 0.50
        self._paint_solid(base, brightness)

    # ----- color helpers -----
    def _color_for_cm(self, cm):
        """1..10: light blue -> deep blue -> purple; 10..20: purple -> dark red -> bright red."""
        # anchors
        light_blue = (168, 216, 255)  # airy low end
        deep_blue  = (0,   72, 255)   # darker mid-blue
        purple     = (128,  0, 255)   # pivot @10
        dark_red   = (139,  0,  0)    # ~15
        bright_red = (255,  0,  0)    # 20

        cm = max(1, min(20, int(cm)))
        if cm <= 5:
            t = (cm - 1) / 4.0
            return self._lerp_rgb(light_blue, deep_blue, t)
        if cm <= 10:
            t = (cm - 5) / 5.0
            return self._lerp_rgb(deep_blue, purple, t)
        if cm <= 15:
            t = (cm - 10) / 5.0
            return self._lerp_rgb(purple, dark_red, t)
        t = (cm - 15) / 5.0
        return self._lerp_rgb(dark_red, bright_red, t)

    @staticmethod
    def _lerp_rgb(a, b, t):
        t = max(0.0, min(1.0, float(t)))
        return (int(a[0] + (b[0] - a[0]) * t),
                int(a[1] + (b[1] - a[1]) * t),
                int(a[2] + (b[2] - a[2]) * t))

    @staticmethod
    def _wheel(pos):
        # strandtest-like wheel (0..255) -> (r,g,b)
        pos = 255 - (pos & 255)
        if pos < 85:
            return (255 - pos * 3, 0, pos * 3)
        if pos < 170:
            pos -= 85
            return (0, pos * 3, 255 - pos * 3)
        pos -= 170
        return (pos * 3, 255 - pos * 3, 0)

# singleton
_leds = SnowLEDs()

# convenience wrappers for the rest of your app
def leds_set_snow(cm_now: int, cm_prev: int):
    _leds.set_snow_value(cm_now, cm_prev)

def leds_rainbow_splash(duration_sec=5.0):
    _leds.rainbow_fade_in(duration_sec)

def leds_clear():
    _leds.clear()

def leds_set_brightness(scale: float):
    _leds.set_global_brightness(scale)

# Apply persisted brightness level to LEDs on import
leds_set_brightness(getattr(brightness_state, "scale", 1.0))

# ----------------------------
# LED demo utilities (no network needed)
# ----------------------------
import sys, os

def leds_demo_sequence(values=None, hold_seconds=5):
    """
    Run a canned sequence of 'new snow' values so you can verify:
      - steady brightness when unchanged
      - breathing when value changes (speed scales with delta)
      - sparkles when >15 cm
      - rainbow splash at start
    """
    try:
        # 1) boot rainbow (shorter so you can iterate fast)
        leds_rainbow_splash(duration_sec=2.0)

        # 2) scripted values (includes repeats to show 'no-breath' steady state)
        if values is None:
            values = [0, 1, 3, 6, 10, 10, 12, 15, 16, 18, 20, 20, 5, 5, 0]

        prev = values[0]
        for cm in values:
            leds_set_snow(cm, prev)
            prev = cm
            time.sleep(hold_seconds)

        # 3) finish clean
        leds_clear()
    except KeyboardInterrupt:
        leds_clear()

def leds_demo_from_cli():
    """
    If you run: python3 snowgui.py --led-demo
    or set env SNOWGUI_LED_DEMO=1, we run the demo and exit.
    You can also pass your own CSV list: --led-demo "0,2,2,8,16,20,20,0"
    """
    argv = sys.argv[1:]
    run_demo = ("--led-demo" in argv) or (os.getenv("SNOWGUI_LED_DEMO") == "1")
    if not run_demo:
        return False

    # Optional custom list after the flag
    custom = None
    for i, tok in enumerate(argv):
        if tok == "--led-demo" and i + 1 < len(argv) and "," in argv[i + 1]:
            try:
                custom = [int(x.strip()) for x in argv[i + 1].split(",")]
            except Exception:
                custom = None
            break

    leds_demo_sequence(values=custom)
    return True


# ----------------------------
# Helpers
# ----------------------------
def _today_str():
    return datetime.datetime.now().strftime("%Y-%m-%d")


def _safe_int(val, default=0):
    """
    Convert strings like '12 cm' -> 12.
    On failure returns default.
    """
    try:
        if isinstance(val, int):
            return val
        if isinstance(val, float):
            return int(val)
        s = "".join(ch for ch in str(val) if ch.isdigit())
        return int(s) if s else default
    except Exception:
        return default


def _load_font(path="fonts/pixem.otf", size=18):
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        print(f"Ã¢Å¡Â Ã¯Â¸Â {path} not found. Using default font.")
        return ImageFont.load_default()


# ----------------------------
# Alarm config
# ----------------------------
def _default_alarm_cfg():
    return {
        "active": False,
        "active_anytime": False,
        "hour": "0",
        "minute": "0",
        "triggered_snow": "0",
        "incremental_snow": "0",
        "state": {"day": _today_str(), "triggered_today": False, "next_threshold": None},
    }


def load_alarm_cfg(force_reload: bool = False):
    """
    Lazily loads alarm.conf into memory and reuses the cached dict for future calls.
    Set force_reload=True to discard the cache and read from disk again.
    """
    global _alarm_cfg_cache
    with _alarm_cfg_lock:
        if _alarm_cfg_cache is not None and not force_reload:
            return _alarm_cfg_cache

        cfg = _default_alarm_cfg()
        try:
            if os.path.exists(ALARM_CONF_FILE):
                with open(ALARM_CONF_FILE, "r") as f:
                    disk = json.load(f)
                for k in ["active", "active_anytime", "hour", "minute", "triggered_snow", "incremental_snow"]:
                    if k in disk:
                        cfg[k] = disk[k]
                if isinstance(disk.get("state"), dict):
                    for k in ["day", "triggered_today", "next_threshold"]:
                        if k in disk["state"]:
                            cfg["state"][k] = disk["state"][k]
        except Exception as e:
            print(f"[Alarm] load_alarm_cfg error: {e}")

        _alarm_cfg_cache = cfg
        return _alarm_cfg_cache


def save_alarm_cfg(cfg):
    global _alarm_cfg_cache
    with _alarm_cfg_lock:
        try:
            _atomic_write_json(cfg, ALARM_CONF_FILE)
            _alarm_cfg_cache = cfg
            print("[Alarm] alarm.conf saved.")
        except Exception as e:
            print(f"[Alarm] save_alarm_cfg error: {e}")


def reset_state_if_new_day(cfg):
    today = _today_str()
    st = cfg["state"]
    if st.get("day") != today:
        st["day"] = today
        st["triggered_today"] = False
        base = max(0, int(cfg.get("triggered_snow") or "0"))
        st["next_threshold"] = base if cfg.get("active_anytime") else None
        save_alarm_cfg(cfg)


# ----------------------------
# Buzzer / Anthem (non-blocking)
# ----------------------------
try:
    import RPi.GPIO as GPIO  # Guarded import for dev machines
    _HAS_GPIO = True
except Exception:
    _HAS_GPIO = False

BUZZER_PIN = 18
NOTES = {
    "C4": 262,
    "E4": 330,
    "G4": 392,
    "B4": 494,
    "C5": 523,
    "REST": 0,
}

_CHORUS = [("G4", 0.18), ("E4", 0.18), ("C4", 0.18),
             ("G4", 0.18), ("E4", 0.18), ("C4", 0.18),
             ("C5", 0.18), ("B4", 0.18), ("G4", 0.45)]

_POWDER_DAY_ANTHEM = _CHORUS * 5

_pwm = None
_anthem_thread = None
_anthem_stop = threading.Event()
_anthem_lock = threading.Lock()


def _setup_buzzer():
    global _pwm
    if not _HAS_GPIO or _pwm is not None:
        return
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(BUZZER_PIN, GPIO.OUT)
    _pwm = GPIO.PWM(BUZZER_PIN, 440)


def _teardown_buzzer():
    global _pwm
    if not _HAS_GPIO:
        return
    try:
        if _pwm is not None:
            _pwm.stop()
        GPIO.cleanup()
    except Exception:
        pass
    _pwm = None


def _play_melody_blocking(melody, stop_event: threading.Event, pause_between_loops=6.0):
    if not _HAS_GPIO:
        while not stop_event.is_set():
            print("Ã°Å¸Å½Â¿ Powder Day Anthem (silent dev mode)")
            time.sleep(sum(d for _, d in melody) + pause_between_loops)
        return

    _setup_buzzer()
    while not stop_event.is_set():
        for note, dur in melody:
            if stop_event.is_set():
                break
            freq = NOTES.get(note, 0)
            if freq <= 0:
                _pwm.stop()
            else:
                _pwm.ChangeFrequency(freq)
                _pwm.start(50)  # duty
            time.sleep(dur)
        try:
            _pwm.stop()
        except Exception:
            pass
        # abortable pause
        for _ in range(int(pause_between_loops * 10)):
            if stop_event.is_set():
                break
            time.sleep(0.1)


def start_powder_day_anthem():
    global _anthem_thread
    with _anthem_lock:
        if _anthem_thread and _anthem_thread.is_alive():
            return
        _anthem_stop.clear()
        _anthem_thread = threading.Thread(
            target=_play_melody_blocking,
            args=(_POWDER_DAY_ANTHEM, _anthem_stop),
            daemon=True,
        )
        _anthem_thread.start()


def stop_powder_day_anthem():
    with _anthem_lock:
        _anthem_stop.set()
        if _anthem_thread and _anthem_thread.is_alive():
            _anthem_thread.join(timeout=2.0)


def check_and_trigger_alarm(current_snow_cm):
    """
    active: fire once at HH:MM if snow Ã¢â€°Â¥ trigger (once per day)
    active_anytime: fire at trigger and each +increment, resetting daily
    """
    cfg = load_alarm_cfg()
    reset_state_if_new_day(cfg)

    active = bool(cfg.get("active"))
    anytime = bool(cfg.get("active_anytime"))
    hr = int(cfg.get("hour") or 0)
    mn = int(cfg.get("minute") or 0)
    trig = max(0, int(cfg.get("triggered_snow") or 0))
    inc = max(0, int(cfg.get("incremental_snow") or 0))
    st = cfg["state"]

    now = datetime.datetime.now()
    matches_time = (now.hour == hr and now.minute == mn)

    # Mode 1: exact time, once/day
    if active and not anytime:
        if (not st["triggered_today"]) and matches_time and current_snow_cm >= trig:
            print(f"[Alarm] Timed trigger {hr:02d}:{mn:02d} | {current_snow_cm} Ã¢â€°Â¥ {trig}")
            start_powder_day_anthem()
            threading.Timer(sum(d for _, d in _POWDER_DAY_ANTHEM), stop_powder_day_anthem).start()
            st["triggered_today"] = True
            save_alarm_cfg(cfg)
            return True
        return False

    # Mode 2: anytime + increments
    if anytime:
        if st.get("next_threshold") is None:
            st["next_threshold"] = trig
        fired = False
        while inc > 0 and current_snow_cm >= int(st["next_threshold"]):
            print(f"[Alarm] Anytime trigger | {current_snow_cm} Ã¢â€°Â¥ {st['next_threshold']} (step {inc})")
            start_powder_day_anthem()
            threading.Timer(sum(d for _, d in _POWDER_DAY_ANTHEM), stop_powder_day_anthem).start()
            st["next_threshold"] = int(st["next_threshold"]) + inc
            fired = True
        if fired:
            save_alarm_cfg(cfg)
        return fired

    return False


# ----------------------------
# Update logic (GitHub)
# ----------------------------
def create_github_session():
    session = requests.Session()
    retry = Retry(total=MAX_RETRIES, backoff_factor=1, status_forcelist=[500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def get_local_version():
    try:
        if not os.path.exists(VERSION_FILE):
            return None
        with open(VERSION_FILE, "r") as f:
            version_str = f.read().strip()
            return version_str if version_str else None
    except Exception as e:
        print(f"Error reading local version: {e}")
        return None


def _draw_version_badge(img, version_text: str):
    """
    Paint the VERSION file contents onto the provided image (bottom-right corner).
    Operates in-place and returns the same image for chaining.
    """
    if not img or not version_text:
        return img

    try:
        draw = ImageDraw.Draw(img)
        font = _load_font(size=16)
        text = version_text.strip()
        pad = 8

        # Pillow 10 removed textsize; textbbox works across modern versions
        bbox = draw.textbbox((0, 0), text, font=font)
        w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]

        # Position badge bottom-right
        margin = 6
        radius = 6
        box_w = w + margin * 2
        box_h = h + margin * 2
        box_x = img.width - box_w - pad
        box_y = img.height - box_h - pad
        box = (box_x, box_y, box_x + box_w, box_y + box_h)

        try:
            draw.rounded_rectangle(box, radius=radius, fill="white")
        except Exception:
            draw.rectangle(box, fill="white")

        # Center text inside the badge
        text_x = box_x + (box_w - w) // 2
        text_y = box_y + (box_h - h) // 2
        text_y = text_y - 5
        draw.text((text_x, text_y), text, fill="black", font=font)
    except Exception as e:
        print(f"[Splash] Failed to render version badge: {e}")

    return img


def get_remote_version():
    repo_path = REPO_URL.replace("https://github.com/", "").replace(".git", "")
    api_url = f"https://api.github.com/repos/{repo_path}/releases/latest"

    headers = {"Accept": "application/vnd.github.v3+json"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"token {GITHUB_TOKEN}"

    session = create_github_session()
    try:
        response = session.get(api_url, timeout=10, headers=headers)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json().get("tag_name")
    except Exception as e:
        print(f"Error fetching remote version: {e}")
        return None
    finally:
        session.close()

def _ensure_git_safe_dir(repo_path):
    """
    Mark the given repository as 'safe' for Git if not already.
    This prevents 'dubious ownership' errors when running as another user.
    """
    try:
        # Check if Git already lists it as safe
        result = subprocess.run(
            ["git", "config", "--global", "--get-all", "safe.directory"],
            capture_output=True,
            text=True
        )
        if repo_path not in result.stdout:
            subprocess.run(
                ["git", "config", "--global", "--add", "safe.directory", repo_path],
                check=True
            )
            print(f"[Update] Added {repo_path} to git safe.directory.")
    except Exception as e:
        print(f"[Update] Could not mark repo safe: {e}")


def update(version_str: str) -> bool:
    """
    Systemd-aware update wrapper:
      - If systemd is present, launch a transient updater unit and return True
        if it was launched successfully (actual update runs in that unit).
      - Otherwise, run the inline git checkout.
    """
    if _is_systemd():
        return _systemd_run_update(version_str)
    return _update_inline_git_checkout(version_str)


def _ensure_heartbeat_symlink() -> bool:
    """
    Make sure the disk-based heartbeat path points at the RAM-backed file.
    Falls back quietly on errors; callers may still write the disk file directly.
    """
    try:
        # Remove incorrect targets so we can recreate the symlink.
        if os.path.islink(HEARTBEAT_FILE):
            target = os.readlink(HEARTBEAT_FILE)
            if target == HEARTBEAT_RAM_FILE:
                return True
            os.unlink(HEARTBEAT_FILE)
        elif os.path.exists(HEARTBEAT_FILE):
            os.remove(HEARTBEAT_FILE)

        os.symlink(HEARTBEAT_RAM_FILE, HEARTBEAT_FILE)
        return True
    except Exception as e:
        print(f"[Heartbeat] Symlink setup failed: {e}")
        return False


def heartbeat():
    while True:
        ts = str(time.time())

        # Primary write goes to RAM to spare the disk.
        try:
            with open(HEARTBEAT_RAM_FILE, "w") as f:
                f.write(ts)
        except Exception as e:
            print(f"[Heartbeat] Write to RAM file failed: {e}")

        # Ensure watchdog path continues to work.
        linked = _ensure_heartbeat_symlink()
        if not linked:
            try:
                with open(HEARTBEAT_FILE, "w") as f:
                    f.write(ts)
            except Exception as e:
                print(f"[Heartbeat] Fallback write failed: {e}")

        time.sleep(HEARTBEAT_INTERVAL)


# ----------------------------
# Display init (guarded)
# ----------------------------
class _DummyDevice:
    width = 320
    height = 240

    def display(self, img):
        # no-op on dev boxes
        pass


device = None  # global display handle


def init_display():
    global device
    try:
        serial = spi(port=0, device=0, gpio_DC=24, gpio_RST=25)
        device = ili9341(serial_interface=serial, width=320, height=240, rotate=0)
        return device
    except Exception as e:
        print(f"Ã¢Å¡Â Ã¯Â¸Â Display init failed ({e}); falling back to dummy device.")
        device = _DummyDevice()
        return device
    
display_lock = threading.RLock()

class _SafeOverlay:
    """
    Wraps the snowfall overlay so GUI keeps running if the overlay crashes.
    Lazily constructs the real overlay on first use to avoid startup breakage.
    """
    def __init__(self, factory):
        self._factory = factory
        self._overlay = None
        self._failed = False
        self._last_error = None

    def _fail(self, exc):
        self._failed = True
        self._last_error = exc
        print(f"[Overlay] Disabled after error: {exc}")

    def _ensure_overlay(self):
        if self._failed or self._overlay is not None:
            return
        try:
            self._overlay = self._factory()
        except Exception as e:
            self._fail(e)

    def _call(self, method, *args, **kwargs):
        if self._failed:
            return
        self._ensure_overlay()
        if not self._overlay:
            return
        try:
            fn = getattr(self._overlay, method, None)
            if fn:
                return fn(*args, **kwargs)
        except Exception as e:
            self._fail(e)

    def update_base(self, *args, **kwargs):
        return self._call("update_base", *args, **kwargs)

    def trigger(self, *args, **kwargs):
        return self._call("trigger", *args, **kwargs)

    def stop(self, *args, **kwargs):
        return self._call("stop", *args, **kwargs)

    def on_enter(self, *args, **kwargs):
        return self._call("on_enter", *args, **kwargs)

    def on_exit(self, *args, **kwargs):
        return self._call("on_exit", *args, **kwargs)

overlay = _SafeOverlay(lambda: SnowfallOverlay(get_size=lambda: (device.width, device.height)))

def _apply_dim_overlay(img, scale: float):
    """
    Software dimming for panels without hardware backlight control.
    Uses a simple brightness enhancer; scale=1 leaves image unchanged.
    """
    try:
        scale = float(scale)
    except Exception:
        return img
    if scale >= 0.999:
        return img
    scale = max(0.05, min(1.0, scale))
    try:
        return ImageEnhance.Brightness(img).enhance(scale)
    except Exception:
        # fallback to simple blend if enhancer is unavailable
        overlay_img = Image.new("RGB", img.size, (0, 0, 0))
        alpha = 1.0 - scale
        return Image.blend(img, overlay_img, alpha)

def present(img):
    global device
    with display_lock:
        if img.mode != "RGB":
            img = img.convert("RGB")
        try:
            dim_scale = getattr(brightness_state, "scale", 1.0)
        except Exception:
            dim_scale = 1.0
        img = _apply_dim_overlay(img, dim_scale)
        try:
            device.display(img)
        except Exception:
            logger.exception("Display update failed; falling back to dummy device.")
            try:
                device = _DummyDevice()
                device.display(img)
            except Exception:
                pass


def _read_selected_resort_index(path="conf/skihill.conf") -> int:
    try:
        with open(path, "r") as f:
            raw = f.read().strip()
        return max(0, int(raw))
    except Exception as e:
        print(f"[SelectResort] Could not read {path}: {e}. Using 0.")
        return 0


def _write_selected_resort_index(index: int, path="conf/skihill.conf") -> bool:
    """Clamp and persist the selected resort index."""
    names = get_resort_names()
    try:
        if names:
            index = max(0, min(index, len(names) - 1))
        else:
            index = 0
        _atomic_write_text(str(index), path)
        return True
    except Exception as e:
        print(f"[SelectResort] Failed to write {path}: {e}")
        return False


def get_resort_names(meta: Optional[dict] = None) -> List[str]:
    source = meta if isinstance(meta, dict) else _load_resort_meta()
    return list(source.keys()) if isinstance(source, dict) else []


def _read_selected_country(path=COUNTRY_CONF_FILE, default=ALL_COUNTRIES_LABEL) -> str:
    try:
        with open(path, "r") as f:
            raw = f.read().strip()
        return raw or default
    except Exception as e:
        print(f"[SelectCountry] Could not read {path}: {e}. Using {default}.")
        return default


def _write_selected_country(country: str, path=COUNTRY_CONF_FILE) -> bool:
    try:
        country = (country or "").strip() or ALL_COUNTRIES_LABEL
        _atomic_write_text(country, path)
        return True
    except Exception as e:
        print(f"[SelectCountry] Failed to write {path}: {e}")
        return False


def _read_selected_region(path=REGION_CONF_FILE, default=ALL_REGIONS_LABEL) -> str:
    try:
        with open(path, "r") as f:
            raw = f.read().strip()
        selected = raw or default
        if selected.casefold() == ALL_RESORTS_LABEL.casefold():
            selected = ALL_REGIONS_LABEL
        return selected
    except Exception as e:
        print(f"[SelectRegion] Could not read {path}: {e}. Using {default}.")
        return default


def _write_selected_region(region: str, path=REGION_CONF_FILE) -> bool:
    try:
        region = (region or "").strip() or ALL_REGIONS_LABEL
        _atomic_write_text(region, path)
        return True
    except Exception as e:
        print(f"[SelectRegion] Failed to write {path}: {e}")
        return False


def get_countries(meta: dict) -> List[str]:
    if not isinstance(meta, dict):
        meta = {}
    country_map = {}
    has_country = False
    has_other = False

    for name in get_resort_names(meta):
        info = meta.get(name)
        country = info.get("country") if isinstance(info, dict) else None
        country = str(country).strip() if country is not None else ""
        if country:
            has_country = True
            key = country.casefold()
            if key not in country_map:
                country_map[key] = country
        else:
            has_other = True

    if not has_country:
        return [ALL_COUNTRIES_LABEL]
    if has_other and OTHER_COUNTRY_LABEL.casefold() not in country_map:
        country_map[OTHER_COUNTRY_LABEL.casefold()] = OTHER_COUNTRY_LABEL

    countries = sorted(country_map.values(), key=lambda s: s.casefold())
    return [ALL_COUNTRIES_LABEL] + countries


def get_regions(meta: dict, selected_country: str = ALL_COUNTRIES_LABEL) -> List[str]:
    if not isinstance(meta, dict):
        meta = {}

    selected_country_key = (selected_country or "").strip().casefold()
    all_countries = (not selected_country_key) or (selected_country_key == ALL_COUNTRIES_LABEL.casefold())

    region_map = {}
    has_region = False
    has_other = False

    for name in get_resort_names(meta):
        info = meta.get(name)
        if not isinstance(info, dict):
            continue

        country = str(info.get("country") or "").strip()
        if not all_countries:
            if selected_country_key == OTHER_COUNTRY_LABEL.casefold():
                if country:
                    continue
            elif country.casefold() != selected_country_key:
                continue

        region = str(info.get("region") or "").strip()
        if region:
            has_region = True
            key = region.casefold()
            if key not in region_map:
                region_map[key] = region
        else:
            has_other = True

    if not has_region:
        return [ALL_REGIONS_LABEL]
    if has_other and OTHER_REGION_LABEL.casefold() not in region_map:
        region_map[OTHER_REGION_LABEL.casefold()] = OTHER_REGION_LABEL

    regions = sorted(region_map.values(), key=lambda s: s.casefold())
    return [ALL_REGIONS_LABEL] + regions


def get_active_resorts(selected_country: str, selected_region: str, meta: dict) -> List[str]:
    names = get_resort_names(meta)
    if not names:
        return []
    if not isinstance(meta, dict):
        meta = {}

    selected_country_key = (selected_country or "").strip().casefold()
    selected_region_key = (selected_region or "").strip().casefold()

    all_countries = (not selected_country_key) or (selected_country_key == ALL_COUNTRIES_LABEL.casefold())
    all_regions = (
        (not selected_region_key)
        or (selected_region_key == ALL_REGIONS_LABEL.casefold())
        or (selected_region_key == ALL_RESORTS_LABEL.casefold())
    )

    results = []
    for name in names:
        info = meta.get(name)
        if not isinstance(info, dict):
            continue

        country = str(info.get("country") or "").strip()
        region = str(info.get("region") or "").strip()

        if not all_countries:
            if selected_country_key == OTHER_COUNTRY_LABEL.casefold():
                if country:
                    continue
            elif country.casefold() != selected_country_key:
                continue

        if not all_regions:
            if selected_region_key == OTHER_REGION_LABEL.casefold():
                if region:
                    continue
            elif region.casefold() != selected_region_key:
                continue

        results.append(name)

    if not results:
        return sorted(names, key=lambda s: s.casefold())
    return sorted(results, key=lambda s: s.casefold())


def current_resort_name() -> str:
    names = get_resort_names()
    if not names:
        return "Resort"
    idx = max(0, min(_read_selected_resort_index(), len(names) - 1))
    return names[idx]


def set_current_resort_by_name(name: str) -> None:
    names = get_resort_names()
    # Persist the global metadata-derived index, not a filtered local index.
    try:
        idx = names.index(name)
    except ValueError:
        print(f"[SelectResort] Unknown resort name '{name}'; keeping existing selection.")
        return
    _write_selected_resort_index(idx)


def cycle_resort_in_active_region(direction: int, meta: Optional[dict] = None) -> bool:
    if direction == 0:
        return False
    meta = meta if meta is not None else _load_resort_meta()
    country = _read_selected_country()
    region = _read_selected_region()
    active = get_active_resorts(country, region, meta)
    if not active:
        return False
    cur_name = current_resort_name()
    if cur_name not in active:
        set_current_resort_by_name(active[0])
        cur_name = active[0]
    idx = active.index(cur_name)
    next_name = active[(idx + direction) % len(active)]
    set_current_resort_by_name(next_name)
    return True


def _resort_slug(name: str) -> str:
    """Convert a resort name to the JSON filename used on the VPS."""
    slug = (name or "").strip()
    slug = slug.replace("'", "").replace("-", "_").replace(" ", "_")
    slug = re.sub(r"_+", "_", slug)
    return slug or "Unknown"


def _load_resort_json(name: str) -> dict:
    """
    Fetch the resort JSON payload from the VPS (with local fallback).
    Returns {} on failure.
    """
    slug = _resort_slug(name)
    base_url = os.getenv("SNOWPLOW_JSON_BASE", "http://vps.snowscraper.ca/json").rstrip("/")
    json_url = f"{base_url}/{slug}.json"
    data = {}

    try:
        resp = requests.get(json_url, timeout=10, headers={"User-Agent": "SnowGUI/2.3.0"})
        resp.raise_for_status()
        data = resp.json() if resp.content else {}
    except Exception as e_http:
        print(f"[{name}] HTTP JSON fetch failed ({e_http}); trying local fallback.")

    if not data:
        try:
            local_dir = os.getenv("SNOWPLOW_JSON_DIR", "/opt/snowplow/data/json")
            local_path = os.path.join(local_dir, f"{slug}.json")
            if os.path.exists(local_path):
                with open(local_path, "r") as f:
                    data = json.load(f)
            else:
                print(f"[{name}] Local JSON not found at {local_path}")
        except Exception as e_file:
            print(f"[{name}] Failed to read local JSON: {e_file}")

    return data if isinstance(data, dict) else {}

def _coerce_float(val, default=None):
    try:
        return float(val)
    except Exception:
        return default if default is not None else val


def _parse_simple_yaml(text: str):
    """
    Minimal YAML-ish parser for resort metadata.
    Supports either a top-level list of maps or a map of maps.
    """
    map_data = {}
    list_data = []
    current_map = None
    current_item = None
    in_list = False

    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        if stripped.startswith("- "):
            in_list = True
            if current_item is not None:
                list_data.append(current_item)
            current_item = {}
            item_text = stripped[2:].strip()
            if item_text and ":" in item_text:
                sub_key, sub_val = item_text.split(":", 1)
                sub_key = sub_key.strip()
                sub_val = sub_val.strip().strip('"').strip("'")
                if sub_key:
                    current_item[sub_key] = _coerce_float(sub_val, sub_val)
            continue

        if line.startswith(" ") and ":" in stripped:
            sub_key, sub_val = stripped.split(":", 1)
            sub_key = sub_key.strip()
            sub_val = sub_val.strip().strip('"').strip("'")
            if not sub_key:
                continue
            target = current_item if in_list else current_map
            if isinstance(target, dict):
                target[sub_key] = _coerce_float(sub_val, sub_val)
            continue

        if ":" not in stripped:
            continue

        in_list = False
        key, val = stripped.split(":", 1)
        key = key.strip()
        val = val.strip()
        if not key:
            continue
        if val:
            cleaned = val.strip('"').strip("'")
            map_data[key] = _coerce_float(cleaned, cleaned)
            current_map = None
        else:
            current_map = map_data.setdefault(key, {})

    if current_item is not None:
        list_data.append(current_item)

    if list_data and not map_data:
        return list_data
    if map_data and not list_data:
        return map_data
    if list_data:
        map_data["resorts"] = list_data
    return map_data


def _normalize_resort_meta(raw) -> dict:
    normalized = {}

    def add_entry(name, info):
        key = str(name or "").strip()
        if not key:
            return

        entry = {}
        if isinstance(info, dict):
            entry.update(info)
        entry["name"] = key

        for field in ("slug", "region", "country"):
            if field in entry and entry[field] is not None:
                text_val = str(entry[field]).strip()
                if text_val:
                    entry[field] = text_val
                else:
                    entry.pop(field, None)

        for lat_key in ("lat", "latitude", "y"):
            if lat_key in entry:
                entry[lat_key] = _coerce_float(entry[lat_key], entry[lat_key])
        for lon_key in ("lon", "long", "lng", "longitude", "x"):
            if lon_key in entry:
                entry[lon_key] = _coerce_float(entry[lon_key], entry[lon_key])

        normalized[key] = entry

    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            add_entry(item.get("name"), item)
        return normalized

    if isinstance(raw, dict):
        list_candidates = raw.get("resorts")
        if isinstance(list_candidates, list):
            for item in list_candidates:
                if not isinstance(item, dict):
                    continue
                add_entry(item.get("name"), item)
            return normalized

        for key, val in raw.items():
            if not isinstance(val, dict):
                continue
            add_entry(val.get("name") or key, val)

    return normalized


@lru_cache(maxsize=1)
def _load_resort_meta(path=RESORT_META_FILE) -> dict:
    """
    Load resort metadata from YAML (or JSON) into a name -> info map.
    Safe to call repeatedly; cache keeps disk IO low.
    """
    if not os.path.exists(path):
        print(f"[Avy] resorts_meta.yaml not found at {path}")
        return {}
    try:
        if yaml:
            with open(path, "r") as f:
                data = yaml.safe_load(f) or {}
        else:
            with open(path, "r") as f:
                raw = f.read()
            try:
                data = json.loads(raw)
            except Exception:
                data = _parse_simple_yaml(raw)
        return _normalize_resort_meta(data)
    except Exception as e:
        print(f"[Avy] Failed to load {path}: {e}")
        return {}


def _get_resort_point(name: str):
    meta = _load_resort_meta().get(name) or {}
    lat = meta.get("lat") or meta.get("latitude") or meta.get("y")
    lon = meta.get("lon") or meta.get("long") or meta.get("lng") or meta.get("longitude") or meta.get("x")
    if lat is None or lon is None:
        return None
    try:
        return float(lat), float(lon)
    except Exception:
        return None


def _extract_danger(payload: dict) -> dict:
    """
    Prefer the /report/dangerRatings structure from avytest.py; fall back to older shapes.
    """
    danger = {"alpine": "N/A", "treeline": "N/A", "below_treeline": "N/A"}

    # Newer AvCan structure: report.dangerRatings is a list of days.
    report = (payload or {}).get("report") or {}
    dr_list = report.get("dangerRatings")
    if isinstance(dr_list, list) and dr_list:
        today = dr_list[0] if isinstance(dr_list[0], dict) else {}
        ratings = today.get("ratings") or {}

        def nice(zone_key):
            zone = ratings.get(zone_key) or {}
            rating = zone.get("rating") or {}
            return rating.get("display") or rating.get("value")

        a = nice("alp")
        t = nice("tln")
        b = nice("btl")
        if a:
            danger["alpine"] = a
        if t:
            danger["treeline"] = t
        if b:
            danger["below_treeline"] = b
        return danger

    # Legacy shapes (dict / list of dicts)
    dr = payload.get("dangerRatings") or payload.get("danger") or payload.get("ratings")
    if isinstance(dr, dict):
        def pick(v):
            if isinstance(v, dict):
                return v.get("rating") or v.get("value") or v.get("label") or str(v)
            return v

        a = dr.get("alpine") or dr.get("Alpine")
        t = dr.get("treeline") or dr.get("Treeline")
        b = dr.get("below_treeline") or dr.get("belowTreeline") or dr.get("Below Treeline")

        if a:
            danger["alpine"] = str(pick(a))
        if t:
            danger["treeline"] = str(pick(t))
        if b:
            danger["below_treeline"] = str(pick(b))

    elif isinstance(dr, list):
        for entry in dr:
            if not isinstance(entry, dict):
                continue
            elev = (entry.get("elevation") or "").lower()
            rating = entry.get("rating") or entry.get("value") or entry.get("label") or ""
            if not rating:
                continue
            if "alpine" in elev:
                danger["alpine"] = rating
            elif "tree" in elev:
                danger["treeline"] = rating
            elif "below" in elev:
                danger["below_treeline"] = rating
    return danger


def _extract_summary(payload: dict) -> str:
    # Prefer report.highlights (HTML-ish), but fall back to older keys.
    report = (payload or {}).get("report") or {}
    highlights = report.get("highlights")
    if isinstance(highlights, str) and highlights.strip():
        try:
            return re.sub(r"<[^>]+>", " ", highlights).strip()
        except Exception:
            return highlights.strip()

    for key in ("summary", "bottomLine", "highlights", "conditionsSummary", "shortText", "outlook"):
        val = payload.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
        forecast = payload.get("forecast")
        if isinstance(forecast, dict):
            inner = forecast.get(key)
            if isinstance(inner, str) and inner.strip():
                return inner.strip()
    return ""


def _extract_issue(payload: dict) -> str:
    report = (payload or {}).get("report") or {}
    for container in (report, payload):
        for key in ("dateIssued", "publishedAt", "issueDate", "createdAt", "validUntil"):
            val = container.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    return ""


def _parse_iso_dt(dt_str):
    if not dt_str:
        return None
    s = str(dt_str).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.datetime.fromisoformat(s)
    except Exception:
        return None


# --- Avy mask assets (colored overlays for alpine/treeline/below TL) ---
_AVY_ASSETS = None


def _load_avy_mask_assets():
    """
    Load background + soft alpha masks once (cached).
    Masks are blurred slightly to avoid jagged edges.
    """
    global _AVY_ASSETS
    if _AVY_ASSETS:
        return _AVY_ASSETS

    base_dir = Path(__file__).resolve().parent / "images"
    def _open_rgba(path, fallback_color=(12, 16, 26, 255)):
        try:
            img = Image.open(path).convert("RGBA").resize((device.width, device.height))
            return img
        except FileNotFoundError:
            print(f"[AvyMask] Missing {path}, using solid fallback.")
            return Image.new("RGBA", (device.width, device.height), fallback_color)
        except Exception as e:
            print(f"[AvyMask] Failed to load {path}: {e}")
            return Image.new("RGBA", (device.width, device.height), fallback_color)

    bg_path = base_dir / "aconditions.png"
    background = _open_rgba(bg_path)

    mask_files = ["topavymask.png", "midavymask.png", "botavymask.png"]
    soft_alphas = []
    for fname in mask_files:
        path = base_dir / fname
        try:
            mask = Image.open(path).convert("L").resize((device.width, device.height))
            # normalize border to black to avoid bleed
            draw = ImageDraw.Draw(mask)
            draw.rectangle((0, 0, mask.width - 1, mask.height - 1), outline=0, width=2)
            alpha = mask.point(lambda p: 0 if p > 250 else 255, "L")
            alpha = alpha.filter(ImageFilter.GaussianBlur(radius=1))
            soft_alphas.append(alpha)
        except FileNotFoundError:
            print(f"[AvyMask] Missing {fname}; mask will be empty.")
            soft_alphas.append(Image.new("L", (device.width, device.height), 0))
        except Exception as e:
            print(f"[AvyMask] Failed to load {fname}: {e}")
            soft_alphas.append(Image.new("L", (device.width, device.height), 0))

    _AVY_ASSETS = {"background": background, "masks": soft_alphas}
    return _AVY_ASSETS


def _avy_color_for_rating(val: str):
    """
    Map danger rating string to RGBA fill.
    Red = High/Considerable/Extreme, Yellow = Moderate, Green = Low.
    """
    r = (val or "").lower()
    if not r or r == "n/a":
        return (120, 130, 150, 120)
    if "low" in r or r.startswith("1"):
        return (3, 109, 9, 180)
    if "moderate" in r or "mod" in r or r.startswith("2"):
        return (240, 178, 0, 190)
    return (209, 9, 6, 190)


def _fetch_point_forecast(lat: float, lon: float) -> dict:
    """
    Behaves like avytest.py's render_summary pipeline:
    - Call point endpoint
    - Normalize list/dict response
    - Prefer report.* fields for title/highlights/danger ratings
    """
    params = {"lat": f"{lat:.6f}", "long": f"{lon:.6f}"}
    try:
        resp = requests.get(AVY_POINT_URL, params=params, headers=AVY_HEADERS, timeout=12)
    except Exception as e:
        raise RuntimeError(f"Forecast fetch failed: {e}")

    if resp.status_code != 200:
        raise RuntimeError(f"avalanche.ca returned HTTP {resp.status_code}: {resp.text[:200]}")

    try:
        payload = resp.json() if resp.content else {}
    except Exception as e:
        raise RuntimeError(f"Failed to parse avalanche.ca JSON: {e}")

    # Normalize list/dict (API sometimes wraps in a list)
    if isinstance(payload, list):
        if not payload:
            raise RuntimeError("No forecast returned for this point.")
        product = payload[0]
    elif isinstance(payload, dict):
        product = payload
    else:
        raise RuntimeError("Unexpected response from avalanche.ca")

    if not isinstance(product, dict):
        raise RuntimeError("Unexpected forecast payload shape.")

    report = product.get("report") or {}
    area = product.get("area") or {}

    issued_raw = report.get("dateIssued") or product.get("dateIssued") or _extract_issue(product)
    issued_dt = _parse_iso_dt(issued_raw)
    issued_fmt = issued_dt.strftime("%b %d %H:%M %Z") if issued_dt else issued_raw

    return {
        "title": report.get("title") or product.get("title") or product.get("name") or "Avalanche Forecast",
        "region": area.get("name") or product.get("areaName") or product.get("region") or product.get("area"),
        "danger": _extract_danger(product),
        "summary": _extract_summary(product) or "No summary text available.",
        "issued": issued_fmt or "",
    }


# ----------------------------
# skiHill scraper
# ----------------------------

def log_snow_data(hill):
    """
    Writes current reading and keeps a history of daily readings for each mountain.
    Structure:
    {
        "Sun Peaks": {
            "current": {"date": "YYYY-MM-DD", "newSnow": int, "weekSnow": int, "baseSnow": int},
            "history": [
                {"date": "YYYY-MM-DD", "newSnow": int, "weekSnow": int, "baseSnow": int},
                ...
            ]
        },
        ...
    }
    """
    today = _today_str()
    log_data = {}

    # Load existing log if present
    if os.path.exists(SNOW_LOG_FILE):
        try:
            with open(SNOW_LOG_FILE, "r") as f:
                log_data = json.load(f)
        except Exception as e:
            print(f"[SnowLog] Error reading log: {e}")

    # Ensure mountain entry exists
    if hill.name not in log_data:
        log_data[hill.name] = {"current": {}, "history": []}

    # Create current reading
    current_reading = {
        "date": today,
        "newSnow": int(hill.newSnow),
        "weekSnow": int(hill.weekSnow),
        "baseSnow": int(hill.baseSnow)
    }

    # Update current
    log_data[hill.name]["current"] = current_reading

    # Only add to history if it's a new day or different from last history entry
    history = log_data[hill.name]["history"]
    if not history or history[-1]["date"] != today:
        history.append(current_reading)
        # Optional: limit history length (e.g., last 365 days)
        history = history[-365:]
        log_data[hill.name]["history"] = history

    # Save log
    try:
        _atomic_write_json(log_data, SNOW_LOG_FILE, indent=2)
        print(f"[SnowLog] Logged data for {hill.name}")
    except Exception as e:
        print(f"[SnowLog] Error writing log: {e}")

class skiHill:
    def __init__(self, name, url, newSnow, weekSnow, baseSnow):
        self.name = name
        self.url = url
        self.newSnow = newSnow
        self.weekSnow = weekSnow
        self.baseSnow = baseSnow

    def getSnow(self):
        if DEV_MODE:
            print("[DEV] Skipping live fetch; using stub values.")
            self.newSnow = 1
            self.weekSnow = 3
            self.baseSnow = 120
            return
        print(f"[getSnow] {self.name}")
        data = _load_resort_json(self.name)
        cur = data.get("current") or {}
        self.newSnow = _safe_int(cur.get("newSnow", 0))
        self.weekSnow = _safe_int(cur.get("weekSnow", 0))
        self.baseSnow = _safe_int(cur.get("baseSnow", 0))
        log_snow_data(self)


def create_selected_hill():
    # Keep skihill.conf index mapped to metadata-derived resort ordering.
    name = current_resort_name()
    return skiHill(name=name, url="", newSnow=0, weekSnow=0, baseSnow=0)

def reload_hill():
    """Refresh the global hill from skihill.conf."""
    global hill
    hill = create_selected_hill()
    print(f"[Hill] Reloaded: {hill.name}")
    return hill


# ----------------------------
# WiÃ¢â‚¬â€˜Fi helpers
# ----------------------------
def get_available_ssids():
    try:
        result = subprocess.run(
            ["sudo", "iwlist", "wlan0", "scan"],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,  # cap scan duration to avoid hanging
        )
        ssids = []
        seen = set()
        for line in result.stdout.splitlines():
            line = line.strip()
            if line.startswith("ESSID:"):
                ssid = line.split(":", 1)[1].strip().strip('"')
                if ssid and ssid not in seen:  # skip hidden and duplicates
                    ssids.append(ssid)
                    seen.add(ssid)
        return ssids
    except subprocess.TimeoutExpired:
        print("[WiFi] iwlist scan timed out after 30s")
        return []
    except subprocess.CalledProcessError as e:
        print(f"Error running iwlist: {e}")
        return []


def reconfigure_wifi():
    try:
        result = subprocess.run(["wpa_cli", "-i", "wlan0", "reconfigure"])
        if result.returncode == 0:
            print("[WiFi] wpa_cli reconfigure succeeded!")
        else:
            print("[WiFi] wpa_cli reconfigure failed!")
    except Exception as e:
        print(f"[WiFi] Error running wpa_cli: {e}")


# ----------------------------
# Touch Controller & Calibration
# ----------------------------
class XPT2046:
    """
    Raw touch reader. Reads 12-bit coordinates from XPT2046 on SPI0.<device>.
    """
    def __init__(self, spi_bus=0, spi_device=1, max_speed=400_000, penirq_gpio=None):
        self.spi = spidev.SpiDev()
        self.spi.open(spi_bus, spi_device)     # set spi_device=0 if T_CS is on CE0
        self.spi.max_speed_hz = max_speed      # 200Ã¢â‚¬â€œ400 kHz is robust
        self.spi.mode = 0b00

        self.penirq_gpio = penirq_gpio
        if _HAS_GPIO and self.penirq_gpio is not None:
            GPIO.setwarnings(False)
            GPIO.setmode(GPIO.BCM)
            GPIO.setup(self.penirq_gpio, GPIO.IN, pull_up_down=GPIO.PUD_UP)  # PENIRQ active-low

    def _read12(self, cmd):
        # Throw-away read to let ADC settle, then real read
        self.spi.xfer2([cmd, 0x00, 0x00])
        r = self.spi.xfer2([cmd, 0x00, 0x00])
        return ((r[1] << 8) | r[2]) >> 4

    def _pressed(self):
        if not (_HAS_GPIO and self.penirq_gpio is not None):
            return True  # fail-open if no IRQ wire yet
        return GPIO.input(self.penirq_gpio) == 0

    def read_touch(self, samples=5, tolerance=50):
        if not self._pressed():
            return None
        readings = []
        for _ in range(samples):
            raw_y = self._read12(0xD0)  # Y
            raw_x = self._read12(0x90)  # X
            if 100 < raw_x < 4000 and 100 < raw_y < 4000:
                readings.append((raw_x, raw_y))
            time.sleep(0.005)

        if len(readings) < 3:
            return None
        xs, ys = zip(*readings)
        if max(xs) - min(xs) > tolerance or max(ys) - min(ys) > tolerance:
            return None
        return (sum(xs)//len(xs), sum(ys)//len(ys))

    def close(self):
        try:
            self.spi.close()
        except Exception:
            pass


class TouchCalibrator:
    def __init__(self):
        self.x_min = 0
        self.x_max = 4095
        self.y_min = 0
        self.y_max = 4095

    def map_raw_to_screen(self, x, y):
        # Avoid divide by zero on bad calibration files
        dx = max(1, (self.x_max - self.x_min))
        dy = max(1, (self.y_max - self.y_min))
        sx = int((x - self.x_min) * device.width / dx)
        sy = int((y - self.y_min) * device.height / dy)
        sx = device.width - 1 - sx
        sy = device.height - 1 - sy
        return (max(0, min(device.width - 1, sx)), max(0, min(device.height - 1, sy)))

    def load(self):
        if not os.path.exists(CALIBRATION_FILE):
            return False
        with open(CALIBRATION_FILE, "r") as f:
            data = json.load(f)
        self.x_min = int(data.get("x_min", 0))
        self.x_max = int(data.get("x_max", 4095))
        self.y_min = int(data.get("y_min", 0))
        self.y_max = int(data.get("y_max", 4095))
        # Sanity check
        if self.x_max <= self.x_min or self.y_max <= self.y_min:
            print("Ã¢Å¡Â Ã¯Â¸Â Calibration file invalid; resetting to defaults.")
            self.x_min, self.y_min, self.x_max, self.y_max = 0, 0, 4095, 4095
            return False
        return True

    # ---- Calibration helpers ----
    def reset_defaults(self):
        self.x_min, self.y_min, self.x_max, self.y_max = 0, 0, 4095, 4095

    def load_safe(self):
        if not os.path.exists(CALIBRATION_FILE):
            print("[Calib] No calibration file found.")
            self.reset_defaults()
            return False
        try:
            with open(CALIBRATION_FILE, "r") as f:
                data = json.load(f)
            self.x_min = int(data.get("x_min", 0))
            self.x_max = int(data.get("x_max", 4095))
            self.y_min = int(data.get("y_min", 0))
            self.y_max = int(data.get("y_max", 4095))
            if self.x_max <= self.x_min or self.y_max <= self.y_min:
                print("[Calib] Calibration file invalid; resetting to defaults.")
                self.reset_defaults()
                return False
            return True
        except Exception as e:
            print(f"[Calib] Failed to read calibration file: {e}")
            self.reset_defaults()
            return False

    def save_safe(self):
        try:
            os.makedirs(os.path.dirname(CALIBRATION_FILE), exist_ok=True)
            with open(CALIBRATION_FILE, "w") as f:
                json.dump(
                    {
                        "x_min": self.x_min,
                        "x_max": self.x_max,
                        "y_min": self.y_min,
                        "y_max": self.y_max,
                    },
                    f,
                    indent=2,
                )
            print(f"[Calib] Saved to {CALIBRATION_FILE}")
            return True
        except Exception as e:
            print(f"[Calib] Failed to save calibration: {e}")
            return False


# ---- On-device calibration workflow ----
def _draw_calibration_target(label: str, pos_xy):
    """Render a simple crosshair target on screen."""
    img = Image.new("RGB", (device.width, device.height), "black")
    draw = ImageDraw.Draw(img)
    cx, cy = pos_xy
    size = 12
    draw.line((cx - size, cy, cx + size, cy), fill="white", width=2)
    draw.line((cx, cy - size, cx, cy + size), fill="white", width=2)
    draw.ellipse((cx - 4, cy - 4, cx + 4, cy + 4), fill="cyan")
    font = _load_font(size=12)
    draw.text((10, 10), f"Tap the crosshair ({label})", fill="white", font=font)
    present(img)


def run_touch_calibration(calibrator: TouchCalibrator, touch: XPT2046, timeout_sec=8.0) -> bool:
    """
    Interactive 4-point calibration. Returns True on success.
    Fails soft (defaults remain) on timeout or hardware errors.
    """
    if touch is None:
        print("[Calib] Touch device not available; skipping calibration.")
        return False

    targets = [
        ("top-left", (12, 12)),
        ("top-right", (device.width - 12, 12)),
        ("bottom-left", (12, device.height - 12)),
        ("bottom-right", (device.width - 12, device.height - 12)),
    ]

    def _wait_for_release(release_timeout=2.0):
        """Wait briefly for finger to lift to avoid reusing same touch."""
        t0 = time.time()
        while time.time() - t0 < release_timeout:
            try:
                if touch.read_touch(samples=3, tolerance=60) is None:
                    return True
            except Exception:
                return True
            time.sleep(0.05)
        return False

    def _dist2(a, b):
        dx = a[0] - b[0]
        dy = a[1] - b[1]
        return dx * dx + dy * dy

    samples = []
    for label, pos in targets:
        _draw_calibration_target(label, pos)
        sample = None
        t0 = time.time()
        last_sample = samples[-1] if samples else None
        while time.time() - t0 < timeout_sec:
            try:
                coord = touch.read_touch(samples=8, tolerance=80)
            except Exception as e:
                print(f"[Calib] Touch read error during {label}: {e}")
                coord = None
            if coord:
                # If we still see the previous point (finger not lifted), wait
                if last_sample and _dist2(coord, last_sample) < 1600:  # ~40 raw units
                    time.sleep(0.05)
                    continue
                sample = coord
                break
            time.sleep(0.05)
        if not sample:
            print(f"[Calib] Timed out waiting for tap at {label}.")
            show_popup_message("Calibration failed", duration=1.5)
            return False
        samples.append(sample)
        _wait_for_release()

    xs = [p[0] for p in samples]
    ys = [p[1] for p in samples]
    try:
        calibrator.x_min = max(0, min(xs))
        calibrator.x_max = max(xs)
        calibrator.y_min = max(0, min(ys))
        calibrator.y_max = max(ys)
        # sanity: ensure spans are reasonable
        if calibrator.x_max - calibrator.x_min < 200 or calibrator.y_max - calibrator.y_min < 200:
            print("[Calib] Computed span too small; keeping defaults.")
            calibrator.reset_defaults()
            return False
        calibrator.save_safe()
        show_popup_message("Calibration saved", duration=1.5)
        return True
    except Exception as e:
        print(f"[Calib] Failed to finalize calibration: {e}")
        calibrator.reset_defaults()
        return False


# ----------------------------
# UI Widgets
# ----------------------------
class Button:
    def __init__(self, x1, y1, x2, y2, label, callback, visible=False):
        self.x1, self.y1 = x1, y1
        self.x2, self.y2 = x2, y2
        self.label = label
        self.callback = callback
        self.visible = visible

    def contains(self, x, y):
        return self.x1 <= x <= self.x2 and self.y1 <= y <= self.y2

    def draw(self, draw_obj):
        if not self.visible:
            return
        draw_obj.rectangle([self.x1, self.y1, self.x2, self.y2], outline="white", fill="gray")
        font = ImageFont.load_default()
        draw_obj.text((self.x1 + 5, self.y1 + 5), self.label, fill="black", font=font)

    def on_press(self):
        print(f"[BUTTON] {self.label}")
        self.callback()

def show_popup_message(text, duration=3):
    """
    Draws a centered popup dialog with the given text for <duration> seconds.
    Compatible with newer Pillow (no .textsize()).
    """
    img = Image.new("RGB", (device.width, device.height), (0, 0, 0))
    draw = ImageDraw.Draw(img)
    font = _load_font(size=16)

    # translucent backdrop
    draw.rectangle(
        (40, 90, device.width - 40, 150),
        fill=(0, 0, 0),
        outline="white",
        width=2,
    )

    # get text dimensions (Pillow 10+)
    bbox = draw.textbbox((0, 0), text, font=font)
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]

    # center text
    x = (device.width - w) // 2
    y = (device.height - h) // 2
    draw.text((x, y), text, fill="white", font=font)

    present(img)
    time.sleep(duration)


class Screen:
    def __init__(self):
        self.buttons = []

    def add_button(self, button):
        self.buttons.append(button)

    def draw(self, draw_obj):
        for btn in self.buttons:
            btn.draw(draw_obj)

    def handle_touch(self, x, y):
        for btn in self.buttons:
            if btn.contains(x, y):
                try:
                    btn.on_press()
                except Exception:
                    logger.exception(
                        "Button callback failed (%s) at (%s,%s)",
                        getattr(btn, "label", "?"),
                        x,
                        y,
                    )


class KeyboardScreen(Screen):
    def __init__(self, prompt, on_submit, screen_manager):
        super().__init__()
        self.prompt = prompt
        self.on_submit = on_submit
        self.screen_manager = screen_manager
        self.input_text = ""
        self.mode = "letters"  # or 'symbols'
        self.shift = False
        self._build_keys()

    def _build_keys(self):
        self.buttons.clear()

        if self.mode == "letters":
            rows = [list("QWERTYUIOP"), list("ASDFGHJKL"), list("ZXCVBNM")]
        else:
            rows = [list("1234567890"), list("!@#$%^&*()"), list("-_=+.,?/")]

        x_start = 10
        y_start = 60
        key_w = 28
        key_h = 28
        spacing = 4

        for row_index, row in enumerate(rows):
            for col_index, char in enumerate(row):
                label = char.upper() if self.shift else char.lower()
                x = x_start + col_index * (key_w + spacing)
                y = y_start + row_index * (key_h + spacing)
                char_label = label
                self.add_button(
                    Button(x, y, x + key_w, y + key_h, char_label, lambda c=char_label: self._append_char(c), visible=True)
                )

        toggle_label = "[123]" if self.mode == "letters" else "[ABC]"
        self.add_button(Button(10, 160, 65, 190, toggle_label, self._toggle_mode, visible=True))

        shift_label = "[CAP]" if not self.shift else "[LWR]"
        self.add_button(Button(70, 160, 125, 190, shift_label, self._toggle_shift, visible=True))

        self.add_button(Button(130, 160, 220, 190, "Space", lambda: self._append_char(" "), visible=True))
        self.add_button(Button(225, 160, 270, 190, "DEL", self._backspace, visible=True))
        self.add_button(Button(275, 160, 310, 190, "Enter", self._submit, visible=True))

    def _toggle_mode(self):
        def delayed_rebuild():
            self.mode = "symbols" if self.mode == "letters" else "letters"
            self._build_keys()
            self.screen_manager.redraw()

        threading.Timer(0.1, delayed_rebuild).start()

    def _toggle_shift(self):
        self.shift = not self.shift
        self._build_keys()

    def _append_char(self, c):
        self.input_text += c
        print(f"[Keyboard] Input now: '{self.input_text}'")

    def _backspace(self):
        self.input_text = self.input_text[:-1]

    def _submit(self):
        self.on_submit(self.input_text)
        self.screen_manager.set_screen(self.screen_manager.previous_screen)

    def draw(self, draw_obj):
        img = Image.new("RGB", (device.width, device.height), "black")
        draw = ImageDraw.Draw(img)
        font = ImageFont.load_default()
        fontTitle = _load_font(size=18)
        draw.text((10, 10), f"{self.prompt}:", fill="white", font=fontTitle)
        draw.text((10, 40), self.input_text, fill="cyan", font=font)
        for btn in self.buttons:
            btn.draw(draw)
        
        if hasattr(self.screen_manager, "overlay"):
            self.screen_manager.overlay.update_base(img)

        present(img) # do NOT call device.display(img) directly anymore


class MainMenuScreen(Screen):
    def __init__(self, screen_manager, hill):
        super().__init__()
        self.screen_manager = screen_manager
        self.hill = hill
        try:
            # dim -> day art, full -> night art
            bg_path = "images/mainmenu_night.png" if getattr(brightness_state, "scale", 1.0) < 0.99 else "images/mainmenu_day.png"
            self.bg_image = Image.open(bg_path).convert("RGB").resize((device.width, device.height))
            draw_wifi_bars_badge(self.bg_image, pos="top-right", margin_y=14)
            if VERBOSE:
                draw_cpu_badge(self.bg_image, pos="top-left")
        except FileNotFoundError:
            print("Ã¢Å¡Â Ã¯Â¸Â images/mainmenu.png not found. Using black background.")
            self.bg_image = Image.new("RGB", (device.width, device.height), "black")

        # top-left dim toggle (invisible hitbox over background art)
        self.add_button(Button(5, 5, 55, 45, "Dim", self._toggle_brightness, visible=False))
        self.add_button(Button(60, 100, 260, 130, "Mountain Report", lambda: screen_manager.set_screen(SnowReportScreen(screen_manager, screen_manager.hill))))
        self.add_button(Button(60, 140, 260, 165, "Avy Conditions", lambda: screen_manager.set_screen(AvyMaskScreen(screen_manager, screen_manager.hill))))
        self.add_button(Button(60, 206, 260, 237, "Config", lambda: screen_manager.set_screen(ImageScreen("images/config.png", screen_manager, screen_manager.hill))))
        self.add_button(Button(60, 175, 260, 200, "Powder Drive", lambda: screen_manager.set_screen(PowderDriveSplashScreen(screen_manager))))
        self.add_button(Button(275, 198, 318, 238, "Update", lambda: screen_manager.set_screen(UpdateScreen(screen_manager, screen_manager.hill)), visible=False))

    def draw(self, draw_obj):
        present(self.bg_image.copy())

    def _toggle_brightness(self):
        brightness_state.cycle()
        leds_set_brightness(brightness_state.scale)
        try:
            show_popup_message(f"Brightness: {brightness_state.name}", duration=1.5)
        except Exception:
            pass
        # Reload main menu to pick up the correct background image
        self.screen_manager.set_screen(MainMenuScreen(self.screen_manager, self.screen_manager.hill))

class ChartScreen(Screen):
    """
    History chart screen:
    - Uses the currently selected hill's URL/name to resolve a Snow Plow-style JSON history feed.
    - Left Y-axis: 7-day & base depth (lines).
    - Right Y-axis: 24h new snow (bars, LED-style colors).
    - Back button bottom-right -> Mountain Report for the same hill.
    """

    def __init__(self, screen_manager, hill):
        super().__init__()
        self.screen_manager = screen_manager
        self.hill = hill

        self.bg_color = (15, 20, 30)
        self.grid_color = (60, 60, 80)
        self.text_color = (220, 220, 220)
        self.font = _load_font(size=12)

        self.url = self._resolve_history_url()
        print(f"[ChartScreen] Using history URL for {getattr(self.hill, 'name', '?')}: {self.url}")

        # Back button bottom-right Ã¢â€ â€™ Mountain Report (same hill)
        self.add_button(Button(
            240, 210, 310, 239,
            "Back",
            lambda: screen_manager.set_screen(
                SnowReportScreen(screen_manager, self.hill)
            ),
            visible=True
        ))

    # ---------- Helpers ----------

    def _text_size(self, draw, text, font):
        try:
            bbox = draw.textbbox((0, 0), text, font=font)
            return bbox[2] - bbox[0], bbox[3] - bbox[1]
        except Exception:
            return draw.textsize(text, font=font)

    def _resolve_history_url(self):
        """
        Decide which JSON history endpoint to use for the current hill.

        Priority:
        1. If hill.url already looks like a JSON endpoint, use it.
        2. Else, derive from hill.name as http://vps.snowscraper.ca/json/Name_With_Underscores.json
        3. Fallback to Banff Sunshine JSON.
        """
        u = str(getattr(self.hill, "url", "") or "").strip()
        name = (getattr(self.hill, "name", "") or "").strip()

        # Direct JSON-style URLs
        if u.endswith(".json") or "/json/" in u:
            return u

        # Derive from hill name if we have one
        if name:
            slug = (
                name.replace("'", "")
                    .replace(" ", "_")
                    .replace("-", "_")
            )
            return f"http://vps.snowscraper.ca/json/{slug}.json"

        # Absolute fallback
        return "http://vps.snowscraper.ca/json/Banff_Sunshine.json"

    def _bar_color_for_cm(self, cm):
        """
        LED-style ramp for 24h snowfall.
        """
        try:
            cm = int(cm)
        except Exception:
            cm = 0
        cm = max(0, cm)

        if cm == 0:
            return (35, 40, 55)          # subtle / no snow
        if cm <= 2:
            return (255, 255, 255)       # white
        if cm <= 5:
            return (168, 216, 255)       # light blue
        if cm <= 8:
            return (0, 72, 255)          # dark blue
        if cm <= 12:
            return (128, 0, 255)         # purple
        if cm <= 15:
            return (200, 0, 100)         # purple/red blend
        if cm <= 18:
            return (139, 0, 0)           # red
        return (255, 0, 0)               # dark red

    # ---------- Data fetch ----------
    def _fetch_history(self):
        try:
            resp = requests.get(self.url, timeout=6)
            resp.raise_for_status()
            payload = resp.json()
        except Exception as e:
            print(f"[ChartScreen] Fetch failed from {self.url}: {e}")
            return []

        entries = []
        if isinstance(payload, dict):
            if isinstance(payload.get("history"), list):
                entries = payload["history"]
            elif isinstance(payload.get("days"), list):
                entries = payload["days"]
            else:
                for k, v in payload.items():
                    if isinstance(v, dict):
                        v = dict(v)
                        v.setdefault("date", k)
                        entries.append(v)
        elif isinstance(payload, list):
            entries = payload

        norm = []
        for e in entries:
            if not isinstance(e, dict):
                continue

            def pick(keys, default=0):
                for k in keys:
                    if k in e:
                        return e[k]
                return default

            date_raw = pick(["date", "day", "ts", "timestamp", "label"], "")
            date_str = str(date_raw)
            label = date_str[-5:] if len(date_str) >= 5 else date_str

            new24 = _safe_int(pick(["newSnow", "new_24", "snow_24h", "24h"], 0))
            week = _safe_int(pick(["weekSnow", "new_7d", "snow_7d", "7d"], 0))
            base = _safe_int(pick(["baseSnow", "base", "base_cm", "baseDepth"], 0))

            norm.append({
                "label": label,
                "new24": new24,
                "week": week,
                "base": base,
            })

        return norm[-18:] if len(norm) > 18 else norm

    # ---------- Draw ----------
    def draw(self, draw_obj):
        img = Image.new("RGB", (device.width, device.height), self.bg_color)
        draw = ImageDraw.Draw(img)

        hist = self._fetch_history()
        if not hist:
            draw.text(
                (28, 100),
                "No chart data.\nCheck VPS JSON.",
                fill=self.text_color,
                font=self.font,
            )
            # Back button visual
            back_label = "Back"
            bx1, by1, bx2, by2 = 240, 210, 310, 239
            draw.rectangle(
                (bx1, by1, bx2, by2),
                outline=self.grid_color,
                fill=(20, 26, 38),
            )
            btw, bth = self._text_size(draw, back_label, self.font)
            draw.text(
                (bx1 + (bx2 - bx1 - btw) // 2,
                 by1 + (by2 - by1 - bth) // 2),
                back_label,
                fill=self.text_color,
                font=self.font,
            )
            present(img)
            return

        # ----- Data prep -----
        labels = [e["label"] for e in hist]
        new24_vals = [e["new24"] for e in hist]
        week_vals = [e["week"] for e in hist]
        base_vals = [e["base"] for e in hist]

        max_new24 = max(new24_vals) if any(new24_vals) else 0
        bar_max = max_new24 + 10 if max_new24 > 0 else 5

        max_week = max(week_vals) if any(week_vals) else 0
        max_base = max(base_vals) if any(base_vals) else 0
        line_max_raw = max(max_week, max_base, 1)
        line_max = 20 if line_max_raw <= 20 else ((line_max_raw + 9) // 10) * 10

        # ----- Layout -----
        left = 35
        right = 290            # leaves space for right Y-axis labels
        top = 30
        bottom = 195

        w = right - left
        h = bottom - top
        n = len(hist)

        full_bar_w = max(3, w // max(n, 1))
        spacing = 2
        bar_w = max(1, full_bar_w - spacing)

        # Chart box
        draw.rectangle(
            (left - 1, top - 1, right + 1, bottom + 1),
            outline=self.grid_color,
            width=1,
        )

        # ----- Grid + Y axes -----
        steps = 4
        for i in range(steps + 1):
            frac = i / steps
            y = bottom - int(h * frac)

            # grid
            draw.line((left, y, right, y), fill=self.grid_color)

            # left axis (7d/base)
            val_left = int(line_max * frac)
            txt_left = str(val_left)
            tw, th = self._text_size(draw, txt_left, self.font)
            draw.text(
                (left - 6 - tw, y - th // 2),
                txt_left,
                fill=self.text_color,
                font=self.font,
            )

            # right axis (24h)
            val_right = int(bar_max * frac)
            txt_right = str(val_right)
            tw2, th2 = self._text_size(draw, txt_right, self.font)
            x_right_label = right + 4
            if x_right_label + tw2 > device.width - 2:
                x_right_label = device.width - 2 - tw2
            draw.text(
                (x_right_label, y - th2 // 2),
                txt_right,
                fill=(120, 180, 255),
                font=self.font,
            )

        if bar_max <= 0:
            bar_max = 1

        # ----- 24h Bars with 2px spacing -----
        for i, e in enumerate(hist):
            val = e["new24"]
            if val <= 0:
                continue
            slot_x = left + i * full_bar_w
            x0 = slot_x + spacing // 2
            x1 = x0 + bar_w - 1
            if x0 >= right:
                continue
            if x1 > right:
                x1 = right
            y = bottom - int((val / float(bar_max)) * h)
            draw.rectangle(
                (x0, y, x1, bottom),
                fill=self._bar_color_for_cm(val),
            )

        # ----- 7d / Base Lines -----
        week_color = (160, 80, 255)
        base_color = (255, 80, 80)

        def plot_line(vals, color):
            pts = []
            for i, val in enumerate(vals):
                v = val or 0
                x = left + i * full_bar_w + full_bar_w // 2
                if x > right:
                    x = right
                y = bottom - int((v / float(line_max)) * h)
                pts.append((x, y))
            if len(pts) > 1:
                draw.line(pts, fill=color, width=2)

        plot_line(week_vals, week_color)
        plot_line(base_vals, base_color)

        # ----- X-axis date labels (start, mid, end) -----
        indices = []
        if n >= 1:
            indices.append(0)
        if n >= 3:
            indices.append(n // 2)
        if n >= 2:
            indices.append(n - 1)
        indices = sorted(set(indices))

        for i in indices:
            lab = labels[i]
            tw, th = self._text_size(draw, lab, self.font)
            x_center = left + i * full_bar_w + full_bar_w // 2
            x = max(left, min(right - tw, x_center - tw // 2))
            y = bottom + 2
            draw.text((x, y), lab, fill=self.text_color, font=self.font)

        # ----- Title (per-hill) -----
        title_font = _load_font(size=16)
        title_name = getattr(self.hill, "name", "History")
        draw.text(
            (40, 8),
            f"{title_name} History",
            fill=self.text_color,
            font=title_font,
        )

        # ----- Compact bottom legend with 24h gradient -----
        # ----- Two-line compact axis legend (lowered + text tweak) -----
        legend_x = 10
        legend_y1 = 211  # was 207, moved down 4px
        block_h = 8
        label_font = self.font

        base_color = (255, 80, 80)
        week_color = (160, 80, 255)

        # --- Line 1: L-Axis [blocks]   R-Axis [gradient] ---
        laxis_txt = "L-Axis:"
        laxis_tw, laxis_th = self._text_size(draw, laxis_txt, label_font)
        draw.text((legend_x, legend_y1), laxis_txt, fill=self.text_color, font=label_font)

        x = legend_x + laxis_tw + 4

        # Base + 7d color blocks
        draw.rectangle((x, legend_y1 + 3, x + 10, legend_y1 + 3 + block_h), fill=base_color)
        x += 12
        draw.rectangle((x, legend_y1 + 3, x + 10, legend_y1 + 3 + block_h), fill=week_color)
        x += 14

        x += 10  # gap before R-axis
        legend_x2 = x
        raxis_txt = "R-Axis:"
        raxis_tw, raxis_th = self._text_size(draw, raxis_txt, label_font)
        draw.text((x, legend_y1), raxis_txt, fill=self.text_color, font=label_font)
        x += raxis_tw + 4

        # 24h Snow gradient block (Snow Scraper canonical)
        grad_w = 60
        grad_stops = [
            (0.00, (168, 216, 255)),  # light blue
            (0.25, (0, 72, 255)),     # deep blue
            (0.50, (128, 0, 255)),    # purple
            (0.75, (139, 0, 0)),      # dark red
            (1.00, (255, 0, 0)),      # bright red
        ]

        def _interp_color(c1, c2, t):
            return tuple(int(c1[i] + (c2[i] - c1[i]) * t) for i in range(3))

        grad_x1 = x
        for i in range(grad_w):
            u = i / float(max(1, grad_w - 1))
            for j in range(len(grad_stops) - 1):
                t0, c0 = grad_stops[j]
                t1, c1 = grad_stops[j + 1]
                if t0 <= u <= t1:
                    lt = (u - t0) / (t1 - t0)
                    col = _interp_color(c0, c1, lt)
                    break
            gx = grad_x1 + i
            draw.line((gx, legend_y1 + 3, gx, legend_y1 + 3 + block_h), fill=col)
        grad_x2 = grad_x1 + grad_w

        # --- Line 2: text labels ---
        legend_y2 = legend_y1 + laxis_th + 4

        # "Base/7D" under L-axis blocks
        b_label = "Base/7D"
        draw.text((legend_x, legend_y2), b_label, fill=self.text_color, font=label_font)

        # "24HR Snow" under gradient
        r_label = "24HR Snow"

        draw.text((legend_x2, legend_y2), r_label, fill=self.text_color, font=label_font)

        # ----- Back button -----
        back_label = "Back"
        bx1, by1, bx2, by2 = 240, 210, 310, 239
        draw.rectangle(
            (bx1, by1, bx2, by2),
            outline=self.grid_color,
            fill=(20, 26, 38),
        )
        btw, bth = self._text_size(draw, back_label, self.font)
        draw.text(
            (bx1 + (bx2 - bx1 - btw) // 2,
             (by1 + (by2 - by1 - bth) // 2 )- 4),
            back_label,
            fill=self.text_color,
            font=self.font,
        )

        if hasattr(self.screen_manager, "overlay"):
            self.screen_manager.overlay.update_base(img)
        present(img)

# ---------------------------------------------------------------------
# Avalanche Forecast (avalanche.ca point API)
# ---------------------------------------------------------------------
class AvyForecastScreen(Screen):
    """
    Minimal text-first avalanche forecast view for 320x240.
    Pulls a point forecast from avalanche.ca using the resort's lat/lon.
    """
    def __init__(self, screen_manager, hill):
        super().__init__()
        self.screen_manager = screen_manager
        self.hill = hill
        self.resort_name = getattr(hill, "name", "") or current_resort_name()
        self.point = _get_resort_point(self.resort_name)
        self.forecast = None
        self.error = None
        self.loading = True
        self.summary_lines = []
        self.scroll_index = 0
        self.header_y = 38
        self.summary_y = self.header_y + 46
        self.summary_line_height = 14

        # Navigation buttons (all visible hitboxes)
        self.add_button(Button(
            240, 205, 318, 236,
            "Back",
            lambda: screen_manager.set_screen(AvyMaskScreen(screen_manager, screen_manager.hill)),
            visible=True
        ))
        self.add_button(Button(12, 2, 117, 30, "PrevResort", lambda: self._cycle_resort(-1), visible=True))
        self.add_button(Button(215, 2, 318, 30, "NextResort", lambda: self._cycle_resort(1), visible=True))
        # Summary scroll controls (visible only when needed)
        self.add_button(Button(280, 36, 318, 60, "Up", lambda: self._scroll_summary(-2), visible=False))
        self.add_button(Button(280, 66, 318, 90, "Dn", lambda: self._scroll_summary(2), visible=False))

        threading.Thread(target=self._load_forecast, daemon=True).start()

    # ---------- Data ----------
    def _cycle_resort(self, direction: int):
        if not cycle_resort_in_active_region(direction):
            return

        new_hill = reload_hill()
        self.screen_manager.hill = new_hill
        self.screen_manager.set_screen(AvyForecastScreen(self.screen_manager, new_hill))

    def _load_forecast(self):
        if not self.point:
            self.error = f"No lat/lon for '{self.resort_name}'. Update {RESORT_META_FILE}."
            self.loading = False
            self.screen_manager.redraw()
            return
        lat, lon = self.point
        try:
            self.forecast = _fetch_point_forecast(lat, lon)
            self._set_summary_lines()
        except Exception as e:
            self.error = str(e)
            self.summary_lines = []
            self.scroll_index = 0
            self._update_scroll_buttons()
        finally:
            self.loading = False
            self.screen_manager.redraw()

    # ---------- Helpers ----------
    def _wrap(self, text, width_chars=36):
        return textwrap.wrap(text or "", width=width_chars)

    def _set_summary_lines(self):
        text = (self.forecast or {}).get("summary", "")
        self.summary_lines = self._wrap(text, 38)
        self.scroll_index = 0
        self._update_scroll_buttons()

    def _max_visible_lines(self):
        available = max(0, 225 - self.summary_y)
        # Hard cap to avoid overlapping the Back button
        return max(1, min(7, available // self.summary_line_height))

    def _update_scroll_buttons(self):
        max_lines = self._max_visible_lines()
        overflow = len(self.summary_lines) > max_lines
        # Buttons: back, prev, next, up, down
        up_btn = self.buttons[3]
        down_btn = self.buttons[4]
        up_btn.visible = overflow
        down_btn.visible = overflow
        if not overflow:
            self.scroll_index = 0
        else:
            max_idx = max(0, len(self.summary_lines) - max_lines)
            self.scroll_index = min(self.scroll_index, max_idx)

    def _scroll_summary(self, delta: int):
        if not self.summary_lines:
            return
        max_lines = self._max_visible_lines()
        max_idx = max(0, len(self.summary_lines) - max_lines)
        self.scroll_index = max(0, min(self.scroll_index + delta, max_idx))
        self._update_scroll_buttons()
        self.screen_manager.redraw()

    def _rating_color(self, rating: str):
        r = (rating or "").lower()
        if not r or r == "n/a":
            return (160, 170, 185)
        if "low" in r:
            return (80, 200, 120)
        if "moderate" in r:
            return (255, 215, 0)
        if "considerable" in r:
            return (255, 140, 0)
        if "high" in r:
            return (255, 69, 58)
        if "extreme" in r:
            return (255, 0, 0)
        return (200, 220, 235)

    def _text_size(self, draw, text, font):
        try:
            bbox = draw.textbbox((0, 0), text, font=font)
            return bbox[2] - bbox[0], bbox[3] - bbox[1]
        except Exception:
            return draw.textsize(text, font=font)

    def _format_issue(self, issued: str, region: str = ""):
        if not issued:
            return "Updated: unknown"
        try:
            ts = issued.replace("Z", "+00:00")
            dt = datetime.datetime.fromisoformat(ts)
            stamp = dt.strftime("%b %d %H:%M")
        except Exception:
            stamp = issued
        region_txt = f" Ã¢â‚¬Â¢ {region}" if region else ""
        return f"Updated {stamp}{region_txt}"

    # ---------- Draw ----------
    def draw(self, draw_obj):
        img = Image.new("RGB", (device.width, device.height), (12, 16, 26))
        draw = ImageDraw.Draw(img)
        title_font = _load_font(size=16)
        body_font = _load_font(size=12)
        small_font = _load_font(size=11)

        # Header
        header_y = self.header_y
        draw.text((12, header_y), "Avalanche Forecast", fill=(235, 245, 255), font=title_font)
        if self.point:
            lat, lon = self.point
            draw.text((12, header_y + 22), f"{self.resort_name} {lat:.3f}, {lon:.3f}", fill=(190, 220, 255), font=body_font)
        else:
            draw.text((12, header_y + 22), self.resort_name, fill=(190, 220, 255), font=body_font)

        # Visible navigation buttons
        back_btn, prev_btn, next_btn, up_btn, down_btn = self.buttons
        btn_outline = (70, 90, 110)
        btn_fill = (24, 30, 40)
        label_fill = (220, 230, 240)
        for btn, label in ((prev_btn, "Prev Resort"), (next_btn, "Next Resort")):
            draw.rectangle((btn.x1, btn.y1, btn.x2, btn.y2), outline=btn_outline, fill=btn_fill)
            btw, bth = self._text_size(draw, label, body_font)
            draw.text(
                (btn.x1 + (btn.x2 - btn.x1 - btw) // 2, (btn.y1 + (btn.y2 - btn.y1 - bth) // 2) - 3),
                label, fill=label_fill, font=body_font
            )
        for btn, label in ((up_btn, "Up"), (down_btn, "Dwn")):
            if not btn.visible:
                continue
            draw.rectangle((btn.x1, btn.y1, btn.x2, btn.y2), outline=btn_outline, fill=btn_fill)
            btw, bth = self._text_size(draw, label, body_font)
            draw.text(
                (btn.x1 + (btn.x2 - btn.x1 - btw) // 2, (btn.y1 + (btn.y2 - btn.y1 - bth) // 2) - 3),
                label, fill=label_fill, font=body_font
            )

        y = self.summary_y
        if self.loading:
            draw.text((12, y), "Loading forecast...", fill=(220, 220, 220), font=body_font)
        elif self.error:
            for line in self._wrap(self.error, 32):
                draw.text((12, y), line, fill=(255, 120, 120), font=body_font)
                y += 16
        elif self.forecast:
            y += 6
            draw.text((12, y), "Summary", fill=(205, 230, 255), font=body_font)
            y += 16
            max_lines = self._max_visible_lines()
            visible_lines = self.summary_lines[self.scroll_index:self.scroll_index + max_lines]
            for line in visible_lines:
                draw.text((12, y), line, fill=(210, 210, 210), font=small_font)
                y += self.summary_line_height

            issue_label = self._format_issue(
                self.forecast.get("issued", ""), self.forecast.get("region", "")
            )
            issue_label_short = issue_label[:25]
            draw.text((12, 225), issue_label_short, fill=(160, 180, 200), font=small_font)
        else:
            draw.text((12, y), "No forecast data.", fill=(220, 220, 220), font=body_font)

        # Back button affordance
        bx1, by1, bx2, by2 = back_btn.x1, back_btn.y1, back_btn.x2, back_btn.y2
        draw.rectangle((bx1, by1, bx2, by2), outline=btn_outline, fill=btn_fill)
        btw, bth = self._text_size(draw, "Back", body_font)
        draw.text(
            (bx1 + (bx2 - bx1 - btw) // 2, (by1 + (by2 - by1 - bth) // 2) - 3),
            "Back", fill=label_fill, font=body_font
        )

        if hasattr(self.screen_manager, "overlay"):
            self.screen_manager.overlay.update_base(img)
        present(img)

# ---------------------------------------------------------------------
# Avalanche Forecast (mask overlay view)
# ---------------------------------------------------------------------
class AvyMaskScreen(Screen):
    """
    Colorizes three elevation bands using the mask assets:
      - Top mask  -> Alpine
      - Mid mask  -> Treeline
      - Bottom    -> Below Treeline
    A "Details" button (top-left) opens the text AvyForecastScreen.
    """
    def __init__(self, screen_manager, hill):
        super().__init__()
        self.screen_manager = screen_manager
        self.hill = hill
        self.resort_name = getattr(hill, "name", "") or current_resort_name()
        self.point = _get_resort_point(self.resort_name)
        self.forecast = None
        self.error = None
        self.loading = True
        self.assets = _load_avy_mask_assets()

        # Buttons: details (visible), prev/next resort (hidden), back (visible)
        self.add_button(Button(6, 7, 47, 49, "Details", lambda: screen_manager.set_screen(AvyForecastScreen(screen_manager, screen_manager.hill)), visible=False))
        self.add_button(Button(280, 6, 312, 37, "PrevResort", lambda: self._cycle_resort(-1), visible=False))
        self.add_button(Button(280, 50, 312, 81, "NextResort", lambda: self._cycle_resort(1), visible=False))
        self.add_button(Button(270, 194, 313, 231, "Back", lambda: screen_manager.set_screen(MainMenuScreen(screen_manager, screen_manager.hill)), visible=False))

        threading.Thread(target=self._load_forecast, daemon=True).start()

    # ---------- Data ----------
    def _cycle_resort(self, direction: int):
        if not cycle_resort_in_active_region(direction):
            return

        new_hill = reload_hill()
        self.screen_manager.hill = new_hill
        self.screen_manager.set_screen(AvyMaskScreen(self.screen_manager, new_hill))

    def _load_forecast(self):
        if not self.point:
            self.error = f"No lat/lon for '{self.resort_name}'. Update {RESORT_META_FILE}."
            self.loading = False
            self.screen_manager.redraw()
            return
        lat, lon = self.point
        try:
            self.forecast = _fetch_point_forecast(lat, lon)
        except Exception as e:
            self.error = str(e)
        finally:
            self.loading = False
            self.screen_manager.redraw()

    # ---------- Helpers ----------
    def _text_size(self, draw, text, font):
        try:
            bbox = draw.textbbox((0, 0), text, font=font)
            return bbox[2] - bbox[0], bbox[3] - bbox[1]
        except Exception:
            return draw.textsize(text, font=font)

    def _danger_tuple(self):
        danger = (self.forecast or {}).get("danger") or {}
        return (
            str(danger.get("alpine", "N/A")),
            str(danger.get("treeline", "N/A")),
            str(danger.get("below_treeline", "N/A")),
        )

    # ---------- Draw ----------
    def draw(self, draw_obj):
        assets = self.assets or _load_avy_mask_assets()
        base = assets.get("background") or Image.new("RGBA", (device.width, device.height), (12, 16, 26, 255))
        masks = assets.get("masks") or []

        ratings = self._danger_tuple()
        display = base.copy()
        for alpha, rating in zip(masks, ratings):
            color = _avy_color_for_rating(rating)
            color_layer = Image.new("RGBA", display.size, color)
            empty = Image.new("RGBA", display.size, (0, 0, 0, 0))
            colored_mask = Image.composite(color_layer, empty, alpha)
            display = Image.alpha_composite(display, colored_mask)

        img = display.convert("RGB")
        draw = ImageDraw.Draw(img)
        title_font = _load_font(size=18)
        label_font = _load_font(size=12)

        # Resort title centered top
        tw, th = self._text_size(draw, self.resort_name, title_font)
        draw.text(((device.width - tw) // 2, 8), self.resort_name, fill=(235, 245, 255), font=title_font)

        # Details button (only if visible)
        btn = self.buttons[0]
        if btn.visible:
            draw.rectangle((btn.x1, btn.y1, btn.x2, btn.y2), outline=(90, 110, 130), fill=(24, 32, 42))
            btw, bth = self._text_size(draw, "Details", label_font)
            draw.text((btn.x1 + (btn.x2 - btn.x1 - btw) // 2, btn.y1 + (btn.y2 - btn.y1 - bth) // 2), "Details", fill=(220, 230, 240), font=label_font)

        # Back button (bottom-right; only if visible)
        back_btn = self.buttons[3]
        if back_btn.visible:
            draw.rectangle((back_btn.x1, back_btn.y1, back_btn.x2, back_btn.y2), outline=(70, 90, 110), fill=(24, 32, 42))
            bbtw, bbth = self._text_size(draw, "Back", label_font)
            draw.text((back_btn.x1 + (back_btn.x2 - back_btn.x1 - bbtw) // 2, back_btn.y1 + (back_btn.y2 - back_btn.y1 - bbth) // 2), "Back", fill=(220, 230, 240), font=label_font)

        # Status / ratings
        status_y = 30
        draw.text((60, 160), "Alpine", fill=(225, 225, 225), font=label_font)
        draw.text((60, 177), "Treeline", fill=(225, 225, 225), font=label_font)
        draw.text((60, 194), "Below Treeline", fill=(225, 225, 225), font=label_font)
        if self.loading:
            draw.text((90, status_y), "Loading forecast...", fill=(220, 220, 220), font=label_font)
        elif self.error:
            for idx, line in enumerate(textwrap.wrap(self.error, 38)):
                draw.text((90, status_y + idx * 14), line, fill=(255, 120, 120), font=label_font)
        else:
            positions = [(195, 160), (195, 177), (195, 194)]
            for pos, val in zip(positions, ratings):
                txt = str(val)
                draw.text(pos, txt[:7], fill=(225, 225, 225), font=label_font)

        if hasattr(self.screen_manager, "overlay"):
            self.screen_manager.overlay.update_base(img)
        present(img)

# ---------------------------------------------------------------------
# Powder Drive Splash + Main Screen
# ---------------------------------------------------------------------
class PowderDriveSplashScreen(Screen):
    """
    Shows pdrive_splash.png for ~2 seconds while the API request runs.
    Then transitions automatically to PowderDriveScreen with results.
    """
    def __init__(self, screen_manager):
        super().__init__()
        self.screen_manager = screen_manager
        try:
            self.splash = Image.open("images/pdrive_splash.png").convert("RGB") \
                .resize((device.width, device.height))
        except FileNotFoundError:
            print("Ã¢Å¡Â Ã¯Â¸Â images/pdrive_splash.png not found, using blank.")
            self.splash = Image.new("RGB", (device.width, device.height), "black")

        # Start worker thread immediately
        threading.Thread(target=self._fetch_and_transition, daemon=True).start()

    def _fetch_and_transition(self):
        # Show splash for at least 2 seconds
        t0 = time.time()

        # 1. Guess location via ipapi.co
        city = "Kamloops, BC"   # safe default so we always have a value
        origin = city
        try:
            r = requests.get("https://ipapi.co/json", timeout=5)
            payload = r.json() if r.content else {}
            city = (payload.get("city") or "").strip() or city
            region = (payload.get("region") or "").strip()
            origin = f"{city}, {region}".strip(", ") if region else city
            origin = origin.strip() or "Kamloops, BC"
            print(f"[PowderDrive] Origin: {origin}")
        except Exception:
            origin = "Kamloops, BC"
            city = origin
            print("[PowderDrive] Origin: default Kamloops, BC")

        # 2. Query PowderDrive API
        url = ("https://plow.snowscraper.ca/api/powderdrive"
               f"?q={requests.utils.quote(origin)}"
               "&max_hours=6&min_snow_cm=0&top_n=5")

        results = []
        try:
            resp = requests.get(url, timeout=20)
            print(f"[PowderDrive] API status: {resp.status_code}")
            data = resp.json()
            results = data.get("results", [])
            print(f"[PowderDrive] API results: {len(results)}")
        except Exception as e:
            print(f"[PowderDrive] API error: {e}")

        # Ensure splash lasts 2s
        dt = time.time() - t0
        if dt < 2:
            time.sleep(2 - dt)

        # Switch to main PD screen
        self.screen_manager.set_screen(
            PowderDriveScreen(self.screen_manager, city, results)
        )

    def draw(self, draw_obj):
        present(self.splash)


class PowderDriveScreen(Screen):
    """
    Displays the Powder Drive results using pdrive.png as a background.
    """
    def __init__(self, screen_manager, origin, results):
        super().__init__()
        self.screen_manager = screen_manager
        self.origin = origin
        self.results = results[:5] if isinstance(results, list) else []

        # Background image (320Ãƒâ€”240)
        try:
            self.bg = Image.open("images/pdrive.png").convert("RGB") \
                .resize((device.width, device.height))
        except Exception:
            print("Ã¢Å¡Â Ã¯Â¸Â Missing images/pdrive.png, using black fill.")
            self.bg = Image.new("RGB", (device.width, device.height), "black")

        # Back button
        self.add_button(Button(
            250, 210, 310, 235,
            "Back",
            lambda: screen_manager.set_screen(
                MainMenuScreen(screen_manager, screen_manager.hill)
            ),
            visible=False
        ))

    def draw(self, draw_obj):
        # Render start: background image
        img = self.bg.copy()
        draw = ImageDraw.Draw(img)

        title_font = _load_font(size=14)
        row_font = _load_font(size=11)
        
        draw.text((186, 39), f"{self.origin[:12]}", fill="white", font=title_font)

        # Table rows
        y = 89
        for item in self.results:
            name = item.get("name", "")
            try:
                snow_val = float(item.get("snow_24h_cm", 0))
                snow = f"{snow_val:.0f} cm"
            except Exception:
                snow = f"{item.get('snow_24h_cm', '')} cm"
            try:
                dist_val = float(item.get("distance_km", 0))
                dist = f"{dist_val:.0f} km"
            except Exception:
                dist = f"{item.get('distance_km', '')} km"

            draw.text((60,  y), name[:13], fill="black", font=row_font)
            draw.text((184, y), dist,      fill="black", font=row_font)
            draw.text((258, y), snow,      fill="black", font=row_font)

            y += 23

        # buttons
        for btn in self.buttons:
            btn.draw(draw)

        # overlay update
        if hasattr(self.screen_manager, "overlay"):
            self.screen_manager.overlay.update_base(img)

        present(img)



class SnowReportScreen(Screen):
    def __init__(self, screen_manager, hill):
        super().__init__()
        self.screen_manager = screen_manager
        self.hill = hill
        try:
            print(f"[SnowReport] Refreshing data for {self.hill.name}...")
            self.hill.getSnow()
            leds_set_snow(self.hill.newSnow, self.hill.newSnow)
        except Exception as e:
            print(f"[SnowReport] Failed to refresh: {e}")
        try:
            self.bg_image = Image.open("images/mreport.png").convert("RGB").resize((device.width, device.height))
            self.image_missing = False
        except FileNotFoundError:
            print("Ã¢Å¡Â Ã¯Â¸Â images/mreport.png not found. Using black background.")
            self.bg_image = Image.new("RGB", (device.width, device.height), "black")
            self.image_missing = True

        # Back button (invisible hitbox as with others)
        self.add_button(
            Button(270, 185, 315, 230, "Back", lambda: screen_manager.set_screen(MainMenuScreen(screen_manager, screen_manager.hill)), visible=False)
        )
        # Charts button (bottom-left, visible)
        self.add_button(
            Button(5, 185, 55, 230, "Charts",
                   lambda: screen_manager.set_screen(ChartScreen(screen_manager, screen_manager.hill)),
                   visible=False)
        )
        # Resort navigation (invisible hitboxes at mid-left / mid-right)
        self.add_button(
            Button(2, 2, 105, 30, "PrevResort", lambda: self._cycle_resort(-1), visible=False)
        )
        self.add_button(
            Button(215, 2, 318, 30, "NextResort", lambda: self._cycle_resort(1), visible=False)
        )

    def _cycle_resort(self, direction: int):
        """Load the previous/next resort and refresh the report screen."""
        if not cycle_resort_in_active_region(direction):
            return

        new_hill = reload_hill()
        self.screen_manager.hill = new_hill
        self.screen_manager.set_screen(SnowReportScreen(self.screen_manager, new_hill))

    def draw(self, draw_obj):
        img = self.bg_image.copy()
        draw = ImageDraw.Draw(img)
        h = self.screen_manager.hill

        # Fonts
        font_title = _load_font("fonts/superpixel.ttf", size=30)
        font_line  = _load_font("fonts/ponderosa.ttf", size=16)

        # Normalize numbers just in case theyÃ¢â‚¬â„¢re strings
        new_cm   = _safe_int(h.newSnow)
        week_cm  = _safe_int(h.weekSnow)
        base_cm  = _safe_int(h.baseSnow)

        # Text block (tweak positions to taste)
        x = 55
        line_h = 26

        # Box where the resort name must fit (tweak to your background art)
        NAME_BOX = (55, 55, 213, 35)  # (x, y, width, height)

        # Draw name: auto-shrinks to fit NAME_BOX, centered
        draw_text_in_box(
            img,
            h.name,
            NAME_BOX,
            font_path="fonts/superpixel.ttf",
            color="white",
            min_sz=12,
            max_sz=38,
            align="center",
        )
        draw.text((x, 115), f"New  Snow: {new_cm}cm",  fill="white", font=font_line)
        draw.text((x, 144), f"Week Snow: {week_cm}cm", fill="white", font=font_line)
        draw.text((x, 173), f"Base Snow: {base_cm}cm", fill="white", font=font_line)

        if self.image_missing:
            f2 = ImageFont.load_default()
            msg = "images/mreport.png not found"
            w, h = draw.textsize(msg, font=f2)
            draw.text(((device.width - w) // 2, (device.height - h) // 2), msg, fill="white", font=f2)

        for btn in self.buttons:
            btn.draw(draw)

        if hasattr(self.screen_manager, "overlay"):
            self.screen_manager.overlay.update_base(img)

        present(img) # do NOT call device.display(img) directly anymore

def _truncate_config_label(value: str, max_len: int = 13) -> str:
    text = str(value or "")
    return text[:max_len]
class SelectCountryScreen(Screen):
    def __init__(self, screen_manager, hill):
        super().__init__()
        self.screen_manager = screen_manager
        self.hill = hill
        self.meta = _load_resort_meta()
        self.countries = get_countries(self.meta)
        self.current_index = 0

        selected = _read_selected_country()
        if self.countries:
            selected_key = (selected or "").casefold()
            for idx, country in enumerate(self.countries):
                if country.casefold() == selected_key:
                    self.current_index = idx
                    break

        try:
            self.bg_image = Image.open("images/select_resort.png").convert("RGB").resize((device.width, device.height))
            self.image_missing = False
        except FileNotFoundError:
            print("[SelectCountry] images/select_resort.png not found. Using black background.")
            self.bg_image = Image.new("RGB", (device.width, device.height), "black")
            self.image_missing = True

        self.add_button(
            Button(270, 190, 300, 220, "Back", lambda: screen_manager.set_screen(ImageScreen("images/config.png", screen_manager, screen_manager.hill)), visible=False)
        )
        self.add_button(Button(272, 108, 298, 135, "Up", self.scroll_up, visible=False))
        self.add_button(Button(272, 140, 298, 165, "Down", self.scroll_down, visible=False))
        self.add_button(Button(60, 175, 260, 200, "SelectCurrent", self.confirm_selection, visible=False))

    def confirm_selection(self):
        if not self.countries:
            self.screen_manager.set_screen(ImageScreen("images/config.png", self.screen_manager, self.screen_manager.hill))
            return

        selected = self.countries[self.current_index]
        _write_selected_country(selected)

        regions = get_regions(self.meta, selected)
        current_region = _read_selected_region()
        current_key = (current_region or "").casefold()
        if not any((region or "").casefold() == current_key for region in regions):
            _write_selected_region(ALL_REGIONS_LABEL)

        print(f"[SelectCountry] Selected: '{selected}' saved to country.conf")
        self.screen_manager.set_screen(SelectRegionScreen(self.screen_manager, self.screen_manager.hill))

    def scroll_up(self):
        if self.current_index > 0:
            self.current_index -= 1
        print(f"[SelectCountry] Scrolled up to index {self.current_index}")

    def scroll_down(self):
        if self.current_index < len(self.countries) - 1:
            self.current_index += 1
        print(f"[SelectCountry] Scrolled down to index {self.current_index}")

    def draw(self, draw_obj):
        img = self.bg_image.copy()
        draw = ImageDraw.Draw(img)
        font = _load_font(size=18)

        if self.image_missing:
            f2 = ImageFont.load_default()
            msg = "images/select_resort.png not found"
            w, h = draw.textsize(msg, font=f2)
            draw.text(((device.width - w) // 2, (device.height - h) // 2), msg, fill="white", font=f2)

        draw.text((73, 105), "Select Country", fill="white", font=font)
        if self.countries:
            if self.current_index > 0:
                draw.text((73, 140), _truncate_config_label(self.countries[self.current_index - 1]), fill="gray", font=font)
            draw.text((73, 175), _truncate_config_label(self.countries[self.current_index]), fill="white", font=font)
            if self.current_index < len(self.countries) - 1:
                draw.text((73, 207), _truncate_config_label(self.countries[self.current_index + 1]), fill="gray", font=font)

        for btn in self.buttons:
            btn.draw(draw)

        if hasattr(self.screen_manager, "overlay"):
            self.screen_manager.overlay.update_base(img)

        present(img) # do NOT call device.display(img) directly anymore


class SelectRegionScreen(Screen):
    def __init__(self, screen_manager, hill):
        super().__init__()
        self.screen_manager = screen_manager
        self.hill = hill
        self.meta = _load_resort_meta()
        self.selected_country = _read_selected_country()
        self.regions = get_regions(self.meta, self.selected_country)
        self.current_index = 0

        selected = _read_selected_region()
        if self.regions:
            selected_key = (selected or "").casefold()
            for idx, region in enumerate(self.regions):
                if region.casefold() == selected_key:
                    self.current_index = idx
                    break

        try:
            self.bg_image = Image.open("images/select_resort.png").convert("RGB").resize((device.width, device.height))
            self.image_missing = False
        except FileNotFoundError:
            print("[SelectRegion] images/select_resort.png not found. Using black background.")
            self.bg_image = Image.new("RGB", (device.width, device.height), "black")
            self.image_missing = True

        self.add_button(
            Button(270, 190, 300, 220, "Back", lambda: screen_manager.set_screen(SelectCountryScreen(screen_manager, screen_manager.hill)), visible=False)
        )
        self.add_button(Button(272, 108, 298, 135, "Up", self.scroll_up, visible=False))
        self.add_button(Button(272, 140, 298, 165, "Down", self.scroll_down, visible=False))
        self.add_button(Button(60, 175, 260, 200, "SelectCurrent", self.confirm_selection, visible=False))

    def confirm_selection(self):
        if not self.regions:
            self.screen_manager.set_screen(ImageScreen("images/config.png", self.screen_manager, self.screen_manager.hill))
            return
        selected = self.regions[self.current_index]
        _write_selected_region(selected)
        print(f"[SelectRegion] Selected: '{selected}' saved to region.conf")
        self.screen_manager.set_screen(SelectResortScreen(self.screen_manager, self.screen_manager.hill))

    def scroll_up(self):
        if self.current_index > 0:
            self.current_index -= 1
        print(f"[SelectRegion] Scrolled up to index {self.current_index}")

    def scroll_down(self):
        if self.current_index < len(self.regions) - 1:
            self.current_index += 1
        print(f"[SelectRegion] Scrolled down to index {self.current_index}")

    def draw(self, draw_obj):
        img = self.bg_image.copy()
        draw = ImageDraw.Draw(img)
        font = _load_font(size=18)

        if self.image_missing:
            f2 = ImageFont.load_default()
            msg = "images/select_resort.png not found"
            w, h = draw.textsize(msg, font=f2)
            draw.text(((device.width - w) // 2, (device.height - h) // 2), msg, fill="white", font=f2)

        draw.text((73, 105), "Select Region", fill="white", font=font)
        if self.regions:
            if self.current_index > 0:
                draw.text((73, 140), _truncate_config_label(self.regions[self.current_index - 1]), fill="gray", font=font)
            draw.text((73, 175), _truncate_config_label(self.regions[self.current_index]), fill="white", font=font)
            if self.current_index < len(self.regions) - 1:
                draw.text((73, 207), _truncate_config_label(self.regions[self.current_index + 1]), fill="gray", font=font)

        for btn in self.buttons:
            btn.draw(draw)

        if hasattr(self.screen_manager, "overlay"):
            self.screen_manager.overlay.update_base(img)


        present(img) # do NOT call device.display(img) directly anymore


class SelectResortScreen(Screen):
    def __init__(self, screen_manager, hill):
        super().__init__()
        self.screen_manager = screen_manager
        self.hill = hill
        self.meta = _load_resort_meta()
        self.selected_country = _read_selected_country()
        self.selected_region = _read_selected_region()
        self.skiHills = get_active_resorts(self.selected_country, self.selected_region, self.meta)
        current_name = current_resort_name()
        if current_name in self.skiHills:
            self.current_index = self.skiHills.index(current_name)
        else:
            self.current_index = 0

        try:
            self.bg_image = Image.open("images/select_resort.png").convert("RGB").resize((device.width, device.height))
            self.image_missing = False
        except FileNotFoundError:
            print("[SelectResort] images/select_resort.png not found. Using black background.")
            self.bg_image = Image.new("RGB", (device.width, device.height), "black")
            self.image_missing = True

        self.add_button(
            Button(270, 190, 300, 220, "Back", lambda: screen_manager.set_screen(SelectRegionScreen(screen_manager, screen_manager.hill)), visible=False)
        )
        self.add_button(Button(272, 108, 298, 135, "Up", self.scroll_up, visible=False))
        self.add_button(Button(272, 140, 298, 165, "Down", self.scroll_down, visible=False))
        self.add_button(Button(60, 175, 260, 200, "SelectCurrent", self.confirm_selection, visible=False))

    def confirm_selection(self):
        if not self.skiHills:
            self.screen_manager.set_screen(ImageScreen("images/config.png", self.screen_manager, self.screen_manager.hill))
            return
        selected = self.skiHills[self.current_index]
        try:
            set_current_resort_by_name(selected)
            names = get_resort_names(self.meta)
            index = names.index(selected) if selected in names else -1
            print(f"[SelectResort] Selected: '{selected}' (index {index}) saved to skihill.conf")
            global hill
            reload_hill()
            self.screen_manager.hill = hill
        except Exception as e:
            print(f"[ERROR] Failed to write skihill.conf: {e}")
        self.screen_manager.set_screen(ImageScreen("images/config.png", self.screen_manager, self.screen_manager.hill))

    def scroll_up(self):
        if self.current_index > 0:
            self.current_index -= 1
        print(f"[SelectResort] Scrolled up to index {self.current_index}")

    def scroll_down(self):
        if self.current_index < len(self.skiHills) - 1:
            self.current_index += 1
        print(f"[SelectResort] Scrolled down to index {self.current_index}")

    def draw(self, draw_obj):
        img = self.bg_image.copy()
        draw = ImageDraw.Draw(img)
        font = _load_font(size=18)

        if self.image_missing:
            f2 = ImageFont.load_default()
            msg = "images/select_resort.png not found"
            w, h = draw.textsize(msg, font=f2)
            draw.text(((device.width - w) // 2, (device.height - h) // 2), msg, fill="white", font=f2)

        draw.text((73, 105), "Select Resort", fill="white", font=font)
        if self.skiHills:
            if self.current_index > 0:
                draw.text((73, 140), _truncate_config_label(self.skiHills[self.current_index - 1]), fill="gray", font=font)
            draw.text((73, 175), _truncate_config_label(self.skiHills[self.current_index]), fill="white", font=font)
            if self.current_index < len(self.skiHills) - 1:
                draw.text((73, 207), _truncate_config_label(self.skiHills[self.current_index + 1]), fill="gray", font=font)

        for btn in self.buttons:
            btn.draw(draw)

        if hasattr(self.screen_manager, "overlay"):
            self.screen_manager.overlay.update_base(img)


        present(img) # do NOT call device.display(img) directly anymore


class ConfigWiFiScreen(Screen):
    def __init__(self, screen_manager, hill):
        super().__init__()
        self.screen_manager = screen_manager
        self.hill = hill
        self.ssid_list = get_available_ssids()
        self.current_index = 0
        self.ssid = self.ssid_list[self.current_index] if self.ssid_list else ""
        self.password = ""

        try:
            self.bg_image = Image.open("images/config_wifi.png").convert("RGB").resize((device.width, device.height))
            self.image_missing = False
        except FileNotFoundError:
            print("Ã¢Å¡Â Ã¯Â¸Â images/config_wifi.png not found. Using black background.")
            self.bg_image = Image.new("RGB", (device.width, device.height), "black")
            self.image_missing = True

        self.add_button(Button(272, 108, 298, 135, "SSID_UP", self.scroll_up, visible=False))
        self.add_button(Button(272, 140, 298, 165, "SSID_DOWN", self.scroll_down, visible=False))
        self.add_button(
            Button(60, 210, 260, 230, "PASSWORD", lambda: self._open_keyboard("Enter PASSWORD", self.set_password), visible=False)
        )
        self.add_button(Button(270, 190, 310, 220, "Back", self.save_and_exit, visible=False))

    def scroll_up(self):
        if self.current_index > 0:
            self.current_index -= 1
            self.ssid = self.ssid_list[self.current_index]
            print(f"[WiFi] SSID changed to: {self.ssid}")

    def scroll_down(self):
        if self.current_index < len(self.ssid_list) - 1:
            self.current_index += 1
            self.ssid = self.ssid_list[self.current_index]
            print(f"[WiFi] SSID changed to: {self.ssid}")

    def _open_keyboard(self, prompt, callback):
        self.screen_manager.previous_screen = self
        self.screen_manager.set_screen(KeyboardScreen(prompt, callback, self.screen_manager))

    def set_password(self, text):
        self.password = text
        print(f"[WiFi] PASSWORD set.")

    def save_and_exit(self):
        # Skip if no password entered
        if not self.password.strip():
            print("[WiFi] No password entered Ã¢â‚¬â€ skipping WiFi update.")
            self.screen_manager.set_screen(
                ImageScreen("images/config.png", self.screen_manager, self.screen_manager.hill)
            )
            return
        try:
            with open("/etc/wpa_supplicant/wpa_supplicant.conf", "w") as f:
                f.write(
                    'ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev\n'
                    "update_config=1\n\n"
                    "network={\n"
                    f'    ssid="{self.ssid}"\n'
                    f'    psk="{self.password}"\n'
                    "    key_mgmt=WPA-PSK\n"
                    "}\n"
                )
            print("[WiFi] wpa_supplicant.conf saved.")
        except Exception as e:
            print(f"[ERROR] Failed to save or apply config: {e}")

        threading.Thread(target=reconfigure_wifi, daemon=True).start()
        # Show confirmation popup
        show_popup_message("WiFi Updated", duration=3)
        self.screen_manager.set_screen(ImageScreen("images/config.png", self.screen_manager, self.screen_manager.hill))

    def draw(self, draw_obj):
        img = self.bg_image.copy()
        draw = ImageDraw.Draw(img)
        font = _load_font(size=18)
        draw.text((73, 105), "Wifi SSID", fill="white", font=font)
        if self.ssid_list:
            draw.text((73, 140), self.ssid_list[self.current_index][:14], fill="white", font=font)
        draw.text((73, 175), "PASSWORD", fill="white", font=font)
        draw.text((73, 207), f"{self.password[:14]}", fill="white", font=font)

        for btn in self.buttons:
            btn.draw(draw)

        if hasattr(self.screen_manager, "overlay"):
            self.screen_manager.overlay.update_base(img)


        present(img) # do NOT call device.display(img) directly anymore


class AlarmScreen(Screen):
    def __init__(self, screen_manager, hill):
        super().__init__()
        self.screen_manager = screen_manager
        self.hill = hill
        self.active = False
        self.active_anytime = False
        self.hour = ""
        self.minute = ""
        self.triggered_snow = ""
        self.incremental_snow = ""
        self.error_message = ""
        self.error_time = 0

        try:
            self.bg_image = Image.open("images/misc.png").convert("RGB").resize((device.width, device.height))
            self.image_missing = False
        except FileNotFoundError:
            print("Ã¢Å¡Â Ã¯Â¸Â images/misc.png not found. Using black background.")
            self.bg_image = Image.new("RGB", (device.width, device.height), "black")
            self.image_missing = True

        try:
            self.inactive_img = Image.open("images/InactiveButtonSmall.png").convert("RGB").resize((40, 20))
        except FileNotFoundError:
            print("Ã¢Å¡Â Ã¯Â¸Â images/InactiveButtonSmall.png not found. No inactive visual will be drawn.")
            self.inactive_img = None

        self._load_config()

        self.active_btn = Button(214, 149, 253, 167, "Active", self.toggle_active, visible=False)
        self.active_any_btn = Button(214, 183, 252, 204, "Active Anytime", self.toggle_active_anytime, visible=False)
        self.add_button(self.active_btn)
        self.add_button(self.active_any_btn)
        self.add_button(
            Button(68, 135, 118, 172, "Hour", lambda: self.open_kb("Enter Hour", self.set_hour), visible=False)
        )
        self.add_button(
            Button(120, 135, 170, 172, "Minute", lambda: self.open_kb("Enter Minute", self.set_minute), visible=False)
        )
        self.add_button(
            Button(172, 135, 210, 172, "Triggered Snow", lambda: self.open_kb("Triggered Snowfall Amount", self.set_triggered_snow), visible=False)
        )
        self.incr_trig_btn = Button(273, 109, 299, 135, "Incr Snow", self.incr_triggered_snow, visible=False)
        self.add_button(self.incr_trig_btn)
        self.decr_trig_btn = Button(273, 139, 299, 166, "Decr Snow", self.decr_triggered_snow, visible=False)
        self.add_button(self.decr_trig_btn)
        self.add_button(
            Button(68, 208, 245, 230, "Snow Increments", lambda: self.open_kb("Incremental Snowfall Amount", self.set_incremental_snow), visible=False)
        )
        self.add_button(
            Button(270, 190, 310, 225, "Back", lambda: screen_manager.set_screen(ImageScreen("images/config.png", screen_manager, screen_manager.hill)), visible=False)
        )

    def _load_config(self):
        cfg = load_alarm_cfg()
        self.active = bool(cfg.get("active"))
        self.active_anytime = bool(cfg.get("active_anytime"))
        self.hour = str(cfg.get("hour", "0"))
        self.minute = str(cfg.get("minute", "0"))
        self.triggered_snow = str(cfg.get("triggered_snow", "0"))
        self.incremental_snow = str(cfg.get("incremental_snow", "0"))

    def _save_from_fields(self):
        cfg = load_alarm_cfg()
        cfg["active"] = bool(self.active)
        cfg["active_anytime"] = bool(self.active_anytime)
        cfg["hour"] = str(self.hour)
        cfg["minute"] = str(self.minute)
        cfg["triggered_snow"] = str(self.triggered_snow)
        cfg["incremental_snow"] = str(self.incremental_snow)
        save_alarm_cfg(cfg)

    def _show_error(self, message):
        self.error_message = message
        self.error_time = time.time()

    def incr_triggered_snow(self):
        self.triggered_snow = str(int(self.triggered_snow or "0") + 1)
        self._save_from_fields()

    def decr_triggered_snow(self):
        cur = int(self.triggered_snow or "1")
        if cur > 1:
            self.triggered_snow = str(cur - 1)
            self._save_from_fields()

    def toggle_active(self):
        self.active = not self.active
        self._save_from_fields()

    def toggle_active_anytime(self):
        self.active_anytime = not self.active_anytime
        self._save_from_fields()

    def open_kb(self, prompt, callback):
        self.screen_manager.previous_screen = self
        self.screen_manager.set_screen(KeyboardScreen(prompt, callback, self.screen_manager))

    def set_hour(self, text):
        if text.isdigit() and 0 <= int(text) <= 23:
            self.hour = text
            self._save_from_fields()
        else:
            self._show_error("Hour must be 0Ã¢â‚¬â€œ23")

    def set_minute(self, text):
        if text.isdigit() and 0 <= int(text) <= 59:
            self.minute = text
            self._save_from_fields()
        else:
            self._show_error("Minute must be 0Ã¢â‚¬â€œ59")

    def set_triggered_snow(self, text):
        if text.isdigit() and 1 <= int(text) <= 100:
            self.triggered_snow = text
            self._save_from_fields()
        else:
            self._show_error("Triggered snow must be 1Ã¢â‚¬â€œ100")

    def set_incremental_snow(self, text):
        if text.isdigit() and 1 <= int(text) <= 20:
            self.incremental_snow = text
            self._save_from_fields()
        else:
            self._show_error("Incremental snow must be 1Ã¢â‚¬â€œ20")

    def draw(self, draw_obj):
        img = self.bg_image.copy()
        draw = ImageDraw.Draw(img)
        font18 = _load_font(size=18)
        font32 = _load_font(size=32)
        font16 = _load_font(size=16)

        draw.text((68, 110), "Alarm Settings", fill="white", font=font18)
        draw.text((68, 135), f"{int(self.hour):02d}", fill="white", font=font32)
        draw.text((120, 135), f"{int(self.minute):02d}", fill="white", font=font32)
        draw.text((172, 145), "@", fill="white", font=font18)
        draw.text((188, 139), f"{self.triggered_snow}", fill="white", font=font16)
        draw.text((187, 154), "cm", fill="white", font=font16)
        draw.text((68, 182), "Always On:", fill="white", font=font18)
        draw.text((68, 204), f"Every +{self.incremental_snow} cm", fill="white", font=font18)

        if self.error_message and time.time() - self.error_time < 3:
            draw.text((10, 220), self.error_message, fill="red", font=font18)

        if not self.active and self.inactive_img:
            img.paste(self.inactive_img, (214, 149))
        if not self.active_anytime and self.inactive_img:
            img.paste(self.inactive_img, (214, 185))

        for btn in self.buttons:
            btn.draw(draw)

        if hasattr(self.screen_manager, "overlay"):
            self.screen_manager.overlay.update_base(img)

        present(img) # do NOT call device.display(img) directly anymore


class ImageScreen(Screen):
    def __init__(self, image_file, screen_manager, hill):
        super().__init__()
        self.image_file = image_file
        self.screen_manager = screen_manager
        self.hill = hill
        try:
            self.bg_image = Image.open(image_file).convert("RGB").resize((device.width, device.height))
            self.image_missing = False
        except FileNotFoundError:
            print(f"Ã¢Å¡Â Ã¯Â¸Â {image_file} not found. Using black background.")
            self.bg_image = Image.new("RGB", (device.width, device.height), "black")
            self.image_missing = True

        self.add_button(
            Button(270, 190, 300, 220, "Back", lambda: screen_manager.set_screen(MainMenuScreen(screen_manager, screen_manager.hill)), visible=False)
        )

        if image_file == "images/config.png":
            self.add_button(
                Button(60, 140, 260, 165, "Select Resort", lambda: screen_manager.set_screen(SelectCountryScreen(screen_manager, screen_manager.hill)))
            )
            self.add_button(
                Button(60, 175, 260, 200, "Config WiFi", lambda: screen_manager.set_screen(ConfigWiFiScreen(screen_manager, screen_manager.hill)))
            )
            self.add_button(
                Button(60, 210, 260, 230, "Set Alarm", lambda: screen_manager.set_screen(AlarmScreen(screen_manager, screen_manager.hill)))
            )

    def draw(self, draw_obj):
        img = self.bg_image.copy()
        draw = ImageDraw.Draw(img)

        if self.image_file == "images/config.png":
            font = _load_font(size=18)
            draw.text((73, 105), "Configuration", fill="white", font=font)
            draw.text((73, 140), "Select Resort", fill="white", font=font)
            draw.text((73, 175), "Config Wifi", fill="white", font=font)
            draw.text((73, 207), "Set Alarm", fill="white", font=font)

        if self.image_missing:
            font2 = ImageFont.load_default()
            msg = f"{os.path.basename(self.image_file)} not found"
            w, h = draw.textsize(msg, font=font2)
            draw.text(((device.width - w) // 2, (device.height - h) // 2), msg, fill="white", font=font2)

        for btn in self.buttons:
            btn.draw(draw)

        if hasattr(self.screen_manager, "overlay"):
            self.screen_manager.overlay.update_base(img)

        present(img) # do NOT call device.display(img) directly anymore


class UpdateScreen(Screen):
    def __init__(self, screen_manager, hill):
        super().__init__()
        self.screen_manager = screen_manager
        self.hill = hill

        # Read versions
        self.current_ver = get_local_version() or "0.0.0"
        self.latest_ver = get_remote_version() or self.current_ver

        def _noop_update():
            print("[Update] Currently installed version is up to date.")
            show_popup_message("Already up to date", duration=3)

        def _do_update():
            print("[Update] Newer version found. Updating...")
            if _is_systemd():
                # Hand off to systemd transient unit; the UI will be stopped/restarted by systemd.
                show_popup_message("UpdatingÃ¢â‚¬Â¦", duration=3)
                ok = update(self.latest_ver)
                if not ok:
                    show_popup_message("Update Failed", duration=3)
                # Whether we see the next line depends on timing, but it's harmless either way:
                self.screen_manager.set_screen(MainMenuScreen(self.screen_manager, self.screen_manager.hill))
            else:
                # Fallback when not running under systemd (e.g., dev box or manual run)
                ok = update(self.latest_ver)
                if ok:
                    show_popup_message("Update Complete", duration=3)
                    self.screen_manager.set_screen(MainMenuScreen(self.screen_manager, self.screen_manager.hill))
                else:
                    show_popup_message("Update Failed", duration=3)

        # Decide which action to expose on the UPDATE button
        try:
            if version.parse(self.latest_ver) > version.parse(self.current_ver):
                self.update_function = _do_update
            else:
                self.update_function = _noop_update
        except Exception:
            # If version parsing fails, fall back to no-op (non-crashing)
            self.update_function = _noop_update

        # Background
        try:
            self.bg_image = Image.open("images/update.png").convert("RGB").resize((device.width, device.height))
            self.image_missing = False
        except FileNotFoundError:
            print("Ã¢Å¡Â Ã¯Â¸Â images/update.png not found. Using black background.")
            self.bg_image = Image.new("RGB", (device.width, device.height), "black")
            self.image_missing = True

        print(f"[Update] Current Version: {self.current_ver}")
        print(f"[Update] Latest Version: {self.latest_ver}")

        # Buttons
        self.add_button(Button(43, 205, 280, 235, "UPDATE", self.update_function, visible=False))
        self.add_button(Button(290, 210, 316, 237, "Back",
                               lambda: screen_manager.set_screen(MainMenuScreen(screen_manager, screen_manager.hill)),
                               visible=False))

    def draw(self, draw_obj):
        img = self.bg_image.copy()
        draw = ImageDraw.Draw(img)

        font = _load_font(size=20)
        draw.text((125, 123), f"{self.current_ver}", fill="white", font=font)
        draw.text((125, 168), f"{self.latest_ver}", fill="white", font=font)

        for btn in self.buttons:
            btn.draw(draw)

        if hasattr(self.screen_manager, "overlay"):
            self.screen_manager.overlay.update_base(img)

        present(img)  # use presenter wrapper



class ScreenManager:
    def __init__(self):
        self.current = None

    def set_screen(self, screen):
        if isinstance(self.current, SnowReportScreen) and hasattr(self, "overlay"):
            self.overlay.on_exit()

        self.current = screen

        if isinstance(screen, SnowReportScreen) and hasattr(self, "overlay"):
            self.overlay.on_enter(present)

        self.redraw()
    def draw(self, draw_obj):
        if self.current:
            self.current.draw(draw_obj)

    def handle_touch(self, x, y):
        if self.current:
            self.current.handle_touch(x, y)
            self.redraw()

    def redraw(self):
        img = Image.new("RGB", (device.width, device.height), "black")
        draw = ImageDraw.Draw(img)

        self.draw(draw)


# ----------------------------
# Main
# ----------------------------
def main():
    global device, hill

    try:
        ensure_journald_volatile()
    except Exception as e:
        logger.warning("Failed to enforce volatile journald storage: %s", e)

    # Init display (guarded) & splash
    init_display()

    # Intialize touchscreen
    touch = None
    try:
        touch = XPT2046(spi_bus=0, spi_device=1, penirq_gpio=22)
    except Exception as e:
        print(f"Ã¢Å¡Â Ã¯Â¸Â Touch init failed: {e}")
        touch = None
    calibrator = TouchCalibrator()

    # Start heartbeat
    heartbeat_thread = threading.Thread(target=heartbeat, daemon=True)
    heartbeat_thread.start()

    # Splash
    try:
        splash = Image.open("images/splashlogo.png").convert("RGB").resize((device.width, device.height))
        splash = _draw_version_badge(splash, get_local_version())
        device.display(splash)
        leds_rainbow_splash(duration_sec=3.0)  # fades in over the 2s splash, then turns LEDs off

    except FileNotFoundError:
        print("Ã¢Å¡Â Ã¯Â¸Â images/splashlogo.png not found; skipping splash.")

    try:
        calib_ok = False
        try:
            calib_ok = calibrator.load_safe()
        except Exception as e:
            print(f"[Calib] Unexpected error loading calibration: {e}")
            calibrator.reset_defaults()

        if not calib_ok:
            print("[Calib] Starting on-device calibration.")
            try:
                calib_ok = run_touch_calibration(calibrator, touch)
            except Exception as e:
                print(f"[Calib] Interactive calibration failed: {e}")
                calib_ok = False

        if not calib_ok:
            print("[Calib] Proceeding with default calibration; touch accuracy may be reduced.")

        # Build global hill instance
        reload_hill()  # sets the global 'hill'

        if DEV_MODE:
            hill.newSnow = 10
            hill.weekSnow = 20
            hill.baseSnow = 187


        last_fetch = 0
        FETCH_PERIOD = 10 * 60  # 10 minutes

        screen_manager = ScreenManager()
        screen_manager.hill = hill
        screen_manager.overlay = overlay
        screen_manager.set_screen(MainMenuScreen(screen_manager, screen_manager.hill))

        while True:
            try:
                current_snow_cm = getattr(main, "_prev_snow_cm", 0)

                if touch:
                    try:
                        coord = touch.read_touch()
                    except Exception:
                        logger.exception("Touch read failed.")
                        coord = None
                    if coord:
                        try:
                            mapped = calibrator.map_raw_to_screen(*coord)
                            if VERBOSE:
                                print(f"Touch @ {mapped}")
                            screen_manager.handle_touch(*mapped)
                        except Exception:
                            active_screen = getattr(screen_manager, "current", None)
                            screen_name = type(active_screen).__name__ if active_screen else "None"
                            logger.exception(
                                "Touch dispatch failed (screen=%s, raw=%s)",
                                screen_name,
                                coord,
                            )

                now_ts = time.time()
                if now_ts - last_fetch > FETCH_PERIOD:
                    try:
                        if not DEV_MODE:
                            hill.getSnow()
                        last_fetch = now_ts
                        print(f"[Snow] {hill.name}: 24h new = {hill.newSnow}")
                    except Exception as e:
                        print(f"[Snow] Fetch failed: {e}")

                    # Refresh the screen so SnowReportScreen shows the latest values
                    try:
                        screen_manager.redraw()
                    except Exception:
                        logger.exception("Screen redraw failed.")

                    try:
                        sn = hill.newSnow
                        if isinstance(sn, str):
                            sn = _safe_int(sn)
                        current_snow_cm = int(sn)

                        prev = getattr(main, "_prev_snow_cm", None)

                        # First run: initialize LEDs once
                        if prev is None:
                            main._prev_snow_cm = current_snow_cm
                            leds_set_snow(current_snow_cm, current_snow_cm)

                        # Subsequent runs: only react when value changes
                        elif current_snow_cm != prev:
                            print(f"[Snow] Change detected: {prev} -> {current_snow_cm}")

                            # Snowfall overlay trigger/stop
                            if current_snow_cm > prev and hasattr(screen_manager, "overlay"):
                                screen_manager.overlay.trigger(current_snow_cm - prev)
                            elif hasattr(screen_manager, "overlay"):
                                screen_manager.overlay.stop()

                            # Update LEDs based on this change
                            leds_set_snow(current_snow_cm, prev)

                            main._prev_snow_cm = current_snow_cm

                    except Exception:
                        current_snow_cm = 0

                try:
                    check_and_trigger_alarm(current_snow_cm)
                except Exception as e:
                    print(f"[Alarm] check failed: {e}")

                time.sleep(0.1)
            except Exception:
                logger.exception("Main loop error; continuing after backoff.")
                time.sleep(0.5)

    except KeyboardInterrupt:
        print("Exiting.")

    finally:
        try:
            stop_powder_day_anthem()
        finally:
            _teardown_buzzer()
            leds_clear()
            try:
                if touch:
                    touch.close()
            except Exception:
                pass


if __name__ == "__main__":
    # If in demo mode, run LEDs with fake inputs and exit early.
    try:
        # Make sure LED hardware is ready for the demo:
        _ = _leds  # ensure class constructed
        if leds_demo_from_cli():
            sys.exit(0)
    except Exception:
        pass

    def _run_with_restart(max_restarts=3, backoff_base=5.0):
        attempts = 0
        while True:
            try:
                main()
                return
            except KeyboardInterrupt:
                raise
            except Exception:
                attempts += 1
                logger.exception(
                    "Fatal error in main; restarting (attempt %s/%s)",
                    attempts,
                    max_restarts,
                )
                try:
                    stop_powder_day_anthem()
                    _teardown_buzzer()
                    leds_clear()
                except Exception:
                    pass
                if attempts >= max_restarts:
                    logger.error("Max restart attempts reached; giving up.")
                    break
                time.sleep(min(backoff_base * attempts, 30.0))

    # normal program startup continues here ...
    _run_with_restart()



