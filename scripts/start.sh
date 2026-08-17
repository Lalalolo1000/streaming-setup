#!/bin/bash
# Idempotent Streamlink/VLC launcher with a tiny /tmp supervisor and health state.
# The whole stream stack runs in one process group so Stop/Restart can terminate
# supervisor + Streamlink + VLC + helper children reliably.
set -u

URL="${1:?URL missing}"
QUALITY="${2:-max480}"
CONNECTOR="${3:-HDMI-A-1}"
COOKIE_FILE="${4:-}"

WORKDIR="${STREAM_MASTER_WORKDIR:-/tmp/stream-master}"
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
URL="$1"; QUALITY="$2"; CONNECTOR="$3"; STREAMLINK="$4"; CVLC="$5"; LOGFILE="$6"; STATEFILE="$7"; RUN_ID="$8"; COOKIE_FILE="${9:-}"
WORKDIR="${STATEFILE%/*}"
PIDFILE="$WORKDIR/stream.pid"
ATTEMPT_LOG="$WORKDIR/current-attempt.log"
PLAYER_ARGS="--fullscreen --no-audio --drm-vout-display=$CONNECTOR"
USER_AGENT='Mozilla/5.0 (X11; Linux armv7l) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'

# The supervisor is launched through setsid and is therefore the leader of the
# controller-owned process group. Record our own PID, not the launcher's guess.
printf '%s\n' "$$" > "$PIDFILE"

case "$QUALITY" in
    max480|auto|max-480|480p|480p,worst)
        STREAM_SELECTOR='best,worst-unfiltered'; SORT_LIMIT='>480p'; QUALITY_POLICY='max480' ;;
    *) STREAM_SELECTOR="$QUALITY"; SORT_LIMIT=''; QUALITY_POLICY="custom:$QUALITY" ;;
esac

