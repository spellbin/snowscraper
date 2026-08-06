"""Resort selection, Snow API access, and local observation history.

This module owns the resort catalog view used by configuration screens and the
small skiHill data object consumed by the GUI. It deliberately does not own the
currently active hill singleton; snowgui.py retains that process-level lifecycle
so screen construction and reload timing remain unchanged.

Selection files continue to store the same values:

* skihill.conf stores an index into metadata-derived resort order;
* country.conf stores the selected country label; and
* region.conf stores the selected region label.

The Snow API is the primary source for the resort universe, current readings,
and 30-day history. Enabled user-created BeautifulSoup modules can override an
existing resort or append a local-only resort without modifying this file. The
bundled metadata remains an offline fallback. The loader retains all legacy
selection aliases, including the historical All Resorts region label and
case-insensitive Other buckets.
"""

import datetime
import json
import os
import threading
import time
from typing import List, Optional
from urllib.parse import quote

import requests

from .avalanche import (
    _load_resort_meta as _load_local_resort_meta,
    _normalize_resort_meta,
)
from .storage import atomic_write_json, atomic_write_text
from .health import health_reporter
from .local_scrapers import (
    LocalScraperError,
    find_enabled_module,
    merge_enabled_module_metadata,
    run_local_scraper,
)


SNOW_LOG_FILE = "/home/pi/snowscraper/logs/snow_log.json"
COUNTRY_CONF_FILE = "conf/country.conf"
REGION_CONF_FILE = "conf/region.conf"
ALL_COUNTRIES_LABEL = "All Countries"
ALL_REGIONS_LABEL = "All Regions"
ALL_RESORTS_LABEL = "All Resorts"
OTHER_COUNTRY_LABEL = "Other"
OTHER_REGION_LABEL = "Other"
DEFAULT_SNOW_API_BASE_URL = "https://plow.snowscraper.ca/api/snow"
DEFAULT_SNOW_API_TIMEOUT_SECONDS = 10.0
API_META_CACHE_SECONDS = 60.0 * 60.0
OFFLINE_META_RETRY_SECONDS = 60.0
SNOW_API_USER_AGENT = "SnowGUI/2.3.0"


class SnowApiError(RuntimeError):
    """Raised when Snow API transport or payload validation fails."""


# Successful API metadata is cached for an hour. If a refresh fails, retain the
# last full API universe and retry shortly; never shrink an online device back
# to the smaller bundled fallback after it has already loaded canonical data.
_meta_cache = None
_meta_cache_source = None
_meta_retry_at = 0.0
_meta_cache_lock = threading.RLock()

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


def _optional_int(value):
    """Coerce an API measurement to int while preserving unavailable as None.

    The Snow API contract distinguishes a verified zero from an absent value.
    This helper must therefore never use truthiness or a zero default.
    """
    if value is None:
        return None
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


def _snow_api_base_url() -> str:
    """Return the configurable Snow API root without a trailing slash."""
    configured = os.getenv("SNOW_API_BASE_URL", DEFAULT_SNOW_API_BASE_URL).strip()
    return (configured or DEFAULT_SNOW_API_BASE_URL).rstrip("/")


def _snow_api_timeout() -> float:
    """Read a positive request timeout, falling back safely on bad config."""
    try:
        timeout = float(
            os.getenv(
                "SNOW_API_TIMEOUT_SECONDS",
                str(DEFAULT_SNOW_API_TIMEOUT_SECONDS),
            )
        )
        return timeout if timeout > 0 else DEFAULT_SNOW_API_TIMEOUT_SECONDS
    except (TypeError, ValueError):
        return DEFAULT_SNOW_API_TIMEOUT_SECONDS


def _resort_api_slug(name: str, meta: Optional[dict] = None) -> str:
    """Resolve a metadata slug, or derive one with the canonical convention."""
    if isinstance(meta, dict):
        info = meta.get(name)
        if isinstance(info, dict) and info.get("slug"):
            return str(info["slug"])
    return _resort_slug(name)


