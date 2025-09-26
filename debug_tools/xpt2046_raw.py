#!/usr/bin/env python3
# xpt2046_raw.py — print raw 12-bit coords when pressed
# sudo python3 xpt2046_raw.py --cs 1 --irq 22
# Expect no output until you actually touch; then see changing 0–4095-ish values. If it spews values without touching, IRQ isn’t wired right.

import time, argparse
import spidev
try:
    import RPi.GPIO as GPIO
    HAS_GPIO = True
except Exception:
    HAS_GPIO = False

def settle_read12(spi, cmd):
    spi.xfer2([cmd,0,0])           # throw-away to settle ADC
    r = spi.xfer2([cmd,0,0])
    return ((r[1] << 8) | r[2]) >> 4

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cs", type=int, default=1, help="chip-select: 0=CE0, 1=CE1")
    ap.add_argument("--irq", type=int, default=22, help="BCM pin for PENIRQ (active-low)")
    ap.add_argument("--speed", type=int, default=400_000, help="SPI speed (Hz)")
    args = ap.parse_args()

    spi = spidev.SpiDev()
    spi.open(0, args.cs)
    spi.max_speed_hz = args.speed
    spi.mode = 0b00

    if not HAS_GPIO:
        raise SystemExit("RPi.GPIO not available. Run on Pi with sudo.")

    GPIO.setwarnings(False)
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(args.irq, GPIO.IN, pull_up_down=GPIO.PUD_UP)

    print(f"Touch on SPI0.{args.cs}, IRQ=GPIO{args.irq}. Press to see raw coords. Ctrl+C to exit.")
    try:
        while True:
            if GPIO.input(args.irq) == 0:   # pressed
                xs, ys = [], []
                for _ in range(5):
                    y = settle_read12(spi, 0xD0)
                    x = settle_read12(spi, 0x90)
                    if 100 < x < 4000 and 100 < y < 4000:
                        xs.append(x); ys.append(y)
                    time.sleep(0.005)
                if len(xs) >= 3:
                    print(f"RAW: X={sum(xs)//len(xs)}  Y={sum(ys)//len(ys)}")
                else:
                    print("RAW: (noisy/invalid sample)")
                # simple debounce
                time.sleep(0.1)
            else:
                time.sleep(0.02)
    except KeyboardInterrupt:
        pass
    finally:
        try: spi.close()
        except: pass
        GPIO.cleanup()

if __name__ == "__main__":
    main()
