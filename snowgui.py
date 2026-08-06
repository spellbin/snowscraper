"""Snow Scraper touchscreen entry point and screen definitions.

Run this file directly on the Raspberry Pi to start the appliance UI.  Cohesive
non-visual concerns live in ``snowscraper_app``:

* ``alarms`` owns alarm persistence and GPIO buzzer playback;
* ``avalanche`` normalizes Avalanche Canada, NWAC, and CAIC forecasts;
* ``brightness`` owns the shared LCD/LED brightness profile;
* ``health`` owns optional anonymous backend reporting and its local preference;
* ``leds`` owns WS2812 rendering and worker threads;
* ``resorts`` owns resort selection and snow-history persistence;
* ``storage`` provides atomic configuration writes; and
* ``system`` owns journald, heartbeat, release, and systemd integration.

This module intentionally retains display composition, touch calibration,
screen transitions, and the main event loop.  Those pieces share the display
and active-hill lifecycle closely, and keeping them together preserves the
original initialization and redraw order used by the physical device.
"""

import time
import datetime
import threading
import json
import os
import spidev

# GPIO is optional on development machines.  The touch controller uses this
# guarded handle for its active-low PENIRQ input; alarm GPIO is encapsulated in
# snowscraper_app.alarms and performs the same guarded import independently.
try:
    import RPi.GPIO as GPIO
    _HAS_GPIO = True
except Exception:
    _HAS_GPIO = False
import subprocess
import requests
import re
import sys, logging
import textwrap
from logging.handlers import RotatingFileHandler
from functools import lru_cache
from pathlib import Path
from packaging import version
from PIL import Image, ImageDraw, ImageFont, ImageEnhance, ImageFilter
from luma.core.interface.serial import spi
from luma.lcd.device import ili9341

# Application subsystems live in a package while this file remains the stable
# executable entry point used by operators and the systemd service.
from snowscraper_app.brightness import (
    BRIGHTNESS_CONF_FILE,
    BRIGHTNESS_LEVELS,
    BrightnessState,
    _read_brightness_index,
    _write_brightness_index,
    brightness_state,
)
from snowscraper_app.storage import (
    atomic_write_json as _atomic_write_json,
    atomic_write_text as _atomic_write_text,
)
from snowscraper_app.alarms import (
    ALARM_CONF_FILE,
    BUZZER_PIN,
    NOTES,
    _default_alarm_cfg,
    _play_melody_blocking,
    _setup_buzzer,
    _teardown_buzzer,
    check_and_trigger_alarm,
    load_alarm_cfg,
    reset_state_if_new_day,
    save_alarm_cfg,
    start_powder_day_anthem,
    stop_powder_day_anthem,
)
from snowscraper_app.leds import (
    LED_BRIGHTNESS_MAX,
    LED_CHANNEL,
    LED_COUNT,
    LED_DMA,
    LED_FREQ_HZ,
    LED_INVERT,
    LED_PIN,
    SnowLEDs,
    _leds,
    leds_clear,
    leds_demo_from_cli,
    leds_demo_sequence,
    leds_rainbow_splash,
    leds_set_brightness,
    leds_set_snow,
)
from snowscraper_app.system import (
    GITHUB_TOKEN,
    HEARTBEAT_FILE,
    HEARTBEAT_INTERVAL,
    HEARTBEAT_RAM_FILE,
    JOURNALD_DROPIN_DIR,
    JOURNALD_VOLATILE_CONF,
    JOURNALD_VOLATILE_CONTENT,
    LOCAL_REPO_PATH,
    MAX_RETRIES,
    REPO_URL,
    RETRY_DELAY,
    SERVICE_NAME,
    UPDATER_UNIT,
    VERSION_FILE,
    _ensure_git_safe_dir,
    _ensure_heartbeat_symlink,
    _is_root,
    _is_systemd,
    _read_effective_journald_storage,
    _systemd_run_update,
    _update_inline_git_checkout,
    _write_journald_volatile_dropin,
    create_github_session,
    ensure_journald_volatile,
    get_local_version,
    get_remote_version,
    heartbeat,
    update,
)
from snowscraper_app.health import health_reporter
from snowscraper_app.avalanche import (
    AVY_HEADERS,
    AVY_POINT_URL,
    CAIC_CENTER_ID,
    CAIC_PRODUCTS_LIMIT,
    CAIC_RESORTS,
    NWAC_API_BASE,
    NWAC_CENTER_ID,
    NWAC_DANGER_TEXT,
    NWAC_RESORTS,
    RESORT_META_FILE,
    _coerce_float,
    _extract_danger,
    _extract_issue,
    _extract_nwac_danger,
    _extract_summary,
    _fetch_caic_forecast,
    _fetch_center_products,
    _fetch_nwac_forecast,
    _fetch_point_forecast,
    _fetch_resort_forecast,
    _get_center_products,
    _get_resort_point,
    _html_to_text,
    _normalize_resort_meta,
    _parse_iso_dt,
    _parse_simple_yaml,
    _pick_latest_caic_product_id,
    _pick_latest_nwac_product_id,
)
from snowscraper_app.resorts import (
    ALL_COUNTRIES_LABEL,
    ALL_REGIONS_LABEL,
    ALL_RESORTS_LABEL,
    COUNTRY_CONF_FILE,
    OTHER_COUNTRY_LABEL,
    OTHER_REGION_LABEL,
    REGION_CONF_FILE,
    SNOW_LOG_FILE,
    _load_resort_meta,
    _load_resort_json,
    _read_selected_country,
    _read_selected_region,
    _read_selected_resort_index,
    _resort_slug,
    _write_selected_country,
    _write_selected_region,
    _write_selected_resort_index,
    cycle_resort_in_active_region,
    current_resort_name,
    fetch_snow_history,
    get_active_resorts,
    get_countries,
    get_regions,
    get_resort_names,
    log_snow_data,
    set_current_resort_by_name,
    skiHill,
    snow_history_url,
)
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
VERBOSE = False # set True for extra console logging ie. each touch read
CALIBRATION_FILE = "/home/pi/snowscraper/conf/touch_calibration.json"
DEV_MODE = False  # set True to avoid hitting live scrapers
print(f"[BOOT] DEV_MODE = {DEV_MODE}")



