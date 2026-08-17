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

On the next physical power-on, the master service starts every configured stream again after its boot-settle delay. Any later master-service restart deliberately performs the same staggered Start/Restart pass.

## Updates

`Update all` treats the master differently:

- Workers: OverlayFS off → reboot → VLC/Streamlink → OverlayFS on → reboot → verify. If protection cannot be restored, the fleet update stops immediately and keeps the recovery guard.
- Master: stop Stream 01 → locally update VLC/Streamlink → restore Stream 01; no OverlayFS changes and no reboot.
- Update maintenance preserves the persisted desired running/stopped state; intentionally stopped streams are not started just because packages were updated.

Git code updates are separate and continue to be handled by `git_update.sh` / the systemd timer.


## Runtime wear on the writable master

The high-churn Stream 01 runtime (`stream.log`, `status.env`, PID files) is placed under `/dev/shm/stream-master` so normal stream supervision does not continuously write those files to the master's persistent root filesystem. Persistent configuration, Git checkout and recovery/job state remain on disk because they must survive reboots.
