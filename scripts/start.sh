#!/bin/bash
# Idempotent Streamlink/VLC launcher with a tiny /tmp supervisor and health state.
set -u

URL="${1:?URL missing}"
QUALITY="${2:-max480}"
CONNECTOR="${3:-HDMI-A-1}"

WORKDIR="/tmp/stream-master"
SUPERVISOR="$WORKDIR/supervisor.sh"
PIDFILE="$WORKDIR/stream.pid"
LOGFILE="$WORKDIR/stream.log"
STATEFILE="$WORKDIR/status.env"
RUN_ID="$(date +%s)-$$-${RANDOM:-0}"
mkdir -p "$WORKDIR"
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"

find_command() {
    command -v "$1" 2>/dev/null || { [ -x "$HOME/.local/bin/$1" ] && printf '%s\n' "$HOME/.local/bin/$1"; }
}
STREAMLINK="$(find_command streamlink)"
CVLC="$(find_command cvlc)"
SETSID="$(find_command setsid)"
[ -n "$STREAMLINK" ] || { echo "ERROR: streamlink not found"; exit 1; }
[ -n "$CVLC" ] || { echo "ERROR: cvlc not found"; exit 1; }
[ -n "$SETSID" ] || { echo "ERROR: setsid not found"; exit 1; }

cat > "$SUPERVISOR" <<'SUPERVISOR_EOF'
#!/bin/bash
set -u
URL="$1"; QUALITY="$2"; CONNECTOR="$3"; STREAMLINK="$4"; CVLC="$5"; LOGFILE="$6"; STATEFILE="$7"; RUN_ID="$8"
WORKDIR="${STATEFILE%/*}"
ATTEMPT_LOG="$WORKDIR/current-attempt.log"
PLAYER_ARGS="--fullscreen --no-audio --drm-vout-display=$CONNECTOR"
USER_AGENT='Mozilla/5.0 (X11; Linux armv7l) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'

case "$QUALITY" in
    max480|auto|max-480|480p|480p,worst)
        STREAM_SELECTOR='best,worst-unfiltered'; SORT_LIMIT='>480p'; QUALITY_POLICY='max480' ;;
    *) STREAM_SELECTOR="$QUALITY"; SORT_LIMIT=''; QUALITY_POLICY="custom:$QUALITY" ;;
esac

