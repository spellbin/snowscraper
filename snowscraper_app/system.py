"""Operating-system integration for the Snow Scraper appliance.

The GUI itself should not need to know how journald, systemd transient units,
Git checkouts, GitHub releases, or watchdog heartbeat files work.  This module
contains those appliance-level concerns and documents their side effects.

Important behavior retained from the original implementation:

* journald is changed only on a systemd host running as root;
* release updates use a transient systemd unit when available and an inline Git
  checkout otherwise;
* heartbeat writes prefer /run (RAM) and maintain the legacy on-disk pathname as
  a symlink for existing watchdogs; and
* all probes fail soft so the touchscreen can continue operating.
"""

import os
import re
import shlex
import subprocess
import textwrap
import time
from typing import Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .health import health_reporter


REPO_URL = "https://github.com/spellbin/snowscraper.git"
LOCAL_REPO_PATH = "/home/pi/snowscraper"
SERVICE_NAME = "snowscraper.service"
UPDATER_UNIT = "snowgui-updater"
VERSION_FILE = os.path.join(LOCAL_REPO_PATH, "VERSION")
MAX_RETRIES = 3
RETRY_DELAY = 5
GITHUB_TOKEN = None

HEARTBEAT_FILE = "/home/pi/snowscraper/heartbeat.txt"
HEARTBEAT_RAM_FILE = "/run/heartbeat.txt"
HEARTBEAT_INTERVAL = 10

# Volatile journald storage reduces routine writes to the Raspberry Pi SD card.
# The limits match the original inline configuration exactly.
JOURNALD_DROPIN_DIR = "/etc/systemd/journald.conf.d"
JOURNALD_VOLATILE_CONF = os.path.join(JOURNALD_DROPIN_DIR, "volatile.conf")
JOURNALD_VOLATILE_CONTENT = """[Journal]
Storage=volatile
RuntimeMaxUse=50M
RuntimeKeepFree=10M
RuntimeMaxFileSize=10M
"""


def _is_systemd() -> bool:
    try:
        return os.path.isdir("/run/systemd/system")
    except Exception:
        return False

def _is_root() -> bool:
    try:
        return hasattr(os, "geteuid") and os.geteuid() == 0
    except Exception:
        return False

