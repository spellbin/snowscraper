"""Resort metadata parsing and avalanche-forecast providers.

Forecast data comes from three upstream systems with different schemas:

* Avalanche Canada uses a point lookup based on resort latitude/longitude;
* NWAC products are selected by forecast-zone id or name; and
* CAIC products are selected by forecast-zone name.

This module normalizes those providers to one dictionary containing title,
region, danger ratings, summary, and issued time.  The touchscreen screens can
therefore render a forecast without knowing which provider supplied it.

Network timeouts, response validation, selection order, and error messages
intentionally match the original snowgui.py implementation. Cache behavior
deliberately does NOT: the original memoized each centre's product list for the
life of the process, which pinned a long-running appliance to whichever forecast
was current when it booted. See ``_get_center_products``.
"""

import datetime
import json
import os
import re
import threading
import time
from functools import lru_cache
from html import unescape
from typing import Optional

import requests

try:
    import yaml  # Optional; a small built-in parser is used when unavailable.
except Exception:
    yaml = None


RESORT_META_FILE = "conf/resorts_meta.yaml"
AVY_POINT_URL = "https://api.avalanche.ca/forecasts/en/products/point"
AVY_HEADERS = {
    "User-Agent": "SnowGUI-Avy/0.1 (+https://www.snowscraper.ca)",
    "Accept": "application/json",
}
NWAC_API_BASE = "https://api.avalanche.org/v2/public"
NWAC_CENTER_ID = "NWAC"
CAIC_CENTER_ID = "CAIC"
CAIC_PRODUCTS_LIMIT = 500

# These resorts are routed to US avalanche centers rather than Avalanche
# Canada's point endpoint.  Zone identifiers/names are provider contracts and
# must remain exact.
NWAC_RESORTS = {
    "Mt Baker": {
        "zone_name": "West Slopes North",
        "lat": 48.862,
        "lon": -121.688,
    },
    "Crystal Mountain": {
        "zone_id": "6",
        "zone_name": "West Slopes South",
        "lat": 46.935,
        "lon": -121.474,
    },
    "Stevens Pass": {
        "zone_id": "2",
        "zone_name": "Stevens Pass",
        "lat": 47.744,
        "lon": -121.089,
    },
    "Alpental": {
        "zone_id": "3",
        "zone_name": "Snoqualmie Pass",
        "lat": 47.445,
        "lon": -121.424,
    },
    "Snoqualmie Pass": {
        "zone_id": "3",
        "zone_name": "Snoqualmie Pass",
        "lat": 47.424,
        "lon": -121.413,
    },
}
CAIC_RESORTS = {
    "Arapahoe Basin": {"zone_name": "Ten Mile Range"},
    "Vail": {"zone_name": "Gore Range"},
    "Beaver Creek": {"zone_name": "Sawatch Mountains"},
    "Breckenridge": {"zone_name": "Ten Mile Range"},
    "Keystone": {"zone_name": "Ten Mile Range"},
    "Copper Mountain": {"zone_name": "Ten Mile Range"},
}
NWAC_DANGER_TEXT = {
    1: "Low",
    2: "Moderate",
    3: "Considerable",
    4: "High",
    5: "Extreme",
}


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
    # Resort selection now consumes the canonical Snow API metadata. Importing
    # lazily avoids a module-import cycle (resorts reuses this module's metadata
    # normalizer) while ensuring newly added API resorts also have avalanche
    # coordinates. The bundled metadata remains the final offline fallback.
    try:
        from .resorts import load_resort_meta

        all_meta = load_resort_meta()
    except Exception:
        all_meta = _load_resort_meta()
    meta = all_meta.get(name) or {}
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


_TAG_RE = re.compile(r"<[^>]+>")

# NWAC and CAIC publish a NEW product (a new id) for each forecast cycle, so the
# product list is not static reference data -- it is the thing that tells us
# which forecast is current. Caching it without expiry pinned an appliance to
# whichever forecast happened to be live when it booted, and these run for
# months at a time: the screen kept presenting a months-old avalanche danger
# rating as today's. The list is cached only long enough to keep repeated screen
# opens off the network.
#
# Entries are (fetched_at_monotonic, products).
_CENTER_PRODUCTS_CACHE = {}
_CENTER_PRODUCTS_LOCK = threading.RLock()


def _center_products_ttl() -> float:
    """Seconds a cached product list stays usable. Read per call so it is
    configurable at runtime and overridable in tests."""
    try:
        ttl = float(os.getenv("AVY_PRODUCTS_TTL_SECONDS", str(CENTER_PRODUCTS_TTL_SECONDS)))
    except (TypeError, ValueError):
        return CENTER_PRODUCTS_TTL_SECONDS
    return ttl if ttl >= 0 else CENTER_PRODUCTS_TTL_SECONDS


# Fifteen minutes. Forecasts change a few times a day, so this is far tighter
# than the data it guards, while still collapsing the burst of loads you get
# from opening the avalanche screen a few times in a row.
CENTER_PRODUCTS_TTL_SECONDS = 900.0


