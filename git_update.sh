#!/usr/bin/env bash
# Safe fast-forward-only code update for Streaming Setup.
# Local nodes.json/runtime are ignored by Git and are never overwritten.
set -euo pipefail
DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REMOTE="${STREAM_MASTER_GIT_REMOTE:-origin}"
BRANCH="${STREAM_MASTER_GIT_BRANCH:-}"
FETCH_TIMEOUT="${STREAM_MASTER_GIT_FETCH_TIMEOUT:-30}"
RUNTIME="$DIR/runtime"
BOOT_MODE=0
[ "${1:-}" = "--boot" ] && BOOT_MODE=1
mkdir -p "$RUNTIME"

log(){ printf '[git-update] %s\n' "$*"; }

# nodes.json is installation state, never deployable code. Refuse to run if the
# current checkout tracks it. This is stricter than .gitignore and prevents an
# accidental commit from turning the local installation config into Git state.
if git -C "$DIR" ls-files --error-unmatch nodes.json >/dev/null 2>&1; then
    log "SICHERHEITSSTOPP: nodes.json ist in Git getrackt. Datei aus Git entfernen; lokales nodes.json bleibt unangetastet."
    exit 7
fi

if [ ! -d "$DIR/.git" ]; then
    log "Kein Git-Checkout: $DIR – Update übersprungen."
    exit 0
fi

if [ -z "$BRANCH" ]; then
    BRANCH="$(git -C "$DIR" branch --show-current 2>/dev/null || true)"
    [ -n "$BRANCH" ] || BRANCH=main
fi

# Never replace code while node maintenance/reboot jobs are active. During the
# boot-time preflight there is no running master yet, so stale runtime "running"
# flags from a hard power-off are ignored. A real recovery guard is never ignored.
if [ -f "$RUNTIME/update_guard.json" ]; then
    log "Update-Wiederherstellung ist offen – Git-Update bis zur sicheren Recovery ausgesetzt."
    exit 0
fi
if [ "$BOOT_MODE" -eq 0 ]; then
    if ! /usr/bin/python3 - "$RUNTIME" <<'PY'
import json, pathlib, sys
r=pathlib.Path(sys.argv[1])
def read(name, default):
    try: return json.loads((r/name).read_text())
    except Exception: return default
u=read('update_ui_state.json', {})
if isinstance(u, dict) and u.get('running'):
    raise SystemExit(1)
j=read('node_jobs.json', {})
if isinstance(j, dict) and any(isinstance(v, dict) and v.get('running') for v in j.values()):
    raise SystemExit(1)
PY
    then
        log "Master ist mit Start/Reboot/Update beschäftigt – Git-Update später erneut versuchen."
        exit 0
    fi
fi

# Tracked local edits are not discarded. This protects emergency changes made on site.
if ! git -C "$DIR" diff --quiet -- || ! git -C "$DIR" diff --cached --quiet --; then
    log "Lokale Änderungen an getrackten Dateien vorhanden – automatisches Update abgebrochen."
    exit 3
fi

if ! git -C "$DIR" remote get-url "$REMOTE" >/dev/null 2>&1; then
    log "Git-Remote '$REMOTE' fehlt – Update übersprungen."
    exit 0
fi

export GIT_TERMINAL_PROMPT=0
# For a private repo/deploy key: accept github.com's key on first use, but do not
# disable host-key checking globally as we deliberately do for the local Pis.
export GIT_SSH_COMMAND="${GIT_SSH_COMMAND:-ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new}"

OLD="$(git -C "$DIR" rev-parse HEAD)"
log "Prüfe $REMOTE/$BRANCH …"
if ! timeout "${FETCH_TIMEOUT}s" git -C "$DIR" fetch --quiet "$REMOTE" "refs/heads/$BRANCH:refs/remotes/$REMOTE/$BRANCH"; then
    log "GitHub nicht erreichbar oder Fetch fehlgeschlagen – alter, funktionierender Stand bleibt aktiv."
    exit 4
fi
REMOTE_SHA="$(git -C "$DIR" rev-parse "$REMOTE/$BRANCH")"

# Also reject a remote commit which tries to introduce nodes.json. The automatic
# updater must never create, merge, replace or delete the local nodes.json.
if git -C "$DIR" cat-file -e "$REMOTE_SHA:nodes.json" 2>/dev/null; then
    log "SICHERHEITSSTOPP: Remote-Commit enthält nodes.json. Code-Update verweigert; lokale nodes.json bleibt unverändert."
    exit 8
fi

if [ "$OLD" = "$REMOTE_SHA" ]; then
    log "Bereits aktuell ($OLD)."
    exit 0
fi

# Refuse force-push/diverged histories on the installation machine.
if ! git -C "$DIR" merge-base --is-ancestor "$OLD" "$REMOTE_SHA"; then
    log "Remote-Historie ist nicht fast-forward. Automatisches Update verweigert."
    exit 5
fi

git -C "$DIR" merge --ff-only --quiet "$REMOTE/$BRANCH"
NEW="$(git -C "$DIR" rev-parse HEAD)"

# Validate the newly checked-out code before the systemd wrapper restarts the
# running master. Since the tree was clean before the fast-forward, a rollback
# to OLD is safe and does not touch ignored local nodes.json/runtime state.
validate_new_code() {
    [ -f "$DIR/master.py" ] && [ -f "$DIR/update_pis.py" ] || return 1
    [ -f "$DIR/web/index.html" ] && [ -f "$DIR/web/app.js" ] && [ -f "$DIR/web/style.css" ] || return 1
    /usr/bin/python3 -m py_compile "$DIR/master.py" "$DIR/update_pis.py" || return 1
    for f in "$DIR"/scripts/*.sh "$DIR"/run_master.sh "$DIR"/git_update.sh "$DIR"/git_update_systemd.sh; do
        [ -f "$f" ] || return 1
        /bin/bash -n "$f" || return 1
    done
}
if ! validate_new_code; then
    log "Neuer Git-Stand besteht die lokalen Syntax-/Dateiprüfungen nicht – Rollback auf $OLD."
    git -C "$DIR" reset --hard --quiet "$OLD"
    exit 6
fi

printf '%s\n' "$NEW" > "$RUNTIME/last_git_commit"
date -u '+%Y-%m-%dT%H:%M:%SZ' > "$RUNTIME/last_git_update_utc"
touch "$RUNTIME/restart_required"
log "Aktualisiert und geprüft: $OLD -> $NEW"
