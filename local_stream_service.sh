#!/usr/bin/env bash
# Starts/stops the stream that runs on the controller master in its own systemd unit.
set -euo pipefail
DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ACTION="${1:-}"
MASTER_IP="${STREAM_MASTER_MASTER_IP:-192.168.0.101}"
NODES="$DIR/nodes.json"
[ -f "$NODES" ] || NODES="$DIR/nodes.default.json"

case "$ACTION" in
  stop)
    exec /bin/bash "$DIR/scripts/kill.sh"
    ;;
  start)
    ;;
  *)
    echo "usage: $0 start|stop" >&2
    exit 2
    ;;
esac

mapfile -d '' VALUES < <(/usr/bin/python3 - "$NODES" "$MASTER_IP" <<'PY'
import json, sys
path, master_ip = sys.argv[1:]
nodes=json.load(open(path,encoding='utf-8'))
masters=[]
for n in nodes:
    target=str(n.get('target',''))
    host=target.rsplit('@',1)[-1] if '@' in target else ''
    if str(n.get('role','node')).lower()=='master' or host==master_ip:
        masters.append(n)
if len(masters)!=1:
    raise SystemExit(f'expected exactly one master node, found {len(masters)}')
n=masters[0]
for value in (str(n.get('url','')), str(n.get('quality','max480')), str(n.get('connector','HDMI-A-1'))):
    sys.stdout.write(value+'\0')
PY
)

URL="${VALUES[0]:-}"
QUALITY="${VALUES[1]:-max480}"
CONNECTOR="${VALUES[2]:-HDMI-A-1}"

# A restart of the unit first executes ExecStop, so an empty URL intentionally
# leaves the master stream stopped.
if [ -z "$URL" ]; then
  echo 'Master stream has no URL configured; leaving it stopped.'
  exit 0
fi

exec /bin/bash "$DIR/scripts/start.sh" "$URL" "$QUALITY" "$CONNECTOR"
