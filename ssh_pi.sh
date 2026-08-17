#!/usr/bin/env bash
set -euo pipefail
PASSFILE="$HOME/.config/stream-master/ssh-password"
[ $# -ge 1 ] || { echo "Benutzung: $0 IP-ODER-user@host [ssh-Argumente...]" >&2; exit 2; }
HOST="$1"; shift
case "$HOST" in *@*) TARGET="$HOST" ;; *) TARGET="pi@$HOST" ;; esac
[ -s "$PASSFILE" ] || { echo "SSH-Passwortdatei fehlt: $PASSFILE" >&2; exit 1; }
command -v sshpass >/dev/null || { echo 'sshpass fehlt. Bitte ./install_master.sh ausführen.' >&2; exit 1; }
exec sshpass -f "$PASSFILE" ssh \
  -o StrictHostKeyChecking=no \
  -o UserKnownHostsFile=/dev/null \
  -o GlobalKnownHostsFile=/dev/null \
  -o UpdateHostKeys=no \
  -o PreferredAuthentications=password \
  -o PubkeyAuthentication=no \
  -o PasswordAuthentication=yes \
  -o KbdInteractiveAuthentication=no \
  "$TARGET" "$@"
