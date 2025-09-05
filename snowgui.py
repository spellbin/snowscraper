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
from logging.handlers import RotatingFileHandler
from pathlib import Path
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from packaging import version
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont
from luma.core.interface.serial import spi
from luma.lcd.device import ili9341

# ----------------------------
# Constants & Config
# ----------------------------
REPO_URL = "https://github.com/spellbin/snowscraper.git"
LOCAL_REPO_PATH = "/home/snowdev/snowgui"  # Replace with your local path
VERSION_FILE = os.path.join(LOCAL_REPO_PATH, "VERSION")  # Path to version file
MAX_RETRIES = 3
RETRY_DELAY = 5
VERBOSE = False # set True for extra console logging ie. each touch read
GITHUB_TOKEN = None  # Optional GitHub token for private repos
CALIBRATION_FILE = "./conf/touch_calibration.json"
HEARTBEAT_FILE = "heartbeat.txt"
ALARM_CONF_FILE = "./conf/alarm.conf"
HEARTBEAT_INTERVAL = 30  # seconds
DEV_MODE = True  # set True to avoid hitting live scrapers
SNOW_LOG_FILE = "logs/snow_log.json"

# ---- Global hill singleton ---------------------------------
hill = None  # skiHill instance; refreshed when skihill.conf changes

# --- Logging bootstrap (keep prints working, also log to file) ---
# Log file lives next to this script: ./logs/snowgui.log
_HERE = Path(__file__).resolve().parent
_LOG_DIR = _HERE / "logs"
_LOG_DIR.mkdir(parents=True, exist_ok=True)
_LOG_PATH = _LOG_DIR / "snowgui.log"

logger = logging.getLogger("snowgui")
logger.setLevel(logging.INFO)

_fmt = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")

# File handler (rotates at ~512 KB, keeps 3 backups)
_fh = RotatingFileHandler(_LOG_PATH, maxBytes=512*1024, backupCount=3)
_fh.setFormatter(_fmt)
_fh.setLevel(logging.INFO)

# Console handler (so you still see output when running interactively)
_sh = logging.StreamHandler(sys.__stdout__)
_sh.setFormatter(_fmt)
_sh.setLevel(logging.INFO)

# Avoid duplicate handlers if the module is reloaded
if not logger.handlers:
    logger.addHandler(_fh)
    logger.addHandler(_sh)

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
        """Set visual state for current snow. Breathing runs only if value changed."""
        cm_now = max(0, min(20, int(cm_now or 0)))
        cm_prev = max(0, min(20, int(cm_prev or 0)))
        with self._lock:
            self._current_cm = cm_now
            self._prev_cm = cm_prev
            self._base_color = self._color_for_cm(cm_now) if cm_now > 0 else (0, 0, 0)

        # Sparkle on heavy snowfall
        if cm_now > 15:
            self._start_sparkle()
        else:
            self._stop_sparkle()

        if cm_now <= 0:
            # off
            self._stop_breathe()
            self._paint_solid((0, 0, 0), 0.0)
            return

        if cm_now != cm_prev:
            # change detected -> start/refresh breathing
            delta = abs(cm_now - cm_prev)
            period = self._breath_period_for_delta(delta)
            self._start_breathe(period_sec=period)
        else:
            # unchanged -> steady, no breathing
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
        r, g, b = rgb
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
        # delta 1 -> slow (~8s), delta ≥10 -> fast (~1.5s)
        delta = max(1, min(10, int(delta)))
        return max(1.5, 8.0 - (delta - 1) * 0.73)

    # ----- sparkle worker (>15 cm) -----
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
        print(f"⚠️ {path} not found. Using default font.")
        return ImageFont.load_default()


