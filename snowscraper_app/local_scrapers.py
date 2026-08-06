"""Discovery, validation, and isolated execution for user-created scrapers.

Local scraper modules live under ``conf/local_scrapers`` so the application's
force-checkout updater does not overwrite them. Each module contains a small
``module.ini`` manifest, a ``scraper.py`` function, and an optional ``ENABLED``
marker. The custom function receives a BeautifulSoup document; the framework
owns network requests, resource limits, output validation, and integration with
the existing ``skiHill`` object.

Modules are ordinary Python and therefore trusted local code, not a security
sandbox. Runtime execution still occurs in a child process with a wall-clock
timeout so a syntax error, exception, or stuck parser cannot freeze the 10 Hz
touchscreen loop indefinitely.
"""

import configparser
from dataclasses import dataclass
import datetime
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Optional


APP_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MODULE_ROOT = APP_ROOT / "conf" / "local_scrapers"
MODULE_ROOT_ENV = "SNOWSCRAPER_LOCAL_SCRAPERS_DIR"
MANIFEST_NAME = "module.ini"
MODULE_CODE_NAME = "scraper.py"
ENABLED_MARKER_NAME = "ENABLED"
MODULE_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{1,47}$")
MAX_MEASUREMENT_CM = 10000
MIN_TIMEOUT_SECONDS = 3.0
MAX_TIMEOUT_SECONDS = 60.0


class LocalScraperError(RuntimeError):
    """A module is invalid, failed, or returned an unsafe payload."""


@dataclass(frozen=True)
class LocalScraperModule:
    """Validated configuration for one user-created BeautifulSoup module."""

    module_id: str
    directory: Path
    resort_name: str
    source_url: str
    country: str
    region: str
    latitude: Optional[float]
    longitude: Optional[float]
    timeout_seconds: float
    fallback_to_snow_api: bool
    enabled: bool

    @property
    def code_path(self) -> Path:
        return self.directory / MODULE_CODE_NAME

    @property
    def manifest_path(self) -> Path:
        return self.directory / MANIFEST_NAME

    @property
    def enabled_path(self) -> Path:
        return self.directory / ENABLED_MARKER_NAME


def module_root(path=None) -> Path:
    """Resolve the preserved user-module directory with an advanced override."""
    if path is not None:
        return Path(path)
    configured = os.getenv(MODULE_ROOT_ENV, "").strip()
    return Path(configured) if configured else DEFAULT_MODULE_ROOT


def _required(section, key: str, manifest_path: Path) -> str:
    value = str(section.get(key, "")).strip()
    if not value:
        raise LocalScraperError(f"{manifest_path}: missing required '{key}'")
    if "\n" in value or "\r" in value:
        raise LocalScraperError(f"{manifest_path}: '{key}' must be one line")
    return value


def _optional_float(section, key: str, manifest_path: Path) -> Optional[float]:
    raw = str(section.get(key, "")).strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError as exc:
        raise LocalScraperError(f"{manifest_path}: '{key}' must be a number") from exc


