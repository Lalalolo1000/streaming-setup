#!/usr/bin/env bash
set -euo pipefail
DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PASSFILE="$HOME/.config/stream-master/ssh-password"

echo 'Streaming Setup – Master installieren'
echo 'Installiert werden: python3, openssh-client, sshpass und git.'

ROOT_TYPE="$(findmnt -n -o FSTYPE / 2>/dev/null || echo unknown)"
if [ "$ROOT_TYPE" = overlay ]; then
    echo
    echo 'HINWEIS: Dieser Raspberry Pi ist der Streaming-Setup-Master und läuft noch mit OverlayFS.'
    echo 'Git-/Konfigurationsänderungen müssen auf dem Master dauerhaft schreibbar sein.'
    echo 'Bitte zuerst ausführen:'
    echo "  $DIR/prepare_master_writable.sh"
    echo 'danach neu starten und install_master.sh erneut ausführen.'
    exit 10
fi
sudo apt-get update
sudo apt-get install -y python3 openssh-client sshpass git

mkdir -p "$HOME/.config/stream-master" "$DIR/runtime"

# Mutable local node configuration is deliberately not tracked by Git.
if [ ! -f "$DIR/nodes.json" ] && [ -f "$DIR/nodes.default.json" ]; then
    cp "$DIR/nodes.default.json" "$DIR/nodes.json"
    echo "Lokale Node-Konfiguration angelegt: $DIR/nodes.json"
fi
chmod 700 "$HOME/.config/stream-master"
chmod +x "$DIR/run_master.sh" "$DIR/install_service.sh" "$DIR/ssh_pi.sh" "$DIR/git_update.sh" "$DIR/git_update_systemd.sh" "$DIR/install_from_github.sh" "$DIR/install_tailscale.sh" "$DIR/migrate_ips_201_to_101.sh" "$DIR/prepare_master_writable.sh" "$DIR/update_master_local.sh" "$DIR/local_stream_service.sh" "$DIR/scripts/"*.sh "$DIR/update_pis.py" "$DIR/master.py"

if [ ! -s "$PASSFILE" ]; then
    echo
    echo 'Für alle Raspberry Pis wird bewusst ausschließlich das gemeinsame SSH-Passwort verwendet.'
    echo 'SSH-Host-Keys werden vom Streaming Setup-Server nicht gespeichert oder geprüft.'
    while true; do
        read -r -s -p 'Gemeinsames SSH-Passwort der Pis: ' PW1; echo
        read -r -s -p 'Passwort wiederholen: ' PW2; echo
        [ -n "$PW1" ] || { echo 'Das Passwort darf nicht leer sein.'; continue; }
        [ "$PW1" = "$PW2" ] || { echo 'Die Passwörter stimmen nicht überein.'; continue; }
        printf '%s\n' "$PW1" > "$PASSFILE"
        unset PW1 PW2
        break
    done
fi
chmod 600 "$PASSFILE"

echo "SSH-Passwortdatei: $PASSFILE"
echo 'Authentifizierung: nur Passwort; bekannte SSH-Host-Keys werden ignoriert.'

/usr/bin/python3 -m py_compile "$DIR/master.py" "$DIR/update_pis.py"
for f in "$DIR/scripts/"*.sh "$DIR/run_master.sh" "$DIR/ssh_pi.sh" "$DIR/git_update.sh" "$DIR/git_update_systemd.sh" "$DIR/install_from_github.sh" "$DIR/install_tailscale.sh" "$DIR/migrate_ips_201_to_101.sh" "$DIR/prepare_master_writable.sh" "$DIR/update_master_local.sh" "$DIR/local_stream_service.sh"; do bash -n "$f"; done

echo
echo 'Installation/Prüfung abgeschlossen.'
echo "Jetzt starten: $DIR/run_master.sh"
echo 'Danach öffnen: http://MASTER-IP:8080/'
echo 'Autostart: ./install_service.sh oder README_AUTOSTART.md'