SOURCE_HINT='other'
EXTRA_ARGS=()
YOUTUBE_COOKIES='not_applicable'
RETRY_STREAMS=30
RETRY_MAX=6
case "$URL" in
    *earthcam.com/*)
        SOURCE_HINT='earthcam'
        EXTRA_ARGS+=("--http-header=User-Agent=$USER_AGENT")
        EXTRA_ARGS+=("--http-header=Referer=https://www.earthcam.com/")
        ;;
    *youtube.com/*|*youtu.be/*)
        SOURCE_HINT='youtube'
        # Streamlink's built-in YouTube plugin sets its own current Chrome UA.
        # Keep just one internal retry for a transient youtubei timeout; a real
        # LOGIN_REQUIRED should leave the inner loop quickly and hit our long
        # YouTube-specific cooldown instead of repeating bot-check requests.
        RETRY_MAX=1
        if [ -n "$COOKIE_FILE" ] && [ -r "$COOKIE_FILE" ] && [ -s "$COOKIE_FILE" ]; then
            EXTRA_ARGS+=("--http-cookies-file=$COOKIE_FILE")
            YOUTUBE_COOKIES='loaded'
        else
            YOUTUBE_COOKIES='missing'
        fi
        ;;
esac

MIN_BACKOFF=30
MAX_BACKOFF=180
BACKOFF_STEP=30
STABLE_SECONDS=180
QUICK_FAIL_SECONDS=120
QUICK_FAIL_LIMIT=10
COOLDOWN_SECONDS=300
MAX_CHRONIC_COOLDOWN=3600
YOUTUBE_LOGIN_COOLDOWN=600
MAX_YOUTUBE_COOLDOWN=3600
MAX_LOG_BYTES=1048576
MAX_ATTEMPT_LOG_BYTES=262144
MONITOR_INTERVAL=10
LAST_STATE_SIG=''
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
cap_file(){
    file="$1"; max="$2"
    [ -f "$file" ] || return 0
    size="$(wc -c < "$file" 2>/dev/null || echo 0)"
    case "$size" in ''|*[!0-9]*) return 0 ;; esac
    if [ "$size" -gt "$max" ]; then
        tmp="$file.cap.$$"
        tail -c "$max" "$file" > "$tmp" 2>/dev/null || return 0
        # Keep the same inode while Streamlink/tee has the file open. Replacing
        # the pathname would make tee continue writing to an unlinked old inode.
        cat "$tmp" > "$file" 2>/dev/null || true
        rm -f "$tmp"
    fi
}
jitter_seconds(){
    base="$1"
    spread=$((base / 10))
    [ "$spread" -ge 1 ] || { printf '%s' "$base"; return; }
    delta=$((RANDOM % (spread * 2 + 1) - spread))
    value=$((base + delta))
    [ "$value" -ge 1 ] || value=1
    printf '%s' "$value"
}
chronic_cooldown(){
    # Repeated batches of quick failures back off much more aggressively. A
    # genuinely stable run resets the level, so a webcam can recover unattended
    # without a dead source hammering its provider for days.
    batches="$1"
    case "$batches" in
        0|1) base=$COOLDOWN_SECONDS ;;
        2) base=900 ;;
        3) base=1800 ;;
        *) base=$MAX_CHRONIC_COOLDOWN ;;
    esac
    jitter_seconds "$base"
}
youtube_cooldown(){
    blocks="$1"
    case "$blocks" in
        0|1) base=$YOUTUBE_LOGIN_COOLDOWN ;;
        2) base=1200 ;;
        3) base=1800 ;;
        *) base=$MAX_YOUTUBE_COOLDOWN ;;
    esac
    jitter_seconds "$base"
}
owns_state(){
    [ ! -r "$STATEFILE" ] && return 0
    current="$(grep '^RUN_ID=' "$STATEFILE" 2>/dev/null | tail -n 1 || true)"
    current="${current#RUN_ID=}"
    [ -z "$current" ] || [ "$current" = "$RUN_ID" ]
}
write_state(){
    owns_state || return 0
    health="$1"; reason="$2"; retry_in="${3:-0}"
    sig="$health|$reason|$retry_in|$SOURCE_NAME|$SELECTED_STREAM|$LAST_ERROR|$LAST_RC|$YOUTUBE_COOKIES"
    [ "$sig" = "$LAST_STATE_SIG" ] && return 0
    LAST_STATE_SIG="$sig"
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
        printf 'YOUTUBE_COOKIES=%s\n' "$(clean_value "$YOUTUBE_COOKIES")"
        printf 'STREAM_UPDATED_UTC=%s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
    } > "$tmp"
    mv "$tmp" "$STATEFILE"
}

classify_attempt(){
    text="$(tail -n 180 "$ATTEMPT_LOG" 2>/dev/null || true)"
    plugin="$(printf '%s\n' "$text" | sed -n 's/.*Found matching plugin \([^ ]*\) for URL.*/\1/p' | tail -n 1)"
    [ -n "$plugin" ] && SOURCE_NAME="$plugin"
    selected="$(printf '%s\n' "$text" | sed -n 's/.*Opening stream: \([^ ]*\).*/\1/p' | tail -n 1)"
    [ -n "$selected" ] && SELECTED_STREAM="$selected"

    # Plugins can emit transient errors and recover in the SAME attempt. A later
    # player start therefore wins over earlier timeout/404/login messages.
    player_start_line="$(printf '%s\n' "$text" | grep -nE 'Starting player:' | tail -n 1 | cut -d: -f1 || true)"
    player_fail_line="$(printf '%s\n' "$text" | grep -nEi 'player.*exited|Failed to start player|Could not open player|VLC.*error|main input error|decoder error' | tail -n 1 | cut -d: -f1 || true)"
    waiting_line="$(printf '%s\n' "$text" | grep -nE 'Waiting for streams, retrying every' | tail -n 1 | cut -d: -f1 || true)"
    player_start_line="${player_start_line:-0}"
    player_fail_line="${player_fail_line:-0}"
    waiting_line="${waiting_line:-0}"

    if [ "$player_fail_line" -gt "$player_start_line" ] && [ "$player_fail_line" -gt 0 ]; then
        CURRENT_HEALTH='player_error'; CURRENT_REASON='VLC/player failed'; LAST_ERROR='player_error'
    elif [ "$player_start_line" -gt 0 ]; then
        CURRENT_HEALTH='live'; CURRENT_REASON='stream opened and player started'; LAST_ERROR='none'
    elif [ "$SOURCE_HINT" = youtube ] && printf '%s\n' "$text" | grep -Eqi 'LOGIN_REQUIRED|Sign in to confirm[^[:cntrl:]]*not a bot'; then
        CURRENT_HEALTH='youtube_login_required'; CURRENT_REASON='YouTube requires sign-in / bot verification'; LAST_ERROR='youtube_login_required'
    elif [ "$waiting_line" -gt 0 ]; then
        CURRENT_HEALTH='waiting'; CURRENT_REASON='stream temporarily unavailable; Streamlink is retrying'; LAST_ERROR='none'
    elif [ "$CURRENT_HEALTH" = live ]; then
        # The bounded attempt log may eventually rotate away the original
        # 'Starting player' line. Preserve an already-confirmed live state until
        # a newer explicit failure/waiting signal is observed.
        CURRENT_HEALTH='live'; CURRENT_REASON='stream opened and player started'; LAST_ERROR='none'
    elif printf '%s\n' "$text" | grep -Eqi '403([^0-9]|$)|Forbidden'; then
        CURRENT_HEALTH='http_403'; CURRENT_REASON='source returned HTTP 403 / Forbidden'; LAST_ERROR='http_403'
    elif printf '%s\n' "$text" | grep -Eqi '404([^0-9]|$)|Not Found'; then
        CURRENT_HEALTH='http_404'; CURRENT_REASON='source returned HTTP 404 / Not Found'; LAST_ERROR='http_404'
    elif printf '%s\n' "$text" | grep -Eqi 'No plugin can handle|No plugin matches|unsupported URL'; then
        CURRENT_HEALTH='unsupported_url'; CURRENT_REASON='Streamlink has no plugin for this URL'; LAST_ERROR='unsupported_url'
    elif printf '%s\n' "$text" | grep -Eqi 'No playable streams|No streams found|No streams available|does not contain any streams'; then
        CURRENT_HEALTH='no_stream'; CURRENT_REASON='source is reachable but no playable stream is available'; LAST_ERROR='no_stream'
    elif printf '%s\n' "$text" | grep -Eqi 'Temporary failure in name resolution|Name or service not known|NameResolution|Connection refused|Network is unreachable|Failed to establish a new connection|Unable to open URL|ConnectionError|ConnectTimeout|Read timed out|timed out'; then
        CURRENT_HEALTH='source_unreachable'; CURRENT_REASON='could not reach the stream source'; LAST_ERROR='source_unreachable'
    elif printf '%s\n' "$text" | grep -Eqi '\[.*\]\[error\]|error:'; then
        err="$(printf '%s\n' "$text" | grep -Ei '\[.*\]\[error\]|error:' | tail -n 1 | sed 's/^[[:space:]]*//')"
        CURRENT_HEALTH='stream_error'; CURRENT_REASON="${err:-Streamlink reported an error}"; LAST_ERROR='stream_error'
    else
        CURRENT_HEALTH='connecting'; CURRENT_REASON='waiting for Streamlink to open the source'
    fi
    write_state "$CURRENT_HEALTH" "$CURRENT_REASON" 0
}

