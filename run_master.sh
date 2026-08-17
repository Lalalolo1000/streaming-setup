#!/usr/bin/env bash
set -euo pipefail
DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PORT="${STREAM_MASTER_PORT:-8080}"
PASSFILE="$HOME/.config/stream-master/ssh-password"
if [ ! -s "$PASSFILE" ]; then
    echo "FEHLER: SSH-Passwortdatei fehlt: $PASSFILE" >&2
    echo "Bitte zuerst ./install_master.sh ausführen oder die Datei manuell anlegen." >&2
    exit 1
fi
cd "$DIR"

# Mandatory Git preflight once per OS boot. Periodic Git checks continue via
# the timer. Recording the kernel boot ID prevents a crashing controller from
# doing three network fetch attempts on every systemd restart.
BOOT_ID="$(cat /proc/sys/kernel/random/boot_id 2>/dev/null || echo unknown)"
BOOT_MARKER="$DIR/runtime/git_preflight_boot_id"
mkdir -p "$DIR/runtime"
chmod 700 "$DIR/runtime" 2>/dev/null || true
LAST_BOOT_ID="$(cat "$BOOT_MARKER" 2>/dev/null || true)"
if [ -d "$DIR/.git" ] && [ "${STREAM_MASTER_GIT_BOOT_UPDATE:-1}" = 1 ] && [ "$LAST_BOOT_ID" != "$BOOT_ID" ]; then
    BOOT_ATTEMPTS="${STREAM_MASTER_GIT_BOOT_ATTEMPTS:-3}"
    BOOT_RETRY_DELAY="${STREAM_MASTER_GIT_BOOT_RETRY_DELAY:-5}"
    BOOT_FETCH_TIMEOUT="${STREAM_MASTER_GIT_BOOT_FETCH_TIMEOUT:-15}"
    case "$BOOT_ATTEMPTS" in ''|*[!0-9]*) BOOT_ATTEMPTS=3 ;; esac
    [ "$BOOT_ATTEMPTS" -ge 1 ] || BOOT_ATTEMPTS=1

    echo "[boot] Mandatory GitHub preflight before Streaming Setup starts."
    GIT_OK=0
    attempt=1
    while [ "$attempt" -le "$BOOT_ATTEMPTS" ]; do
        echo "[boot] GitHub check $attempt/$BOOT_ATTEMPTS …"
        if STREAM_MASTER_GIT_FETCH_TIMEOUT="$BOOT_FETCH_TIMEOUT" /bin/bash "$DIR/git_update.sh" --boot; then
            GIT_OK=1
            break
        fi
        if [ "$attempt" -lt "$BOOT_ATTEMPTS" ]; then
            echo "[boot] Git check failed; retrying in ${BOOT_RETRY_DELAY}s …" >&2
            sleep "$BOOT_RETRY_DELAY"
        fi
        attempt=$((attempt + 1))
    done
    if [ "$GIT_OK" -ne 1 ]; then
        echo "[boot] GitHub preflight could not complete; starting the last known-good local code." >&2
    fi
    # Mark the check as attempted even when GitHub was unavailable. This is
    # intentionally once-per-OS-boot; the regular timer will retry later.
    printf '%s\n' "$BOOT_ID" > "$BOOT_MARKER"
else
    if [ "$LAST_BOOT_ID" = "$BOOT_ID" ]; then
        echo "[boot] Git preflight already attempted this OS boot; starting local code."
    else
        echo "[boot] No Git checkout (or boot Git disabled); starting local code."
    fi
fi

# If a periodic Git update requested this service restart, reaching this point
# means the new code is now being loaded.
rm -f "$DIR/runtime/restart_required" 2>/dev/null || true
exec /usr/bin/python3 -u "$DIR/master.py" --port "$PORT"
