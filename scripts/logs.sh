#!/bin/bash
set -u
LOGFILE="/tmp/stream-master/stream.log"
if [ -f "$LOGFILE" ]; then
    tail -n 200 "$LOGFILE"
else
    echo "No stream log yet."
fi
