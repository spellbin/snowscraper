#!/usr/bin/env python3
# debug_hud.py — Draw CPU badge + Wi-Fi bars badge
from __future__ import annotations
import re, shutil, subprocess
from pathlib import Path
from typing import Optional, Tuple
from PIL import Image, ImageDraw, ImageFont

# ---------- Sensors ----------
THERMAL_PATH = Path("/sys/class/thermal/thermal_zone0/temp")
WIRELESS_PROC = Path("/proc/net/wireless")

def cpu_temp_c() -> Optional[float]:
    try:
        return int(THERMAL_PATH.read_text().strip()) / 1000.0
    except Exception:
        return None

def wifi_iface_guess() -> str:
    try:
        out = subprocess.check_output(["iw", "dev"], text=True)
        m = re.search(r"Interface\s+(\S+)", out)
        return m.group(1) if m else "wlan0"
    except Exception:
        return "wlan0"

def rssi_dbm(iface: Optional[str] = None) -> Optional[float]:
    iface = iface or wifi_iface_guess()
    if shutil.which("iw"):
        try:
            out = subprocess.check_output(["iw", "dev", iface, "link"], text=True, stderr=subprocess.STDOUT)
            m = re.search(r"signal:\s*([-]?\d+)\s*dBm", out)
            if m:
                return float(m.group(1))
        except Exception:
            pass
    try:
        if WIRELESS_PROC.exists():
            for line in WIRELESS_PROC.read_text().splitlines():
                if line.strip().startswith(iface + ":"):
                    parts = line.split()
                    nums = [p.rstrip(".") for p in parts if re.fullmatch(r"-?\d+\.?", p)]
                    if nums:
                        return float(nums[-1])
    except Exception:
        pass
    return None

def rssi_percent(rssi_dbm_value: Optional[float]) -> Optional[int]:
    if rssi_dbm_value is None:
        return None
    # Map: -90 dBm → 0%, -30 dBm → 100%
    pct = 2 * (rssi_dbm_value + 100)
    return max(0, min(100, int(pct)))

# ---------- Drawing helpers ----------
def load_small_font():
    try:
        return ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 12)
    except Exception:
        return ImageFont.load_default()

def color_for_temp_c(t: Optional[float]) -> Tuple[int,int,int]:
    if t is None: return (200,200,200)
    if t < 55:    return (80,220,120)   # green
    if t < 70:    return (255,180,60)   # amber
    return (255,80,80)                  # red

def color_for_wifi_pct(p: Optional[int]) -> Tuple[int,int,int]:
    if p is None: return (200,200,200)
    if p >= 67:   return (80,220,120)   # green
    if p >= 50:   return (255,200,80)   # amber
    if p >= 35:   return (255,150,70)   # orange
    return (255,80,80)                  # red

def _anchor_xy(W, H, w, h, pos: str, margin: int) -> Tuple[int,int]:
    if pos == "top-left":
        return margin, margin
    if pos == "top-right":
        return W - w - margin, margin
    if pos == "bottom-left":
        return margin, H - h - margin
    # bottom-right
    return W - w - margin, H - h - margin

# ---------- Public overlays ----------
def draw_cpu_badge(img,
                   temp_c_val: Optional[float] = None,
                   pos: str = "top-left",
                   margin: int = 6,
                   pad: int = 6):
    """
    Draws a small rounded rectangle badge with CPU temperature text.
    """
    if temp_c_val is None:
        temp_c_val = cpu_temp_c()

    draw = ImageDraw.Draw(img, "RGBA")
    font = load_small_font()
    text = "CPU --.-°C" if temp_c_val is None else f"CPU {temp_c_val:.1f}°C"

    bbox = draw.textbbox((0,0), text, font=font)
    tw, th = bbox[2]-bbox[0], bbox[3]-bbox[1]
    bw, bh = tw + pad*2, th + pad*2

    W, H = img.size
    x0, y0 = _anchor_xy(W, H, bw, bh, pos, margin)
    x1, y1 = x0 + bw, y0 + bh

    draw.rounded_rectangle((x0, y0, x1, y1), radius=8, fill=(0,0,0,140))
    draw.text((x0 + pad, y0 + pad - 2), text, font=font, fill=color_for_temp_c(temp_c_val))

def draw_wifi_bars_badge(img,
                         rssi_dbm_val: Optional[float] = None,
                         pos: str = "top-right",
                         margin: int = 6,
                         pad_x: int = 8,
                         pad_y: int = 6,
                         bars: int = 4):
    """
    Draws a bars-only Wi-Fi badge (no percentage text).
    Bars are color-coded; count/height reflect strength.
    """
    if rssi_dbm_val is None:
        rssi_dbm_val = rssi_dbm()
    pct = rssi_percent(rssi_dbm_val)

    # Determine bars "on"
    if pct is None:
        on = 0
    elif pct >= 90:
        on = bars
    elif pct >= 75:
        on = max(3, min(bars, bars-1))
    elif pct >= 50:
        on = max(2, min(bars, bars-2))
    elif pct >= 25:
        on = 1
    else:
        on = 0

    color = color_for_wifi_pct(pct)
    off_color = (120,120,120)

    # Bar geometry
    bar_w = 10
    gap = 3
    base_h = 6
    max_h = base_h + (bars-1)*4
    total_w = bars*bar_w + (bars-1)*gap
    total_h = max_h

    # Badge rect
    bw = total_w + pad_x*2
    bh = total_h + pad_y*2

    draw = ImageDraw.Draw(img, "RGBA")
    W, H = img.size
    x0, y0 = _anchor_xy(W, H, bw, bh, pos, margin)
    x1, y1 = x0 + bw, y0 + bh

    # Background
    draw.rounded_rectangle((x0, y0, x1, y1), radius=8, fill=(0,0,0,140))

    # Bars aligned to bottom inside the badge
    origin_x = x0 + pad_x
    baseline_y = y1 - pad_y

    for i in range(bars):
        h = base_h + i*4
        bx0 = origin_x + i*(bar_w + gap)
        by0 = baseline_y - h
        bx1 = bx0 + bar_w
        by1 = baseline_y
        fill = color if i < on else off_color
        draw.rounded_rectangle((bx0, by0, bx1, by1), radius=2, fill=fill)

# ---------- Demo ----------
if __name__ == "__main__":
    from PIL import Image
    frame = Image.new("RGB", (320, 240), (15,20,30))
    g = ImageDraw.Draw(frame)
    for x in range(0, 320, 16): g.line((x,0,x,240), fill=(25,30,40))
    for y in range(0, 240, 16): g.line((0,y,320,y), fill=(25,30,40))

    draw_cpu_badge(frame, pos="top-right")
    draw_wifi_bars_badge(frame, pos="top-right", margin=36)  # stack under CPU
    out = "/tmp/debug_split_preview.png"
    frame.save(out)
    print(f"Wrote {out}")
