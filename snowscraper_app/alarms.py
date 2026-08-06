"""Alarm configuration and passive-buzzer playback.

This module owns both halves of the powder-day alarm:

* persisted scheduling state in alarm.conf; and
* non-blocking melody playback through Raspberry Pi GPIO 18.

Keeping them together makes the state transition that triggers the buzzer easy to
follow. GPIO remains optional so development machines behave exactly like the
original application: alarm timing still runs, but playback is logged instead of
sent to hardware.
"""

import datetime
import json
import os
import threading
import time

from .storage import atomic_write_json


ALARM_CONF_FILE = "/home/pi/snowscraper/conf/alarm.conf"

# The cache avoids reading the SD card on every pass through snowgui's main loop.
# An RLock is used because UI saves and alarm checks can occur from different
# threads, and save_alarm_cfg updates the cache while holding this same lock.
_alarm_cfg_cache = None
_alarm_cfg_lock = threading.RLock()


def _today_str():
    """Return the local calendar date used to reset once-per-day alarm state."""
    return datetime.datetime.now().strftime("%Y-%m-%d")


# Keep the historical private helper name used by the extracted implementation.
_atomic_write_json = atomic_write_json


def _default_alarm_cfg():
    return {
        "active": False,
        "active_anytime": False,
        "hour": "0",
        "minute": "0",
        "triggered_snow": "0",
        "incremental_snow": "0",
        "state": {"day": _today_str(), "triggered_today": False, "next_threshold": None},
    }


def load_alarm_cfg(force_reload: bool = False):
    """
    Lazily loads alarm.conf into memory and reuses the cached dict for future calls.
    Set force_reload=True to discard the cache and read from disk again.
    """
    global _alarm_cfg_cache
    with _alarm_cfg_lock:
        if _alarm_cfg_cache is not None and not force_reload:
            return _alarm_cfg_cache

        cfg = _default_alarm_cfg()
        try:
            if os.path.exists(ALARM_CONF_FILE):
                with open(ALARM_CONF_FILE, "r") as f:
                    disk = json.load(f)
                for k in ["active", "active_anytime", "hour", "minute", "triggered_snow", "incremental_snow"]:
                    if k in disk:
                        cfg[k] = disk[k]
                if isinstance(disk.get("state"), dict):
                    for k in ["day", "triggered_today", "next_threshold"]:
                        if k in disk["state"]:
                            cfg["state"][k] = disk["state"][k]
        except Exception as e:
            print(f"[Alarm] load_alarm_cfg error: {e}")

        _alarm_cfg_cache = cfg
        return _alarm_cfg_cache


def save_alarm_cfg(cfg):
    global _alarm_cfg_cache
    with _alarm_cfg_lock:
        try:
            _atomic_write_json(cfg, ALARM_CONF_FILE)
            _alarm_cfg_cache = cfg
            print("[Alarm] alarm.conf saved.")
        except Exception as e:
            print(f"[Alarm] save_alarm_cfg error: {e}")


def reset_state_if_new_day(cfg):
    today = _today_str()
    st = cfg["state"]
    if st.get("day") != today:
        st["day"] = today
        st["triggered_today"] = False
        base = max(0, int(cfg.get("triggered_snow") or "0"))
        st["next_threshold"] = base if cfg.get("active_anytime") else None
        save_alarm_cfg(cfg)


# ----------------------------
# Buzzer / Anthem (non-blocking)
# ----------------------------
try:
    import RPi.GPIO as GPIO  # Guarded import for dev machines
    _HAS_GPIO = True
except Exception:
    _HAS_GPIO = False

BUZZER_PIN = 18
NOTES = {
    "C4": 262,
    "E4": 330,
    "G4": 392,
    "B4": 494,
    "C5": 523,
    "REST": 0,
}

_CHORUS = [("G4", 0.18), ("E4", 0.18), ("C4", 0.18),
             ("G4", 0.18), ("E4", 0.18), ("C4", 0.18),
             ("C5", 0.18), ("B4", 0.18), ("G4", 0.45)]

_POWDER_DAY_ANTHEM = _CHORUS * 5

