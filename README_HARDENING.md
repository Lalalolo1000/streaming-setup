# 24/7 hardening and device load

This repository is designed so the Raspberry Pi players spend almost all of their time doing one thing: decoding their configured stream. Management traffic is deliberately bounded.

## Background load

For workers `.102`–`.124`:

- TCP/22 reachability probe: every ~30 s, no SSH authentication and no remote command.
- Supervisor audit: about once every 5 min per configured/running node, staggered over the interval and using the small `scripts/probe.sh`.
- Browser state refresh: HTTP to the master only; it does **not** recursively SSH to every worker.
- Real `check.sh`: manual **Jetzt prüfen**, explicit follow-up checks, or maintenance diagnostics.

The master stream is audited locally. Its high-frequency runtime is under `/dev/shm/stream-master`.

## Startup

The web controller becomes available first. Automatic stream launch then waits 60 s, starts Stream 01, and starts the remaining nodes about 5 s apart. The recovery monitor is gated until that first staggered pass is complete, so it cannot bypass the boot-settle delay. Workers missed by the first pass are retried for up to 15 minutes.

## Unexpected worker reboot

The master notices a normal reboot through TCP reachability. After the worker returns it waits 15 s, checks the supervisor once, and restores the stream only if its desired state is `running`. A fast reboot that occurs completely between two TCP samples is still caught by the five-minute supervisor audit. Persistent authentication or remote-script errors do not trigger 30-second SSH loops; they fall back to the normal five-minute audit cadence.

## Stream/source failure

A worker's supervisor survives source/network/VLC failures and retries locally; the master does not repeatedly relaunch it. Normal outer retries use 30/60/90/120/150/180 s with jitter. Repeated batches of fast failures use progressively longer approximately 5/15/30/60 min cooldowns. Persistent YouTube login/bot challenges likewise back off from roughly 10 min toward 60 min. A stable run resets penalties.

Stream logs are bounded (`stream.log` ~1 MiB, current-attempt log ~256 KiB), and stable status is not rewritten every few seconds when nothing changed.

## Software update safety

Worker updates are sequential. OverlayFS is disabled only for the worker currently being updated. A fsynced `runtime/update_guard.json` records the dangerous window. If an error leaves a worker potentially writable and automatic relocking cannot be verified, Update All stops immediately; later workers **and the master's local package update** are skipped until recovery succeeds.

The master is different: it stays writable, stops Stream 01, updates VLC/Streamlink locally, and restores Stream 01. It never enters the worker OverlayFS/two-reboot updater.

Software maintenance preserves the persisted desired stream state: an intentionally stopped worker is updated but remains stopped afterward; running streams are restarted with the current URL/cookie payload.

Package maintenance checks for a plausible clock and, when systemd reports NTP explicitly unsynchronized, waits up to 60 seconds before refusing the repository update. `apt-get update` treats repository errors as fatal instead of silently continuing from stale indexes.

Git deployment, worker software updates and fleet power operations share `runtime/maintenance.lock`. A Git fast-forward therefore cannot modify scripts halfway through long maintenance. The worker updater inherits the locked file descriptor from the controller, so the lock remains held even if the controller process itself is stopped while `update_pis.py` is recovering a worker. Periodic Git checks first use `ls-remote`, and only fetch if the remote SHA changed. The Git updater also defers while the staggered startup/retry pass is still running, so a timer tick cannot restart the controller halfway through boot orchestration.

## Reboot / shutdown all

Reboot All handles workers in small batches. Every worker job in a batch must return successfully before the next batch proceeds. The master only reboots after every worker succeeded.

Shutdown All sends worker shutdowns first and requires every worker to be positively observed offline. If even one worker cannot be confirmed down, the master remains online so the problem can be inspected.

If the controller itself crashes during the worker stage of a fleet operation, it does not blindly continue to power off/reboot the master after restart. The fleet operation is marked interrupted and the master stays online.

## Persistent writes and SD-card wear

Workers can remain on OverlayFS/boot-RO, so stream logs and transient supervisors do not become normal persistent SD writes. On the writable master, high-churn stream state lives in RAM. Persistent writes are reserved for things that must survive a reboot: Git, `nodes.json`, desired state, update guards and operation state.

For the hardware image itself also verify:

```bash
swapon --show
findmnt -n -o SOURCE,FSTYPE,OPTIONS /
findmnt -T /dev/shm
```

Workers should have no SD-backed swap. The master should have a normal writable root; workers should report an OverlayFS root when locked.

## Configuration durability and backup

`nodes.json` is local and intentionally excluded from Git. Each successful config save keeps `nodes.json.bak`, and important config/job/guard writes are atomic; safety-critical state is fsynced.

Use:

```bash
./backup_local_state.sh /path/on/external/storage
```

to make an off-device backup archive of local configuration/runtime intent. Cookies and the shared SSH password are **not** included unless explicitly requested.

## Security boundary

The web controller has powerful reboot/shutdown/update actions and does not pretend the Admin warning is authentication. Keep port 8080 on a trusted installation LAN or private Tailnet; do not port-forward it to the public Internet and do not use Tailscale Funnel for it. If the surrounding network is not trusted, add firewall/ACL/application authentication before deployment.

## Recommended maintenance cadence

Do not run **Update all** on a fixed daily schedule merely to stay current. Package upgrades are among the most write-heavy operations in the installation. Update when there is a reason (Streamlink/source breakage, VLC fix, planned maintenance). For a new Streamlink release, the safest procedure is to update one non-critical worker first, observe it, and then run Update All.
