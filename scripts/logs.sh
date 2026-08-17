#!/bin/bash
set -u
WORKDIR="${STREAM_MASTER_WORKDIR:-/tmp/stream-master}"
LOGFILE="$WORKDIR/stream.log"
if [ -f "$LOGFILE" ]; then
    tail -n 200 "$LOGFILE"
else
    echo "No stream log yet."
fi
