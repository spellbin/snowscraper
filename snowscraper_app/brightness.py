"""Persisted brightness profiles shared by the LCD and WS2812 LEDs.

The display is dimmed in software by :mod:`snowgui`; the LED controller applies
the same scalar to its RGB values.  ``brightness_state`` is deliberately a
process-wide singleton so both output paths always use the same selected
profile, matching the behavior of the original monolithic implementation.
"""

from .storage import atomic_write_text


BRIGHTNESS_CONF_FILE = "/home/pi/snowscraper/conf/brightness.conf"

# Each profile combines the numeric brightness scale and the matching main-menu
# artwork.  The order is user-visible because tapping the brightness control
# cycles through this list and persists its numeric index.
BRIGHTNESS_LEVELS = [
    {"name": "Full", "scale": 1.0, "menu_bg": "images/mainmenu_night.png"},
    {"name": "Dim", "scale": 0.35, "menu_bg": "images/mainmenu_day.png"},
]


def read_brightness_index(path=BRIGHTNESS_CONF_FILE, default=0) -> int:
    """Read and clamp the persisted profile index.

    Missing, malformed, or out-of-range configuration must never prevent the UI
    from starting.  Invalid values therefore fall back to ``default``.
    """
    try:
        with open(path, "r") as config_file:
            raw = config_file.read().strip()
        index = int(raw)
        return max(0, min(index, len(BRIGHTNESS_LEVELS) - 1))
    except Exception:
        return default


def write_brightness_index(index: int, path=BRIGHTNESS_CONF_FILE) -> bool:
    """Clamp and persist a profile index, returning whether the write succeeded."""
    try:
        index = max(0, min(index, len(BRIGHTNESS_LEVELS) - 1))
        atomic_write_text(str(index), path)
        return True
    except Exception as exc:
        print(f"[Brightness] Failed to write {path}: {exc}")
        return False


class BrightnessState:
    """Mutable view of the currently selected brightness profile.

    The object exposes convenient attributes (``name``, ``scale``, and
    ``menu_bg``) because those are read frequently during rendering.  Mutating
    methods immediately persist the new numeric index, preserving the original
    one-tap behavior.
    """

    def __init__(self):
        self.levels = list(BRIGHTNESS_LEVELS)
        self.index = read_brightness_index()
        self._apply_index(self.index)

    def _apply_index(self, index: int):
        """Apply a clamped index without writing it to disk."""
        if not self.levels:
            self.levels = [
                {"name": "Full", "scale": 1.0, "menu_bg": "images/mainmenu.png"}
            ]
        self.index = max(0, min(index, len(self.levels) - 1))
        level = self.levels[self.index]
        self.name = level.get("name", "")
        self.scale = float(level.get("scale", 1.0))
        self.menu_bg = level.get("menu_bg", "images/mainmenu.png")

    def cycle(self):
        """Advance to the next profile and persist the resulting index."""
        next_index = (self.index + 1) % len(self.levels)
        self._apply_index(next_index)
        write_brightness_index(self.index)

    def set_index(self, index: int):
        """Select a specific profile by index and persist it."""
        self._apply_index(index)
        write_brightness_index(self.index)

    def is_dim(self) -> bool:
        """Return ``True`` whenever the active scale meaningfully dims output."""
        return self.scale < 0.99


brightness_state = BrightnessState()

# Compatibility aliases retain the names historically exposed by snowgui.py.
_read_brightness_index = read_brightness_index
_write_brightness_index = write_brightness_index
