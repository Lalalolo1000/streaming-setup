#!/usr/bin/env bash
set -eu

echo 'Shutdown requested.'
sudo -n systemctl poweroff --no-block
