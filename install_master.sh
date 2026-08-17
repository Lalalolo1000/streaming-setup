#!/usr/bin/env bash
set -euo pipefail
DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PASSFILE="$HOME/.config/stream-master/ssh-password"

if [ "$(id -u)" -eq 0 ]; then
    echo 'Bitte install_master.sh als normalen pi-Benutzer starten, nicht mit sudo.' >&2
    echo 'Das Skript verwendet sudo nur für die benötigten Systempakete.' >&2
    exit 2
fi

echo 'Streaming Setup – Master installieren'
echo 'Installiert werden: python3, openssh-client, sshpass, git, util-linux, pipx und VLC; Streamlink wird via pipx eingerichtet.'

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
sudo apt-get install -y python3 openssh-client sshpass git util-linux pipx vlc

# The master is also Stream 01, so it must be able to run the exact same local
# Streamlink/VLC stack as a worker. Keep Streamlink in the user's pipx venv so
# Update All can upgrade it without sudo.
if [ ! -x "$HOME/.local/bin/streamlink" ]; then
    echo 'Streamlink fehlt im Benutzer-pipx; wird installiert.'
    pipx install streamlink
fi

mkdir -p "$HOME/.config/stream-master" "$DIR/runtime"
chmod 700 "$DIR/runtime"

# Mutable local node configuration is deliberately not tracked by Git.
if [ ! -f "$DIR/nodes.json" ] && [ -f "$DIR/nodes.default.json" ]; then
    cp "$DIR/nodes.default.json" "$DIR/nodes.json"
    echo "Lokale Node-Konfiguration angelegt: $DIR/nodes.json"
fi
[ ! -f "$DIR/nodes.json" ] || chmod 600 "$DIR/nodes.json"
[ ! -f "$DIR/youtube-cookies.txt" ] || chmod 600 "$DIR/youtube-cookies.txt"
chmod 700 "$HOME/.config/stream-master"
chmod +x "$DIR/run_master.sh" "$DIR/install_service.sh" "$DIR/ssh_pi.sh" "$DIR/git_update.sh" "$DIR/git_update_systemd.sh" "$DIR/install_from_github.sh" "$DIR/install_tailscale.sh" "$DIR/migrate_ips_201_to_101.sh" "$DIR/prepare_master_writable.sh" "$DIR/update_master_local.sh" "$DIR/local_stream_service.sh" "$DIR/backup_local_state.sh" "$DIR/selftest.py" "$DIR/scripts/"*.sh "$DIR/update_pis.py" "$DIR/master.py"

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

if ! sudo -n true >/dev/null 2>&1; then
    echo >&2
    echo 'FEHLER: Der Master-Benutzer hat kein nicht-interaktives sudo (sudo -n).' >&2
    echo 'Streaming Setup benötigt das für lokale Reboots, Shutdown und Paketupdates.' >&2
    echo 'Richte NOPASSWD sudo für diesen dedizierten Master-Benutzer ein und führe install_master.sh erneut aus.' >&2
    exit 12
fi

/usr/bin/python3 -m py_compile "$DIR/master.py" "$DIR/update_pis.py" "$DIR/selftest.py"
/usr/bin/python3 "$DIR/selftest.py"
for f in "$DIR/scripts/"*.sh "$DIR/run_master.sh" "$DIR/ssh_pi.sh" "$DIR/git_update.sh" "$DIR/git_update_systemd.sh" "$DIR/install_from_github.sh" "$DIR/install_tailscale.sh" "$DIR/migrate_ips_201_to_101.sh" "$DIR/prepare_master_writable.sh" "$DIR/update_master_local.sh" "$DIR/local_stream_service.sh" "$DIR/backup_local_state.sh"; do bash -n "$f"; done

echo
echo 'Installation/Prüfung abgeschlossen.'
echo "Jetzt starten: $DIR/run_master.sh"
echo 'Danach öffnen: http://MASTER-IP:8080/'
echo 'Autostart: ./install_service.sh oder README_AUTOSTART.md'
