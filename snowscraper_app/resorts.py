"""Resort selection, public snow JSON loading, and local snow history.

This module owns the resort catalog view used by configuration screens and the
small skiHill data object consumed by the GUI. It deliberately does not own the
currently active hill singleton; snowgui.py retains that process-level lifecycle
so screen construction and reload timing remain unchanged.

Selection files continue to store the same values:

* skihill.conf stores an index into metadata-derived resort order;
* country.conf stores the selected country label; and
* region.conf stores the selected region label.

The loader also retains all legacy aliases and fallback behavior, including the
historical All Resorts region label and case-insensitive Other buckets.
"""

import datetime
import json
import os
import re
from typing import List, Optional

import requests

from .avalanche import _load_resort_meta
from .storage import atomic_write_json, atomic_write_text


SNOW_LOG_FILE = "/home/pi/snowscraper/logs/snow_log.json"
COUNTRY_CONF_FILE = "conf/country.conf"
REGION_CONF_FILE = "conf/region.conf"
ALL_COUNTRIES_LABEL = "All Countries"
ALL_REGIONS_LABEL = "All Regions"
ALL_RESORTS_LABEL = "All Resorts"
OTHER_COUNTRY_LABEL = "Other"
OTHER_REGION_LABEL = "Other"

# This mirrors snowgui.DEV_MODE.  The main loop skips live getSnow calls while
# development mode is active, and the guard here preserves skiHill.getSnow's
# historical behavior when that method is exercised directly.
DEV_MODE = False


# Compatibility aliases let the extracted source keep its historical helper
# names while sharing the well-documented atomic persistence implementation.
_atomic_write_text = atomic_write_text
_atomic_write_json = atomic_write_json


def _today_str():
    """Return the local date key used by each resort's rolling history."""
    return datetime.datetime.now().strftime("%Y-%m-%d")


def _safe_int(value, default=0):
    """Convert numeric values or strings such as '12 cm' to an integer."""
    try:
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
        digits = "".join(character for character in str(value) if character.isdigit())
        return int(digits) if digits else default
    except Exception:
        return default


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

