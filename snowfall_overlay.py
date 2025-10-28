# snowfall_overlay.py (Presenter‑Driven v2)
# Raspberry Pi Zero 2 W • Python 3.9
#
# Purpose
# -------
# Run an animated snowfall "over" the ScreenManager even when the ScreenManager
# itself only redraws on input/fetch intervals. We do this by:
#   • Caching the latest SnowReportScreen base frame (set_base/update_base)
#   • Animating on a separate thread
#   • Compositing base + snow into a reusable frame buffer
#   • Calling a thread‑safe `present(frame)` function that writes to the device
#     (ScreenManager must use the SAME present() wrapper so nobody fights the LCD)
#
# CPU is adaptive and soft‑capped (~80%), memory is monitored to detect churn.
# Density/speed scale with positive snowfall delta. When you leave SnowReportScreen
# the overlay idles (or you can .stop() it explicitly). See PATCH NOTES at bottom.

from __future__ import annotations
import os, time, threading, random, gc
from typing import Callable, List, Tuple, Optional

from PIL import Image, ImageDraw
import psutil

# ---------------- Tunables for Pi Zero 2 W ----------------
MAX_CPU_PCT = 80.0        # Target upper bound across the whole process
BASE_FPS    = 24          # Normal animation target
PEAK_FPS    = 28          # Upper bound under light load
MIN_FPS     = 10          # Backoff floor under heavy load
MEM_RESET_MB = 64         # If RSS grows by this much, recycle buffers
FRAME_JITTER = 0.004      # Jitter to desync with other loops
POOL_PAD     = 16         # Extra preallocated flakes to avoid alloc churn

# Map delta(cm) 1..10 → flake count & speed multiplier
_DENSITY = [20, 35, 55, 75, 95, 115, 130, 145, 155, 165]
_SPEED   = [1.0,1.05,1.10,1.15,1.22,1.30,1.40,1.55,1.75,2.15]

class _Flake:
    __slots__ = ("x","y","vy","w")
    def __init__(self):
        self.x = 0
        self.y = 0
        self.vy = 0.0
        self.w = 1

