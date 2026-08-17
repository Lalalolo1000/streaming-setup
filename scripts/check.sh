#!/bin/bash
# Fast machine-readable health check used by the web UI.
set -u
WORKDIR="${STREAM_MASTER_WORKDIR:-/tmp/stream-master}"
PIDFILE="$WORKDIR/stream.pid"
STATEFILE="$WORKDIR/status.env"
INFOFILE="/var/lib/stream-master/update-info"
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"

state_value(){ key="$1"; value=''; [ -r "$STATEFILE" ] && value="$(grep "^${key}=" "$STATEFILE" 2>/dev/null | tail -n 1 || true)"; printf '%s' "${value#*=}"; }
PID=''; [ -s "$PIDFILE" ] && PID="$(cat "$PIDFILE" 2>/dev/null || true)"; case "$PID" in ''|*[!0-9]*) PID='' ;; esac
STATUS=stopped
if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
    cmd="$(tr '\0' ' ' < "/proc/$PID/cmdline" 2>/dev/null || true)"
    case "$cmd" in *"$WORKDIR/supervisor.sh"*) STATUS=running ;; *) PID='' ;; esac
fi

echo "STATUS=$STATUS"
echo "SUPERVISOR_PID=${PID:-none}"
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
    STREAM_REASON='legacy supervisor; press Start / restart once for detailed health'
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
printf 'RUN_ID=%s\nSTREAM_HEALTH=%s\nSTREAM_REASON=%s\nSTREAM_SOURCE=%s\nSTREAM_QUALITY_POLICY=%s\nSELECTED_STREAM=%s\nSTREAM_LAST_ERROR=%s\nSTREAM_LAST_RC=%s\nSTREAM_RETRY_IN=%s\nYOUTUBE_COOKIES=%s\nSTREAM_UPDATED_UTC=%s\n' "$RUN_ID" "$STREAM_HEALTH" "$STREAM_REASON" "$STREAM_SOURCE" "$STREAM_QUALITY_POLICY" "$SELECTED_STREAM" "$STREAM_LAST_ERROR" "$STREAM_LAST_RC" "$STREAM_RETRY_IN" "$YOUTUBE_COOKIES" "$STREAM_UPDATED_UTC"

ROOT_FSTYPE="$(findmnt -n -o FSTYPE / 2>/dev/null || echo unknown)"
[ "$ROOT_FSTYPE" = overlay ] && OVERLAY_ACTIVE=yes || OVERLAY_ACTIVE=no
BOOT_MOUNT=''; findmnt -n /boot/firmware >/dev/null 2>&1 && BOOT_MOUNT=/boot/firmware || { findmnt -n /boot >/dev/null 2>&1 && BOOT_MOUNT=/boot || true; }
BOOT_MODE=unknown; BOOT_OPTIONS=unknown
if [ -n "$BOOT_MOUNT" ]; then
    BOOT_OPTIONS="$(findmnt -n -o OPTIONS "$BOOT_MOUNT" 2>/dev/null || echo unknown)"
    case ",$BOOT_OPTIONS," in *,ro,*) BOOT_MODE=ro ;; *,rw,*) BOOT_MODE=rw ;; esac
fi
if [ "$OVERLAY_ACTIVE" = yes ] && [ "$BOOT_MODE" = ro ]; then LOCKED=yes; elif [ "$OVERLAY_ACTIVE" = no ] || [ "$BOOT_MODE" = rw ]; then LOCKED=no; else LOCKED=unknown; fi
echo "LOCKED=$LOCKED"
echo "OVERLAY_ACTIVE=$OVERLAY_ACTIVE"
echo "ROOT_FSTYPE=$ROOT_FSTYPE"
echo "BOOT_MODE=$BOOT_MODE"

LAST_UPDATE_UTC=unknown; RECORDED_STREAMLINK_VERSION=''; RECORDED_VLC_VERSION=''
if [ -r "$INFOFILE" ]; then
    v="$(grep '^LAST_UPDATE_UTC=' "$INFOFILE" 2>/dev/null | tail -n1 || true)"; [ -n "$v" ] && LAST_UPDATE_UTC="${v#*=}"
    v="$(grep '^STREAMLINK_VERSION=' "$INFOFILE" 2>/dev/null | tail -n1 || true)"; [ -n "$v" ] && RECORDED_STREAMLINK_VERSION="${v#*=}"
    v="$(grep '^VLC_VERSION=' "$INFOFILE" 2>/dev/null | tail -n1 || true)"; [ -n "$v" ] && RECORDED_VLC_VERSION="${v#*=}"
fi
echo "LAST_UPDATE_UTC=$LAST_UPDATE_UTC"
if [ -n "$RECORDED_STREAMLINK_VERSION" ]; then STREAMLINK_VERSION="$RECORDED_STREAMLINK_VERSION"; else STREAMLINK="$(command -v streamlink 2>/dev/null || true)"; [ -z "$STREAMLINK" ] && [ -x "$HOME/.local/bin/streamlink" ] && STREAMLINK="$HOME/.local/bin/streamlink"; [ -n "$STREAMLINK" ] && STREAMLINK_VERSION="$("$STREAMLINK" --version 2>/dev/null | head -n1 || true)" || STREAMLINK_VERSION=missing; fi
if [ -n "$RECORDED_VLC_VERSION" ]; then VLC_VERSION="$RECORDED_VLC_VERSION"; else command -v cvlc >/dev/null 2>&1 && VLC_VERSION="$(cvlc --version 2>&1 | head -n1 || true)" || VLC_VERSION=missing; fi
echo "STREAMLINK_VERSION=$STREAMLINK_VERSION"
echo "VLC_VERSION=$VLC_VERSION"
echo "HOSTNAME=$(hostname 2>/dev/null || true)"
