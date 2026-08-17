#!/usr/bin/env bash
set -euo pipefail
DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
RUN_USER="${SUDO_USER:-$USER}"
RUN_GROUP="$(id -gn "$RUN_USER")"
RUN_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"
SERVICE=/etc/systemd/system/stream-master.service
UPDATE_SERVICE=/etc/systemd/system/stream-master-git-update.service
UPDATE_TIMER=/etc/systemd/system/stream-master-git-update.timer
LOCAL_STREAM_SERVICE=/etc/systemd/system/streaming-setup-local-stream.service
PORT="${STREAM_MASTER_PORT:-8080}"
MASTER_IP="${STREAM_MASTER_MASTER_IP:-192.168.0.101}"
GIT_INTERVAL="${STREAM_MASTER_GIT_INTERVAL:-5min}"
GIT_BRANCH="${STREAM_MASTER_GIT_BRANCH:-}"
CONFIG_DIR="$RUN_HOME/.config/stream-master"
GIT_ENV="$CONFIG_DIR/github.env"

case "$DIR" in
    *[[:space:]]*) echo "Project path may not contain spaces for this systemd installer: $DIR" >&2; exit 1 ;;
esac
if [ "$RUN_USER" = root ]; then
    echo 'Refusing to install the web controller as root.' >&2
    echo 'Run this script as your normal login user (it will call sudo itself).' >&2
    exit 1
fi

mkdir -p "$CONFIG_DIR"
chmod 700 "$CONFIG_DIR"
if [ -z "$GIT_BRANCH" ] && [ -d "$DIR/.git" ]; then
    GIT_BRANCH="$(git -C "$DIR" branch --show-current 2>/dev/null || true)"
fi
[ -n "$GIT_BRANCH" ] || GIT_BRANCH=main
cat > "$GIT_ENV" <<EOF_ENV
STREAM_MASTER_GIT_REMOTE=origin
STREAM_MASTER_GIT_BRANCH=$GIT_BRANCH
STREAM_MASTER_GIT_FETCH_TIMEOUT=30
STREAM_MASTER_GIT_BOOT_UPDATE=1
EOF_ENV
chmod 600 "$GIT_ENV"

sudo tee "$SERVICE" >/dev/null <<EOF_UNIT
[Unit]
Description=Streaming Setup Raspberry Pi stream controller
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=$RUN_USER
Group=$RUN_GROUP
WorkingDirectory=$DIR
Environment=PYTHONUNBUFFERED=1
Environment=STREAM_MASTER_PORT=$PORT
Environment=STREAM_MASTER_AUTOSTART=1
Environment=STREAM_MASTER_MASTER_IP=$MASTER_IP
EnvironmentFile=-$GIT_ENV
ExecStart=/bin/bash "$DIR/run_master.sh"
Restart=on-failure
RestartSec=3
# If stopped during a node software update, update_pis.py gets time to relock OverlayFS.
KillMode=control-group
TimeoutStopSec=780

[Install]
WantedBy=multi-user.target
EOF_UNIT

# Root-owned wrapper is used only so it can restart the system service after a
# successful Git fast-forward. The Git commands themselves run as RUN_USER.
sudo tee "$UPDATE_SERVICE" >/dev/null <<EOF_UNIT
[Unit]
Description=Streaming Setup GitHub code update
Wants=network-online.target
After=network-online.target
ConditionPathIsDirectory=$DIR/.git

[Service]
Type=oneshot
Environment=PROJECT_DIR=$DIR
Environment=PROJECT_USER=$RUN_USER
EnvironmentFile=-$GIT_ENV
ExecStart=/bin/bash "$DIR/git_update_systemd.sh"
EOF_UNIT

sudo tee "$UPDATE_TIMER" >/dev/null <<EOF_UNIT
[Unit]
Description=Periodically check GitHub for Streaming Setup updates

[Timer]
# Boot-time pull already happens in run_master.sh. Wait until the daily startup
# sequence has had time to reach and start the Pis before the first timer run.
OnBootSec=20min
OnUnitActiveSec=$GIT_INTERVAL
AccuracySec=30s
RandomizedDelaySec=20s
Unit=stream-master-git-update.service

[Install]
WantedBy=timers.target
EOF_UNIT

# The master is also Stream 01. Keep its stream in a separate cgroup so a Git
# restart of stream-master.service does not kill VLC/Streamlink on the master.
sudo tee "$LOCAL_STREAM_SERVICE" >/dev/null <<EOF_UNIT
[Unit]
Description=Streaming Setup local master stream
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
User=$RUN_USER
Group=$RUN_GROUP
WorkingDirectory=$DIR
Environment=STREAM_MASTER_MASTER_IP=$MASTER_IP
ExecStart=/bin/bash "$DIR/local_stream_service.sh" start
ExecStop=/bin/bash "$DIR/local_stream_service.sh" stop
TimeoutStartSec=45
TimeoutStopSec=30
KillMode=control-group

[Install]
WantedBy=multi-user.target
EOF_UNIT

sudo systemctl daemon-reload
sudo systemctl enable --now stream-master.service
sudo systemctl enable --now stream-master-git-update.timer
# The local-stream unit is intentionally not enabled on its own; the controller's
# once-per-boot startup pass starts/restarts it using the current nodes.json URL.

echo
echo 'Streaming Setup wurde als stream-master.service installiert und gestartet.'
echo "GitHub-Prüfung: alle $GIT_INTERVAL (nur wenn dieses Verzeichnis ein Git-Checkout ist)."
echo 'Status: sudo systemctl status stream-master --no-pager'
echo 'Logs:   journalctl -u stream-master -f'
echo 'Git:    systemctl list-timers stream-master-git-update.timer'
echo 'Master stream: systemctl status streaming-setup-local-stream --no-pager'
echo "URL:    http://$(hostname -I 2>/dev/null | awk '{print $1}'):$PORT/"
