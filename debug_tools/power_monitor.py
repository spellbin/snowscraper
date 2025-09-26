#!/usr/bin/env python3
# power_monitor.py — watch undervoltage/thermal flags and core volts/temp
# Examples:
# Live watch: python3 power_monitor.py
# Log every 5s to CSV: python3 power_monitor.py --interval 5 --csv power_log.csv
# Tip: any non-zero bits in get_throttled mean you’ve had a brownout/thermal event. For release, you want it staying at throttled=0x0 under load.

import subprocess, time, argparse, csv, sys, datetime as dt

FLAGS = {
    0: "Under-voltage now",
    1: "ARM freq capped now",
    2: "Currently throttled",
    3: "Soft temp limit active",
    16: "Under-voltage has occurred",
    17: "ARM freq capped has occurred",
    18: "Throttling has occurred",
    19: "Soft temp limit has occurred",
}

def vc(cmd):
    out = subprocess.check_output(["/usr/bin/vcgencmd"] + cmd.split(), text=True).strip()
    return out

def parse_throttled(s):
    # format: "throttled=0x50000"
    val = int(s.split("=")[1], 16)
    active = [name for bit, name in FLAGS.items() if val & (1<<bit)]
    return val, active

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=float, default=1.0, help="seconds between samples")
    ap.add_argument("--csv", type=str, default="", help="optional CSV path to log")
    args = ap.parse_args()

    writer = None
    if args.csv:
        f = open(args.csv, "a", newline="")
        writer = csv.writer(f)
        writer.writerow(["timestamp","core_volts","temp_c","throttled_hex","flags"])

    try:
        while True:
            throttled_raw = vc("get_throttled")
            volts = vc("measure_volts")
            temp = vc("measure_temp")
            hexval, flags = parse_throttled(throttled_raw)
            ts = dt.datetime.now().isoformat(timespec="seconds")
            line = f"{ts} | {volts} | {temp} | {throttled_raw}" + ("" if not flags else " | " + ", ".join(flags))
            print(line)
            if writer:
                vnum = float(volts.split("=")[1].split("V")[0])
                tnum = float(temp.split("=")[1].split("'")[0])
                writer.writerow([ts, vnum, tnum, hex(hexval), ";".join(flags)])
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    if subprocess.call(["which","vcgencmd"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) != 0:
        sys.exit("vcgencmd not found. Install raspberrypi-utils or run on Raspberry Pi OS.")
    main()
