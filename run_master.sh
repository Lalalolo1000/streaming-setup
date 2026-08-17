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

# Pull code before Python starts. Failure is deliberately non-fatal: if GitHub or
# the internet is unavailable, the last known-good local code still starts.
if [ -d "$DIR/.git" ] && [ "${STREAM_MASTER_GIT_BOOT_UPDATE:-1}" = 1 ]; then
    echo "[boot] Prüfe GitHub auf neuen Streaming Setup-Code …"
    if ! /bin/bash "$DIR/git_update.sh" --boot; then
        echo "[boot] Git-Update fehlgeschlagen; starte vorhandenen Stand." >&2
    fi
fi

# If a periodic Git update requested this service restart, reaching this point
# means the new code is now being loaded.
rm -f "$DIR/runtime/restart_required" 2>/dev/null || true
exec /usr/bin/python3 -u "$DIR/master.py" --port "$PORT"