SOURCE_HINT='other'
EXTRA_ARGS=()
case "$URL" in
    *earthcam.com/*)
        SOURCE_HINT='earthcam'
        EXTRA_ARGS+=("--http-header=User-Agent=$USER_AGENT")
        EXTRA_ARGS+=("--http-header=Referer=https://www.earthcam.com/")
        ;;
    *youtube.com/*|*youtu.be/*)
        SOURCE_HINT='youtube'
        EXTRA_ARGS+=("--http-header=User-Agent=$USER_AGENT")
        ;;
esac

MIN_BACKOFF=10
MAX_BACKOFF=120
STABLE_SECONDS=600
QUICK_FAIL_SECONDS=120
QUICK_FAIL_LIMIT=6
COOLDOWN_SECONDS=900
STOP=0
CHILD=''
MONITOR=''
SELECTED_STREAM='unknown'
SOURCE_NAME="$SOURCE_HINT"
LAST_ERROR='none'
LAST_RC='none'
CURRENT_HEALTH='connecting'
CURRENT_REASON='starting Streamlink'

clean_value(){ printf '%s' "$1" | tr '\r\n' '  ' | cut -c1-350; }
owns_state(){
    [ ! -r "$STATEFILE" ] && return 0
    current="$(grep '^RUN_ID=' "$STATEFILE" 2>/dev/null | tail -n 1 || true)"
    current="${current#RUN_ID=}"
    [ -z "$current" ] || [ "$current" = "$RUN_ID" ]
}
write_state(){
    owns_state || return 0
    health="$1"; reason="$2"; retry_in="${3:-0}"
    tmp="$STATEFILE.tmp.$$"
    {
        printf 'RUN_ID=%s\n' "$(clean_value "$RUN_ID")"
        printf 'STREAM_HEALTH=%s\n' "$(clean_value "$health")"
        printf 'STREAM_REASON=%s\n' "$(clean_value "$reason")"
        printf 'STREAM_SOURCE=%s\n' "$(clean_value "$SOURCE_NAME")"
        printf 'STREAM_QUALITY_POLICY=%s\n' "$(clean_value "$QUALITY_POLICY")"
        printf 'SELECTED_STREAM=%s\n' "$(clean_value "$SELECTED_STREAM")"
        printf 'STREAM_LAST_ERROR=%s\n' "$(clean_value "$LAST_ERROR")"
        printf 'STREAM_LAST_RC=%s\n' "$(clean_value "$LAST_RC")"
        printf 'STREAM_RETRY_IN=%s\n' "$(clean_value "$retry_in")"
        printf 'STREAM_UPDATED_UTC=%s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
    } > "$tmp"
    mv "$tmp" "$STATEFILE"
}

classify_attempt(){
    text="$(tail -n 120 "$ATTEMPT_LOG" 2>/dev/null || true)"
    plugin="$(printf '%s\n' "$text" | sed -n 's/.*Found matching plugin \([^ ]*\) for URL.*/\1/p' | tail -n 1)"
    [ -n "$plugin" ] && SOURCE_NAME="$plugin"
    selected="$(printf '%s\n' "$text" | sed -n 's/.*Opening stream: \([^ ]*\).*/\1/p' | tail -n 1)"
    [ -n "$selected" ] && SELECTED_STREAM="$selected"

    if printf '%s\n' "$text" | grep -Eqi '403([^0-9]|$)|Forbidden'; then
        CURRENT_HEALTH='http_403'; CURRENT_REASON='source returned HTTP 403 / Forbidden'; LAST_ERROR='http_403'
    elif printf '%s\n' "$text" | grep -Eqi '404([^0-9]|$)|Not Found'; then
        CURRENT_HEALTH='http_404'; CURRENT_REASON='source returned HTTP 404 / Not Found'; LAST_ERROR='http_404'
    elif printf '%s\n' "$text" | grep -Eqi 'No plugin can handle|No plugin matches|unsupported URL'; then
        CURRENT_HEALTH='unsupported_url'; CURRENT_REASON='Streamlink has no plugin for this URL'; LAST_ERROR='unsupported_url'
    elif printf '%s\n' "$text" | grep -Eqi 'No playable streams|No streams found|No streams available|does not contain any streams'; then
        CURRENT_HEALTH='no_stream'; CURRENT_REASON='source is reachable but no playable stream is available'; LAST_ERROR='no_stream'
    elif printf '%s\n' "$text" | grep -Eqi 'Temporary failure in name resolution|Name or service not known|NameResolution|Connection refused|Network is unreachable|Failed to establish a new connection|Unable to open URL|ConnectionError|ConnectTimeout|Read timed out|timed out'; then
        CURRENT_HEALTH='source_unreachable'; CURRENT_REASON='could not reach the stream source'; LAST_ERROR='source_unreachable'
    elif printf '%s\n' "$text" | grep -Eqi 'player.*exited|Failed to start player|Could not open player|VLC.*error|main input error|decoder error'; then
        CURRENT_HEALTH='player_error'; CURRENT_REASON='VLC/player failed'; LAST_ERROR='player_error'
    elif printf '%s\n' "$text" | grep -Eqi '\[.*\]\[error\]|error:'; then
        err="$(printf '%s\n' "$text" | grep -Ei '\[.*\]\[error\]|error:' | tail -n 1 | sed 's/^[[:space:]]*//')"
        CURRENT_HEALTH='stream_error'; CURRENT_REASON="${err:-Streamlink reported an error}"; LAST_ERROR='stream_error'
    elif printf '%s\n' "$text" | grep -Eq 'Starting player:|Opening stream:'; then
        CURRENT_HEALTH='live'; CURRENT_REASON='stream opened and player started'; LAST_ERROR='none'
    else
        CURRENT_HEALTH='connecting'; CURRENT_REASON='waiting for Streamlink to open the source'
    fi
    write_state "$CURRENT_HEALTH" "$CURRENT_REASON" 0
}

monitor_attempt(){ while [ -n "$CHILD" ] && kill -0 "$CHILD" 2>/dev/null; do classify_attempt; sleep 2; done; }
stop_supervisor(){
    STOP=1
    [ -n "$MONITOR" ] && kill -TERM "$MONITOR" 2>/dev/null || true
    [ -n "$CHILD" ] && kill -TERM "$CHILD" 2>/dev/null || true
}
trap stop_supervisor TERM INT HUP

backoff=$MIN_BACKOFF
quick_failures=0
write_state 'starting' 'supervisor started' 0