def clear_center_products_cache() -> None:
    """Drop cached product lists; useful after a suspend or in tests."""
    with _CENTER_PRODUCTS_LOCK:
        _CENTER_PRODUCTS_CACHE.clear()


def _html_to_text(text: str) -> str:
    if not text:
        return ""
    text = _TAG_RE.sub(" ", text)
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _fetch_center_products(center_id: str, limit: Optional[int] = None):
    params = {"avalanche_center_id": center_id, "type": "forecast"}
    if limit:
        params["limit"] = str(limit)
    try:
        resp = requests.get(NWAC_API_BASE + "/products", params=params, headers=AVY_HEADERS, timeout=20)
    except Exception as e:
        raise RuntimeError(f"{center_id} products fetch failed: {e}")
    if resp.status_code != 200:
        raise RuntimeError(f"{center_id} products HTTP {resp.status_code}: {resp.text[:200]}")
    try:
        payload = resp.json() if resp.content else []
    except Exception as e:
        raise RuntimeError(f"Failed to parse {center_id} products JSON: {e}")
    if not isinstance(payload, list):
        raise RuntimeError(f"Unexpected {center_id} products payload type: {type(payload)!r}")
    return payload


def _get_center_products(center_id: str, limit: Optional[int] = None):
    """Return this centre's forecast products, refetching once the cache ages out.

    A failed refresh raises rather than falling back to the expired list. For an
    avalanche danger rating, "unavailable" is honest and the screen already
    renders it; silently showing an old forecast as current is not.
    """
    cache_key = (center_id, limit)
    with _CENTER_PRODUCTS_LOCK:
        entry = _CENTER_PRODUCTS_CACHE.get(cache_key)
        if entry is not None and (time.monotonic() - entry[0]) < _center_products_ttl():
            return entry[1]

    # Deliberately outside the lock: this is a 20s request, and holding a
    # process-wide lock across it stalls every other forecast load behind it.
    # Two threads racing here merely repeat an idempotent GET.
    products = _fetch_center_products(center_id, limit=limit)
    with _CENTER_PRODUCTS_LOCK:
        _CENTER_PRODUCTS_CACHE[cache_key] = (time.monotonic(), products)
    return products


def _pick_latest_nwac_product_id(products, zone_id: str = None, zone_name: str = None) -> int:
    zone_name_key = str(zone_name).strip().casefold() if zone_name is not None else None

    def matches(product):
        for zone in product.get("forecast_zone") or []:
            if zone_id is not None and str(zone.get("zone_id")) == str(zone_id):
                return True
            if zone_name_key is not None:
                for key in ("name", "zone_name", "display"):
                    val = zone.get(key)
                    if isinstance(val, str) and val.strip().casefold() == zone_name_key:
                        return True
                area = zone.get("area")
                if isinstance(area, dict):
                    for key in ("name", "display"):
                        val = area.get(key)
                        if isinstance(val, str) and val.strip().casefold() == zone_name_key:
                            return True
                label = zone.get("label")
                if isinstance(label, str) and label.strip().casefold() == zone_name_key:
                    return True
        area = product.get("area")
        if zone_name_key is not None and isinstance(area, dict):
            for key in ("name", "display"):
                val = area.get(key)
                if isinstance(val, str) and val.strip().casefold() == zone_name_key:
                    return True
        if zone_name_key is not None:
            for key in ("areaName", "zone_name", "zoneName", "region"):
                val = product.get(key)
                if isinstance(val, str) and val.strip().casefold() == zone_name_key:
                    return True
        return False

    candidates = [product for product in products if isinstance(product, dict) and matches(product)]
    if not candidates:
        zone_desc = f"zone_id={zone_id}" if zone_id is not None else f"zone_name={zone_name}"
        raise RuntimeError(f"No NWAC products found for {zone_desc}")

    def published_dt(product):
        return _parse_iso_dt(product.get("published_time")) or datetime.datetime.fromtimestamp(
            0, tz=datetime.timezone.utc
        )

    candidates.sort(key=published_dt, reverse=True)
    return int(candidates[0]["id"])


def _danger_text(level):
    if level is None:
        return None
    try:
        return NWAC_DANGER_TEXT.get(int(level))
    except Exception:
        return None


def _extract_nwac_danger(danger_list) -> dict:
    danger = {"alpine": "N/A", "treeline": "N/A", "below_treeline": "N/A"}
    if not isinstance(danger_list, list) or not danger_list:
        return danger

    current = None
    for entry in danger_list:
        if isinstance(entry, dict) and entry.get("valid_day") == "current":
            current = entry
            break
    if current is None:
        current = danger_list[0] if isinstance(danger_list[0], dict) else {}

    for source_key, target_key in (
        ("upper", "alpine"),
        ("middle", "treeline"),
        ("lower", "below_treeline"),
    ):
        text = _danger_text(current.get(source_key))
        if text:
            danger[target_key] = text
    return danger


