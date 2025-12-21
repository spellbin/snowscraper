#!/usr/bin/env bash
set -euo pipefail

# ---------------------------------------------------------
# Snow Scraper microSD stress / quality test
#
# Script location: /home/pi/snowscraper/debug_tools/sd_qual_test.sh
# Logs location:   /home/pi/snowscraper/logs/
# Temp files:      /home/pi/snowscraper/sdtest/ (created/removed by test)
#
# Device ID is derived from hostname (e.g. ss25)
#
# Usage:
#   sudo ./sd_qual_test.sh --quick
#   sudo ./sd_qual_test.sh --factory
#   sudo ./sd_qual_test.sh --burnin
#   sudo ./sd_qual_test.sh --factory --rand-iters 4000
# ---------------------------------------------------------

# --- Base paths ---
BASE_DIR="/home/pi/snowscraper"
LOG_DIR="$BASE_DIR/logs"
WORKDIR="$BASE_DIR/sdtest"

# --- Device identity ---
DEVICE_ID="$(hostname)"

usage() {
  cat <<EOF
Usage:
  sudo $0 [--quick|--factory|--burnin] [--seq-mb N] [--rand-mb N] [--rand-iters N] [--block-kb N]

Presets:
  --quick     SEQ_MB=128  RAND_MB=32   RAND_ITERS=800   BLOCK_KB=4
  --factory   SEQ_MB=256  RAND_MB=64   RAND_ITERS=2000  BLOCK_KB=4  (default)
  --burnin    SEQ_MB=1024 RAND_MB=256  RAND_ITERS=12000 BLOCK_KB=4

Overrides (optional):
  --seq-mb N
  --rand-mb N
  --rand-iters N
  --block-kb N

Examples:
  sudo $0 --quick
  sudo $0 --burnin
  sudo $0 --factory --rand-iters 4000
EOF
}

# Defaults (factory)
SEQ_MB="${SEQ_MB:-256}"
RAND_MB="${RAND_MB:-64}"
RAND_ITERS="${RAND_ITERS:-2000}"
BLOCK_KB="${BLOCK_KB:-4}"

# Parse args (presets + overrides)
while [[ $# -gt 0 ]]; do
  case "$1" in
    --quick)
      SEQ_MB=128; RAND_MB=32; RAND_ITERS=800; BLOCK_KB=4; shift ;;
    --factory)
      SEQ_MB=256; RAND_MB=64; RAND_ITERS=2000; BLOCK_KB=4; shift ;;
    --burnin)
      SEQ_MB=1024; RAND_MB=256; RAND_ITERS=12000; BLOCK_KB=4; shift ;;
    --seq-mb)
      SEQ_MB="$2"; shift 2 ;;
    --rand-mb)
      RAND_MB="$2"; shift 2 ;;
    --rand-iters)
      RAND_ITERS="$2"; shift 2 ;;
    --block-kb)
      BLOCK_KB="$2"; shift 2 ;;
    -h|--help)
      usage; exit 0 ;;
    *)
      echo "Unknown arg: $1"
      usage
      exit 1 ;;
  esac
done

TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
LOGFILE="$LOG_DIR/${DEVICE_ID}_sd_qual_report_${TIMESTAMP}.log"

# --- Helpers ---
say() { echo "[$(date -Is)] $*" | tee -a "$LOGFILE"; }
run() { say "\$ $*"; "$@" 2>&1 | tee -a "$LOGFILE"; }

require_root() {
  if [[ "$EUID" -ne 0 ]]; then
    echo "Run as root: sudo $0"
    exit 1
  fi
}

now_ns() {
  local t
  t=$(date +%s%N 2>/dev/null) || t=""
  if [[ -n "$t" && "$t" =~ ^[0-9]+$ ]]; then
    echo "$t"
  else
    echo $(( $(date +%s) * 1000000000 ))
  fi
}

fmt_hms() {
  local s="$1"
  local h=$((s/3600)); local m=$(((s%3600)/60)); local r=$((s%60))
  if (( h > 0 )); then printf "%dh%02dm%02ds" "$h" "$m" "$r"
  elif (( m > 0 )); then printf "%dm%02ds" "$m" "$r"
  else printf "%ds" "$r"
  fi
}

# ---------------------------------------------------------
# Info + sanity
# ---------------------------------------------------------
mmc_info() {
  say "=== DEVICE INFO ==="
  say "device_id: $DEVICE_ID"
  say "kernel: $(uname -a)"
  say "preset/params: SEQ_MB=$SEQ_MB RAND_MB=$RAND_MB RAND_ITERS=$RAND_ITERS BLOCK_KB=$BLOCK_KB"

  if [[ "$DEVICE_ID" =~ ^ss[0-9]+$ ]]; then
    say "device_id format OK"
  else
    say "WARN: hostname does not match expected ssNN format"
  fi

  say "=== MMC INFO ==="
  if [[ -e /sys/block/mmcblk0/device/cid ]]; then
    say "cid:    $(cat /sys/block/mmcblk0/device/cid)"
    say "name:   $(cat /sys/block/mmcblk0/device/name)"
    say "manfid: $(cat /sys/block/mmcblk0/device/manfid)"
    say "oemid:  $(cat /sys/block/mmcblk0/device/oemid)"
    say "sectors:$(cat /sys/block/mmcblk0/size)"
  else
    say "FAIL: mmcblk0 not found"
    exit 2
  fi

  run lsblk -o NAME,SIZE,FSTYPE,MOUNTPOINT,LABEL,MODEL
}