def _read_effective_journald_storage() -> Optional[str]:
    """
    Returns the effective Storage= mode for journald, or None if unknown.
    Prefers systemd-analyze to read the merged config; falls back to dir heuristics.
    """
    # Preferred: merged config view
    try:
        res = subprocess.run(
            ["systemd-analyze", "cat-config", "systemd/journald.conf"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        if res.stdout:
            for line in res.stdout.splitlines():
                m = re.match(r"^\s*Storage\s*=\s*(\w+)", line)
                if m:
                    return m.group(1).strip().lower()
    except Exception as e:
        print(f"[Journald] systemd-analyze probe failed: {e}")

    # Heuristic: presence of volatile vs persistent log dirs
    try:
        if os.path.isdir("/run/log/journal") and not os.path.isdir("/var/log/journal"):
            return "volatile"
        if os.path.isdir("/var/log/journal"):
            return "persistent"
    except Exception:
        pass
    return None

def _write_journald_volatile_dropin() -> bool:
    """
    Writes the drop-in that forces volatile journald storage.
    Returns True on success.
    """
    try:
        os.makedirs(JOURNALD_DROPIN_DIR, exist_ok=True)
        # Write atomically to avoid partial configs
        tmp_path = JOURNALD_VOLATILE_CONF + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(JOURNALD_VOLATILE_CONTENT)
        os.replace(tmp_path, JOURNALD_VOLATILE_CONF)
        return True
    except Exception as e:
        print(f"[Journald] Failed to write drop-in: {e}")
        return False

def ensure_journald_volatile():
    """
    Ensures journald writes only to RAM (Storage=volatile). Safe no-op if already set.
    """
    if not _is_systemd():
        print("[Journald] Not running under systemd; skipping journald configuration check.")
        return

    current = _read_effective_journald_storage()
    if current == "volatile":
        print("[Journald] Storage already volatile; no action needed.")
        return

    if not _is_root():
        print("[Journald] WARNING: need root to enforce volatile journald storage.")
        return

    if not _write_journald_volatile_dropin():
        print("[Journald] ERROR: could not write volatile drop-in.")
        return

    try:
        subprocess.run(
            ["systemctl", "restart", "systemd-journald.service"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        print("[Journald] Requested systemd-journald restart to apply volatile storage.")
    except Exception as e:
        print(f"[Journald] WARNING: failed to restart journald: {e}")

    # Re-check effective config to confirm
    post = _read_effective_journald_storage()
    if post != "volatile":
        print(f"[Journald] WARNING: expected volatile storage, detected '{post}'.")
    else:
        print("[Journald] Volatile storage confirmed.")

def _update_inline_git_checkout(version_str: str) -> bool:
    """
    Original inline update (used when systemd is not available).
    """
    if not version_str:
        return False
    try:
        _ensure_git_safe_dir(LOCAL_REPO_PATH)

        if not os.path.exists(os.path.join(LOCAL_REPO_PATH, ".git")):
            print("Repository not found, cloning...")
            subprocess.run(
                ["git", "clone", REPO_URL, LOCAL_REPO_PATH],
                check=True, capture_output=True, text=True
            )

        subprocess.run(
            ["git", "fetch", "--all", "--tags"],
            cwd=LOCAL_REPO_PATH, check=True, capture_output=True, text=True
        )
        subprocess.run(
            ["git", "checkout", f"tags/{version_str}", "-f"],
            cwd=LOCAL_REPO_PATH, check=True, capture_output=True, text=True
        )

        with open(VERSION_FILE, "w") as f:
            f.write(version_str)

        return True

    except subprocess.CalledProcessError as e:
        print(f"[Update] Inline update failed: {e.stderr}")
        return False
    except Exception as e:
        print(f"[Update] Inline update error: {e}")
        return False

def _systemd_run_update(version_str: str) -> bool:
    """
    Launch the updater as a transient systemd unit (older systemd compatible).
    - Avoids --replace (not present on some Raspberry Pi builds).
    - Uses a unique unit name to prevent collisions.
    - Probes for --collect support.
    - Requires running as root (system scope).
    Returns True if the transient unit was started successfully.
    """
    import os, time, textwrap, subprocess

    # Must be root to create a *system* transient unit.
    if os.geteuid() != 0:
        print("[Update] Not running as root: cannot create a system transient unit.")
        return False

    # Probe systemd-run flags on this OS
    try:
        help_txt = subprocess.run(
            ["systemd-run", "--help"], capture_output=True, text=True
        ).stdout
    except Exception as e:
        print(f"[Update] systemd-run unavailable: {e}")
        return False

    def _has(flag: str) -> bool:
        # simple string probe is sufficient for our needs
        return flag in help_txt

    # Unique unit name so we don't need --replace
    unit_name = f"snowgui-updater-{int(time.time())}"

    # The payload script the unit will execute
    script = textwrap.dedent(f"""\
        set -euo pipefail

        REPO=/home/pi/snowscraper
        TAG="{version_str}"

        # --- find runuser (path can vary on some images) ---------------------
        RUNUSER="$(command -v runuser || true)"
        if [ -z "$RUNUSER" ]; then
          for CAND in /sbin/runuser /usr/sbin/runuser /bin/runuser /usr/bin/runuser; do
            [ -x "$CAND" ] && RUNUSER="$CAND" && break
          done
        fi
        if [ -z "$RUNUSER" ]; then
          echo "[Updater] ERROR: runuser not found."
          exit 127
        fi

        # --- detect service to stop/start (non-fatal if missing) ------------
        detect_service() {{
          local candidates="snowscraper.service snowgui.service"
          local picked=""
          for S in $candidates; do
            systemctl list-unit-files | awk '{{print $1}}' | grep -xq "$S" && {{ picked="$S"; break; }}
            systemctl status "$S" >/dev/null 2>&1 && {{ picked="$S"; break; }}
          done
          echo "$picked"
        }}

        SVC="$(detect_service || true)"
        if [ -n "$SVC" ]; then
          echo "[Updater] Using service: $SVC"
          echo "[Updater] Stopping $SVC"
          systemctl stop "$SVC" || echo "[Updater] WARNING: stop failed; continuing."
        else
          echo "[Updater] WARNING: No matching service found; proceeding without stop/start."
        fi

        echo "[Updater] Ensuring repo exists: $REPO"
        if [ ! -d "$REPO/.git" ]; then
          echo "[Updater] ERROR: $REPO is not a git repo"
          [ -n "$SVC" ] && systemctl start "$SVC" || true
          exit 128
        fi

        echo "[Updater] Fetching tags (as pi)"
        "$RUNUSER" -u pi -- git -c safe.directory="$REPO" -C "$REPO" fetch --all --prune --tags

        echo "[Updater] Verifying tag $TAG exists"
        "$RUNUSER" -u pi -- git -c safe.directory="$REPO" -C "$REPO" rev-parse "refs/tags/$TAG" >/dev/null

        echo "[Updater] Checking out tag $TAG (force)"
        "$RUNUSER" -u pi -- git -c safe.directory="$REPO" -C "$REPO" checkout -f "tags/$TAG"

        echo "[Updater] Writing VERSION file"
        printf "%s" "$TAG" | "$RUNUSER" -u pi -- tee "$REPO/VERSION" >/dev/null

        if [ -n "$SVC" ]; then
          echo "[Updater] Starting $SVC"
          systemctl start "$SVC" || echo "[Updater] WARNING: start failed."
        fi

        echo "[Updater] Done."
    """)

    # Build the systemd-run command with only flags supported on this box
    cmd = [
        "systemd-run",
        "--quiet",
        "--unit", unit_name,
        "--property=Type=oneshot",
        "--property=RemainAfterExit=no",
    ]
    if _has("--collect"):
        cmd.append("--collect")

    # Environment (harmless even if the script doesn't use VER directly)
    cmd += [
        "--setenv", "GIT_TERMINAL_PROMPT=0",
        "--setenv", "HOME=/home/pi",
        "--setenv", f"VER={version_str}",
        "/bin/bash", "-lc", script,
    ]

    try:
        res = subprocess.run(cmd, check=True, capture_output=True, text=True)
        out = res.stdout.strip()
        if out:
            print(f"[Update] systemd-run started: {out}")
        else:
            print(f"[Update] systemd-run started unit {unit_name}")
        return True
    except subprocess.CalledProcessError as e:
        print(f"[Update] systemd-run failed (rc={e.returncode})")
        if e.stdout:
            print(f"[Update] stdout:\n{e.stdout}")
        if e.stderr:
            print(f"[Update] stderr:\n{e.stderr}")
        return False

def create_github_session():
    session = requests.Session()
    retry = Retry(total=MAX_RETRIES, backoff_factor=1, status_forcelist=[500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def get_local_version():
    try:
        if not os.path.exists(VERSION_FILE):
            return None
        with open(VERSION_FILE, "r") as f:
            version_str = f.read().strip()
            return version_str if version_str else None
    except Exception as e:
        print(f"Error reading local version: {e}")
        return None


def get_remote_version():
    repo_path = REPO_URL.replace("https://github.com/", "").replace(".git", "")
    api_url = f"https://api.github.com/repos/{repo_path}/releases/latest"

    headers = {"Accept": "application/vnd.github.v3+json"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"token {GITHUB_TOKEN}"

    session = create_github_session()
    try:
        response = session.get(api_url, timeout=10, headers=headers)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json().get("tag_name")
    except Exception as e:
        print(f"Error fetching remote version: {e}")
        return None
    finally:
        session.close()

def _ensure_git_safe_dir(repo_path):
    """
    Mark the given repository as 'safe' for Git if not already.
    This prevents 'dubious ownership' errors when running as another user.
    """
    try:
        # Check if Git already lists it as safe
        result = subprocess.run(
            ["git", "config", "--global", "--get-all", "safe.directory"],
            capture_output=True,
            text=True
        )
        if repo_path not in result.stdout:
            subprocess.run(
                ["git", "config", "--global", "--add", "safe.directory", repo_path],
                check=True
            )
            print(f"[Update] Added {repo_path} to git safe.directory.")
    except Exception as e:
        print(f"[Update] Could not mark repo safe: {e}")


def update(version_str: str) -> bool:
    """
    Systemd-aware update wrapper:
      - If systemd is present, launch a transient updater unit and return True
        if it was launched successfully (actual update runs in that unit).
      - Otherwise, run the inline git checkout.
    """
    if _is_systemd():
        return _systemd_run_update(version_str)
    return _update_inline_git_checkout(version_str)


def _ensure_heartbeat_symlink() -> bool:
    """
    Make sure the disk-based heartbeat path points at the RAM-backed file.
    Falls back quietly on errors; callers may still write the disk file directly.
    """
    try:
        # Remove incorrect targets so we can recreate the symlink.
        if os.path.islink(HEARTBEAT_FILE):
            target = os.readlink(HEARTBEAT_FILE)
            if target == HEARTBEAT_RAM_FILE:
                return True
            os.unlink(HEARTBEAT_FILE)
        elif os.path.exists(HEARTBEAT_FILE):
            os.remove(HEARTBEAT_FILE)

        os.symlink(HEARTBEAT_RAM_FILE, HEARTBEAT_FILE)
        return True
    except Exception as e:
        print(f"[Heartbeat] Symlink setup failed: {e}")
        return False


def heartbeat():
    # Remote reporting is a separate daemon worker so a slow backend request can
    # never delay the ten-second local watchdog file update. Its anonymous ID and
    # opt-out preference are owned locally by the reporter, not by systemd.
    health_reporter.start(app_version=get_local_version())
    while True:
        ts = str(time.time())

        # Primary write goes to RAM to spare the disk.
        try:
            with open(HEARTBEAT_RAM_FILE, "w") as f:
                f.write(ts)
        except Exception as e:
            print(f"[Heartbeat] Write to RAM file failed: {e}")

        # Ensure watchdog path continues to work.
        linked = _ensure_heartbeat_symlink()
        if not linked:
            try:
                with open(HEARTBEAT_FILE, "w") as f:
                    f.write(ts)
            except Exception as e:
                print(f"[Heartbeat] Fallback write failed: {e}")

        time.sleep(HEARTBEAT_INTERVAL)
