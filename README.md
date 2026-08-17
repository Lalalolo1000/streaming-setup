# Streaming Setup

Minimaler Master-Controller für 24 Raspberry-Pi-Streamanzeigen.

## Standardnetz

Die mitgelieferte Default-Konfiguration enthält:

```text
Stream 01 → pi@192.168.0.101
Stream 02 → pi@192.168.0.102
...
Stream 24 → pi@192.168.0.124
```

Die Stream-URLs sind zunächst leer und werden über die Weboberfläche eingetragen.

Wenn du eine bestehende Installation mit einer lokalen `nodes.json` aus dem alten `.201-.224`-Bereich übernimmst, einmal ausführen:

```bash
./migrate_ips_201_to_101.sh
```

## Wichtige Trennung: Code vs. lokale Konfiguration

Im Git-Repository liegt:

```text
nodes.default.json
```

Beim ersten Installieren/Starten entsteht daraus lokal:

```text
nodes.json
```

`nodes.json` ist in `.gitignore`, liegt **nicht im Repository** und wird von automatischen GitHub-Codeupdates weder gepusht, gemergt, ersetzt noch gelöscht. Dort bleiben deine Namen, IPs und Streamlinks ausschließlich lokal erhalten. Der Updater verweigert sogar einen Remote-Commit, der versehentlich eine `nodes.json` enthält.

## Installation auf dem Master

```bash
./install_master.sh
./install_service.sh
```

Installiert werden die Master-/Stream-Grundabhängigkeiten:

```text
python3
openssh-client
sshpass
git
util-linux
pipx + Streamlink
VLC
```

Der Master ist zugleich Stream 01 und bekommt deshalb VLC/Streamlink lokal mitinstalliert. Es gibt weiterhin keine Flask-/Node-/npm-/Datenbank-Abhängigkeit. Für unbeaufsichtigte lokale Reboot-/Shutdown-/Update-Aktionen prüft der Installer außerdem, dass `sudo -n` für den dedizierten Master-Benutzer funktioniert.

Weboberfläche:

```text
http://MASTER-IP:8080/
```

## SSH zu den 24 Pis

Der Controller verwendet bewusst nur das gemeinsame Passwort aus:

```text
~/.config/stream-master/ssh-password
```

Für diese lokalen Pi-Verbindungen werden Hostkeys nicht gespeichert/geprüft und Public-Key-Login ist deaktiviert. Dadurch verursachen geklonte Pis bei wiederverwendeten IPs keine `REMOTE HOST IDENTIFICATION HAS CHANGED`-Fehler.

Manuell mit denselben Regeln:

```bash
./ssh_pi.sh 192.168.0.101
```

## Täglicher Betrieb

Bei **jedem Start des Master-Servers** wird für jeden Pi mit eingetragener URL automatisch `start.sh` gestartet. Die Starts werden absichtlich verteilt: Master zuerst, danach jeweils etwa **5 Sekunden Abstand** zwischen den Nodes. Noch bootende Pis blockieren den ersten Durchlauf nicht und werden danach erneut versucht. Dadurch reicht es im Normalfall, die gesamte Installation morgens einzuschalten.

Auch ein GitHub-bedingter Neustart des Master-Dienstes startet/restartet alle konfigurierten Streams einmal.

## Automatische GitHub-Codeupdates

Wenn der Projektordner ein Git-Clone ist:

- beim Boot: kurze GitHub-Codeprüfung vor dem Serverstart
- danach: standardmäßig alle 5 Minuten prüfen
- nur Fast-Forward
- lokale Codeänderungen niemals überschreiben
- neuen Commit vor dem Neustart syntaxprüfen
- bei fehlerhaftem Commit automatisch auf den alten Commit zurückrollen
- GitHub-Ausfall verhindert den lokalen Serverstart nicht

Siehe **README_GITHUB_UPDATES.md**.

## Optional Tailscale

Für Fernwartung nur des Masters siehe **README_REMOTE_ACCESS.md**.

## Weitere Dateien

- `README_AUTOSTART.md` – systemd, täglicher Start, Crash-Recovery
- `README_GITHUB_UPDATES.md` – GitHub-Workflow von zuhause
- `README_REMOTE_ACCESS.md` – optional Tailscale
- `README_STREAMING_SETUP.md` – UI/Projektbesonderheiten
- `README_HARDENING.md` – 24/7-Schutz, Last, Recovery und Betriebsgrenzen

## Master is also Stream 01 (.101)