def load_module(directory) -> LocalScraperModule:
    """Load and strictly validate one module directory."""
    directory = Path(directory)
    module_id = directory.name
    manifest_path = directory / MANIFEST_NAME
    code_path = directory / MODULE_CODE_NAME
    if not MODULE_ID_RE.fullmatch(module_id):
        raise LocalScraperError(
            f"{directory}: folder name must match {MODULE_ID_RE.pattern}"
        )
    if not manifest_path.is_file():
        raise LocalScraperError(f"{directory}: missing {MANIFEST_NAME}")
    if not code_path.is_file():
        raise LocalScraperError(f"{directory}: missing {MODULE_CODE_NAME}")

    parser = configparser.ConfigParser(interpolation=None)
    try:
        with manifest_path.open("r", encoding="utf-8") as manifest_file:
            parser.read_file(manifest_file)
    except (OSError, configparser.Error) as exc:
        raise LocalScraperError(f"{manifest_path}: cannot read manifest: {exc}") from exc
    if "local_scraper" not in parser:
        raise LocalScraperError(f"{manifest_path}: missing [local_scraper] section")
    section = parser["local_scraper"]

    resort_name = _required(section, "resort_name", manifest_path)
    source_url = _required(section, "source_url", manifest_path)
    if not source_url.startswith(("https://", "http://")):
        raise LocalScraperError(
            f"{manifest_path}: source_url must begin with https:// or http://"
        )
    country = str(section.get("country", "")).strip()
    region = str(section.get("region", "")).strip()
    latitude = _optional_float(section, "latitude", manifest_path)
    longitude = _optional_float(section, "longitude", manifest_path)
    if latitude is not None and not -90 <= latitude <= 90:
        raise LocalScraperError(f"{manifest_path}: latitude must be -90..90")
    if longitude is not None and not -180 <= longitude <= 180:
        raise LocalScraperError(f"{manifest_path}: longitude must be -180..180")

    try:
        timeout_seconds = float(section.get("timeout_seconds", "20"))
    except ValueError as exc:
        raise LocalScraperError(
            f"{manifest_path}: timeout_seconds must be a number"
        ) from exc
    if not MIN_TIMEOUT_SECONDS <= timeout_seconds <= MAX_TIMEOUT_SECONDS:
        raise LocalScraperError(
            f"{manifest_path}: timeout_seconds must be "
            f"{MIN_TIMEOUT_SECONDS:g}..{MAX_TIMEOUT_SECONDS:g}"
        )
    try:
        fallback = section.getboolean("fallback_to_snow_api", fallback=True)
    except ValueError as exc:
        raise LocalScraperError(
            f"{manifest_path}: fallback_to_snow_api must be true or false"
        ) from exc

    return LocalScraperModule(
        module_id=module_id,
        directory=directory,
        resort_name=resort_name,
        source_url=source_url,
        country=country,
        region=region,
        latitude=latitude,
        longitude=longitude,
        timeout_seconds=timeout_seconds,
        fallback_to_snow_api=fallback,
        enabled=(directory / ENABLED_MARKER_NAME).is_file(),
    )


def discover_modules(root=None) -> tuple[list[LocalScraperModule], list[str]]:
    """Return valid modules and human-readable errors without crashing the GUI."""
    root_path = module_root(root)
    if not root_path.is_dir():
        return [], []
    modules = []
    errors = []
    for directory in sorted(root_path.iterdir(), key=lambda path: path.name.casefold()):
        if not directory.is_dir() or directory.name.startswith("."):
            continue
        try:
            modules.append(load_module(directory))
        except LocalScraperError as exc:
            errors.append(str(exc))
    return modules, errors


def find_enabled_module(resort_name: str, root=None) -> Optional[LocalScraperModule]:
    """Resolve the one enabled module attached to a resort display name."""
    target = str(resort_name or "").strip().casefold()
    modules, errors = discover_modules(root)
    for error in errors:
        print(f"[LocalScraper] Ignoring invalid module: {error}")
    matches = [
        module for module in modules
        if module.enabled and module.resort_name.casefold() == target
    ]
    if len(matches) > 1:
        names = ", ".join(module.module_id for module in matches)
        raise LocalScraperError(
            f"Multiple enabled local modules target '{resort_name}': {names}"
        )
    return matches[0] if matches else None


def merge_enabled_module_metadata(base_meta: dict, root=None) -> dict:
    """Append enabled local-only resorts while preserving canonical API order.

    If a module targets an existing resort, canonical metadata wins for fields
    it already supplies. Module metadata fills only missing fields and records
    the local module ID for diagnostics.
    """
    modules, errors = discover_modules(root)
    for error in errors:
        print(f"[LocalScraper] Metadata skipped invalid module: {error}")
    enabled = [module for module in modules if module.enabled]
    if not enabled:
        return base_meta
    merged = dict(base_meta or {})
    for module in enabled:
        # Treat display names case-insensitively for attachment so a harmless
        # capitalization mismatch cannot create a duplicate picker entry.
        attached_name = next(
            (
                name for name in merged
                if str(name).casefold() == module.resort_name.casefold()
            ),
            module.resort_name,
        )
        entry = dict(merged.get(attached_name) or {})
        entry.setdefault("name", attached_name)
        entry.setdefault("slug", attached_name.replace(" ", "_"))
        if module.country:
            entry.setdefault("country", module.country)
        if module.region:
            entry.setdefault("region", module.region)
        if module.latitude is not None:
            entry.setdefault("lat", module.latitude)
        if module.longitude is not None:
            entry.setdefault("lon", module.longitude)
        entry["local_scraper_id"] = module.module_id
        merged[attached_name] = entry
    return merged