# ---- Global hill singleton ---------------------------------
hill = None  # skiHill instance; refreshed when skihill.conf changes

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
    """Line-buffered stream adapter that routes legacy print calls to logging.

    Much of the original UI reports status with ``print``.  Redirecting stdout
    and stderr through this adapter keeps those messages visible both on the
    console and in the rotating log without rewriting every call site.
    """

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


def _snow_cm_text(value) -> str:
    """Format a Snow API measurement without turning unavailable into zero."""
    if value is None:
        return "N/A"
    try:
        return f"{int(round(float(value)))}cm"
    except (TypeError, ValueError):
        return "N/A"


def _load_font(path="fonts/pixem.otf", size=18):
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        print(f"Ã¢Å¡Â Ã¯Â¸Â {path} not found. Using default font.")
        return ImageFont.load_default()


# ----------------------------
# Alarm config
# ----------------------------
# ----------------------------
# Update logic (GitHub)
# ----------------------------
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


# ----------------------------
# Display init (guarded)
# ----------------------------
class _DummyDevice:
    """Development fallback matching the small display API used by the UI."""

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


# ----------------------------
# skiHill scraper
# ----------------------------

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
    """Persist and apply raw XPT2046 bounds for the 320x240 display.

    The touch controller reports 12-bit ADC coordinates.  Calibration stores
    the observed extrema, maps them into display pixels, reverses both axes to
    match the panel mounting, and clamps the result to valid screen bounds.
    """

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
    """Rectangular touch target with an optional visible debug-style control."""

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
    """Base screen that owns buttons and dispatches touches to their callbacks."""

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
    """On-device keyboard used to collect SSIDs, passwords, and short text.

    Letter/symbol and case changes rebuild the same button grid.  Submission
    calls the supplied callback and returns to ``previous_screen`` on the shared
    screen manager.
    """

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
    """Image-backed home screen and navigation hub for all appliance features."""

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
    - Uses the Snow API 30-day history endpoint for the selected resort.
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
        """Return the canonical Snow API history URL for status logging."""
        name = (getattr(self.hill, "name", "") or "").strip() or "Sun Peaks"
        return snow_history_url(name)

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
            payload = fetch_snow_history(self.hill.name)
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

            # History labels are daily/24h values. The Snow API contract says
            # daySnow is authoritative; newSnow is only a legacy-row fallback.
            new24 = _safe_int(
                pick(["daySnow", "newSnow", "new_24", "snow_24h", "24h"], 0)
            )
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
        try:
            self.forecast = _fetch_resort_forecast(self.resort_name, self.point)
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
        try:
            self.forecast = _fetch_resort_forecast(self.resort_name, self.point)
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
    """Current-resort snowfall summary with links to history and navigation.

    The screen reads the shared hill object populated by the main fetch loop,
    updates the LED ring to the displayed value, and renders the canonical 24h,
    seven-day, and base-depth measurements.
    """

    def __init__(self, screen_manager, hill):
        super().__init__()
        self.screen_manager = screen_manager
        self.hill = hill
        try:
            print(f"[SnowReport] Refreshing data for {self.hill.name}...")
            self.hill.getSnow()
            if self.hill.newSnow is None:
                leds_clear()
            else:
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

        # Preserve Snow API null as N/A. A verified zero still renders as 0cm.
        new_cm = _snow_cm_text(h.newSnow)
        week_cm = _snow_cm_text(h.weekSnow)
        base_cm = _snow_cm_text(h.baseSnow)

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
        draw.text((x, 115), f"New  Snow: {new_cm}",  fill="white", font=font_line)
        draw.text((x, 144), f"Week Snow: {week_cm}", fill="white", font=font_line)
        draw.text((x, 173), f"Base Snow: {base_cm}", fill="white", font=font_line)

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
    """First step of resort filtering: choose a country or show all countries."""

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
    """Second resort-filter step, scoped to the persisted country selection."""

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
    """Final resort picker that persists the metadata-order resort index."""

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
    """Scan for Wi-Fi networks and collect credentials with the touch keyboard.

    This screen performs privileged appliance configuration.  It retains the
    original wpa_supplicant file format and asynchronous reconfigure behavior.
    """

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
    """Edit timed and incremental powder-day alarm settings.

    Values are loaded from the shared alarm cache, edited through touch controls,
    and persisted through the alarm subsystem without blocking the render loop.
    """

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