def snow_api_url(endpoint: str, resort_name: Optional[str] = None) -> str:
    """Build a public Snow API URL for a fixed endpoint and optional resort."""
    path = str(endpoint or "").strip("/")
    url = f"{_snow_api_base_url()}/{path}"
    if resort_name is not None:
        slug = quote(_resort_api_slug(resort_name), safe="_-")
        url = f"{url}/{slug}"
    return url


def _snow_api_get(endpoint: str, resort_name: Optional[str] = None) -> dict:
    """GET and validate a JSON-object response from the public Snow API."""
    url = snow_api_url(endpoint, resort_name)
    try:
        response = requests.get(
            url,
            timeout=_snow_api_timeout(),
            headers={"User-Agent": SNOW_API_USER_AGENT, "Accept": "application/json"},
        )
        response.raise_for_status()
        payload = response.json() if response.content else {}
    except Exception as exc:
        raise SnowApiError(f"Snow API request failed for {url}: {exc}") from exc

    if not isinstance(payload, dict):
        raise SnowApiError(f"Snow API returned a non-object payload for {url}")
    return payload


def clear_resort_meta_cache() -> None:
    """Clear the process metadata cache; primarily useful after config changes."""
    global _meta_cache, _meta_cache_source, _meta_retry_at
    with _meta_cache_lock:
        _meta_cache = None
        _meta_cache_source = None
        _meta_retry_at = 0.0


def load_resort_meta(force_refresh: bool = False) -> dict:
    """Load canonical resort metadata from Snow API with an offline fallback.

    API metadata is normalized into the historical name-to-info mapping used by
    the touchscreen screens. Successful data refreshes hourly; bundled fallback
    data and failed refreshes are retried after OFFLINE_META_RETRY_SECONDS.
    """
    global _meta_cache, _meta_cache_source, _meta_retry_at
    with _meta_cache_lock:
        now = time.monotonic()
        if not force_refresh and isinstance(_meta_cache, dict):
            if now < _meta_retry_at:
                return merge_enabled_module_metadata(_meta_cache)

        try:
            payload = _snow_api_get("resorts/meta")
            normalized = _normalize_resort_meta(payload)
            if not normalized:
                raise SnowApiError("Snow API resort metadata contains no resorts")
            _meta_cache = normalized
            _meta_cache_source = "api"
            _meta_retry_at = now + API_META_CACHE_SECONDS
            return merge_enabled_module_metadata(_meta_cache)
        except SnowApiError as exc:
            print(f"[Resorts] {exc}; using bundled metadata fallback.")
            if _meta_cache_source == "api" and isinstance(_meta_cache, dict):
                _meta_retry_at = now + OFFLINE_META_RETRY_SECONDS
                return merge_enabled_module_metadata(_meta_cache)
            fallback = _load_local_resort_meta()
            if fallback:
                _meta_cache = fallback
                _meta_cache_source = "offline"
                _meta_retry_at = now + OFFLINE_META_RETRY_SECONDS
                return merge_enabled_module_metadata(_meta_cache)
            if isinstance(_meta_cache, dict):
                return merge_enabled_module_metadata(_meta_cache)
            # A local-only module must remain selectable even if both the Snow
            # API and the bundled metadata are unavailable.
            return merge_enabled_module_metadata({})


# Historical private name retained for snowgui compatibility.
_load_resort_meta = load_resort_meta


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
    """Convert a display name using the Snow API's canonical slug convention.

    Only spaces become underscores. Apostrophes, hyphens, accents, and existing
    underscores are part of the canonical metadata slug and must be preserved;
    ``snow_api_url`` percent-encodes characters that are unsafe in a URL path.
    """
    slug = (name or "").strip().replace(" ", "_")
    return slug or "Unknown"