# ----------------------------
# Alarm config
# ----------------------------
def load_alarm_cfg():
    """
    alarm.conf schema:
    {
      "active": bool,
      "active_anytime": bool,
      "hour": "HH",
      "minute": "MM",
      "triggered_snow": "int",
      "incremental_snow": "int",
      "state": {"day": "YYYY-MM-DD", "triggered_today": bool, "next_threshold": int|null}
    }
    """
    cfg = {
        "active": False,
        "active_anytime": False,
        "hour": "0",
        "minute": "0",
        "triggered_snow": "0",
        "incremental_snow": "0",
        "state": {"day": _today_str(), "triggered_today": False, "next_threshold": None},
    }
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
    return cfg


def save_alarm_cfg(cfg):
    try:
        with open(ALARM_CONF_FILE, "w") as f:
            json.dump(cfg, f)
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
    "D4": 294,
    "E4": 330,
    "F4": 349,
    "G4": 392,
    "A4": 440,
    "B4": 494,
    "C5": 523,
    "D5": 587,
    "E5": 659,
    "F5": 698,
    "G5": 784,
    "A5": 880,
    "REST": 0,
}

_CHORUS = [
    ("G4", 0.2),
    ("E4", 0.2),
    ("C4", 0.2),
    ("G4", 0.2),
    ("E4", 0.2),
    ("C4", 0.2),
    ("F4", 0.2),
    ("G4", 0.2),
    ("E4", 0.2),
    ("D4", 0.7),
    ("G4", 0.2),
    ("E4", 0.2),
    ("C4", 0.2),
    ("G4", 0.2),
    ("E4", 0.2),
    ("C4", 0.2),
    ("C5", 0.2),
    ("B4", 0.2),
    ("G4", 0.6),
]
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
            print("🎿 Powder Day Anthem (silent dev mode)")
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
    active: fire once at HH:MM if snow ≥ trigger (once per day)
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
            print(f"[Alarm] Timed trigger {hr:02d}:{mn:02d} | {current_snow_cm} ≥ {trig}")
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
            print(f"[Alarm] Anytime trigger | {current_snow_cm} ≥ {st['next_threshold']} (step {inc})")
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


def update(version_str: str) -> bool:
    if not version_str:
        return False
    try:
        if not os.path.exists(os.path.join(LOCAL_REPO_PATH, ".git")):
            print("Repository not found, cloning...")
            subprocess.run(
                ["git", "clone", REPO_URL, LOCAL_REPO_PATH],
                check=True,
                capture_output=True,
                text=True,
            )

        subprocess.run(
            ["git", "fetch", "--all", "--tags"],
            cwd=LOCAL_REPO_PATH,
            check=True,
            capture_output=True,
            text=True,
        )

        subprocess.run(
            ["git", "checkout", f"tags/{version_str}", "-f"],
            cwd=LOCAL_REPO_PATH,
            check=True,
            capture_output=True,
            text=True,
        )

        with open(VERSION_FILE, "w") as f:
            f.write(version_str)

        return True

    except subprocess.CalledProcessError as e:
        print(f"Update failed: {e.stderr}")
        return False
    except Exception as e:
        print(f"Error during update: {e}")
        return False


def heartbeat():
    while True:
        with open(HEARTBEAT_FILE, "w") as f:
            f.write(str(time.time()))
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
        print(f"⚠️ Display init failed ({e}); falling back to dummy device.")
        device = _DummyDevice()
        return device


