#!/usr/bin/env bash
# Called as root by stream-master-git-update.service.
set -euo pipefail
DIR="${PROJECT_DIR:?PROJECT_DIR missing}"
RUN_USER="${PROJECT_USER:?PROJECT_USER missing}"
RUN_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"
RUNTIME="$DIR/runtime"
[ -n "$RUN_HOME" ] || { echo "Cannot determine home for $RUN_USER" >&2; exit 1; }

run_as_user() {
    if command -v runuser >/dev/null 2>&1; then
        runuser -u "$RUN_USER" -- env HOME="$RUN_HOME" USER="$RUN_USER" STREAM_MASTER_GIT_REMOTE="${STREAM_MASTER_GIT_REMOTE:-origin}" STREAM_MASTER_GIT_BRANCH="${STREAM_MASTER_GIT_BRANCH:-main}" STREAM_MASTER_GIT_FETCH_TIMEOUT="${STREAM_MASTER_GIT_FETCH_TIMEOUT:-30}" "$@"
    else
        sudo -u "$RUN_USER" env HOME="$RUN_HOME" USER="$RUN_USER" STREAM_MASTER_GIT_REMOTE="${STREAM_MASTER_GIT_REMOTE:-origin}" STREAM_MASTER_GIT_BRANCH="${STREAM_MASTER_GIT_BRANCH:-main}" STREAM_MASTER_GIT_FETCH_TIMEOUT="${STREAM_MASTER_GIT_FETCH_TIMEOUT:-30}" "$@"
    fi
}

if [ ! -d "$DIR/.git" ]; then
    echo '[git-update] No .git directory; nothing to update.'
    exit 0
fi

set +e
run_as_user /bin/bash "$DIR/git_update.sh"
RC=$?
set -e
if [ "$RC" -ne 0 ]; then
    echo "[git-update] Update check returned rc=$RC; running version remains active."
    exit 0
fi

# A successful pull leaves this marker. If a node maintenance job started in the
# tiny window between fetch and restart, defer the restart until a later timer run.
if [ -f "$RUNTIME/restart_required" ]; then
    if ! /usr/bin/python3 - "$RUNTIME" <<'PY'
import json, pathlib, sys
r=pathlib.Path(sys.argv[1])
def read(name, default):
    try: return json.loads((r/name).read_text())
    except Exception: return default
if (r/'update_guard.json').exists(): raise SystemExit(1)
u=read('update_ui_state.json', {})
if isinstance(u,dict) and u.get('running'): raise SystemExit(1)
j=read('node_jobs.json', {})
if isinstance(j,dict) and any(isinstance(v,dict) and v.get('running') for v in j.values()): raise SystemExit(1)
f=read('fleet_job.json', {})
if isinstance(f,dict) and f.get('running'): raise SystemExit(1)
PY
    then
        echo '[git-update] New code is ready, but node maintenance is active; master restart deferred.'
        exit 0
    fi
    echo '[git-update] New code is ready; restarting stream-master service.'
    systemctl restart stream-master.service
fi
