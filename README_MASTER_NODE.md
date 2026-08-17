# Master node (.101)

The machine at `192.168.0.101` has two jobs:

1. it runs the Streaming Setup web controller;
2. it can also run Stream 01 like the other Raspberry Pis.

Because Git, `nodes.json` and runtime state must survive reboots, the master should **not** use the read-only root OverlayFS used on the worker Pis.

## One-time preparation

If `/` is currently `overlay`:

```bash
./prepare_master_writable.sh
sudo reboot
```

Then:

```bash
findmnt -n -o FSTYPE /
```

Expected: a normal filesystem such as `ext4`, not `overlay`.

The boot partition may remain read-only; Git and the controller only require the root filesystem to be persistent/writable.

## Why self-SSH is avoided

The master is explicitly recognized as the local machine. `start.sh`, `check.sh`, `logs.sh` and `kill.sh` are executed locally for it. This avoids password SSH loops, self-SSH failures, and the ambiguity of waiting for the controller's own SSH port during a reboot.

## Fleet power sequence

### Reboot all

```text
worker nodes reboot
        ↓
wait until worker jobs finish
        ↓
master (.101) reboots last
        ↓
stream-master.service starts again
        ↓
master reboot job completes
```

### Shutdown all

```text
worker nodes power off
        ↓
wait until shutdown jobs finish
        ↓
master (.101) powers off last
```

On the next physical power-on, the once-per-boot startup pass starts every configured stream again.

## Updates

`Update all` treats the master differently:

- Workers: OverlayFS off → reboot → VLC/Streamlink → OverlayFS on → reboot → verify.
- Master: local VLC/Streamlink update only; no OverlayFS changes and no reboot.

Git code updates are separate and continue to be handled by `git_update.sh` / the systemd timer.