monitor_attempt(){ while [ -n "$CHILD" ] && kill -0 "$CHILD" 2>/dev/null; do classify_attempt; cap_file "$LOGFILE" "$MAX_LOG_BYTES"; cap_file "$ATTEMPT_LOG" "$MAX_ATTEMPT_LOG_BYTES"; sleep "$MONITOR_INTERVAL"; done; }
stop_supervisor(){
    STOP=1
    [ -n "$MONITOR" ] && kill -TERM "$MONITOR" 2>/dev/null || true
    [ -n "$CHILD" ] && kill -TERM "$CHILD" 2>/dev/null || true
}
trap stop_supervisor TERM INT HUP

backoff=$MIN_BACKOFF
quick_failures=0
failure_batches=0
youtube_blocks=0
write_state 'starting' 'supervisor started' 0

while [ "$STOP" -eq 0 ]; do
    started="$(date +%s)"
    : > "$ATTEMPT_LOG"
    SELECTED_STREAM='unknown'; CURRENT_HEALTH='connecting'; CURRENT_REASON='starting Streamlink'
    write_state 'connecting' 'opening stream source' 0
    {
        echo "===== attempt $(date -Is 2>/dev/null || date) ====="
        echo "run_id=$RUN_ID url=$URL selector=$STREAM_SELECTOR policy=$QUALITY_POLICY connector=$CONNECTOR source_hint=$SOURCE_HINT"
        echo "backoff=${backoff}s quick_failures=$quick_failures failure_batches=$failure_batches youtube_blocks=$youtube_blocks youtube_cookies=$YOUTUBE_COOKIES"
    } >> "$LOGFILE"

    CMD=(
        "$STREAMLINK"
        "--player=$CVLC"
        "--player-args=$PLAYER_ARGS"
        "--retry-streams=$RETRY_STREAMS"
        "--retry-max=$RETRY_MAX"
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

    # LOGIN_REQUIRED / bot-verification is different from a normal network or
    # HLS error. Frequent restarts only send more rejected requests.
    # Pause this source for ten minutes, then try from a clean outer state.
    if [ "$CURRENT_HEALTH" = youtube_login_required ]; then
        youtube_blocks=$((youtube_blocks + 1))
        delay="$(youtube_cooldown "$youtube_blocks")"
        write_state 'youtube_login_required' "YouTube requires sign-in / bot verification; retrying in ${delay}s" "$delay"
        echo "YouTube login/bot verification requested; block level=$youtube_blocks, waiting ${delay}s (jittered) before retry." >> "$LOGFILE"
        sleep "$delay" & wait $! || true
        quick_failures=0
        backoff=$MIN_BACKOFF
        continue
    fi

    if [ "$CURRENT_HEALTH" = live ] || [ "$CURRENT_HEALTH" = connecting ]; then
        CURRENT_REASON="Streamlink exited unexpectedly (rc=$rc)"; LAST_ERROR='stream_error'
    fi

    if [ "$runtime" -ge "$STABLE_SECONDS" ]; then
        quick_failures=0; backoff=$MIN_BACKOFF; failure_batches=0; youtube_blocks=0
        echo 'Stable run; restart penalty reset.' >> "$LOGFILE"
    elif [ "$runtime" -lt "$QUICK_FAIL_SECONDS" ]; then
        quick_failures=$((quick_failures + 1))
    fi

    if [ "$quick_failures" -ge "$QUICK_FAIL_LIMIT" ]; then
        failure_batches=$((failure_batches + 1))
        delay="$(chronic_cooldown "$failure_batches")"
        write_state 'cooldown' "too many quick failures; last error: $CURRENT_REASON" "$delay"
        echo "Too many quick failures; chronic level=$failure_batches, cooling down ${delay}s (jittered)." >> "$LOGFILE"
        sleep "$delay" & wait $! || true
        quick_failures=0; backoff=$MIN_BACKOFF
        continue
    fi

    delay="$(jitter_seconds "$backoff")"
    write_state 'retrying' "$CURRENT_REASON; retrying in ${delay}s" "$delay"
    echo "Restarting in ${delay}s (base ${backoff}s, jittered)." >> "$LOGFILE"
    cap_file "$LOGFILE" "$MAX_LOG_BYTES"
    cap_file "$ATTEMPT_LOG" "$MAX_ATTEMPT_LOG_BYTES"
    sleep "$delay" & wait $! || true
    if [ "$backoff" -lt "$MAX_BACKOFF" ]; then
        backoff=$((backoff + BACKOFF_STEP)); [ "$backoff" -le "$MAX_BACKOFF" ] || backoff=$MAX_BACKOFF
    fi
done
write_state 'stopped' 'supervisor stopped' 0
rm -f "$PIDFILE"
echo "Supervisor stopped $(date -Is 2>/dev/null || date)" >> "$LOGFILE"
SUPERVISOR_EOF
chmod 700 "$SUPERVISOR"

valid_supervisor_pid(){
    pid="$1"
    case "$pid" in ''|*[!0-9]*) return 1 ;; esac
    [ -r "/proc/$pid/cmdline" ] || return 1
    cmd="$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true)"
    case "$cmd" in *"$WORKDIR/supervisor.sh"*) return 0 ;; *) return 1 ;; esac
}