`192.168.0.101` is special: it runs this controller **and** can run one of the 24 streams.
The default config marks it with `"role": "master"`. Existing local `nodes.json` files
are also recognized by the `.101` address, so Git never has to overwrite local config.

The master intentionally stays writable. Worker Pis may remain protected by OverlayFS.
If the master still uses OverlayFS, run once:

```bash
./prepare_master_writable.sh
sudo reboot
```

After reboot, verify:

```bash
findmnt -n -o FSTYPE /
```

It must not print `overlay`.

### Operations involving the master

- Start / Stop / Check / Logs for Stream 01 run **locally**. The controller never SSHes into itself for these actions.
- Reboot Stream 01 reboots the master locally. The reboot job is persisted; after boot the controller finishes the job and starts/checks the stream again.
- `Alle neu starten`: all worker Pis are rebooted and allowed to come back first; the master reboots **last**.
- `Alle herunterfahren`: all worker Pis are shut down first; the master powers off **last**.
- `Alle aktualisieren`: worker Pis use the existing OverlayFS-safe two-reboot updater. The master is never sent through that updater. Instead its VLC + Streamlink packages are updated locally without rebooting or toggling OverlayFS.
- The master's per-node update button runs the local VLC + Streamlink updater. The worker OverlayFS updater itself still rejects the master intentionally.

The controller recognizes only one master. Do not mark a second node as `role=master`.


## Stream-Verhalten beim Serverstart

Bei **jedem Start des Streaming-Setup-Servers** werden alle Nodes mit konfigurierter URL einmal neu gestartet: `.101` zuerst, danach die übrigen Nodes mit standardmäßig 5 Sekunden Abstand. Noch bootende Pis werden bis zu 15 Minuten in späteren, ebenfalls gedrosselten Runden erneut versucht. Danach übernimmt auf jedem Pi der lokale Stream-Supervisor.

Der Supervisor behandelt vorübergehende Streamlink-Fehler innerhalb eines laufenden Versuchs nicht sofort als endgültigen Quellenfehler. Normale äußere Neustartabstände sind **30, 60, 90, 120, 150 und maximal 180 Sekunden**, jeweils mit etwas Zufalls-Jitter. Nach zehn schnellen Fehlern folgt zunächst eine ca. 5-minütige Pause; bei dauerhaft toten Quellen steigt diese Schonzeit stufenweise bis maximal etwa 60 Minuten und wird nach einem stabilen Lauf wieder zurückgesetzt.


## Optional YouTube cookies

If YouTube returns `LOGIN_REQUIRED` / `Sign in to confirm you’re not a bot`, you can place a Netscape-format cookie export directly in the repository folder on the master:

```text
youtube-cookies.txt
```

Then protect it locally:

```bash
chmod 600 youtube-cookies.txt
```

The file is intentionally listed in `.gitignore` and the automatic Git updater also refuses any local or remote commit which tracks it. **Do not commit this file to the public repository.**

Whenever a YouTube stream is started/restarted, the master sends the cookie file in the same SSH stdin payload as `start.sh` and writes it to the worker only as:

```text
/tmp/stream-master/youtube-cookies.txt
```

Workers therefore do not need a permanent secret on their OverlayFS root. The cookie is used only for YouTube URLs. If the local cookie file is removed, the next Start/Restart also deletes the temporary worker copy.

YouTube `LOGIN_REQUIRED` is treated specially: Streamlink gets only one additional internal retry after about 30 seconds. If the bot/login challenge persists, the outer watcher starts with about 10 minutes of cooldown and increases repeated challenge cooldowns up to about one hour. Normal network/HLS/VLC errors use 30, 60, 90, 120, 150 and 180 second retries with jitter. Repeated batches of rapid failures use progressively longer 5/15/30/60-minute cooldowns. A stable run resets those penalties.

## Reliable Stop / Restart

Each stream generation now runs in its own process group. Stop/Restart first terminates that complete group, which includes the supervisor, Streamlink, VLC and helper children such as ffmpeg/mux processes. A `/proc` scan remains as compatibility cleanup for older generations. This avoids YouTube-specific child processes surviving a Stop action.

## Conservative boot startup

At controller startup the sequence is now: mandatory bounded GitHub preflight → web controller starts → 60-second worker boot-settle delay → Stream 01, then the remaining configured streams at 5-second intervals. The delay can be changed with `STREAM_MASTER_AUTOSTART_INITIAL_DELAY` when running `install_service.sh`.