def _read_selected_resort_index(path="conf/skihill.conf") -> int:
    try:
        with open(path, "r") as f:
            raw = f.read().strip()
        return max(0, int(raw))
    except Exception as e:
        print(f"[SelectResort] Could not read {path}: {e}. Using 0.")
        return 0


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
        with open(SNOW_LOG_FILE, "w") as f:
            json.dump(log_data, f, indent=2)
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

        if self.name == "Red Mountain":
            print("Hello my name is " + self.name)
            response = requests.get(self.url, timeout=10)
            response.raise_for_status()
            response_text = response.text

            patterns = {
                "24_hour": r'"Metric24hours":\s*(\d+)',
                "7_day": r'"Metric7days":\s*(\d+)',
                "base_depth": r'"MetricAlpineSnowDepth":\s*(\d+)',
            }

            snow_metrics = {}
            for metric, pattern in patterns.items():
                match = re.search(pattern, response_text)
                snow_metrics[metric] = int(match.group(1)) if match else 0

            self.newSnow = snow_metrics["24_hour"]
            self.weekSnow = snow_metrics["7_day"]
            self.baseSnow = snow_metrics["base_depth"]
            log_snow_data(self)

        if self.name == "Banff Sunshine":
            print("Hello my name is " + self.name)
            response = requests.get(self.url, timeout=10)
            if response.status_code == 200:
                soup = BeautifulSoup(response.text, "html.parser")
                sunshine_section = soup.find("div", class_="sv-print")
                if sunshine_section:
                    table = sunshine_section.find("table", class_="stats")
                    if table and table.find("tbody"):
                        cells = table.find("tbody").find_all("td")
                        if len(cells) >= 5:
                            overnight = _safe_int(cells[0].get_text(strip=True))
                            seven_day = _safe_int(cells[2].get_text(strip=True))
                            base = _safe_int(cells[3].get_text(strip=True))
                            self.newSnow = overnight
                            self.weekSnow = seven_day
                            self.baseSnow = base
                            log_snow_data(self)
                        else:
                            print("[Banff Sunshine] Snowfall data columns missing.")
                    else:
                        print("[Banff Sunshine] Snowfall table not found.")
                else:
                    print("[Banff Sunshine] Section not found.")
            else:
                print(f"[Banff Sunshine] HTTP Status: {response.status_code}")

        if self.name == "Lake Louise":
            print("Hello my name is " + self.name)
            response = requests.get(self.url, timeout=10)
            if response.status_code == 200:
                soup = BeautifulSoup(response.text, "html.parser")
                ll_section = soup.find("div", class_="ll-print")
                if ll_section:
                    table = ll_section.find("table", class_="stats")
                    if table and table.find("tbody"):
                        cells = table.find("tbody").find_all("td")
                        if len(cells) >= 5:
                            overnight = _safe_int(cells[0].get_text(strip=True))
                            seven_day = _safe_int(cells[2].get_text(strip=True))
                            base = _safe_int(cells[3].get_text(strip=True))
                            self.newSnow = overnight
                            self.weekSnow = seven_day
                            self.baseSnow = base
                            log_snow_data(self)
                        else:
                            print("[Lake Louise] Snowfall data columns missing.")
                    else:
                        print("[Lake Louise] Snowfall table not found.")
                else:
                    print("[Lake Louise] Section not found.")
            else:
                print(f"[Lake Louise] HTTP Status: {response.status_code}")

        if self.name == "Revelstoke":
            print("Hello my name is " + self.name)
            response = requests.get(self.url, timeout=10)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")

            section = soup.find("section", class_="snow-report__section")
            if not section:
                raise ValueError("Snow report section not found")

            new_snow_div = section.find("div", class_="snow-report__new")
            value = new_snow_div.find("span", class_="value").text.strip() if new_snow_div else "0"
            self.newSnow = _safe_int(value)

            amounts_container = section.find("div", class_="snow-report__amounts")
            amounts = amounts_container.find_all("div", class_="snow-report__amount") if amounts_container else []
            for amount in amounts:
                title = amount.find("h2", class_="snow-report__title")
                if not title:
                    continue
                title_text = title.text.strip()
                value_span = amount.find("span", class_="value")
                value = value_span.text.strip() if value_span else "0"

                if title_text == "Base Depth":
                    self.baseSnow = _safe_int(value)
                elif title_text == "7 days":
                    self.weekSnow = _safe_int(value)
            log_snow_data(self)

        if self.name == "Sun Peaks":
            print("Hello my name is " + self.name)
            response = requests.get(self.url, timeout=10)
            soup = BeautifulSoup(response.text, "html.parser")

            node = soup.find("span", class_="snow-new")
            self.newSnow = _safe_int(node.text.strip() if node else "0")

            node = soup.find("span", class_="snow-7")
            self.weekSnow = _safe_int(node.text.strip() if node else "0")

            values = []
            html_array = soup.find_all("span", class_="value_switch value_cm") or []
            for html_string in html_array:
                soup2 = BeautifulSoup(str(html_string), "html.parser")
                span_element = soup2.find("span", class_="value_switch value_cm")
                if span_element and span_element.text:
                    values.append(_safe_int(span_element.text.strip()))
            if len(values) >= 3:
                self.baseSnow = values[2]
            else:
                self.baseSnow = 0
            log_snow_data(self)

        if self.name == "Whistler":
            print("Hello my name is " + self.name)
            response = requests.get(self.url, timeout=10)
            if response.status_code == 200:
                soup = BeautifulSoup(response.content, "html.parser")
                script_tag = soup.find("script", string=re.compile(r"FR\.snowReportData\s*="))
                if script_tag and script_tag.string:
                    script_content = script_tag.string
                    match = re.search(r"FR\.snowReportData\s*=\s*({.*?});", script_content, re.DOTALL)
                    if match:
                        json_data = match.group(1)
                        snow_report_data = json.loads(json_data)
                        overnight_cm = snow_report_data.get("OvernightSnowfall", {}).get("Centimeters")
                        seven_day_cm = snow_report_data.get("SevenDaySnowfall", {}).get("Centimeters")
                        base_depth_cm = snow_report_data.get("BaseDepth", {}).get("Centimeters")
                        self.newSnow = _safe_int(overnight_cm)
                        self.weekSnow = _safe_int(seven_day_cm)
                        self.baseSnow = _safe_int(base_depth_cm)
                        log_snow_data(self)
                    else:
                        print("[Whistler] Failed to extract JSON data from the script tag.")
                else:
                    print("[Whistler] Script tag containing snow report data not found.")
            else:
                print(f"[Whistler] HTTP Status: {response.status_code}")

        if self.name == "Big White":
            print("Hello my name is " + self.name)
            response = requests.get(self.url, timeout=10)
            soup = BeautifulSoup(response.text, "html.parser")

            span_elements = soup.find_all("span", class_="bigger-font")
            if span_elements:
                for index, span in enumerate(span_elements, start=1):
                    text = span.text.replace("&nbsp;", " ")
                    if index == 5:
                        self.newSnow = _safe_int(text)
                    elif index == 7:
                        self.baseSnow = _safe_int(text)

            big_font_elements = soup.find_all(class_="big-font")
            if big_font_elements:
                for index, element in enumerate(big_font_elements, start=1):
                    text = element.text.replace("&nbsp;", " ")
                    if index == 2:
                        self.weekSnow = _safe_int(text)
            log_snow_data(self)