kill_recorded_group(){
    pid=''
    [ -s "$PIDFILE" ] && pid="$(cat "$PIDFILE" 2>/dev/null || true)"
    valid_supervisor_pid "$pid" || return 0
    pgrp="$(awk '{print $5}' "/proc/$pid/stat" 2>/dev/null || true)"
    if [ "$pgrp" = "$pid" ]; then
        echo "Stopping previous stream process group $pid"
        kill -TERM -- "-$pid" 2>/dev/null || true
        sleep 2
        # Kill the group once more even if its leader already exited; a stubborn
        # VLC/ffmpeg child may still be holding the old process group alive.
        kill -KILL -- "-$pid" 2>/dev/null || true
    else
        kill -TERM "$pid" 2>/dev/null || true
    fi
}

# Fallback for older generations which predate process-group ownership, or for a
# stale/missing PID file. These Pis are dedicated stream players, so matching the
# controller supervisor plus Streamlink/VLC is intentional.
find_matching_pids(){
    for proc in /proc/[0-9]*; do
        [ -r "$proc/cmdline" ] || continue
        pid="${proc##*/}"; [ "$pid" = "$$" ] && continue
        cmd="$(tr '\0' ' ' < "$proc/cmdline" 2>/dev/null || true)"
        case "$cmd" in
            */stream-master/supervisor.sh*|*/bin/streamlink\ *|*/streamlink/bin/python\ *|*/bin/cvlc\ *) printf '%s\n' "$pid" ;;
        esac
    done
}

