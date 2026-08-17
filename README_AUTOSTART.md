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

Der Start/Restart-Pass läuft bei **jedem Start des Python-Masterdienstes**. Das ist absichtlich so: täglicher Power-on, manueller Service-Neustart und ein erfolgreicher GitHub-Codeupdate wenden die aktuelle Konfiguration jeweils erneut auf alle Streams an. Die Startaufrufe erfolgen dabei nicht gleichzeitig: der Master kommt zuerst, anschließend werden die konfigurierten Nodes standardmäßig im Abstand von 5 Sekunden gestartet. Offline Nodes werden erst nach dem ersten Durchlauf erneut versucht.

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

## Master .101 and daily power-on

The controller master is also Stream 01. It must stay writable; see `README_MASTER_NODE.md`.
At a fresh OS boot, `stream-master.service` starts and the once-per-boot worker starts every configured stream. A Git-triggered service restart during the same OS boot does not repeat the whole startup pass.

Fleet reboot/shutdown actions persist their state under `runtime/`. This is important because the controller intentionally disappears when the master reboots or powers off. The master always performs its own power action last.

## Unexpected worker power loss recovery

The master has a low-load recovery monitor for `.102`–`.124`.

- Every **30 seconds** it only tests whether TCP port 22 is reachable. This is not an authenticated SSH login and does not execute anything on the Pi.
- The monitor starts after a **30 second initial delay**, because the normal staggered startup pass already owns the first boot period.
- Only after a worker was actually observed offline and then reachable again does the master wait **15 seconds**, run one `check.sh` over SSH, and inspect the supervisor.
- As a safety net for an unusually fast reboot that falls completely between two TCP probes, each worker also gets **one SSH supervisor audit about every 5 minutes**. Audits are staggered across the five-minute window instead of being fired at all workers together. With all 23 workers running this averages about one authenticated SSH audit every 13 seconds across the whole fleet; the 30-second TCP liveness polling remains unchanged.
- If `STATUS=running`, nothing is restarted — even when Streamlink itself is currently retrying.
- If the `/tmp` supervisor is gone and the desired state is `running`, the master sends the current `start.sh`, URL and optional YouTube cookie again.
- If the stream was intentionally stopped, a later Pi reboot leaves it stopped.

Desired stream state is persisted locally in `runtime/desired_streams.json`. A normal master/server startup intentionally sets every configured URL back to `running`, because server startup is defined as an installation-wide restart pass.

The browser dashboard's automatic SSH health refresh is **60 seconds** while the tab is visible. Manual checks remain immediate. When the dashboard is closed/hidden, those UI SSH checks do not run.

Optional environment overrides:

```text
STREAM_MASTER_RECOVERY_INTERVAL=30
STREAM_MASTER_RECOVERY_INITIAL_DELAY=30
STREAM_MASTER_RECOVERY_BOOT_SETTLE=15
STREAM_MASTER_RECOVERY_AUDIT_INTERVAL=300
STREAM_MASTER_POWERLOSS_RECOVERY=1
```

## Boot settle delay before Start All

The controller web UI starts immediately, but the automatic stream-start pass now waits **60 seconds by default** before sending the first Start request. After that, the existing 5-second staggering remains in place. This gives worker Pis time to finish booting, initialize networking/DRM and become stable before Streamlink/VLC are launched.

Override when installing the service, for example:

```bash
STREAM_MASTER_AUTOSTART_INITIAL_DELAY=90 ./install_service.sh
```

The initial settle delay does **not** consume the 15-minute retry window for late/offline Pis.

## Mandatory Git preflight on service start

Every `stream-master.service` start performs a blocking GitHub check **before** `master.py` is loaded. The default is three attempts with a 15-second fetch timeout and 5 seconds between attempts. If a newer fast-forward commit exists it is pulled and validated first. If GitHub remains unavailable, the service deliberately starts the last known-good local checkout so an Internet outage cannot disable the installation.
