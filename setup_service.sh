#!/usr/bin/env bash
set -euo pipefail

# -----------------------------
# Settings you may customize
# -----------------------------
APP_DIR="/home/pi/snowscraper"
APP_ENTRY="snowgui.py"
PYTHON_BIN="/usr/bin/python3"

SERVICE_NAME="snowscraper.service"
HC_NAME="snowgui-healthcheck"
HC_SCRIPT="/usr/local/bin/${HC_NAME}"
UNIT_DIR="/etc/systemd/system"

# Heartbeat file written by your app
HEARTBEAT_FILE="${APP_DIR}/heartbeat.txt"

# Timeouts (seconds)
HEARTBEAT_TIMEOUT=120    # stale threshold
UPTIME_GRACE=180         # skip checks for this long after (re)start
BOOT_GRACE=120           # timer waits this long after boot before first run
TIMER_INTERVAL=60        # check frequency in seconds

# -----------------------------
# Root check
# -----------------------------
if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  echo "Please run as root: sudo bash $0"
  exit 1
fi

# -----------------------------
# Remove old watchdog if present
# -----------------------------
if systemctl list-unit-files | grep -q "^snowscraper.service"; then
  echo "[INFO] Disabling/removing legacy snowscraper.service"
  systemctl stop snowscraper.service || true
  systemctl disable snowscraper.service || true
  rm -f "${UNIT_DIR}/snowscraper.service"
fi
if systemctl list-unit-files | grep -q "^snowscraper.timer"; then
  echo "[INFO] Disabling/removing legacy snowscraper.timer"
  systemctl stop snowscraper.timer || true
  systemctl disable snowscraper.timer || true
  rm -f "${UNIT_DIR}/snowscraper.timer"
fi

# -----------------------------
# Create snowscraper.service
# -----------------------------
echo "[INFO] Writing ${UNIT_DIR}/${SERVICE_NAME}"
cat > "${UNIT_DIR}/${SERVICE_NAME}" <<EOF
[Unit]
Description=Snow Scraper GUI
Wants=network-online.target
After=network-online.target
StartLimitIntervalSec=60
StartLimitBurst=5

[Service]
Type=simple
WorkingDirectory=${APP_DIR}
ExecStart=${PYTHON_BIN} ${APP_DIR}/${APP_ENTRY}
User=root
Restart=always
RestartSec=3
MemoryMax=250M
OOMPolicy=restart
KillMode=control-group
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

# -----------------------------
# Healthcheck script
# -----------------------------
echo "[INFO] Writing ${HC_SCRIPT}"
cat > "${HC_SCRIPT}" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

SERVICE="snowscraper.service"
HB="__HEARTBEAT_FILE__"
HEARTBEAT_TIMEOUT=__HEARTBEAT_TIMEOUT__
UPTIME_GRACE=__UPTIME_GRACE__

# service active?
if ! systemctl is-active --quiet "$SERVICE"; then
  echo "Service not active; skipping."
  exit 0
fi

# service uptime grace (monotonic since boot)
start_us=$(systemctl show -p ExecMainStartTimestampMonotonic --value "$SERVICE" || echo "")
if [[ -z "$start_us" || "$start_us" == "0" ]]; then
  echo "No ExecMainStartTimestampMonotonic; skipping."
  exit 0
fi

now_s=$(cut -d' ' -f1 < /proc/uptime | awk '{printf("%.0f",$1)}')
start_s=$(( start_us / 1000000 ))
uptime=$(( now_s - start_s ))

if (( uptime < UPTIME_GRACE )); then
  echo "Within uptime grace (${uptime}s < ${UPTIME_GRACE}s); skipping."
  exit 0
fi

# heartbeat freshness
if [[ ! -f "$HB" ]]; then
  echo "No heartbeat file yet; skipping (post-grace)."
  exit 0
fi

mt=$(stat -c %Y "$HB" 2>/dev/null || echo 0)
age=$(( now_s - mt ))

if (( age > HEARTBEAT_TIMEOUT )); then
  echo "Heartbeat stale: ${age}s > ${HEARTBEAT_TIMEOUT}s. Restarting ${SERVICE}."
  systemctl restart "$SERVICE"
else
  echo "Heartbeat OK: ${age}s <= ${HEARTBEAT_TIMEOUT}s."
fi
EOF

# Inject variables into the healthcheck script
sed -i \
  -e "s#__HEARTBEAT_FILE__#${HEARTBEAT_FILE//\//\\/}#g" \
  -e "s#__HEARTBEAT_TIMEOUT__#${HEARTBEAT_TIMEOUT}#g" \
  -e "s#__UPTIME_GRACE__#${UPTIME_GRACE}#g" \
  "${HC_SCRIPT}"

chmod +x "${HC_SCRIPT}"

# -----------------------------
# Healthcheck oneshot service
# -----------------------------
echo "[INFO] Writing ${UNIT_DIR}/${HC_NAME}.service"
cat > "${UNIT_DIR}/${HC_NAME}.service" <<EOF
[Unit]
Description=Snow Scraper heartbeat health check
Wants=${SERVICE_NAME}
After=${SERVICE_NAME}

[Service]
Type=oneshot
ExecStart=${HC_SCRIPT}
User=root
EOF

# -----------------------------
# Healthcheck timer
# -----------------------------
echo "[INFO] Writing ${UNIT_DIR}/${HC_NAME}.timer"
cat > "${UNIT_DIR}/${HC_NAME}.timer" <<EOF
[Unit]
Description=Run Snow Scraper health check periodically

[Timer]
OnBootSec=${BOOT_GRACE}
OnUnitActiveSec=${TIMER_INTERVAL}s
Unit=${HC_NAME}.service

[Install]
WantedBy=timers.target
EOF

# -----------------------------
# Reload, enable, start
# -----------------------------
echo "[INFO] Reloading systemd units"
systemctl daemon-reload

echo "[INFO] Enabling ${SERVICE_NAME}"
systemctl enable "${SERVICE_NAME}"

echo "[INFO] Enabling & starting ${HC_NAME}.timer"
systemctl enable --now "${HC_NAME}.timer"

echo "[INFO] Starting ${SERVICE_NAME}"
systemctl restart "${SERVICE_NAME}"

# -----------------------------
# Status summary
# -----------------------------
echo
echo "=== Install complete ==="
systemctl --no-pager --full status "${SERVICE_NAME}" || true
echo
systemctl --no-pager --full status "${HC_NAME}.timer" || true
echo
echo "Next checks:"
echo "  journalctl -u ${SERVICE_NAME} -f"
echo "  systemctl list-timers '*${HC_NAME}*'"
