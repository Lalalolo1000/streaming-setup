# Optionaler Fernzugriff über Tailscale

Tailscale ist **nicht** für den normalen Betrieb von Streaming Setup erforderlich. Es ist nur ein sinnvoller Notfall-/Fernwartungszugang zum Master.

## Installation

Im Projekt liegt:

```bash
./install_tailscale.sh
```

Alternativ gemäß Tailscale-Dokumentation:

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

Adresse prüfen:

```bash
tailscale ip
tailscale status
```

Dann kannst du aus deinem Tailnet die Oberfläche erreichen:

```text
http://TAILSCALE-IP:8080/
```

Der Webserver lauscht ohnehin auf `0.0.0.0:8080`; du musst dafür keinen Port am Router freigeben.

## SSH zum Master

Du kannst weiterhin den normalen OpenSSH-Server des Masters über die Tailscale-IP verwenden.

Optional bietet Tailscale auch Tailscale SSH:

```bash
sudo tailscale set --ssh
```

Das betrifft nur den Zugang **zum Master**. Die Verbindungen vom Master zu den 24 Raspberry Pis bleiben unverändert Passwort-only im lokalen `192.168.0.x`-Netz.

## Empfehlung

Kein Tailscale Funnel / keine öffentliche Freigabe der Adminoberfläche verwenden. Die Oberfläche enthält Neustart-, Shutdown- und Update-Funktionen und sollte nur im vertrauenswürdigen LAN oder Tailnet erreichbar sein.