_pwm = None
_anthem_thread = None
_anthem_stop = threading.Event()
_anthem_lock = threading.Lock()


def _setup_buzzer():
    global _pwm
    if not _HAS_GPIO or _pwm is not None:
        return
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(BUZZER_PIN, GPIO.OUT)
    _pwm = GPIO.PWM(BUZZER_PIN, 440)


def _teardown_buzzer():
    global _pwm
    if not _HAS_GPIO:
        return
    try:
        if _pwm is not None:
            _pwm.stop()
        GPIO.cleanup()
    except Exception:
        pass
    _pwm = None


def _play_melody_blocking(melody, stop_event: threading.Event, pause_between_loops=6.0):
    if not _HAS_GPIO:
        while not stop_event.is_set():
            print("Ã°Å¸Å½Â¿ Powder Day Anthem (silent dev mode)")
            time.sleep(sum(d for _, d in melody) + pause_between_loops)
        return

    _setup_buzzer()
    while not stop_event.is_set():
        for note, dur in melody:
            if stop_event.is_set():
                break
            freq = NOTES.get(note, 0)
            if freq <= 0:
                _pwm.stop()
            else:
                _pwm.ChangeFrequency(freq)
                _pwm.start(50)  # duty
            time.sleep(dur)
        try:
            _pwm.stop()
        except Exception:
            pass
        # abortable pause
        for _ in range(int(pause_between_loops * 10)):
            if stop_event.is_set():
                break
            time.sleep(0.1)


def start_powder_day_anthem():
    global _anthem_thread
    with _anthem_lock:
        if _anthem_thread and _anthem_thread.is_alive():
            return
        _anthem_stop.clear()
        _anthem_thread = threading.Thread(
            target=_play_melody_blocking,
            args=(_POWDER_DAY_ANTHEM, _anthem_stop),
            daemon=True,
        )
        _anthem_thread.start()


def stop_powder_day_anthem():
    with _anthem_lock:
        _anthem_stop.set()
        if _anthem_thread and _anthem_thread.is_alive():
            _anthem_thread.join(timeout=2.0)


def check_and_trigger_alarm(current_snow_cm):
    """
    active: fire once at HH:MM if snow Ã¢â€°Â¥ trigger (once per day)
    active_anytime: fire at trigger and each +increment, resetting daily
    """
    cfg = load_alarm_cfg()
    reset_state_if_new_day(cfg)

    active = bool(cfg.get("active"))
    anytime = bool(cfg.get("active_anytime"))
    hr = int(cfg.get("hour") or 0)
    mn = int(cfg.get("minute") or 0)
    trig = max(0, int(cfg.get("triggered_snow") or 0))
    inc = max(0, int(cfg.get("incremental_snow") or 0))
    st = cfg["state"]

    now = datetime.datetime.now()
    matches_time = (now.hour == hr and now.minute == mn)

    # Mode 1: exact time, once/day
    if active and not anytime:
        if (not st["triggered_today"]) and matches_time and current_snow_cm >= trig:
            print(f"[Alarm] Timed trigger {hr:02d}:{mn:02d} | {current_snow_cm} Ã¢â€°Â¥ {trig}")
            start_powder_day_anthem()
            threading.Timer(sum(d for _, d in _POWDER_DAY_ANTHEM), stop_powder_day_anthem).start()
            st["triggered_today"] = True
            save_alarm_cfg(cfg)
            return True
        return False

    # Mode 2: anytime + increments
    if anytime:
        if st.get("next_threshold") is None:
            st["next_threshold"] = trig
        fired = False
        while inc > 0 and current_snow_cm >= int(st["next_threshold"]):
            print(f"[Alarm] Anytime trigger | {current_snow_cm} Ã¢â€°Â¥ {st['next_threshold']} (step {inc})")
            start_powder_day_anthem()
            threading.Timer(sum(d for _, d in _POWDER_DAY_ANTHEM), stop_powder_day_anthem).start()
            st["next_threshold"] = int(st["next_threshold"]) + inc
            fired = True
        if fired:
            save_alarm_cfg(cfg)
        return fired

    return False


