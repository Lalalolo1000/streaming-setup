#!/usr/bin/env bash
set -eu

echo 'Reboot requested.'
sudo -n systemctl reboot --no-block
