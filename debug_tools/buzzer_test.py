#!/usr/bin/env python3
# buzzer_test.py — Snow Scraper–compatible buzzer tester
# Target: Raspberry Pi Zero 2 W • Python 3.9
# Uses the same libs/pin as Snow Scraper (RPi.GPIO PWM on GPIO18)

import time
import math
import sys
import argparse

try:
    import RPi.GPIO as GPIO
    _HAS_GPIO = True
except Exception:
    _HAS_GPIO = False

BUZZER_PIN = 18  # same as Snow Scraper
DEFAULT_DUTY = 50  # %; passive buzzers like ~30–60

# Note map matches Snow Scraper for familiarity
NOTES = {
    "C4": 262, "D4": 294, "E4": 330, "F4": 349, "G4": 392, "A4": 440, "B4": 494,
    "C5": 523, "D5": 587, "E5": 659, "F5": 698, "G5": 784, "A5": 880, "REST": 0,
}

_pwm = None

def setup():
    global _pwm
    if not _HAS_GPIO:
        print("⚠️  RPi.GPIO not available (dev machine). Running in silent mode.")
        return
    GPIO.setwarnings(False)
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(BUZZER_PIN, GPIO.OUT)
    _pwm = GPIO.PWM(BUZZER_PIN, 440)  # freq will be adjusted per tone

def cleanup():
    global _pwm
    if not _HAS_GPIO:
        return
    try:
        if _pwm:
            _pwm.stop()
        GPIO.cleanup()
    except Exception:
        pass
    _pwm = None

def _sleep(sec):
    # tiny helper for interruptible sleeps
    t0 = time.time()
    while time.time() - t0 < sec:
        time.sleep(0.005)

def tone(freq_hz: int, dur_s: float, duty: int = DEFAULT_DUTY):
    """Play a single tone. freq_hz=0 for silence."""
    if dur_s <= 0:
        return
    if not _HAS_GPIO:
        # dev-mode print
        sys.stdout.write(f"[tone] {freq_hz:4d} Hz for {dur_s:.3f}s\n")
        _sleep(dur_s)
        return

    global _pwm
    if freq_hz <= 0:
        # silence: stop output cleanly
        try:
            _pwm.stop()
        except Exception:
            pass
        _sleep(dur_s)
        return

    if _pwm is None:
        setup()
    try:
        _pwm.ChangeFrequency(freq_hz)
        _pwm.start(max(0, min(100, duty)))
    except Exception:
        pass
    _sleep(dur_s)
    try:
        _pwm.stop()
    except Exception:
        pass

def sweep(f_start: int, f_end: int, dur_s: float, steps: int = 120, duty: int = DEFAULT_DUTY, curve: str = "linear"):
    """Frequency sweep for sirens/chirps."""
    if steps < 1:
        return
    for i in range(steps):
        t = i / float(steps - 1) if steps > 1 else 1.0
        if curve == "exp":
            # perceptually nicer
            f = int(f_start * ((f_end / max(1, f_start)) ** t))
        else:
            f = int(f_start + (f_end - f_start) * t)
        if not _HAS_GPIO:
            sys.stdout.write(f"[sweep] {f:4d} Hz\r"); sys.stdout.flush()
        if _HAS_GPIO:
            _pwm.ChangeFrequency(max(1, f))
            _pwm.start(duty)
        time.sleep(dur_s / steps)
    if _HAS_GPIO:
        _pwm.stop()
    if not _HAS_GPIO:
        sys.stdout.write("\n")

# -----------------------
# Ten alarm programs
# -----------------------

def alarm_1_beep_beep():
    """Two quick beeps, pause; classic 'beep-beep' trio."""
    for _ in range(3):
        tone(1000, 0.12); tone(0, 0.05)
        tone(1000, 0.12); tone(0, 0.25)

def alarm_2_siren():
    """Wide linear siren sweep up/down."""
    for _ in range(3):
        sweep(500, 1600, 0.9, duty=55)
        sweep(1600, 500, 0.9, duty=55)
        tone(0, 0.15)

def alarm_3_exp_chirps():
    """Rapid exponential 'whoop' chirps."""
    for _ in range(6):
        sweep(600, 2400, 0.22, curve="exp", duty=50)
        tone(0, 0.06)

def alarm_4_cuckoo():
    """Two-tone 'cuckoo' like old clocks."""
    a, b = 600, 800
    for _ in range(4):
        tone(a, 0.18); tone(0, 0.05)
        tone(b, 0.18); tone(0, 0.30)

def alarm_5_klaxon():
    """Slow alternating tones—industrial vibe."""
    for _ in range(4):
        tone(440, 0.35, duty=60); tone(0, 0.07)
        tone(370, 0.35, duty=60); tone(0, 0.20)