class AnonymousHealthScreen(Screen):
    """Customer-facing control for optional pseudonymous health reporting.

    The preference is entirely local. Turning sharing off stops future reports
    immediately and does not change snow data, alarms, LEDs, updates, or the
    disk-based watchdog heartbeat.
    """

    def __init__(self, screen_manager, hill):
        super().__init__()
        self.screen_manager = screen_manager
        self.hill = hill
        try:
            self.bg_image = Image.open("images/config.png").convert("RGB").resize(
                (device.width, device.height)
            )
            self.image_missing = False
        except FileNotFoundError:
            self.bg_image = Image.new("RGB", (device.width, device.height), "black")
            self.image_missing = True

        # The complete centre bar is a generous 200×32 px touch target. The
        # lower-right artwork retains the same Back target as other config pages.
        self.add_button(Button(60, 132, 260, 168, "Toggle anonymous health", self._toggle))
        self.add_button(Button(
            270, 190, 300, 220,
            "Back",
            lambda: screen_manager.set_screen(
                ImageScreen("images/config.png", screen_manager, screen_manager.hill)
            ),
        ))

    def _toggle(self):
        enabled = not health_reporter.reporting_enabled
        saved = health_reporter.set_reporting_enabled(enabled)
        if saved:
            state = "on" if enabled else "off"
            show_popup_message(f"Anonymous health: {state}", duration=1.5)
        else:
            show_popup_message("Could not save preference", duration=2)
        self.screen_manager.set_screen(
            AnonymousHealthScreen(self.screen_manager, self.screen_manager.hill)
        )

    def draw(self, draw_obj):
        img = self.bg_image.copy()
        draw = ImageDraw.Draw(img)
        title_font = _load_font(size=17)
        body_font = _load_font(size=14)
        detail_font = _load_font(size=12)
        enabled = health_reporter.reporting_enabled

        draw.text((73, 105), "Anonymous Health", fill="white", font=title_font)
        draw.text(
            (73, 140),
            f"Sharing: {'ON' if enabled else 'OFF'}",
            fill="#8BE28B" if enabled else "#D8D8D8",
            font=body_font,
        )
        draw.text((73, 175), "No hostname or account", fill="white", font=detail_font)
        draw.text((73, 207), "Tap sharing to change", fill="white", font=detail_font)

        if self.image_missing:
            draw.text((8, 8), "config.png missing", fill="white", font=detail_font)
        present(img)


