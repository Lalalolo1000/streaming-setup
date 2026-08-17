# GitHub-Deployment und automatische Code-Updates

Ziel: Du änderst den Code zuhause, pushst auf GitHub und der Master übernimmt den neuen Stand automatisch.

## Empfohlene Architektur

- **Code** liegt im GitHub-Repository.
- **`nodes.json`** ist ausschließlich lokale Installationskonfiguration und wird **niemals** von Git verwaltet.
- Das Repository enthält `nodes.default.json` mit `192.168.0.101` bis `.124`.
- Beim ersten Start/Installieren wird daraus lokal `nodes.json` erzeugt.
- `runtime/` und das gemeinsame Pi-Passwort bleiben ebenfalls lokal.

Dadurch überschreibt ein Code-Update weder deine 24 Links/Namen noch laufende Recovery-Daten. Zusätzlich verweigert `git_update.sh` jedes Update, falls `nodes.json` lokal getrackt ist oder im Remote-Commit auftaucht.

## Einmalige Einrichtung

Am saubersten ist es, den Master direkt als Git-Clone zu installieren.

### Öffentliches Repository

```bash
sudo apt-get update
sudo apt-get install -y git
git clone https://github.com/DEINNAME/streaming-setup.git ~/streaming-setup
cd ~/streaming-setup
./install_master.sh
./install_service.sh
```

### Privates Repository

Für ein privates Repository empfiehlt sich auf dem Master ein **read-only GitHub Deploy Key**. Der Key ist nur für das GitHub-Repository gedacht; die 24 Pis verwenden weiterhin ausschließlich das gemeinsame Passwort.

Nachdem der Git-Zugriff auf das Repository vom Master ohne Interaktion funktioniert:

```bash
cd ~/streaming-setup
./install_master.sh
./install_service.sh
```

## Danach zuhause

```bash
git add .
git commit -m "update installation"
git push origin main
```

Der Master prüft standardmäßig alle **5 Minuten** auf neue Commits.

Manuell prüfen:

```bash
sudo systemctl start stream-master-git-update.service
journalctl -u stream-master-git-update -n 100 --no-pager
```

Timer prüfen:

```bash
systemctl list-timers stream-master-git-update.timer
```

## Was beim Update passiert

1. Der Updater prüft, ob gerade ein Pi-Reboot, ein VLC/Streamlink-Update oder eine Recovery läuft.
2. Lokale Änderungen an **getrackten Code-Dateien** führen zum Abbruch; sie werden niemals automatisch verworfen.
3. GitHub wird abgefragt.
4. Nur ein **Fast-Forward** auf den konfigurierten Branch wird akzeptiert.
5. Python- und Shell-Syntax sowie Pflichtdateien werden geprüft.
6. Schlägt diese Prüfung fehl, wird der vorherige Commit automatisch wiederhergestellt.
7. Nur bei einem erfolgreichen Code-Update wird `stream-master.service` neu gestartet.

Wichtig: Ein erfolgreicher Git-bedingter Service-Neustart führt anschließend absichtlich wieder einen `Start / Restart all` für alle konfigurierten Streams aus. Dadurch läuft nach einem Code-Deploy überall garantiert der neue Start-/Supervisor-Code.

## Boot ohne Internet/GitHub

GitHub ist **keine Abhängigkeit für den Betrieb**. Beim Master-Boot wird kurz versucht, neuen Code zu holen. Wenn GitHub/Internet nicht erreichbar ist, startet anschließend der lokal vorhandene letzte Stand ganz normal.

## Branch / Intervall ändern

Branch bei der Service-Installation:

```bash
STREAM_MASTER_GIT_BRANCH=main ./install_service.sh
```

Prüfintervall, z. B. 2 Minuten:

```bash
STREAM_MASTER_GIT_INTERVAL=2min ./install_service.sh
```

Danach werden die systemd-Units neu geschrieben und aktiviert.

## Master must stay writable

The controller master (`192.168.0.101`) intentionally does not use root OverlayFS.
`git_update.sh` refuses to deploy if `/` is currently `overlay`, because such a checkout would not persist reliably across reboot.

One-time fix:

```bash
./prepare_master_writable.sh
sudo reboot
```

Worker nodes remain unchanged and can continue using OverlayFS.

The master stream itself runs in the separate `streaming-setup-local-stream.service` cgroup. Therefore restarting `stream-master.service` after a Git update does not stop Stream 01.