def alarm_6_tritone():
    """C–E–G arpeggio repeating (bright, attention-grabbing)."""
    seq = [NOTES["C5"], NOTES["E5"], NOTES["G5"]]
    for _ in range(5):
        for f in seq:
            tone(f, 0.18); tone(0, 0.05)
        tone(0, 0.20)

def alarm_7_ping_decay():
    """Bell-ish ping with faux decay (duty ramps down)."""
    base = 1600
    for _ in range(5):
        for d in (70, 55, 40, 30, 20):
            if _HAS_GPIO:
                _pwm.ChangeFrequency(base)
                _pwm.start(d)
                time.sleep(0.05)
            else:
                sys.stdout.write(f"[ping] {base}Hz duty {d}%\n")
                time.sleep(0.05)
        tone(0, 0.20)

def alarm_8_sos_morse():
    """... --- ... at ~700 Hz."""
    f = 700
    dit, dah, gap = 0.10, 0.30, 0.10
    # S
    for _ in range(3): tone(f, dit); tone(0, gap)
    time.sleep(0.2)
    # O
    for _ in range(3): tone(f, dah); tone(0, gap)
    time.sleep(0.2)
    # S
    for _ in range(3): tone(f, dit); tone(0, gap)

def alarm_9_pager_stutter():
    """Buzz-stutter like an old pager/phone."""
    f = 1200
    for _ in range(8):
        tone(f, 0.06); tone(0, 0.04)
        tone(f, 0.06); tone(0, 0.20)

def alarm_10_powder_mini():
    """Mini version of your Powder-Day motif using the same note set."""
    motif = [("G4", 0.18), ("E4", 0.18), ("C4", 0.18),
             ("G4", 0.18), ("E4", 0.18), ("C4", 0.18),
             ("C5", 0.18), ("B4", 0.18), ("G4", 0.45)]
    for note, dur in motif * 2:
        freq = NOTES.get(note, 0)
        tone(freq, dur)
        tone(0, 0.04)

ALARM_BANK = [
    ("Beep-Beep (short twin beeps)", alarm_1_beep_beep),
    ("Siren (wide sweep)",            alarm_2_siren),
    ("Chirps (fast whoops)",          alarm_3_exp_chirps),
    ("Cuckoo (two-tone)",             alarm_4_cuckoo),
    ("Klaxon (slow two-tone)",        alarm_5_klaxon),
    ("Tri-tone (C-E-G arpeggio)",     alarm_6_tritone),
    ("Ping w/ decay",                 alarm_7_ping_decay),
    ("Morse SOS",                     alarm_8_sos_morse),
    ("Pager stutter",                 alarm_9_pager_stutter),
    ("Powder-Day mini motif",         alarm_10_powder_mini),
]

def list_alarms():
    for i, (name, _) in enumerate(ALARM_BANK, 1):
        print(f"{i:2d}. {name}")

def run_alarm(index: int, repeat: int = 1, pause: float = 0.5):
    index = max(1, min(len(ALARM_BANK), index))
    name, fn = ALARM_BANK[index - 1]
    print(f"▶️  Alarm {index}: {name}")
    for _ in range(max(1, repeat)):
        fn()
        time.sleep(pause)

def main():
    parser = argparse.ArgumentParser(description="Passive buzzer test (GPIO18, RPi.GPIO PWM).")
    parser.add_argument("-n", "--number", type=int, help="Alarm number (1-10). If omitted, plays all.")
    parser.add_argument("-r", "--repeat", type=int, default=1, help="How many times to repeat the selection.")
    parser.add_argument("-p", "--pause", type=float, default=0.6, help="Pause between repeats/alarms (seconds).")
    parser.add_argument("--menu", action="store_true", help="Interactive menu.")
    args = parser.parse_args()

    setup()
    try:
        if args.menu and sys.stdin.isatty():
            while True:
                print("\n=== Buzzer Test Menu ===")
                list_alarms()
                print("  0. Quit")
                try:
                    choice = int(input("Select alarm (0-10): ").strip())
                except Exception:
                    continue
                if choice == 0:
                    break
                run_alarm(choice, repeat=args.repeat, pause=args.pause)
        else:
            if args.number:
                run_alarm(args.number, repeat=args.repeat, pause=args.pause)
            else:
                # Play all, sequentially
                for i in range(1, len(ALARM_BANK) + 1):
                    run_alarm(i, repeat=1, pause=args.pause)
    except KeyboardInterrupt:
        pass
    finally:
        cleanup()

if __name__ == "__main__":
    main()