class ImageScreen(Screen):
    """Static image-backed submenu with invisible touch targets over its artwork."""

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
                Button(60, 202, 160, 232, "Set Alarm", lambda: screen_manager.set_screen(AlarmScreen(screen_manager, screen_manager.hill)))
            )
            self.add_button(
                Button(160, 202, 260, 232, "Privacy", lambda: screen_manager.set_screen(AnonymousHealthScreen(screen_manager, screen_manager.hill)))
            )

    def draw(self, draw_obj):
        img = self.bg_image.copy()
        draw = ImageDraw.Draw(img)

        if self.image_file == "images/config.png":
            font = _load_font(size=18)
            draw.text((73, 105), "Configuration", fill="white", font=font)
            draw.text((73, 140), "Select Resort", fill="white", font=font)
            draw.text((73, 175), "Config Wifi", fill="white", font=font)
            # Split the final artwork slot so existing alarm access remains in
            # place while the optional reporting control is easy to discover.
            small_font = _load_font(size=14)
            draw.text((73, 207), "Alarm", fill="white", font=small_font)
            draw.text((168, 207), "Privacy", fill="white", font=small_font)

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
    """Compare local and GitHub release versions and launch a safe update.

    Version discovery happens on a worker thread so network latency does not
    freeze touch handling.  The system module selects systemd or inline Git
    update behavior according to the host environment.
    """

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
                show_popup_message("Updating...", duration=3)
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
    """Own the active screen, redraw it, and coordinate snowfall-overlay hooks."""

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
    """Initialize hardware and run the resilient 10 Hz touchscreen event loop.

    Snow data is refreshed every ten minutes.  Touch reads, screen redraws,
    alarms, LEDs, and the snowfall overlay all fail soft so a single peripheral
    or network error does not stop the appliance.  Cleanup in ``finally`` turns
    off PWM/LED output and closes the touch SPI handle on every exit path.
    """
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
            hill.daySnow = 10
            hill.weekSnow = 20
            hill.baseSnow = 187


        last_fetch = 0
        FETCH_PERIOD = 10 * 60  # 10 minutes
        FETCH_RETRY_PERIOD = 60  # avoid hammering the API during an outage

        screen_manager = ScreenManager()
        screen_manager.hill = hill
        screen_manager.overlay = overlay
        screen_manager.set_screen(MainMenuScreen(screen_manager, screen_manager.hill))

        while True:
            try:
                current_snow_cm = getattr(main, "_prev_snow_cm", None)

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
                        print(f"[Snow] {hill.name}: new snow = {hill.newSnow}")
                    except Exception as e:
                        print(f"[Snow] Fetch failed: {e}")
                        # Schedule a short retry without making every 10 Hz loop
                        # iteration hit the API while connectivity is down.
                        last_fetch = now_ts - (FETCH_PERIOD - FETCH_RETRY_PERIOD)

                    # Refresh the screen so SnowReportScreen shows the latest values
                    try:
                        screen_manager.redraw()
                    except Exception:
                        logger.exception("Screen redraw failed.")

                    try:
                        prev = getattr(main, "_prev_snow_cm", None)
                        sn = hill.newSnow

                        if sn is None:
                            current_snow_cm = None
                            if prev is not None:
                                print("[Snow] Current new-snow value is unavailable.")
                                if hasattr(screen_manager, "overlay"):
                                    screen_manager.overlay.stop()
                                leds_clear()
                            main._prev_snow_cm = None
                        else:
                            if isinstance(sn, str):
                                sn = _safe_int(sn)
                            current_snow_cm = int(sn)

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
                        current_snow_cm = None

                if current_snow_cm is not None:
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
