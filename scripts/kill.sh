#!/bin/bash
# Stop the complete controller-owned stream process group. Falls back to /proc
# scanning for old supervisors created before process-group tracking existed.
set -u
WORKDIR="${STREAM_MASTER_WORKDIR:-/tmp/stream-master}"
PIDFILE="$WORKDIR/stream.pid"
STATEFILE="$WORKDIR/status.env"
SELF=$$

write_stopped() {
    mkdir -p "$WORKDIR"
    cat > "$STATEFILE" <<EOF_STATE
STREAM_HEALTH=stopped
STREAM_REASON=stopped by operator
STREAM_SOURCE=unknown
STREAM_QUALITY_POLICY=unknown
SELECTED_STREAM=unknown
STREAM_LAST_ERROR=none
STREAM_LAST_RC=none
STREAM_RETRY_IN=0
YOUTUBE_COOKIES=unknown
STREAM_UPDATED_UTC=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
EOF_STATE
}

valid_supervisor_pid(){
    pid="$1"
    case "$pid" in ''|*[!0-9]*) return 1 ;; esac
    [ -r "/proc/$pid/cmdline" ] || return 1
    cmd="$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true)"
    case "$cmd" in *"$WORKDIR/supervisor.sh"*) return 0 ;; *) return 1 ;; esac
}

find_matching_pids() {
    for proc in /proc/[0-9]*; do
        [ -r "$proc/cmdline" ] || continue
        pid="${proc##*/}"
        [ "$pid" = "$SELF" ] && continue
        cmd="$(tr '\0' ' ' < "$proc/cmdline" 2>/dev/null || true)"
        case "$cmd" in
            */stream-master/supervisor.sh*|*/bin/streamlink\ *|*/streamlink/bin/python\ *|*/bin/cvlc\ *) printf '%s\n' "$pid" ;;
        esac
    done
}

FOUND=0
PID=''
[ -s "$PIDFILE" ] && PID="$(cat "$PIDFILE" 2>/dev/null || true)"
if valid_supervisor_pid "$PID"; then
    FOUND=1
    pgrp="$(awk '{print $5}' "/proc/$PID/stat" 2>/dev/null || true)"
    if [ "$pgrp" = "$PID" ]; then
        echo "Stopping stream process group $PID"
        # This catches Streamlink, VLC, ffmpeg/mux helpers, tee/monitor children,
        # and anything else a particular YouTube stream may have spawned.
        kill -TERM -- "-$PID" 2>/dev/null || true
        sleep 2
        # The supervisor may exit before one of its descendants. Negative-PID
        # KILL still targets any survivors in the old process group.
        kill -KILL -- "-$PID" 2>/dev/null || true
    else
        # Should not happen for new starts, but do not send a negative kill to
        # an unrelated process group if an old supervisor is encountered.
        kill -TERM "$PID" 2>/dev/null || true
    fi
fi

# Always perform a final compatibility sweep. This also removes orphaned
# Streamlink/VLC processes left by old versions or a previously interrupted stop.
PIDS="$(find_matching_pids)"
if [ -n "$PIDS" ]; then
    FOUND=1
    echo "Stopping remaining controller stream processes: $PIDS"
    for pid in $PIDS; do kill -TERM "$pid" 2>/dev/null || true; done
    sleep 2
    PIDS="$(find_matching_pids)"
    if [ -n "$PIDS" ]; then
        echo "Force killing survivors: $PIDS"
        for pid in $PIDS; do kill -KILL "$pid" 2>/dev/null || true; done
    fi
fi

rm -f "$PIDFILE"
write_stopped
if [ "$FOUND" -eq 1 ]; then echo "Killed."; else echo "No supervisor/Streamlink/VLC processes found; state set to stopped."; fi
