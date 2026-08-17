#!/usr/bin/env bash
# Safe package update for the writable controller master.
# Stream 01 is stopped only if its local service is active, then restored.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
export LC_ALL=C
export PIP_NO_CACHE_DIR=1
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"
LOCAL_STREAM_SERVICE="${STREAM_MASTER_LOCAL_STREAM_SERVICE:-streaming-setup-local-stream.service}"
DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
RESTORE_STREAM=0
EXPECT_STREAM=0

restore_stream(){
  rc=$?
  trap - EXIT
  if [ "$RESTORE_STREAM" -eq 1 ]; then
    echo '===== restoring master stream ====='
    if ! sudo -n systemctl restart "$LOCAL_STREAM_SERVICE"; then
      echo 'ERROR: master packages were updated but Stream 01 could not be restored.' >&2
      rc=40
    fi
  fi
  exit "$rc"
}
trap restore_stream EXIT

echo '===== MASTER LOCAL PACKAGE UPDATE ====='
echo 'OverlayFS is not touched and the controller service remains online.'

if /usr/bin/python3 - "$DIR/nodes.json" "${STREAM_MASTER_MASTER_IP:-192.168.0.101}" <<'PY_EXPECT' >/dev/null 2>&1
import json, sys
path, master_ip = sys.argv[1:]
try: nodes=json.load(open(path,encoding='utf-8'))
except Exception: raise SystemExit(1)
for n in nodes:
    target=str(n.get('target',''))
    host=target.rsplit('@',1)[-1] if '@' in target else ''
    if str(n.get('role','node')).lower()=='master' or host==master_ip:
        raise SystemExit(0 if str(n.get('url','')).strip() else 1)
raise SystemExit(1)
PY_EXPECT
then
  EXPECT_STREAM=1
fi

if systemctl is-active --quiet "$LOCAL_STREAM_SERVICE" 2>/dev/null; then
  RESTORE_STREAM=1
  echo '===== stopping Stream 01 during package replacement ====='
  sudo -n systemctl stop "$LOCAL_STREAM_SERVICE"
elif [ -x "$DIR/scripts/probe.sh" ] && STREAM_MASTER_WORKDIR="${STREAM_MASTER_WORKDIR:-/dev/shm/stream-master}" /bin/bash "$DIR/scripts/probe.sh" 2>/dev/null | grep -q '^STATUS=running$'; then
  # Compatibility with an older installation where the local supervisor was
  # started directly instead of through the dedicated systemd unit.
  RESTORE_STREAM=1
  echo '===== stopping legacy Stream 01 supervisor during package replacement ====='
  STREAM_MASTER_WORKDIR="${STREAM_MASTER_WORKDIR:-/dev/shm/stream-master}" /bin/bash "$DIR/scripts/kill.sh" >/dev/null 2>&1 || true
fi

echo '===== clock sanity ====='
NOW_EPOCH="$(date +%s)"
if [ "$NOW_EPOCH" -lt 1735689600 ] || [ "$NOW_EPOCH" -gt 2082758400 ]; then
  echo 'ERROR: system clock is implausible; refusing package-signature update.' >&2
  exit 25
fi
if command -v timedatectl >/dev/null 2>&1; then
  SYNC="$(timedatectl show -p NTPSynchronized --value 2>/dev/null || true)"
  if [ "$SYNC" = no ]; then
    echo 'NTP is not synchronized yet; waiting up to 60 seconds.'
    for _ in 1 2 3 4 5 6 7 8 9 10 11 12; do
      sleep 5
      SYNC="$(timedatectl show -p NTPSynchronized --value 2>/dev/null || true)"
      [ "$SYNC" != no ] && break
    done
  fi
  echo "ntp_synchronized=${SYNC:-unknown}"
  [ "$SYNC" != no ] || { echo 'ERROR: NTP is still unsynchronized; refusing repository update.' >&2; exit 26; }
fi

echo '===== apt update ====='
sudo -n apt-get \
  -o APT::Update::Error-Mode=any \
  -o DPkg::Lock::Timeout=120 \
  -o Acquire::Retries=3 \
  -o Acquire::http::Timeout=30 \
  -o Acquire::https::Timeout=30 \
  update

echo '===== VLC only-upgrade ====='
sudo -n apt-get \
  -y \
  -o DPkg::Lock::Timeout=120 \
  -o Acquire::Retries=3 \
  -o Acquire::http::Timeout=30 \
  -o Acquire::https::Timeout=30 \
  -o Dpkg::Options::=--force-confold \
  install --only-upgrade vlc

echo '===== Streamlink pipx upgrade ====='
PIPX="$(command -v pipx 2>/dev/null || true)"
[ -n "$PIPX" ] || [ ! -x /usr/bin/pipx ] || PIPX=/usr/bin/pipx
[ -n "$PIPX" ] || { echo 'ERROR: pipx is not installed.' >&2; exit 30; }
if command -v timeout >/dev/null 2>&1; then
  timeout --signal=TERM --kill-after=30s 900 "$PIPX" upgrade streamlink
else
  "$PIPX" upgrade streamlink
fi

STREAMLINK="$(command -v streamlink 2>/dev/null || true)"
[ -n "$STREAMLINK" ] || [ ! -x "$HOME/.local/bin/streamlink" ] || STREAMLINK="$HOME/.local/bin/streamlink"
[ -n "$STREAMLINK" ] || { echo 'ERROR: streamlink executable not found after upgrade.' >&2; exit 31; }
STREAMLINK_VERSION="$("$STREAMLINK" --version 2>/dev/null | head -n 1)"
VLC_VERSION="$(dpkg-query -W -f='${Version}' vlc 2>/dev/null || true)"
[ -n "$VLC_VERSION" ] || VLC_VERSION="$(cvlc --version 2>&1 | head -n 1 || true)"
OS_NAME="$(. /etc/os-release; printf '%s' "$PRETTY_NAME")"
UPDATED_UTC="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"

sudo -n mkdir -p /var/lib/stream-master
{
  printf 'LAST_UPDATE_UTC=%s\n' "$UPDATED_UTC"
  printf 'STREAMLINK_VERSION=%s\n' "$STREAMLINK_VERSION"
  printf 'VLC_VERSION=%s\n' "$VLC_VERSION"
  printf 'OS=%s\n' "$OS_NAME"
  printf 'UPDATE_KIND=master-local-vlc+streamlink\n'
} | sudo -n tee /var/lib/stream-master/update-info >/dev/null
sudo -n chmod 0644 /var/lib/stream-master/update-info
sudo -n apt-get clean
sudo -n sync

echo "master_streamlink=$STREAMLINK_VERSION"
echo "master_vlc=$VLC_VERSION"
echo "master_last_update_utc=$UPDATED_UTC"

if [ "$RESTORE_STREAM" -eq 1 ]; then
  echo '===== restoring master stream ====='
  sudo -n systemctl restart "$LOCAL_STREAM_SERVICE"
  RESTORE_STREAM=0
  if [ "$EXPECT_STREAM" -eq 1 ]; then
    sleep 2
    PROBE="$(STREAM_MASTER_WORKDIR="${STREAM_MASTER_WORKDIR:-/dev/shm/stream-master}" /bin/bash "$DIR/scripts/probe.sh" 2>/dev/null || true)"
    if ! printf '%s
' "$PROBE" | grep -q '^STATUS=running$'; then
      echo 'ERROR: master packages updated, but Stream 01 supervisor did not return.' >&2
      printf '%s
' "$PROBE" >&2
      exit 41
    fi
  fi
fi

echo 'MASTER LOCAL PACKAGE UPDATE COMPLETE'
