#!/usr/bin/env bash
# Safe package update for the controller master. No OverlayFS toggling, no reboot.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
export LC_ALL=C
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"

echo '===== MASTER LOCAL PACKAGE UPDATE ====='
echo 'The controller stays online; OverlayFS is not touched.'

sudo -n apt-get \
  -o Acquire::Retries=3 \
  -o Acquire::http::Timeout=30 \
  -o Acquire::https::Timeout=30 \
  update

sudo -n apt-get \
  -y \
  -o Acquire::Retries=3 \
  -o Acquire::http::Timeout=30 \
  -o Acquire::https::Timeout=30 \
  -o Dpkg::Options::=--force-confold \
  install --only-upgrade vlc

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
sudo -n sync

echo "master_streamlink=$STREAMLINK_VERSION"
echo "master_vlc=$VLC_VERSION"
echo "master_last_update_utc=$UPDATED_UTC"
echo 'MASTER LOCAL PACKAGE UPDATE COMPLETE'