free_space_check() {
  say "=== DISK FREE ==="
  run df -h /

  avail_mb=$(df -Pm / | awk 'NR==2{print $4}')
  need_mb=$((SEQ_MB + RAND_MB + 200))

  if (( avail_mb < need_mb )); then
    say "FAIL: insufficient free space (${avail_mb}MB available, need ${need_mb}MB)"
    exit 3
  fi
}

dmesg_scan() {
  say "=== DMESG MMC SCAN ==="
  run dmesg --color=never | egrep -i \
    "mmc|mmcblk|I/O error|timeout|crc|error -110|error -84" | tail -n 80 || true
}

# ---------------------------------------------------------
# Tests
# ---------------------------------------------------------
seq_write_read_hash() {
  say "=== SEQUENTIAL WRITE/READ TEST (${SEQ_MB}MB) ==="
  mkdir -p "$WORKDIR"
  local f="$WORKDIR/seq_test.bin"
  local hash1 hash2

  run dd if=/dev/urandom of="$f" bs=1M count="$SEQ_MB" conv=fsync status=progress

  # Compute hash as a pure value (avoids tee/diff edge cases)
  hash1="$(sha256sum "$f" | awk '{print $1}')"
  say "sha256(write): $hash1"

  sync
  echo 3 > /proc/sys/vm/drop_caches

  hash2="$(sha256sum "$f" | awk '{print $1}')"
  say "sha256(read):  $hash2"

  if [[ "$hash1" != "$hash2" ]]; then
    say "FAIL: sequential hash mismatch"
    exit 4
  fi

  rm -f "$f"
  sync
}

random_churn() {
  say "=== RANDOM ${BLOCK_KB}K WRITE CHURN (${RAND_ITERS} iters) ==="
  mkdir -p "$WORKDIR"
  local f="$WORKDIR/rand_test.bin"
  local blocks=$(( (RAND_MB * 1024) / BLOCK_KB ))

  run dd if=/dev/zero of="$f" bs=1M count="$RAND_MB" conv=fsync status=progress
  say "Random churn: file=${RAND_MB}MB, block=${BLOCK_KB}KB, blocks=${blocks}, iters=${RAND_ITERS}"

  # --- ETA calibration ---
  local eta_sample=200   # measure early throughput after this many iters
  local eta_every=500    # update ETA every N iters after calibration
  local t0_ns t1_ns elapsed_ns ips remaining eta_s

  t0_ns="$(now_ns)"

  for ((i=1; i<=RAND_ITERS; i++)); do
    off=$(( RANDOM % blocks ))
    dd if=/dev/urandom of="$f" bs="${BLOCK_KB}K" count=1 seek="$off" \
      conv=notrunc oflag=sync status=none || {
        say "FAIL: random write error at iteration $i"
        exit 5
      }

    # Calibrate ETA once we have a sample
    if (( i == eta_sample )); then
      t1_ns="$(now_ns)"
      elapsed_ns=$((t1_ns - t0_ns))
      if (( elapsed_ns > 0 )); then
        ips=$(( (eta_sample * 1000000000) / elapsed_ns ))
        remaining=$(( RAND_ITERS - i ))
        if (( ips > 0 )); then
          eta_s=$(( remaining / ips ))
          say "ETA estimate after ${eta_sample} iters: ~$(fmt_hms "$eta_s") remaining (${ips} iters/sec)"
        else
          say "ETA estimate: unable to compute (iters/sec=0)"
        fi
      fi
    fi

    # Periodic progress + dmesg spot-check
    if (( i % 250 == 0 )); then
      say "random churn progress: $i / $RAND_ITERS"
      dmesg | tail -n 20 | egrep -i \
        "mmc|mmcblk|I/O error|timeout|crc|error -110|error -84" && \
        say "WARN: mmc-related messages detected" || true
    fi

    # ETA refresh
    if (( i > eta_sample && i % eta_every == 0 )); then
      t1_ns="$(now_ns)"
      elapsed_ns=$((t1_ns - t0_ns))
      if (( elapsed_ns > 0 )); then
        ips=$(( (i * 1000000000) / elapsed_ns ))
        remaining=$(( RAND_ITERS - i ))
        if (( ips > 0 )); then
          eta_s=$(( remaining / ips ))
          say "ETA update: ~$(fmt_hms "$eta_s") remaining (${ips} iters/sec)"
        fi
      fi
    fi
  done

  rm -f "$f"
  sync
}

fs_sanity() {
  say "=== FILESYSTEM SANITY ==="
  rootdev=$(findmnt -n -o SOURCE /)
  say "rootdev: $rootdev"

  if [[ "$rootdev" == /dev/mmcblk0p* ]]; then
    run tune2fs -l "$rootdev" | egrep -i \
      "Filesystem state|Errors behavior|Last mount time|Last checked|Mount count"
  fi
}

# ---------------------------------------------------------
# Main
# ---------------------------------------------------------
main() {
  require_root
  mkdir -p "$LOG_DIR" "$WORKDIR"
  touch "$LOGFILE"

  say "Snow Scraper microSD quality test START"
  say "logfile: $LOGFILE"

  mmc_info
  free_space_check
  dmesg_scan
  seq_write_read_hash
  random_churn
  dmesg_scan
  fs_sanity

  say "RESULT=PASS DEVICE=$DEVICE_ID"
  say "Snow Scraper microSD quality test COMPLETE"
}

main "$@"