def create_selected_hill():
    names = [
        "Sun Peaks",
        "Silver Star",
        "Big White",
        "Whistler",
        "Revelstoke",
        "Kicking Horse",
        "Lake Louise",
        "Banff Sunshine",
        "Red Mountain",
        "White Water",
    ]
    urls = {
        "Sun Peaks": "https://www.sunpeaksresort.com/snow-report",
        "Silver Star": "https://www.skisilverstar.com/conditions/",
        "Big White": "https://www.bigwhite.com/conditions/snow-report",
        "Whistler": "https://www.whistlerblackcomb.com/the-mountain/mountain-conditions/snow-and-weather-report.aspx",
        "Revelstoke": "https://www.revelstokemountainresort.com/mountain-report",
        "Kicking Horse": "https://kickinghorseresort.com/conditions/snow-report/",
        "Lake Louise": "https://www.skilouise.com/conditions-and-weather/",
        "Banff Sunshine": "https://www.skibanff.com/conditions",
        "Red Mountain": "https://api.redresort.com/snowreport",
        "White Water": "https://skiwhitewater.com/snow-report/",
    }
    idx = max(0, min(_read_selected_resort_index(), len(names) - 1))
    name = names[idx]
    return skiHill(name=name, url=urls.get(name, ""), newSnow=0, weekSnow=0, baseSnow=0)

