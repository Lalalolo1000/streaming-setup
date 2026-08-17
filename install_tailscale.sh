#!/usr/bin/env bash
# Optional remote access for the MASTER only. Not required for normal LAN operation.
set -euo pipefail
cat <<'MSG'
Tailscale wird optional auf dem Streaming Setup-Master installiert.
Danach kannst du UI und SSH über dein Tailnet erreichen, ohne Ports am Router zu öffnen.
MSG
read -r -p 'Tailscale jetzt mit dem offiziellen Installationsskript installieren? [y/N] ' ANS
case "$ANS" in y|Y|yes|YES|j|J|ja|JA) ;; *) exit 0;; esac
command -v curl >/dev/null 2>&1 || { sudo apt-get update; sudo apt-get install -y curl; }
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
cat <<'MSG'

Danach:
  tailscale ip
  tailscale status

UI: http://TAILSCALE-IP:8080/
Optional Tailscale SSH: sudo tailscale set --ssh
MSG