class SnowfallOverlay:
    """Presenter‑driven snowfall overlay.

    Lifecycle:
      • on_enter(present)   – call when SnowReportScreen becomes active
      • update_base(img)    – call whenever SnowReportScreen renders a fresh base
      • trigger(delta_cm)   – start/refresh animation if delta > 0
      • stop()              – end animation (e.g., when delta <= 0 or leaving)
      • on_exit()           – call when leaving SnowReportScreen (optional; idles)
    """
    def __init__(self, get_size: Callable[[], Tuple[int,int]]):
        self.get_size = get_size
        self._present: Optional[Callable[[Image.Image], None]] = None

        self._lock = threading.RLock()
        self._stop_ev = threading.Event()
        self._thr: Optional[threading.Thread] = None

        self._allowed = False      # True only while SnowReportScreen is current
        self._running = False      # True only while we have a positive delta
        self._density = 0
        self._speed_mul = 1.0

        # Buffers
        self._base: Optional[Image.Image] = None     # last full SnowReportScreen frame (RGBA)
        self._overlay: Optional[Image.Image] = None  # per‑frame snow layer (RGBA)
        self._draw: Optional[ImageDraw.ImageDraw] = None
        self._frame: Optional[Image.Image] = None    # composited output (RGBA)

        # Flakes
        self._pool: List[_Flake] = []
        self._flakes: List[_Flake] = []

        # Stats
        self._proc = psutil.Process(os.getpid())
        self._last_rss = self._proc.memory_info().rss
        self._last_rss_check = time.time()

    # ---------------- Public API ----------------
    def on_enter(self, present: Callable[[Image.Image], None]):
        """Bind the device presenter and allow animation to run on this screen."""
        with self._lock:
            self._present = present
            self._allowed = True
            self._ensure_buffers()
        self._start_if_needed()

    def on_exit(self):
        """Indicate we left SnowReportScreen. Thread stays alive but idles."""
        with self._lock:
            self._allowed = False
            # Optional: also stop the storm when leaving
            self._running = False
            self._density = 0

    def update_base(self, img: Image.Image):
        """Provide the latest SnowReportScreen background (single, full frame)."""
        if img is None:
            return
        with self._lock:
            w,h = self.get_size()
            if img.mode != "RGBA":
                img = img.convert("RGBA")
            if img.size != (w,h):
                # Avoid resampling artifacts by letterboxing if sizes differ.
                # For Snow Scraper this should match, but we fail safe.
                base = Image.new("RGBA", (w,h), (0,0,0,255))
                base.paste(img, (0,0))
                self._base = base
            else:
                # Keep a copy so the UI thread can discard its reference
                self._base = img.copy()
            self._ensure_buffers()

    def trigger(self, delta_cm: int):
        """Start/refresh the snowfall for a positive delta (cm)."""
        print(f"[Snow] Triggering Snowfall Overlay: {delta_cm} cm")
        delta = max(1, min(10, int(delta_cm)))
        with self._lock:
            self._density = _DENSITY[delta-1]
            self._speed_mul = _SPEED[delta-1]
            self._ensure_flake_pool(self._density + POOL_PAD)
            self._seed_flakes(self._density)
            self._running = True
        self._start_if_needed()

    def stop(self):
        """Stop snowfall (keeps thread/canvas; cheap to restart)."""
        with self._lock:
            self._running = False
            self._density = 0
            if self._draw and self._overlay:
                self._draw.rectangle((0,0,self._overlay.width,self._overlay.height), fill=(0,0,0,0))

    # Back‑compat, not used in v2 path (ScreenManager-driven). Safe no‑op.
    def blit_onto(self, base_img: Image.Image):  # noqa: kept for compatibility
        with self._lock:
            if not self._overlay:
                return
            base_img.paste(self._overlay, (0,0), self._overlay)

    # ---------------- Internals ----------------
    def _start_if_needed(self):
        if self._thr and self._thr.is_alive():
            return
        self._stop_ev.clear()
        self._thr = threading.Thread(target=self._loop, name="SnowfallOverlay", daemon=True)
        try:
            os.nice(5)
        except Exception:
            pass
        self._thr.start()

    def _ensure_buffers(self):
        w,h = self.get_size()
        if self._overlay is None or self._overlay.size != (w,h):
            self._overlay = Image.new("RGBA", (w,h), (0,0,0,0))
            self._draw = ImageDraw.Draw(self._overlay, "RGBA")
        if self._frame is None or self._frame.size != (w,h):
            self._frame = Image.new("RGBA", (w,h), (0,0,0,255))

    def _ensure_flake_pool(self, want: int):
        while len(self._pool) < want:
            self._pool.append(_Flake())

    def _seed_flakes(self, n: int):
        if not self._overlay:
            self._ensure_buffers()
        w,h = self._overlay.size
        rng = random.Random(os.getpid() ^ int(time.time()))
        self._flakes = self._pool[:n]
        for f in self._flakes:
            f.x = rng.randrange(0, w)
            f.y = rng.randrange(-h, 0)
            f.vy = (0.9 + rng.random()*0.8) * self._speed_mul
            r = rng.random()
            if r < 0.6:
                f.w = 2   # 60% 2×2
            elif r < 0.9:
                f.w = 4   # 30% 3×3
            else:
                f.w = 5   # 10% 4×4

    def _loop(self):
        last = time.perf_counter()
        rng = random.Random()
        while not self._stop_ev.is_set():
            with self._lock:
                allowed = self._allowed
                running = self._running
                present = self._present
                base = self._base
                overlay = self._overlay
                draw = self._draw
                frame = self._frame

            if not (allowed and running and present and base and overlay and draw and frame):
                time.sleep(0.05)
                continue

            # Adaptive pacing based on process CPU load
            cpu_pct = self._proc.cpu_percent(interval=None)
            target_fps = BASE_FPS
            if cpu_pct > MAX_CPU_PCT:
                target_fps = max(MIN_FPS, int(BASE_FPS * 0.5))
            elif cpu_pct < 40.0:
                target_fps = min(PEAK_FPS, BASE_FPS + 2)

            dt_target = 1.0 / float(target_fps)
            now = time.perf_counter()
            dt = now - last
            if dt < dt_target:
                time.sleep(max(0.0, dt_target - dt) + rng.uniform(0.0, FRAME_JITTER))
                now = time.perf_counter()
                dt = now - last
            last = now

            # ----- Update + draw snow layer -----
            with self._lock:
                w,h = overlay.size
                draw.rectangle((0,0,w,h), fill=(0,0,0,0))
                for f in self._flakes:
                    f.y += f.vy
                    if f.y >= h:
                        f.y = -2
                        f.x = (f.x + rng.randrange(-8,9)) % w
                    x0 = f.x; y0 = int(f.y); x1 = x0 + f.w; y1 = y0 + f.w
                    draw.rectangle((x0,y0,x1,y1), fill=(255,255,255,220))

                # Compose: base → frame, then paste overlay with alpha, then present
                frame.paste(base)
                frame.paste(overlay, (0,0), overlay)
                to_present = frame  # avoid extra copy

            try:
                present(to_present)
            except Exception:
                # If we lose the device for a moment, just back off briefly
                time.sleep(0.05)

            # Memory sentinel (low frequency)
            t = time.time()
            if t - self._last_rss_check > 2.0:
                self._last_rss_check = t
                rss = self._proc.memory_info().rss
                if rss - self._last_rss > MEM_RESET_MB * 1024 * 1024:
                    with self._lock:
                        # Recreate buffers to defragment and drop leaked refs
                        self._overlay = Image.new("RGBA", self._overlay.size, (0,0,0,0))
                        self._draw = ImageDraw.Draw(self._overlay, "RGBA")
                        self._frame = Image.new("RGBA", self._frame.size, (0,0,0,255))
                    gc.collect()
                    self._last_rss = rss

    # Optional hard shutdown if you truly want to stop the thread.
    def shutdown(self):
        self._stop_ev.set()
        t = self._thr
        if t and t.is_alive():
            t.join(timeout=1.0)
        self._thr = None