# Start/restart is intentionally idempotent: destroy the old complete stack,
# including the retry watcher, before creating a new generation.
kill_recorded_group
PIDS="$(find_matching_pids)"
for pid in $PIDS; do kill -TERM "$pid" 2>/dev/null || true; done
for _ in 1 2 3; do sleep 1; PIDS="$(find_matching_pids)"; [ -z "$PIDS" ] && break; done
PIDS="$(find_matching_pids)"
for pid in $PIDS; do kill -KILL "$pid" 2>/dev/null || true; done
rm -f "$PIDFILE"

COOKIE_STATUS='not_applicable'
case "$URL" in
    *youtube.com/*|*youtu.be/*)
        if [ -n "$COOKIE_FILE" ] && [ -r "$COOKIE_FILE" ] && [ -s "$COOKIE_FILE" ]; then COOKIE_STATUS='loaded'; else COOKIE_STATUS='missing'; fi
        ;;
esac

{
    echo "===== supervisor start $(date -Is 2>/dev/null || date) ====="
    echo "run_id=$RUN_ID"
    echo "streamlink=$STREAMLINK"
    echo "cvlc=$CVLC"
    echo "url=$URL"
    echo "quality=$QUALITY"
    echo "connector=$CONNECTOR"
    echo "youtube_cookies=$COOKIE_STATUS"
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
YOUTUBE_COOKIES=$COOKIE_STATUS
STREAM_UPDATED_UTC=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
EOF_STATE

nohup "$SETSID" bash "$SUPERVISOR" "$URL" "$QUALITY" "$CONNECTOR" "$STREAMLINK" "$CVLC" "$LOGFILE" "$STATEFILE" "$RUN_ID" "$COOKIE_FILE" >> "$LOGFILE" 2>&1 < /dev/null &
LAUNCH_PID=$!
# supervisor.sh writes the authoritative process-group leader PID itself.
for _ in 1 2 3 4 5; do
    sleep 0.2
    PID=''; [ -s "$PIDFILE" ] && PID="$(cat "$PIDFILE" 2>/dev/null || true)"
    valid_supervisor_pid "$PID" && break
    kill -0 "$LAUNCH_PID" 2>/dev/null || break
done
PID=''; [ -s "$PIDFILE" ] && PID="$(cat "$PIDFILE" 2>/dev/null || true)"
if valid_supervisor_pid "$PID"; then
    echo "Started supervisor PID $PID (run $RUN_ID)"
else
    echo 'ERROR: supervisor exited immediately'
    rm -f "$PIDFILE"
    tail -n 80 "$LOGFILE" 2>/dev/null || true
    exit 1
fi