def number_from_text(value, default=None):
    """Extract the first signed decimal from text or a BeautifulSoup element.

    Examples: ``"12 cm"`` -> ``12`` and ``"5.5 in"`` -> ``5.5``. Missing
    elements return ``default`` so a selector that finds no value can represent
    unavailable data as ``None`` instead of fabricating zero.
    """
    if value is None:
        return default
    if hasattr(value, "get_text"):
        value = value.get_text(" ", strip=True)
    match = re.search(r"[-+]?\d+(?:[.,]\d+)?", str(value))
    if not match:
        return default
    try:
        number = float(match.group(0).replace(",", "."))
        return int(number) if number.is_integer() else number
    except ValueError:
        return default


def inches_to_cm(value):
    """Convert an inch measurement to rounded whole centimetres."""
    if value is None:
        return None
    try:
        return int(round(float(value) * 2.54))
    except (TypeError, ValueError):
        return None


def normalize_scrape_result(result: dict) -> dict:
    """Validate a custom module result and build the canonical current block."""
    if not isinstance(result, dict):
        raise LocalScraperError("scrape(soup) must return a dictionary")
    allowed = {"date", "newSnow", "daySnow", "weekSnow", "baseSnow"}
    unknown = sorted(set(result) - allowed)
    if unknown:
        raise LocalScraperError(f"unsupported result field(s): {', '.join(unknown)}")

    normalized = {}
    for field in ("newSnow", "daySnow", "weekSnow", "baseSnow"):
        value = result.get(field)
        if value is None:
            normalized[field] = None
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise LocalScraperError(f"{field} must be a number or None")
        rounded = int(round(value))
        if not 0 <= rounded <= MAX_MEASUREMENT_CM:
            raise LocalScraperError(
                f"{field} must be between 0 and {MAX_MEASUREMENT_CM} cm"
            )
        normalized[field] = rounded
    if all(normalized[field] is None for field in normalized):
        raise LocalScraperError(
            "scrape(soup) returned no snow values; check the CSS selectors"
        )

    date_text = str(result.get("date") or datetime.date.today().isoformat()).strip()
    try:
        datetime.date.fromisoformat(date_text)
    except ValueError as exc:
        raise LocalScraperError("date must use YYYY-MM-DD format") from exc
    return {"date": date_text, **normalized}


def run_local_scraper(
    module: LocalScraperModule,
    *,
    fixture_path=None,
) -> dict:
    """Run a module in a child Python process and return a Snow API-like object."""
    command = [
        sys.executable,
        "-B",
        "-m",
        "snowscraper_app.local_scraper_runner",
        str(module.directory),
    ]
    if fixture_path is not None:
        command.extend(["--fixture", str(fixture_path)])
    try:
        completed = subprocess.run(
            command,
            cwd=str(APP_ROOT),
            capture_output=True,
            text=True,
            timeout=module.timeout_seconds + 5.0,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise LocalScraperError(
            f"module '{module.module_id}' exceeded its {module.timeout_seconds:g}s timeout"
        ) from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "unknown error").strip()
        raise LocalScraperError(
            f"module '{module.module_id}' failed: {detail[-1000:]}"
        )
    diagnostics = completed.stderr.strip()
    if diagnostics:
        print(f"[LocalScraper:{module.module_id}] {diagnostics[-2000:]}")
    try:
        envelope = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise LocalScraperError(
            f"module '{module.module_id}' returned an invalid runner response"
        ) from exc
    if not isinstance(envelope, dict) or not isinstance(envelope.get("current"), dict):
        raise LocalScraperError(
            f"module '{module.module_id}' returned an incomplete runner response"
        )
    return envelope
