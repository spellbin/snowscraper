#!/usr/bin/env python3
# spi_sanity.py — verify spidev nodes and open your touch CS
# sudo python3 spi_sanity.py 1 (if touch on CE1)
# sudo python3 spi_sanity.py 0 (if touch on CE0)

import os, sys
import spidev

DEV = int(sys.argv[1]) if len(sys.argv) > 1 else 1  # 0 for CE0, 1 for CE1
print("Devices present:", os.listdir("/dev"))
paths = [p for p in os.listdir("/dev") if p.startswith("spidev")]
print("SPI nodes:", paths or "(none)")

spi = spidev.SpiDev()
try:
    spi.open(0, DEV)
    spi.max_speed_hz = 400_000
    spi.mode = 0b00
    print(f"OK: Opened SPI0.{DEV} at 400kHz, mode 0")
    # probe: transfer zeros (should not error)
    spi.xfer2([0x00,0x00,0x00])
    print("xfer2() works. CS wiring likely good.")
finally:
    try: spi.close()
    except: pass
