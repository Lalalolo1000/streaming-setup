#!/usr/bin/env bash
# Create an off-device backup of local installation state without Git-tracked code.
set -euo pipefail
DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
DEST="${1:-}"
INCLUDE_SECRETS="${STREAM_MASTER_BACKUP_INCLUDE_SECRETS:-0}"
if [ -z "$DEST" ]; then
    echo "usage: $0 /path/to/backup-directory" >&2
    echo "Set STREAM_MASTER_BACKUP_INCLUDE_SECRETS=1 only if the destination is protected." >&2
    exit 2
fi
mkdir -p "$DEST"
STAMP="$(date -u '+%Y%m%dT%H%M%SZ')"
OUT="$DEST/streaming-setup-local-state-$STAMP.tar.gz"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/state"
copy_if_present(){ if [ -f "$1" ]; then cp -p "$1" "$2"; fi; return 0; }
copy_if_present "$DIR/nodes.json" "$TMP/state/nodes.json"
copy_if_present "$DIR/nodes.json.bak" "$TMP/state/nodes.json.bak"
copy_if_present "$DIR/runtime/desired_streams.json" "$TMP/state/desired_streams.json"
copy_if_present "$DIR/runtime/update_guard.json" "$TMP/state/update_guard.json"
copy_if_present "$DIR/runtime/node_jobs.json" "$TMP/state/node_jobs.json"
copy_if_present "$DIR/runtime/fleet_job.json" "$TMP/state/fleet_job.json"
if [ "$INCLUDE_SECRETS" = 1 ]; then
    copy_if_present "$DIR/youtube-cookies.txt" "$TMP/state/youtube-cookies.txt"
    copy_if_present "$HOME/.config/stream-master/ssh-password" "$TMP/state/ssh-password"
fi
printf 'created_utc=%s\nproject=%s\nsecrets_included=%s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$DIR" "$INCLUDE_SECRETS" > "$TMP/state/BACKUP_INFO.txt"
tar -C "$TMP" -czf "$OUT" state
chmod 600 "$OUT"
echo "$OUT"