while [ "$STOP" -eq 0 ]; do
    started="$(date +%s)"
    : > "$ATTEMPT_LOG"
    SELECTED_STREAM='unknown'; CURRENT_HEALTH='connecting'; CURRENT_REASON='starting Streamlink'
    write_state 'connecting' 'opening stream source' 0
    {
        echo "===== attempt $(date -Is 2>/dev/null || date) ====="
        echo "run_id=$RUN_ID url=$URL selector=$STREAM_SELECTOR policy=$QUALITY_POLICY connector=$CONNECTOR source_hint=$SOURCE_HINT"
        echo "backoff=${backoff}s quick_failures=$quick_failures"
    } >> "$LOGFILE"

    CMD=(
        "$STREAMLINK"
        "--player=$CVLC"
        "--player-args=$PLAYER_ARGS"
        "--retry-streams=15"
        "--retry-max=3"
        "--retry-open=3"
        "--stream-segment-attempts=5"
        "--hls-playlist-reload-attempts=5"
    )
    [ -n "$SORT_LIMIT" ] && CMD+=("--stream-sorting-excludes=$SORT_LIMIT")
    CMD+=("${EXTRA_ARGS[@]}")
    CMD+=("$URL" "$STREAM_SELECTOR")

    "${CMD[@]}" > >(tee -a "$LOGFILE" "$ATTEMPT_LOG" >/dev/null) 2>&1 &
    CHILD=$!
    monitor_attempt & MONITOR=$!
    wait "$CHILD"; rc=$?; LAST_RC="$rc"; CHILD=''
    [ -n "$MONITOR" ] && kill -TERM "$MONITOR" 2>/dev/null || true
    [ -n "$MONITOR" ] && wait "$MONITOR" 2>/dev/null || true
    MONITOR=''
    classify_attempt
    [ "$STOP" -eq 0 ] || break

    runtime=$(( $(date +%s) - started ))
    echo "Streamlink exited rc=$rc after ${runtime}s" >> "$LOGFILE"
    if [ "$CURRENT_HEALTH" = live ] || [ "$CURRENT_HEALTH" = connecting ]; then
        CURRENT_REASON="Streamlink exited unexpectedly (rc=$rc)"; LAST_ERROR='stream_error'
    fi

    if [ "$runtime" -ge "$STABLE_SECONDS" ]; then
        quick_failures=0; backoff=$MIN_BACKOFF
        echo 'Stable run; restart penalty reset.' >> "$LOGFILE"
    elif [ "$runtime" -lt "$QUICK_FAIL_SECONDS" ]; then
        quick_failures=$((quick_failures + 1))
    fi

    if [ "$quick_failures" -ge "$QUICK_FAIL_LIMIT" ]; then
        write_state 'cooldown' "too many quick failures; last error: $CURRENT_REASON" "$COOLDOWN_SECONDS"
        echo "Too many quick failures; cooling down ${COOLDOWN_SECONDS}s." >> "$LOGFILE"
        sleep "$COOLDOWN_SECONDS" & wait $! || true
        quick_failures=0; backoff=$MIN_BACKOFF
        continue
    fi

    write_state 'retrying' "$CURRENT_REASON; retrying in ${backoff}s" "$backoff"
    echo "Restarting in ${backoff}s." >> "$LOGFILE"
    sleep "$backoff" & wait $! || true
    if [ "$backoff" -lt "$MAX_BACKOFF" ]; then
        backoff=$((backoff * 2)); [ "$backoff" -le "$MAX_BACKOFF" ] || backoff=$MAX_BACKOFF
    fi
done
write_state 'stopped' 'supervisor stopped' 0
echo "Supervisor stopped $(date -Is 2>/dev/null || date)" >> "$LOGFILE"
SUPERVISOR_EOF
chmod 700 "$SUPERVISOR"

# Start is intentionally idempotent: stop any previous generation first.
find_matching_pids(){
    for proc in /proc/[0-9]*; do
        [ -r "$proc/cmdline" ] || continue
        pid="${proc##*/}"; [ "$pid" = "$$" ] && continue
        cmd="$(tr '\0' ' ' < "$proc/cmdline" 2>/dev/null || true)"
        case "$cmd" in
            *"$WORKDIR/supervisor.sh"*|*/streamlink\ *|*/cvlc\ *) printf '%s\n' "$pid" ;;
        esac
    done
}
PIDS="$(find_matching_pids)"
for pid in $PIDS; do kill -TERM "$pid" 2>/dev/null || true; done
for _ in 1 2 3; do sleep 1; PIDS="$(find_matching_pids)"; [ -z "$PIDS" ] && break; done
PIDS="$(find_matching_pids)"
for pid in $PIDS; do kill -KILL "$pid" 2>/dev/null || true; done

{
    echo "===== supervisor start $(date -Is 2>/dev/null || date) ====="
    echo "run_id=$RUN_ID"
    echo "streamlink=$STREAMLINK"
    echo "cvlc=$CVLC"
    echo "url=$URL"
    echo "quality=$QUALITY"
    echo "connector=$CONNECTOR"
} > "$LOGFILE"

cat > "$STATEFILE" <<EOF_STATE
RUN_ID=$RUN_ID
STREAM_HEALTH=starting
STREAM_REASON=start requested
STREAM_SOURCE=unknown
STREAM_QUALITY_POLICY=$QUALITY
SELECTED_STREAM=unknown
STREAM_LAST_ERROR=none
STREAM_LAST_RC=none
STREAM_RETRY_IN=0
STREAM_UPDATED_UTC=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
EOF_STATE

nohup "$SETSID" bash "$SUPERVISOR" "$URL" "$QUALITY" "$CONNECTOR" "$STREAMLINK" "$CVLC" "$LOGFILE" "$STATEFILE" "$RUN_ID" >> "$LOGFILE" 2>&1 < /dev/null &
PID=$!
printf '%s\n' "$PID" > "$PIDFILE"
sleep 1
if kill -0 "$PID" 2>/dev/null; then
    echo "Started supervisor PID $PID (run $RUN_ID)"
else
    echo 'ERROR: supervisor exited immediately'
    rm -f "$PIDFILE"
    tail -n 80 "$LOGFILE" 2>/dev/null || true
    exit 1
fi