def _pick_latest_caic_product_id(products, zone_name: str) -> int:
    zone_target = str(zone_name).strip().casefold()
    candidates = []
    for product in products:
        if not isinstance(product, dict):
            continue
        for zone in product.get("forecast_zone") or []:
            if str(zone.get("name") or "").strip().casefold() == zone_target:
                candidates.append(product)
                break

    if not candidates:
        raise RuntimeError(f"No CAIC products found for zone_name={zone_name}")

    newest_dt = None
    newest = []
    for product in candidates:
        published = _parse_iso_dt(product.get("published_time"))
        if newest_dt is None or (published and published > newest_dt):
            newest_dt = published
            newest = [product]
        elif published == newest_dt:
            newest.append(product)

    newest.sort(key=lambda product: len(product.get("forecast_zone") or []))
    return int(newest[0]["id"])


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


def _fetch_nwac_forecast(resort_name: str, nwac_meta: dict) -> dict:
    zone_id = nwac_meta.get("zone_id")
    zone_id = str(zone_id) if zone_id is not None else None
    zone_name = str(nwac_meta["zone_name"])
    products = _get_center_products(NWAC_CENTER_ID)
    product_id = _pick_latest_nwac_product_id(products, zone_id=zone_id, zone_name=zone_name)
    try:
        resp = requests.get(f"{NWAC_API_BASE}/product/{product_id}", headers=AVY_HEADERS, timeout=20)
    except Exception as e:
        raise RuntimeError(f"NWAC forecast fetch failed for {resort_name}: {e}")
    if resp.status_code != 200:
        raise RuntimeError(f"NWAC forecast HTTP {resp.status_code}: {resp.text[:200]}")
    try:
        payload = resp.json() if resp.content else {}
    except Exception as e:
        raise RuntimeError(f"Failed to parse NWAC forecast JSON: {e}")
    if not isinstance(payload, dict):
        raise RuntimeError(f"Unexpected NWAC product payload type: {type(payload)!r}")

    issued_raw = payload.get("published_time") or payload.get("publishedTime")
    issued_dt = _parse_iso_dt(issued_raw)
    issued_fmt = issued_dt.strftime("%b %d %H:%M %Z") if issued_dt else (issued_raw or "")

    summary_html = payload.get("bottom_line") or payload.get("hazard_discussion") or ""
    return {
        "title": f"{zone_name} Avalanche Forecast",
        "region": zone_name,
        "danger": _extract_nwac_danger(payload.get("danger") or []),
        "summary": _html_to_text(summary_html) or "No summary text available.",
        "issued": issued_fmt,
    }


def _fetch_caic_forecast(resort_name: str, caic_meta: dict) -> dict:
    zone_name = str(caic_meta["zone_name"])
    products = _get_center_products(CAIC_CENTER_ID, limit=CAIC_PRODUCTS_LIMIT)
    product_id = _pick_latest_caic_product_id(products, zone_name)
    try:
        resp = requests.get(f"{NWAC_API_BASE}/product/{product_id}", headers=AVY_HEADERS, timeout=20)
    except Exception as e:
        raise RuntimeError(f"CAIC forecast fetch failed for {resort_name}: {e}")
    if resp.status_code != 200:
        raise RuntimeError(f"CAIC forecast HTTP {resp.status_code}: {resp.text[:200]}")
    try:
        payload = resp.json() if resp.content else {}
    except Exception as e:
        raise RuntimeError(f"Failed to parse CAIC forecast JSON: {e}")
    if not isinstance(payload, dict):
        raise RuntimeError(f"Unexpected CAIC product payload type: {type(payload)!r}")

    issued_raw = payload.get("published_time") or payload.get("publishedTime")
    issued_dt = _parse_iso_dt(issued_raw)
    issued_fmt = issued_dt.strftime("%b %d %H:%M %Z") if issued_dt else (issued_raw or "")

    summary_html = payload.get("bottom_line") or payload.get("hazard_discussion") or ""
    return {
        "title": f"{zone_name} Avalanche Forecast",
        "region": zone_name,
        "danger": _extract_nwac_danger(payload.get("danger") or []),
        "summary": _html_to_text(summary_html) or "No summary text available.",
        "issued": issued_fmt,
    }


def _fetch_resort_forecast(resort_name: str, point) -> dict:
    nwac_meta = NWAC_RESORTS.get(resort_name)
    if nwac_meta:
        return _fetch_nwac_forecast(resort_name, nwac_meta)

    caic_meta = CAIC_RESORTS.get(resort_name)
    if caic_meta:
        return _fetch_caic_forecast(resort_name, caic_meta)

    if not point:
        raise RuntimeError(
            f"No lat/lon for '{resort_name}' in Snow API or {RESORT_META_FILE}."
        )

    lat, lon = point
    return _fetch_point_forecast(lat, lon)