# -----------------------------------------------------------------------------
# PATCH NOTES – integrate with ScreenManager & SnowReportScreen
# -----------------------------------------------------------------------------
# 1) In snowgui.py (top‑level), wrap device.display with a global presenter that
#    owns the LCD lock. EVERY screen should use this instead of device.display.
#
#    import threading
#    display_lock = threading.RLock()
#    def present(img):
#        with display_lock:
#            device.display(img)
#
# 2) Instantiate the overlay after init_display():
#
#    from snowfall_overlay import SnowfallOverlay
#    overlay = SnowfallOverlay(get_size=lambda: (device.width, device.height))
#    screen_manager.overlay = overlay
#
# 3) In ScreenManager.set_screen(new_screen):
#
#    if isinstance(self.current, SnowReportScreen) and hasattr(self, "overlay"):
#        self.overlay.on_exit()
#    self.current = new_screen
#    if isinstance(new_screen, SnowReportScreen) and hasattr(self, "overlay"):
#        self.overlay.on_enter(present)
#    self.redraw()
#
# 4) In SnowReportScreen.draw(...): build your base frame as usual into `img`.
#    Before the first present, update the overlay base and then present once:
#
#      img = Image.new("RGBA", (device.width, device.height))
#      ... draw text/icons ...
#      if hasattr(self.screen_manager, "overlay"):
#          self.screen_manager.overlay.update_base(img)
#      present(img)  # one immediate draw so screen updates right away
#
# 5) Where you detect snowfall deltas (new > prev), start/stop the overlay:
#
#    if current_snow_cm != prev_snow_cm:
#        if current_snow_cm > prev_snow_cm and hasattr(screen_manager, "overlay"):
#            screen_manager.overlay.trigger(current_snow_cm - prev_snow_cm)
#        else:
#            screen_manager.overlay.stop()
#
# Result: the overlay thread continuously composites base+snow and calls
# present(img) at a safe FPS while SnowReportScreen is active. ScreenManager
# remains event‑driven; the overlay is what produces the animation.