def reload_hill():
    """Refresh the global hill from skihill.conf."""
    global hill
    hill = create_selected_hill()
    print(f"[Hill] Reloaded: {hill.name}")
    return hill


# ----------------------------
# Wi‑Fi helpers
# ----------------------------
def get_available_ssids():
    try:
        result = subprocess.run(
            ["sudo", "wpa_cli", "-i", "wlan0", "scan_results"],
            capture_output=True,
            text=True,
            check=True,
        )
        ssids = []
        lines = result.stdout.splitlines()
        for line in lines[1:]:
            parts = line.split("\t")
            if len(parts) >= 5:
                ssid = parts[4].strip()
                if ssid:
                    ssids.append(ssid)
        return ssids
    except subprocess.CalledProcessError as e:
        print(f"Error running wpa_cli: {e}")
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
    def __init__(self, spi_bus=0, spi_device=1):
        self.spi = spidev.SpiDev()
        self.spi.open(spi_bus, spi_device)
        self.spi.max_speed_hz = 500000
        self.spi.mode = 0b00

    def _read_channel(self, cmd):
        response = self.spi.xfer2([cmd, 0x00, 0x00])
        value = ((response[1] << 8) | response[2]) >> 4
        return value

    def read_touch(self, samples=5, tolerance=50):
        readings = []
        for _ in range(samples):
            raw_y = self._read_channel(0xD0)
            raw_x = self._read_channel(0x90)
            if 100 < raw_x < 4000 and 100 < raw_y < 4000:
                readings.append((raw_x, raw_y))
            time.sleep(0.01)

        if len(readings) < 3:
            return None

        xs, ys = zip(*readings)
        if max(xs) - min(xs) > tolerance or max(ys) - min(ys) > tolerance:
            return None

        avg_x = sum(xs) // len(xs)
        avg_y = sum(ys) // len(ys)
        return (avg_x, avg_y)

    def close(self):
        self.spi.close()


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
            print("⚠️ Calibration file invalid; resetting to defaults.")
            self.x_min, self.y_min, self.x_max, self.y_max = 0, 0, 4095, 4095
            return False
        return True


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
                btn.on_press()


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

        shift_label = "[↑]" if not self.shift else "[↓]"
        self.add_button(Button(70, 160, 125, 190, shift_label, self._toggle_shift, visible=True))

        self.add_button(Button(130, 160, 220, 190, "Space", lambda: self._append_char(" "), visible=True))
        self.add_button(Button(225, 160, 270, 190, "←", self._backspace, visible=True))
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
        device.display(img)


class MainMenuScreen(Screen):
    def __init__(self, screen_manager, hill):
        super().__init__()
        self.screen_manager = screen_manager
        self.hill = hill
        try:
            self.bg_image = Image.open("images/mainmenu.png").convert("RGB").resize((device.width, device.height))
        except FileNotFoundError:
            print("⚠️ images/mainmenu.png not found. Using black background.")
            self.bg_image = Image.new("RGB", (device.width, device.height), "black")

        self.add_button(Button(60, 100, 260, 130, "Mountain Report", lambda: screen_manager.set_screen(SnowReportScreen(screen_manager, screen_manager.hill))))
        self.add_button(Button(60, 140, 260, 165, "Avy Conditions", lambda: screen_manager.set_screen(ImageScreen("images/aconditions.png", screen_manager, screen_manager.hill))))
        self.add_button(Button(60, 175, 260, 200, "Config", lambda: screen_manager.set_screen(ImageScreen("images/config.png", screen_manager, screen_manager.hill))))
        self.add_button(Button(60, 210, 260, 230, "Update", lambda: screen_manager.set_screen(UpdateScreen(screen_manager, screen_manager.hill)), visible=False))

    def draw(self, draw_obj):
        device.display(self.bg_image.copy())

