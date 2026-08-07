"""Optional anonymous operational health reporting.

SnowScraper installations in customers' homes have no fleet-management
channel and are not backend-managed Powder Oracle devices. On first launch,
this module creates a random pseudonymous installation ID in a gitignored local
state file. The reporter sends only application health: version, selected
resort, process uptime, last successful Snow API fetch, and current fetch error.
It deliberately does not read or send a hostname, account, device claim,
configuration, bearer token, or other customer identity.

Reporting defaults on so already-deployed open-source units can participate as
soon as they install a release containing this module. Customers can disable it
from the touchscreen; that preference is persisted beside the anonymous ID and
survives git-based application updates. Reporting is always fail-soft and runs
in a daemon thread, so backend outages never interrupt the GUI, local watchdog,
snow fetching, LEDs, or alarms. An outage is logged once rather than once per
attempt, retried with exponential backoff, and every interval carries jitter so
the fleet does not re-converge on the backend in lockstep after one.
"""

import datetime
import json
import os
from pathlib import Path
import random
import re
import threading
import time
from typing import Optional
import uuid

import requests


DEFAULT_HEARTBEAT_URL = (
    "https://plow.snowscraper.ca/api/v1/snowscraper/heartbeat"
)
# Ten minutes. This is a liveness signal with a 30-minute staleness threshold,
# not a metrics feed: a minute-by-minute cadence bought no extra fidelity and
# cost 1440 requests per installation per day. Keep this in step with the
# backend's SNOWSCRAPER_STALE_AFTER_SECONDS, which must stay comfortably above
# it or the whole fleet reads stale between check-ins.
DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 600.0
DEFAULT_HEARTBEAT_TIMEOUT_SECONDS = 10.0
# Spread the fleet's timers. Without it every Pi restarted by the same power cut
# stays aligned forever and re-converges on the backend together after any
# outage. +/-10% of the interval is enough to smear a herd across a minute.
HEARTBEAT_JITTER_FRACTION = 0.1
# A backend outage should not be hammered every interval by every installation.
# 10 min -> at most 1 h between attempts while it stays down.
HEARTBEAT_MAX_BACKOFF_MULTIPLIER = 6
# Bounds on a server-supplied cadence, so a bad or hostile `next_heartbeat_seconds`
# can neither turn this into a busy loop nor quietly silence reporting.
MIN_ACCEPTED_INTERVAL_SECONDS = 60.0
MAX_ACCEPTED_INTERVAL_SECONDS = 21600.0
DEFAULT_STATE_PATH = Path(__file__).resolve().parent.parent / "conf" / "health.json"
HEARTBEAT_USER_AGENT = "SnowGUI-Health"
MAX_REPORTED_ERROR_LENGTH = 1000
SCRAPER_ID_RE = re.compile(r"^ss_[0-9a-f]{32}$")


def _utc_now_text() -> str:
    """Return a compact, timezone-explicit timestamp for the wire payload."""
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _positive_float(name: str, default: float) -> float:
    """Read a positive numeric environment setting with a safe fallback."""
    try:
        value = float(os.getenv(name, str(default)))
        return value if value > 0 else default
    except (TypeError, ValueError):
        return default


def _default_reporting_enabled() -> bool:
    """Allow an advanced local install to default out through its environment."""
    value = os.getenv("SNOWSCRAPER_HEALTH_REPORTING", "1").strip().casefold()
    return value not in {"0", "false", "no", "off", "disabled"}


