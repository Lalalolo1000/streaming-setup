# Streaming Setup automatisch starten und am Leben halten

Die empfohlene Installation verwendet zwei systemd-Komponenten auf dem Master:

- `stream-master.service` – Webserver, SSH-Steuerung und täglicher Start der Streams
- `stream-master-git-update.timer` – regelmäßige GitHub-Codeprüfung

Auf den 24 Raspberry Pis wird dafür **kein zusätzlicher permanenter Service** benötigt.

## Installation

Als normaler Benutzer im Projektordner:

```bash
./install_master.sh
./install_service.sh
```

Status:

```bash
systemctl is-enabled stream-master
systemctl is-active stream-master
sudo systemctl status stream-master --no-pager
```

Logs:

```bash
journalctl -u stream-master -f
```

## Was beim täglichen Einschalten passiert

Wenn Master und Pis morgens gemeinsam Strom bekommen:

1. Linux startet den Master.
2. `stream-master.service` wartet auf `network-online.target`.
3. Vor dem Python-Start wird ein kurzer GitHub-Updateversuch gemacht.
4. Ist GitHub nicht erreichbar, wird trotzdem sofort der lokale letzte Code verwendet.
5. Die Weboberfläche startet auf Port 8080.
6. Der Master startet im Hintergrund jeden **konfigurierten** Pi-Stream einmal.
7. Noch nicht erreichbare Pis werden bis zu 15 Minuten erneut versucht.
8. Sobald `start.sh` auf einem Pi erfolgreich gestartet wurde, übernimmt dort der `/tmp`-Supervisor Streamlink/VLC-Retries.

Der einmalige Tagesstart ist an die aktuelle Linux-**Boot-ID** gebunden. Ein späterer Neustart nur des Python-Dienstes – zum Beispiel nach einem GitHub-Codeupdate – startet deshalb nicht alle Videos unnötig neu.

## Wenn ein Streamprozess abstürzt

Der Pi-Supervisor startet Streamlink/VLC mit kontrolliertem Backoff neu. Dafür muss der Master nicht eingreifen.

Wenn dagegen der komplette Pi selbst neu bootet, ist dessen `/tmp`-Supervisor weg. Ein über die Weboberfläche ausgelöster **Neu starten**-Vorgang wird vom Master verfolgt und startet den Stream nach dem Pi-Reboot automatisch neu.

## Master-Prozess stürzt ab

Die Unit verwendet:

```ini
Restart=on-failure
RestartSec=3
```

Test:

```bash
kill -KILL "$(systemctl show -p MainPID --value stream-master)"
sleep 5
systemctl is-active stream-master
```

Erwartet: `active`.

## GitHub-Timer

```bash
systemctl list-timers stream-master-git-update.timer
```

Logs:

```bash
journalctl -u stream-master-git-update -n 100 --no-pager
```

Der erste Timerlauf nach einem Boot wartet absichtlich 20 Minuten, weil der Boot selbst bereits einen GitHub-Check macht und die 24 Pis zunächst sauber hochfahren sollen. Danach ist das Standardintervall 5 Minuten.

Mehr dazu: `README_GITHUB_UPDATES.md`.

## Wichtig bei VLC-/Streamlink-Wartung

Während eines Node-Updates wird OverlayFS zeitweise deaktiviert. Deshalb:

- den Master nicht absichtlich ausschalten
- keinen Git-Code-Restart während dieser Wartung erzwingen
- bei `Sicherheitsprüfung erforderlich` zuerst die Admin-Recovery ausführen

Der Git-Timer überspringt Updates automatisch, solange eine solche Wartung oder Recovery aktiv ist.