class SnowReportScreen(Screen):
    def __init__(self, screen_manager, hill):
        super().__init__()
        self.screen_manager = screen_manager
        self.hill = hill
        try:
            self.bg_image = Image.open("images/mreport.png").convert("RGB").resize((device.width, device.height))
            self.image_missing = False
        except FileNotFoundError:
            print("⚠️ images/mreport.png not found. Using black background.")
            self.bg_image = Image.new("RGB", (device.width, device.height), "black")
            self.image_missing = True

        # Back button (invisible hitbox as with others)
        self.add_button(
            Button(270, 190, 300, 220, "Back", lambda: screen_manager.set_screen(MainMenuScreen(screen_manager, screen_manager.hill)), visible=False)
        )

    def draw(self, draw_obj):
        img = self.bg_image.copy()
        draw = ImageDraw.Draw(img)
        h = self.screen_manager.hill

        # Fonts
        font_title = _load_font("fonts/superpixel.ttf", size=30)
        font_line  = _load_font("fonts/ponderosa.ttf", size=16)

        # Normalize numbers just in case they’re strings
        new_cm   = _safe_int(h.newSnow)
        week_cm  = _safe_int(h.weekSnow)
        base_cm  = _safe_int(h.baseSnow)

        # Text block (tweak positions to taste)
        x = 55
        line_h = 26

        draw.text((x, 55), f"{h.name}", fill="white", font=font_title)
        draw.text((x, 115), f"24hr Snow: {new_cm}cm",  fill="white", font=font_line)
        draw.text((x, 144), f"Week Snow: {week_cm}cm", fill="white", font=font_line)
        draw.text((x, 173), f"Base Snow: {base_cm}cm", fill="white", font=font_line)

        if self.image_missing:
            f2 = ImageFont.load_default()
            msg = "images/mreport.png not found"
            w, h = draw.textsize(msg, font=f2)
            draw.text(((device.width - w) // 2, (device.height - h) // 2), msg, fill="white", font=f2)

        for btn in self.buttons:
            btn.draw(draw)

        device.display(img)

class SelectResortScreen(Screen):
    def __init__(self, screen_manager, hill):
        super().__init__()
        self.screen_manager = screen_manager
        self.hill = hill
        self.skiHills = [
            "Sun Peaks",
            "Silver Star",
            "Big White",
            "Whistler",
            "Revelstoke",
            "Kicking Horse",
            "Lake Louise",
            "Banff Sunshine",
            "Red Mountain",
            "White Water",
        ]
        self.current_index = 0

        try:
            self.bg_image = Image.open("images/select_resort.png").convert("RGB").resize((device.width, device.height))
            self.image_missing = False
        except FileNotFoundError:
            print("⚠️ images/select_resort.png not found. Using black background.")
            self.bg_image = Image.new("RGB", (device.width, device.height), "black")
            self.image_missing = True

        self.add_button(
            Button(270, 190, 300, 220, "Back", lambda: screen_manager.set_screen(ImageScreen("images/config.png", screen_manager, screen_manager.hill)), visible=False)
        )
        self.add_button(Button(272, 108, 298, 135, "Up", self.scroll_up, visible=False))
        self.add_button(Button(272, 140, 298, 165, "Down", self.scroll_down, visible=False))
        self.add_button(Button(60, 175, 260, 200, "SelectCurrent", self.confirm_selection, visible=False))

    def confirm_selection(self):
        index = self.current_index
        selected = self.skiHills[index]
        try:
            with open("conf/skihill.conf", "w") as f:
                f.write(str(index))
            print(f"[SelectResort] Selected: '{selected}' (index {index}) saved to skihill.conf")
            # --- NEW: refresh the global hill and ScreenManager’s reference
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
        if self.current_index > 0:
            draw.text((73, 140), self.skiHills[self.current_index - 1], fill="gray", font=font)
        draw.text((73, 175), self.skiHills[self.current_index], fill="white", font=font)
        if self.current_index < len(self.skiHills) - 1:
            draw.text((73, 207), self.skiHills[self.current_index + 1], fill="gray", font=font)

        for btn in self.buttons:
            btn.draw(draw)

        device.display(img)


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
            print("⚠️ images/config_wifi.png not found. Using black background.")
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
        try:
            with open("wpa_supplicant.conf", "w") as f:
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
        self.screen_manager.set_screen(ImageScreen("images/config.png", self.screen_manager, self.screen_manager.hill))

    def draw(self, draw_obj):
        img = self.bg_image.copy()
        draw = ImageDraw.Draw(img)
        font = _load_font(size=18)
        draw.text((73, 105), "Wifi SSID", fill="white", font=font)
        if self.ssid_list:
            draw.text((73, 140), self.ssid_list[self.current_index][:14], fill="white", font=font)
        draw.text((73, 175), "PASSWORD", fill="white", font=font)
        draw.text((73, 207), f"{self.password}", fill="white", font=font)

        for btn in self.buttons:
            btn.draw(draw)

        device.display(img)


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
            print("⚠️ images/misc.png not found. Using black background.")
            self.bg_image = Image.new("RGB", (device.width, device.height), "black")
            self.image_missing = True

        try:
            self.inactive_img = Image.open("images/InactiveButtonSmall.png").convert("RGB").resize((40, 20))
        except FileNotFoundError:
            print("⚠️ images/InactiveButtonSmall.png not found. No inactive visual will be drawn.")
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
            self._show_error("Hour must be 0–23")

    def set_minute(self, text):
        if text.isdigit() and 0 <= int(text) <= 59:
            self.minute = text
            self._save_from_fields()
        else:
            self._show_error("Minute must be 0–59")

    def set_triggered_snow(self, text):
        if text.isdigit() and 1 <= int(text) <= 100:
            self.triggered_snow = text
            self._save_from_fields()
        else:
            self._show_error("Triggered snow must be 1–100")

    def set_incremental_snow(self, text):
        if text.isdigit() and 1 <= int(text) <= 20:
            self.incremental_snow = text
            self._save_from_fields()
        else:
            self._show_error("Incremental snow must be 1–20")

    def draw(self, draw_obj):
        img = self.bg_image.copy()
        draw = ImageDraw.Draw(img)
        font18 = _load_font(size=18)
        font32 = _load_font(size=32)
        font16 = _load_font(size=16)

        draw.text((68, 110), "Alarm Settings", fill="white", font=font18)
        draw.text((68, 135), f"{self.hour}", fill="white", font=font32)
        draw.text((120, 135), f"{self.minute}", fill="white", font=font32)
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

        device.display(img)


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
            print(f"⚠️ {image_file} not found. Using black background.")
            self.bg_image = Image.new("RGB", (device.width, device.height), "black")
            self.image_missing = True

        self.add_button(
            Button(270, 190, 300, 220, "Back", lambda: screen_manager.set_screen(MainMenuScreen(screen_manager, screen_manager.hill)), visible=False)
        )

        if image_file == "images/config.png":
            self.add_button(
                Button(60, 140, 260, 165, "Select Resort", lambda: screen_manager.set_screen(SelectResortScreen(screen_manager, screen_manager.hill)))
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

        device.display(img)


class UpdateScreen(Screen):
    def __init__(self, screen_manager, hill):
        super().__init__()
        self.screen_manager = screen_manager
        self.hill = hill
        self.current_ver = get_local_version() or "0.0.0"
        self.latest_ver = get_remote_version() or self.current_ver

        def _noop_update():
            print("[Update] Currently installed version is up to date.")

        try:
            if version.parse(self.latest_ver) > version.parse(self.current_ver):
                self.update_function = lambda: (print("[Update] Newer version found. Updating..."), update(self.latest_ver))[1]
            else:
                self.update_function = _noop_update
        except Exception:
            self.update_function = _noop_update

        try:
            self.bg_image = Image.open("images/update.png").convert("RGB").resize((device.width, device.height))
            self.image_missing = False
        except FileNotFoundError:
            print("⚠️ images/update.png not found. Using black background.")
            self.bg_image = Image.new("RGB", (device.width, device.height), "black")
            self.image_missing = True

        print(f"[Update] Current Version: {self.current_ver}")
        print(f"[Update] Latest Version: {self.latest_ver}")

        self.add_button(Button(43, 205, 280, 235, "UPDATE", self.update_function, visible=False))
        self.add_button(Button(290, 210, 316, 237, "Back", lambda: screen_manager.set_screen(MainMenuScreen(screen_manager, screen_manager.hill)), visible=False))

    def draw(self, draw_obj):
        img = self.bg_image.copy()
        draw = ImageDraw.Draw(img)

        font = _load_font(size=20)
        draw.text((125, 123), f"{self.current_ver}", fill="white", font=font)
        draw.text((125, 168), f"{self.latest_ver}", fill="white", font=font)

        for btn in self.buttons:
            btn.draw(draw)

        device.display(img)


class ScreenManager:
    def __init__(self):
        self.current = None

    def set_screen(self, screen):
        self.current = screen
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

    # Init display (guarded) & splash
    init_display()

    # Intialize touchscreen
    touch = None
    try:
        touch = XPT2046()
    except Exception as e:
        print(f"⚠️ Touch init failed: {e}")
        touch = None
    calibrator = TouchCalibrator()

    # Start heartbeat
    heartbeat_thread = threading.Thread(target=heartbeat, daemon=True)
    heartbeat_thread.start()

    # Splash
    try:
        splash = Image.open("images/splashlogo.png").convert("RGB").resize((device.width, device.height))
        device.display(splash)
        leds_rainbow_splash(duration_sec=3.0)  # fades in over the 2s splash, then turns LEDs off

    except FileNotFoundError:
        print("⚠️ images/splashlogo.png not found; skipping splash.")

    try:
        if os.path.exists(CALIBRATION_FILE):
            calibrator.load()
        else:
            print("No calibration file found. Touchscreen Calibration required.")
            exit(1)

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
        screen_manager.set_screen(MainMenuScreen(screen_manager, screen_manager.hill))

        while True:
            if touch:
                coord = touch.read_touch()
                if coord:
                    mapped = calibrator.map_raw_to_screen(*coord)
                    if VERBOSE:
                        print(f"Touch @ {mapped}")
                    screen_manager.handle_touch(*mapped)

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
                    pass
            try:
                sn = hill.newSnow
                if isinstance(sn, str):
                    sn = _safe_int(sn)
                current_snow_cm = int(sn)
                if not hasattr(main, "_prev_snow_cm"):
                    main._prev_snow_cm = current_snow_cm
                #update LEDs if snow amount changed
                if current_snow_cm != main._prev_snow_cm:
                    print(f"[Snow] Change detected: {main._prev_snow_cm} -> {current_snow_cm}")
                leds_set_snow(current_snow_cm, main._prev_snow_cm)
                main._prev_snow_cm = current_snow_cm

            except Exception:
                current_snow_cm = 0

            try:
                check_and_trigger_alarm(current_snow_cm)
            except Exception as e:
                print(f"[Alarm] check failed: {e}")

            time.sleep(0.1)

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

    # normal program startup continues here ...
    main()