def fetch_current_snow(name: str) -> dict:
    """Fetch a local module override or the Snow API current endpoint.

    Local modules run in a bounded child process. Their manifest defaults to a
    Snow API fallback so a beginner's broken selector does not take an existing
    resort offline. A local-only module can disable that fallback. Transport and
    malformed-payload failures still raise so callers retain the last readings.
    """
    module = find_enabled_module(name)
    if module is not None:
        try:
            print(f"[LocalScraper] Running '{module.module_id}' for {name}")
            return run_local_scraper(module)
        except LocalScraperError as exc:
            if not module.fallback_to_snow_api:
                raise
            print(
                f"[LocalScraper] {exc}; falling back to the Snow API for {name}."
            )
    payload = _snow_api_get("current", name)
    if not isinstance(payload.get("current"), dict):
        raise SnowApiError(f"Snow API current payload is malformed for {name}")
    return payload


def fetch_snow_history(name: str) -> dict:
    """Fetch Snow API history, falling back to the Pi log for a local resort."""
    try:
        payload = _snow_api_get("history30", name)
    except SnowApiError:
        # A local-only resort has no server history endpoint. Successful module
        # readings already enter the ordinary daily log, so reuse that data for
        # the existing chart instead of creating a second history format.
        module = find_enabled_module(name)
        if module is None:
            raise
        try:
            with open(SNOW_LOG_FILE, "r", encoding="utf-8") as history_file:
                local_log = json.load(history_file)
        except (OSError, ValueError, TypeError):
            local_log = {}
        resort_log = local_log.get(name) if isinstance(local_log, dict) else None
        history = resort_log.get("history") if isinstance(resort_log, dict) else []
        return {
            "history": history if isinstance(history, list) else [],
            "source": {"provider": "local_log", "module": module.module_id},
        }
    if not isinstance(payload.get("history"), list):
        raise SnowApiError(f"Snow API history payload is malformed for {name}")
    return payload


def snow_history_url(name: str) -> str:
    """Return the canonical API URL used for the resort's 30-day chart."""
    return snow_api_url("history30", name)


def _load_resort_json(name: str) -> dict:
    """Compatibility wrapper for the former static-JSON loader."""
    return fetch_current_snow(name)

def log_snow_data(hill):
    """
    Writes current reading and keeps a history of daily readings for each mountain.
    Structure:
    {
        "Sun Peaks": {
            "current": {"date": "YYYY-MM-DD", "newSnow": int|null,
                        "daySnow": int|null, "weekSnow": int|null,
                        "baseSnow": int|null},
            "history": [
                {"date": "YYYY-MM-DD", ...},
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
        "newSnow": _optional_int(hill.newSnow),
        "daySnow": _optional_int(getattr(hill, "daySnow", None)),
        "weekSnow": _optional_int(hill.weekSnow),
        "baseSnow": _optional_int(hill.baseSnow),
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
        self.daySnow = newSnow
        self.weekSnow = weekSnow
        self.baseSnow = baseSnow
        self.freshness = None
        self.source = None
        self.scraper_disabled = False
        # Resort selection is useful health context even before the first Snow
        # API request completes. This does not trigger network I/O.
        health_reporter.set_selected_resort(name)

    def getSnow(self):
        if DEV_MODE:
            print("[DEV] Skipping live fetch; using stub values.")
            self.newSnow = 1
            self.daySnow = 1
            self.weekSnow = 3
            self.baseSnow = 120
            return
        print(f"[getSnow] {self.name}")
        try:
            data = _load_resort_json(self.name)
        except Exception as exc:
            # Retain the previous readings exactly as before, while making the
            # current fetch failure visible to the remote health monitor.
            health_reporter.record_snow_fetch_failure(exc, self.name)
            raise
        cur = data["current"]
        source = data.get("source") if isinstance(data.get("source"), dict) else None
        self.url = str(source.get("url")) if source and source.get("url") else snow_api_url("current", self.name)
        self.newSnow = _optional_int(cur.get("newSnow"))
        self.daySnow = _optional_int(cur.get("daySnow"))
        self.weekSnow = _optional_int(cur.get("weekSnow"))
        self.baseSnow = _optional_int(cur.get("baseSnow"))
        self.freshness = data.get("freshness") if isinstance(data.get("freshness"), dict) else None
        self.source = source
        self.scraper_disabled = bool(data.get("scraper_disabled"))
        health_reporter.record_snow_fetch_success(self.name)
        log_snow_data(self)
