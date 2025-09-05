#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Standalone touchscreen calibrator for Snow Scraper
Target: Raspberry Pi Zero 2 W, Python 3.9

- Uses the same 2-point method as your app (Top-Left, Bottom-Right).
- Reads raw touch from XPT2046 on SPI bus 0, device 1 (same as your code).
- Draws prompts on the ILI9341 via luma.lcd exactly like the app.
- Writes JSON: {"x_min": ..., "x_max": ..., "y_min": ..., "y_max": ...}
- Always saves to ./conf/touch_calibration.json alongside snowgui.py
"""

import os
import sys
import time
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

# --- SPI Touch (XPT2046) ---
import spidev

# --- LCD (ILI9341 via luma.lcd) ---
from luma.core.interface.serial import spi as luma_spi
from luma.lcd.device import ili9341
from luma.core.render import canvas


# ----------------------------
# Constants / Paths
# ----------------------------
HERE = Path(__file__).resolve().parent
CALIBRATION_FILE = HERE / "conf/touch_calibration.json"   # match Snow Scraper location & name
LCD_WIDTH = 320
LCD_HEIGHT = 240
LCD_ROTATE = 0  # set to 0/90/180/270 to match your Snow Scraper


# ----------------------------
# Touch driver (same wiring/logic as in your app)
# ----------------------------
class XPT2046:
    """
    Raw touch reader. Reads 12-bit coordinates from XPT2046 on SPI0.1.
    """
    def __init__(self, spi_bus=0, spi_device=1, max_speed=500_000):
        self.spi = spidev.SpiDev()
        self.spi.open(spi_bus, spi_device)
        self.spi.max_speed_hz = max_speed
        self.spi.mode = 0b00

    def _read_channel(self, cmd):
        # Send 3 bytes: command, 0x00, 0x00 -> read 12-bit value
        resp = self.spi.xfer2([cmd, 0x00, 0x00])
        return ((resp[1] << 8) | resp[2]) >> 4

    def read_touch(self, samples=5, tolerance=50):
        """
        Average a handful of close-together samples. Return (x, y) or None.
        Filters out jumps > tolerance to avoid jitter.
        """
        readings = []
        for _ in range(samples):
            raw_y = self._read_channel(0xD0)  # Y first on XPT2046 (cmd 0xD0)
            raw_x = self._read_channel(0x90)  # then X (cmd 0x90)
            if 100 < raw_x < 4000 and 100 < raw_y < 4000:
                readings.append((raw_x, raw_y))
            time.sleep(0.01)

        if len(readings) < 3:
            return None

        xs, ys = zip(*readings)
        if max(xs) - min(xs) > tolerance or max(ys) - min(ys) > tolerance:
            return None

        return (sum(xs) // len(xs), sum(ys) // len(ys))

    def close(self):
        try:
            self.spi.close()
        except Exception:
            pass


# ----------------------------
# Calibrator (same 2-point scheme & JSON format as your app)
# ----------------------------
class TwoPointCalibrator:
    def __init__(self, lcd):
        self.lcd = lcd
        # Defaults mirror the app’s initial ranges
        self.x_min = 0
        self.x_max = 4095
        self.y_min = 0
        self.y_max = 4095

    def _prompt_and_get_raw(self, label, sx, sy, touch):
        # Draw target
        with canvas(self.lcd) as draw:
            draw.rectangle((0, 0, self.lcd.width - 1, self.lcd.height - 1), outline="white")
            size = 5
            draw.ellipse((sx - size, sy - size, sx + size, sy + size), fill="red")
            draw.text((10, 10), f"Touch {label} corner", fill="white")

        print(f"Touch the {label} corner... (hold briefly, then release)")
        start = time.time()
        while time.time() - start < 20.0:  # 20s per point
            coord = touch.read_touch()
            if coord:
                print(f"{label} raw touch: {coord}")
                time.sleep(0.5)  # small debounce so we don’t double-read
                return coord
            time.sleep(0.01)
        raise RuntimeError(f"Timeout waiting for {label} corner touch.")

    def run_and_save(self, touch):
        # Targets (inset from corners, same as your app’s feel)
        inset = 20
        tl = ("Top-Left", inset, inset)
        br = ("Bottom-Right", LCD_WIDTH - inset, LCD_HEIGHT - inset)

        # Collect two raw points
        (rx0, ry0) = self._prompt_and_get_raw(*tl, touch=touch)
        (rx1, ry1) = self._prompt_and_get_raw(*br, touch=touch)

        if rx0 == rx1 or ry0 == ry1:
            raise RuntimeError("Calibration failed (identical raw points).")

        # Store min/max just like your app
        self.x_min, self.x_max = sorted((rx0, rx1))
        self.y_min, self.y_max = sorted((ry0, ry1))

        # Save EXACT SAME JSON format as Snow Scraper
        data = {
            "x_min": int(self.x_min),
            "x_max": int(self.x_max),
            "y_min": int(self.y_min),
            "y_max": int(self.y_max),
        }
        CALIBRATION_FILE.write_text(__import__("json").dumps(data, indent=2))
        print(f"Saved calibration to {CALIBRATION_FILE}")

        # Brief “done” screen
        with canvas(self.lcd) as draw:
            draw.text((10, 10), "Calibration complete", fill="white")
            draw.text((10, 28), str(CALIBRATION_FILE), fill="white")
        time.sleep(1.2)


# ----------------------------
# Main
# ----------------------------
def main():
    # Init LCD exactly like Snow Scraper (SPI0.0, DC=24, RST=25)
    serial = luma_spi(port=0, device=0, gpio_DC=24, gpio_RST=25)
    lcd = ili9341(serial_interface=serial, width=LCD_WIDTH, height=LCD_HEIGHT, rotate=LCD_ROTATE)

    # Touch
    touch = XPT2046(spi_bus=0, spi_device=1)

    try:
        calibrator = TwoPointCalibrator(lcd)
        calibrator.run_and_save(touch)
    except KeyboardInterrupt:
        pass
    finally:
        touch.close()


if __name__ == "__main__":
    # Run with root so we can access /dev/spidev* and /dev/input (if needed later)
    if os.geteuid() != 0:
        print("Tip: run with sudo for reliable SPI access (sudo python3 calibrate_touchscreen.py)")
    main()