class RemoteHealthReporter:
    """Thread-safe, pseudonymous current-state reporter for one installation."""

    def __init__(self, state_path=None):
        self.url = (
            os.getenv("SNOWSCRAPER_HEARTBEAT_URL", DEFAULT_HEARTBEAT_URL).strip()
            or DEFAULT_HEARTBEAT_URL
        )
        self.interval_seconds = _positive_float(
            "SNOWSCRAPER_HEARTBEAT_INTERVAL_SECONDS",
            DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
        )
        # An operator who set the interval explicitly outranks the backend's
        # suggestion; see _adopt_server_interval.
        self._interval_pinned = bool(
            os.getenv("SNOWSCRAPER_HEARTBEAT_INTERVAL_SECONDS", "").strip()
        )
        self.timeout_seconds = _positive_float(
            "SNOWSCRAPER_HEARTBEAT_TIMEOUT_SECONDS",
            DEFAULT_HEARTBEAT_TIMEOUT_SECONDS,
        )
        self.state_path = Path(state_path or DEFAULT_STATE_PATH)
        self._started_at = time.monotonic()
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread = None
        self._app_version = None
        self._selected_resort = None
        self._last_snow_fetch_at = None
        self._last_error = None
        self._failure_streak = 0
        self._last_failure_text = None

        # State creation is local-only and must never make application import
        # fail. If the filesystem is temporarily read-only, the runtime ID still
        # works for this process; a later successful launch will persist one.
        state = self._load_state()
        existing_id = state.get("scraper_id")
        self.scraper_id = (
            existing_id
            if isinstance(existing_id, str) and SCRAPER_ID_RE.fullmatch(existing_id)
            else f"ss_{uuid.uuid4().hex}"
        )
        enabled = state.get("reporting_enabled")
        self._reporting_enabled = (
            enabled if isinstance(enabled, bool) else _default_reporting_enabled()
        )
        if existing_id != self.scraper_id or not isinstance(enabled, bool):
            self._persist_state()

    @property
    def reporting_enabled(self) -> bool:
        """Return the customer's persisted anonymous-reporting preference."""
        with self._lock:
            return self._reporting_enabled

    def _load_state(self) -> dict:
        """Read valid local state; malformed or missing files are regenerated."""
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError, TypeError):
            return {}

    def _persist_state(self) -> bool:
        """Atomically save only the random ID and the opt-out preference."""
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.state_path.with_name(
                f".{self.state_path.name}.{os.getpid()}.tmp"
            )
            document = {
                "scraper_id": self.scraper_id,
                "reporting_enabled": self._reporting_enabled,
            }
            temporary.write_text(
                json.dumps(document, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.state_path)
            return True
        except OSError as exc:
            print(f"[RemoteHealth] Could not save local preference: {exc}")
            return False

    def set_reporting_enabled(self, enabled: bool) -> bool:
        """Persist an on-device opt-in/out change and wake the worker promptly."""
        with self._lock:
            self._reporting_enabled = bool(enabled)
            saved = self._persist_state()
        # Waking makes enablement report immediately and makes disablement take
        # effect without waiting for the remainder of the sixty-second interval.
        self._wake_event.set()
        return saved

    def set_app_version(self, version: Optional[str]) -> None:
        with self._lock:
            text = str(version or "").strip()
            self._app_version = text[:96] or None

    def set_selected_resort(self, resort: Optional[str]) -> None:
        with self._lock:
            text = str(resort or "").strip()
            self._selected_resort = text[:160] or None

    def record_snow_fetch_success(self, resort: Optional[str] = None) -> None:
        """Record a verified Snow API fetch and clear its prior fetch error."""
        with self._lock:
            if resort:
                self._selected_resort = str(resort).strip()[:160] or None
            self._last_snow_fetch_at = _utc_now_text()
            self._last_error = None

    def record_snow_fetch_failure(self, error, resort: Optional[str] = None) -> None:
        """Retain the last successful fetch time and publish the current error."""
        with self._lock:
            if resort:
                self._selected_resort = str(resort).strip()[:160] or None
            text = str(error or "Snow API fetch failed").strip()
            self._last_error = text[:MAX_REPORTED_ERROR_LENGTH]

    def payload(self) -> dict:
        """Build the intentionally minimal snapshot without holding during I/O."""
        with self._lock:
            return {
                "scraper_id": self.scraper_id,
                "app_version": self._app_version,
                "selected_resort": self._selected_resort,
                "uptime_seconds": max(0, int(time.monotonic() - self._started_at)),
                "last_snow_fetch_at": self._last_snow_fetch_at,
                "last_error": self._last_error,
                "reported_at": _utc_now_text(),
            }

    def _note_failure(self, exc) -> None:
        """Record a failed attempt, logging an outage once rather than forever.

        Heartbeats are frequent and fail-soft, so an unreachable backend used to
        write one line per attempt into journald indefinitely -- 1440 a day per
        installation at the old cadence. Log the start of an outage and any
        change in its cause; stay quiet for the identical failure repeating.
        """
        text = f"{type(exc).__name__}: {exc}"
        with self._lock:
            self._failure_streak += 1
            streak = self._failure_streak
            changed = text != self._last_failure_text
            self._last_failure_text = text
        if streak == 1 or changed:
            print(f"[RemoteHealth] Heartbeat failed ({text}); backing off, will retry.")

    def _note_success(self) -> None:
        """Clear the failure streak, reporting recovery if there was one."""
        with self._lock:
            streak = self._failure_streak
            self._failure_streak = 0
            self._last_failure_text = None
        if streak:
            print(f"[RemoteHealth] Heartbeat recovered after {streak} failed attempt(s).")

    def _adopt_server_interval(self, response) -> None:
        """Let the backend retune the cadence without a client release.

        This reporter is open source and updates on the customer's schedule, so
        the response is the only lever that reaches an already-deployed Pi. An
        explicit local setting still wins, and the value is bounds-checked.
        """
        if self._interval_pinned:
            return
        try:
            seconds = float((response.json() or {}).get("next_heartbeat_seconds"))
        except (AttributeError, TypeError, ValueError):
            return
        if not MIN_ACCEPTED_INTERVAL_SECONDS <= seconds <= MAX_ACCEPTED_INTERVAL_SECONDS:
            return
        with self._lock:
            changed = seconds != self.interval_seconds
            self.interval_seconds = seconds
        if changed:
            print(f"[RemoteHealth] Backend set the heartbeat interval to {seconds:g}s.")

    def _next_delay(self) -> float:
        """Seconds to wait before the next attempt: backoff, then jitter."""
        with self._lock:
            interval = self.interval_seconds
            streak = self._failure_streak
        backoff = min(2 ** streak, HEARTBEAT_MAX_BACKOFF_MULTIPLIER) if streak else 1
        delay = interval * backoff
        return max(1.0, delay * (1.0 + random.uniform(
            -HEARTBEAT_JITTER_FRACTION, HEARTBEAT_JITTER_FRACTION
        )))

    def send_once(self) -> bool:
        """Send one anonymous heartbeat, never disrupting the application."""
        if not self.reporting_enabled:
            return False
        try:
            response = requests.post(
                self.url,
                json=self.payload(),
                timeout=self.timeout_seconds,
                headers={
                    "Accept": "application/json",
                    "User-Agent": f"{HEARTBEAT_USER_AGENT}/{self._app_version or 'unknown'}",
                },
            )
            response.raise_for_status()
        except Exception as exc:
            self._note_failure(exc)
            return False
        self._adopt_server_interval(response)
        self._note_success()
        return True

    def start(self, app_version: Optional[str] = None) -> bool:
        """Start one daemon worker; repeated calls are harmless."""
        if app_version is not None:
            self.set_app_version(app_version)
        with self._lock:
            if self._thread and self._thread.is_alive():
                return True
            self._stop_event.clear()
            self._wake_event.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="snowscraper-remote-health",
                daemon=True,
            )
            self._thread.start()
            return True

    def stop(self) -> None:
        """Request worker shutdown; primarily used by tests and clean exits."""
        self._stop_event.set()
        self._wake_event.set()
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=min(self.timeout_seconds + 1.0, 15.0))

    def _run(self) -> None:
        # Send immediately on startup or opt-in. The separate wake event lets a
        # touchscreen preference change interrupt the ordinary reporting delay.
        while not self._stop_event.is_set():
            if self.reporting_enabled:
                self.send_once()
            woken = self._wake_event.wait(self._next_delay())
            self._wake_event.clear()
            if woken:
                # A preference change is a deliberate act at the glass; don't
                # make the customer wait out an outage backoff to see it apply.
                with self._lock:
                    self._failure_streak = 0


# Process-wide reporter shared by the resort fetcher, watchdog, and privacy UI.
health_reporter = RemoteHealthReporter()
