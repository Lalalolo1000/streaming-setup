#!/bin/bash
# Kill controller-owned supervisor + Streamlink + VLC by scanning /proc.
set -u
WORKDIR="/tmp/stream-master"
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
STREAM_UPDATED_UTC=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
EOF_STATE
}

find_matching_pids() {
    for proc in /proc/[0-9]*; do
        [ -r "$proc/cmdline" ] || continue
        pid="${proc##*/}"
        [ "$pid" = "$SELF" ] && continue
        cmd="$(tr '\0' ' ' < "$proc/cmdline" 2>/dev/null || true)"
        case "$cmd" in
            *"$WORKDIR/supervisor.sh"*|*/bin/streamlink*|*/cvlc*) printf '%s\n' "$pid" ;;
        esac
    done
}

PIDS="$(find_matching_pids)"
if [ -z "$PIDS" ]; then
    echo "No supervisor/Streamlink/VLC processes found."
    rm -f "$WORKDIR/stream.pid"
    write_stopped
    exit 0
fi

echo "Stopping:"
for pid in $PIDS; do
    cmd="$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true)"
    echo "  $pid  $cmd"
done
for pid in $PIDS; do kill -TERM "$pid" 2>/dev/null || true; done
sleep 2
PIDS="$(find_matching_pids)"
if [ -n "$PIDS" ]; then
    echo "Force killing survivors: $PIDS"
    for pid in $PIDS; do kill -KILL "$pid" 2>/dev/null || true; done
fi
rm -f "$WORKDIR/stream.pid"
write_stopped
echo "Killed."
