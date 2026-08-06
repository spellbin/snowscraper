"""WS2812 snow indicator and command-line LED demonstration mode.

The seven-pixel ring is intentionally isolated from the GUI.  It translates a
snowfall value into color, breathing speed, and optional sparkles while exposing
small wrapper functions to the rest of the application.  Hardware initialization
is fail-soft: when rpi_ws281x or the physical ring is unavailable, a dummy strip
preserves the same control flow for development and testing.

Threading behavior is unchanged from the original implementation.  Separate
daemon workers own breathing and sparkle effects, and each transition stops the
previous worker before changing modes.
"""

import math
import os
import random
import sys
import threading
import time

from .brightness import brightness_state


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


