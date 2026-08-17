#!/usr/bin/env bash
set -euo pipefail

ROOT_TYPE="$(findmnt -n -o FSTYPE / 2>/dev/null || echo unknown)"
if [ "$ROOT_TYPE" != "overlay" ]; then
    echo "Master root is already writable (FSTYPE=$ROOT_TYPE). Nothing to do."
    exit 0
fi

if ! command -v raspi-config >/dev/null 2>&1; then
    echo 'ERROR: raspi-config is not installed; cannot disable Raspberry Pi OverlayFS automatically.' >&2
    exit 1
fi

cat <<'MSG'
The controller master must stay writable because its Git checkout, runtime state,
and local configuration must persist across reboots.

This disables OverlayFS for the MASTER ONLY. The boot partition read-only setting
is intentionally left unchanged. Worker Pis can remain OverlayFS-protected.
MSG

sudo -n true
sudo -n raspi-config nonint disable_overlayfs
sudo -n sync

echo
echo 'OverlayFS has been configured OFF for the master.'
echo 'Reboot the master now, then verify:'
echo '  findmnt -n -o FSTYPE /'
echo 'It must NOT print: overlay'
echo
echo 'Reboot now with:'
echo '  sudo reboot'
