#!/bin/bash
# Minimal supervisor/stream probe used by the master's 5-minute audit.
# Deliberately avoids findmnt, package-version commands and other heavier checks.
set -u
WORKDIR="${STREAM_MASTER_WORKDIR:-/tmp/stream-master}"
PIDFILE="$WORKDIR/stream.pid"
STATEFILE="$WORKDIR/status.env"

state_value(){ key="$1"; value=''; [ -r "$STATEFILE" ] && value="$(grep "^${key}=" "$STATEFILE" 2>/dev/null | tail -n 1 || true)"; printf '%s' "${value#*=}"; }
PID=''; [ -s "$PIDFILE" ] && PID="$(cat "$PIDFILE" 2>/dev/null || true)"; case "$PID" in ''|*[!0-9]*) PID='' ;; esac
STATUS=stopped
if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
    cmd="$(tr '\0' ' ' < "/proc/$PID/cmdline" 2>/dev/null || true)"
    case "$cmd" in *"$WORKDIR/supervisor.sh"*) STATUS=running ;; *) PID='' ;; esac
fi

RUN_ID="$(state_value RUN_ID)"
STREAM_HEALTH="$(state_value STREAM_HEALTH)"
STREAM_REASON="$(state_value STREAM_REASON)"
STREAM_SOURCE="$(state_value STREAM_SOURCE)"
STREAM_QUALITY_POLICY="$(state_value STREAM_QUALITY_POLICY)"
SELECTED_STREAM="$(state_value SELECTED_STREAM)"
STREAM_LAST_ERROR="$(state_value STREAM_LAST_ERROR)"
STREAM_LAST_RC="$(state_value STREAM_LAST_RC)"
STREAM_RETRY_IN="$(state_value STREAM_RETRY_IN)"
YOUTUBE_COOKIES="$(state_value YOUTUBE_COOKIES)"
STREAM_UPDATED_UTC="$(state_value STREAM_UPDATED_UTC)"

if [ "$STATUS" = stopped ]; then
    if [ -n "$RUN_ID" ] && [ -n "$STREAM_HEALTH" ] && [ "$STREAM_HEALTH" != stopped ]; then
        old="$STREAM_HEALTH"
        STREAM_HEALTH=supervisor_down
        STREAM_REASON="supervisor process is not running; last state was $old"
    else
        STREAM_HEALTH=stopped
        [ -n "$STREAM_REASON" ] || STREAM_REASON='stream is not running'
    fi
elif [ -z "$STREAM_HEALTH" ]; then
    STREAM_HEALTH=running
    STREAM_REASON='supervisor running'
fi

[ -n "$RUN_ID" ] || RUN_ID=unknown
[ -n "$STREAM_HEALTH" ] || STREAM_HEALTH=unknown
[ -n "$STREAM_REASON" ] || STREAM_REASON=unknown
[ -n "$STREAM_SOURCE" ] || STREAM_SOURCE=unknown
[ -n "$STREAM_QUALITY_POLICY" ] || STREAM_QUALITY_POLICY=unknown
[ -n "$SELECTED_STREAM" ] || SELECTED_STREAM=unknown
[ -n "$STREAM_LAST_ERROR" ] || STREAM_LAST_ERROR=none
[ -n "$STREAM_LAST_RC" ] || STREAM_LAST_RC=none
[ -n "$STREAM_RETRY_IN" ] || STREAM_RETRY_IN=0
[ -n "$YOUTUBE_COOKIES" ] || YOUTUBE_COOKIES=unknown
[ -n "$STREAM_UPDATED_UTC" ] || STREAM_UPDATED_UTC=unknown

printf 'STATUS=%s\nSUPERVISOR_PID=%s\nRUN_ID=%s\nSTREAM_HEALTH=%s\nSTREAM_REASON=%s\nSTREAM_SOURCE=%s\nSTREAM_QUALITY_POLICY=%s\nSELECTED_STREAM=%s\nSTREAM_LAST_ERROR=%s\nSTREAM_LAST_RC=%s\nSTREAM_RETRY_IN=%s\nYOUTUBE_COOKIES=%s\nSTREAM_UPDATED_UTC=%s\n' \
  "$STATUS" "${PID:-none}" "$RUN_ID" "$STREAM_HEALTH" "$STREAM_REASON" "$STREAM_SOURCE" "$STREAM_QUALITY_POLICY" "$SELECTED_STREAM" "$STREAM_LAST_ERROR" "$STREAM_LAST_RC" "$STREAM_RETRY_IN" "$YOUTUBE_COOKIES" "$STREAM_UPDATED_UTC"
