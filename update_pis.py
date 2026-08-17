#!/usr/bin/env python3
"""
Update ONLY VLC + Streamlink on Raspberry Pi display nodes protected by OverlayFS.

Run this script on the MASTER/LAPTOP, not on the Pi.

Per-node workflow:
  1. Stop the current stream.
  2. Disable OverlayFS and reboot.
  3. Verify the real root filesystem is writable.
  4. apt-get update + apt-get install --only-upgrade vlc.
  5. pipx upgrade streamlink as the normal Pi user.
  6. Persist last-update timestamp and versions.
  7. Re-enable OverlayFS and reboot.
  8. Verify OverlayFS is active, boot is still read-only, and versions work.
  9. Optionally restart the stream.

The boot read-only setting is NEVER changed by this updater.
There is NO apt full-upgrade.

Requires:
  - nodes.json next to this file
  - scripts/kill.sh and, for --start-after, scripts/start.sh
  - password-only SSH via ~/.config/stream-master/ssh-password + sshpass; host keys ignored
  - passwordless sudo on each Pi ("sudo -n true" must work)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import selectors
import signal
import shlex
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
NODES_FILE = APP_DIR / "nodes.json"
NODES_DEFAULT_FILE = APP_DIR / "nodes.default.json"
SCRIPTS_DIR = APP_DIR / "scripts"
SSH_PASSWORD_FILE = Path.home() / ".config" / "stream-master" / "ssh-password"
RUNTIME_DIR = APP_DIR / "runtime"
GUARD_FILE = RUNTIME_DIR / "update_guard.json"

SSH_CONNECT_TIMEOUT = 6
REBOOT_DOWN_TIMEOUT = 60
REBOOT_UP_TIMEOUT = 300


class UpdateError(RuntimeError):
    pass


def write_guard(node: dict, *, stage: str, maintenance_open: bool, start_after: bool, note: str = "") -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    value = {
        "name": node_name(node),
        "target": str(node.get("target", "")),
        "node": node,
        "stage": stage,
        "maintenance_open": bool(maintenance_open),
        "start_after": bool(start_after),
        "note": note,
        "updated_at": time.time(),
    }
    tmp = GUARD_FILE.with_name(GUARD_FILE.name + ".tmp")
    tmp.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    tmp.replace(GUARD_FILE)


def clear_guard() -> None:
    try:
        GUARD_FILE.unlink()
    except FileNotFoundError:
        pass


def read_guard() -> dict:
    try:
        value = json.loads(GUARD_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise UpdateError("No interrupted-update recovery guard exists") from exc
    if not isinstance(value, dict) or not isinstance(value.get("node"), dict):
        raise UpdateError("Recovery guard is invalid")
    return value


def _interrupt(signum, frame):
    raise UpdateError(f"updater interrupted by signal {signum}; entering recovery")


signal.signal(signal.SIGTERM, _interrupt)
signal.signal(signal.SIGINT, _interrupt)


def read_nodes() -> list[dict]:
    if not NODES_FILE.exists() and NODES_DEFAULT_FILE.is_file():
        value = json.loads(NODES_DEFAULT_FILE.read_text(encoding="utf-8"))
        if not isinstance(value, list):
            raise UpdateError("nodes.default.json must contain a JSON list")
        tmp = NODES_FILE.with_name(NODES_FILE.name + ".tmp")
        tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        tmp.replace(NODES_FILE)
    value = json.loads(NODES_FILE.read_text(encoding="utf-8"))
    if not isinstance(value, list):
        raise UpdateError("nodes.json must contain a JSON list")
    return value


def node_name(node: dict) -> str:
    return str(node.get("name") or node.get("target") or "node")


def ssh_base(node: dict) -> list[str]:
    """Use the shared password only and deliberately ignore SSH host keys."""
    target = str(node.get("target", ""))
    if not re.fullmatch(r"[A-Za-z0-9_.-]+@[A-Za-z0-9_.-]+", target):
        raise UpdateError(f"Invalid SSH target: {target!r}")
    if not SSH_PASSWORD_FILE.is_file():
        raise UpdateError(f"Required SSH password file is missing: {SSH_PASSWORD_FILE}")
    sshpass = shutil.which("sshpass")
    if not sshpass:
        raise UpdateError("sshpass is required for password-only SSH. Install it with: sudo apt install sshpass")
    port = int(node.get("port", 22))
    return [
        sshpass, "-f", str(SSH_PASSWORD_FILE),
        "ssh", "-p", str(port),
        "-o", f"ConnectTimeout={SSH_CONNECT_TIMEOUT}",
        "-o", "ServerAliveInterval=10",
        "-o", "ServerAliveCountMax=2",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "GlobalKnownHostsFile=/dev/null",
        "-o", "UpdateHostKeys=no",
        "-o", "PreferredAuthentications=password",
        "-o", "PubkeyAuthentication=no",
        "-o", "PasswordAuthentication=yes",
        "-o", "KbdInteractiveAuthentication=no",
        target,
    ]


def host_port(node: dict) -> tuple[str, int]:
    target = str(node["target"])
    return target.rsplit("@", 1)[1], int(node.get("port", 22))


def tcp_up(node: dict, timeout: float = 2.0) -> bool:
    host, port = host_port(node)
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def run_ssh(
    node: dict,
    command: str,
    *,
    timeout: int = 60,
    check: bool = True,
    live: bool = False,
    quiet: bool = False,
) -> tuple[int, str]:
    name = node_name(node)
    args = ssh_base(node) + [command]

    if not quiet:
        print(f"\n[{name}] $ {command}", flush=True)

    if live:
        # Use non-blocking reads so the timeout still fires if SSH/apt/pipx
        # stops producing output. A blocking readline() could otherwise leave
        # the updater, and therefore the web UI, stuck forever.
        proc = subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
        )
        assert proc.stdout is not None
        sel = selectors.DefaultSelector()
        sel.register(proc.stdout, selectors.EVENT_READ)
        started = time.monotonic()
        chunks: list[str] = []
        pending = ""

        try:
            while True:
                if time.monotonic() - started > timeout:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
                    raise UpdateError(f"{name}: command timed out after {timeout}s")

                events = sel.select(timeout=0.5)
                for key, _ in events:
                    data = os.read(key.fileobj.fileno(), 4096)
                    if not data:
                        continue
                    text = data.decode("utf-8", errors="replace")
                    chunks.append(text)
                    pending += text
                    while "\n" in pending:
                        line, pending = pending.split("\n", 1)
                        if not quiet:
                            print(f"[{name}] {line}", flush=True)

                if proc.poll() is not None:
                    # Drain any final buffered output.
                    try:
                        while True:
                            data = os.read(proc.stdout.fileno(), 4096)
                            if not data:
                                break
                            text = data.decode("utf-8", errors="replace")
                            chunks.append(text)
                            pending += text
                    except OSError:
                        pass
                    break

            if pending and not quiet:
                print(f"[{name}] {pending}", flush=True)
            code = proc.wait()
            output = "".join(chunks).strip()
        except BaseException:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
            raise
        finally:
            sel.close()
    else:
        try:
            done = subprocess.run(
                args,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise UpdateError(f"{name}: command timed out after {timeout}s") from exc

        code = done.returncode
        output = done.stdout.strip()
        if output and not quiet:
            print(f"[{name}] {output}", flush=True)

    if check and code != 0:
        raise UpdateError(
            f"{name}: remote command failed with rc={code}"
            + (f"\n{output}" if output else "")
        )

    return code, output


def run_local_script(
    node: dict,
    script_path: Path,
    remote_args: list[str] | None = None,
) -> tuple[int, str]:
    remote_args = remote_args or []
    if not script_path.is_file():
        raise UpdateError(f"Missing local script: {script_path}")

    remote = "bash -s --"
    if remote_args:
        remote += " " + " ".join(shlex.quote(x) for x in remote_args)

    name = node_name(node)
    print(f"\n[{name}] sending {script_path.name}", flush=True)

    done = subprocess.run(
        ssh_base(node) + [remote],
        input=script_path.read_bytes(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=60,
        check=False,
    )
    output = done.stdout.decode("utf-8", errors="replace").strip()
    if output:
        print(f"[{name}] {output}", flush=True)
    return done.returncode, output


def wait_for_ssh(node: dict, online: bool, timeout: int) -> None:
    name = node_name(node)
    wanted = "online" if online else "offline"
    deadline = time.monotonic() + timeout
    auth_failures = 0

    print(f"[{name}] waiting for SSH to go {wanted}...", flush=True)

    while time.monotonic() < deadline:
        up = tcp_up(node)
        if up == online:
            if online:
                code, output = run_ssh(node, "true", timeout=10, check=False, quiet=True)
                if code == 0:
                    print(f"[{name}] SSH is online and authenticated.", flush=True)
                    return

                if "Permission denied" in output:
                    auth_failures += 1
                    if auth_failures >= 3:
                        raise UpdateError(
                            f"{name}: Pi is online, but SSH authentication failed. "
                            f"Check the SSH key or password in {SSH_PASSWORD_FILE}."
                        )
                else:
                    auth_failures = 0
            else:
                print(f"[{name}] SSH is offline.", flush=True)
                return
        time.sleep(2)

    raise UpdateError(f"{name}: SSH did not become {wanted} within {timeout}s")


def reboot_and_wait(node: dict) -> None:
    name = node_name(node)
    run_ssh(node, "sudo -n sync; sudo -n reboot", timeout=15, check=False)
    time.sleep(2)

    try:
        wait_for_ssh(node, online=False, timeout=REBOOT_DOWN_TIMEOUT)
    except UpdateError:
        print(
            f"[{name}] warning: did not observe SSH go offline; continuing to wait for boot.",
            flush=True,
        )

    wait_for_ssh(node, online=True, timeout=REBOOT_UP_TIMEOUT)
    time.sleep(2)


def preflight(node: dict) -> None:
    run_ssh(
        node,
        r'''set -eu
printf 'host='; hostname
printf 'os='; . /etc/os-release; printf '%s %s\n' "$PRETTY_NAME" "$(dpkg --print-architecture)"
printf 'sudo='; sudo -n true && echo yes
printf 'raspi-config='; command -v raspi-config
printf 'pipx='; command -v pipx || { [ -x /usr/bin/pipx ] && echo /usr/bin/pipx; } || true
printf 'streamlink='; command -v streamlink || { [ -x "$HOME/.local/bin/streamlink" ] && echo "$HOME/.local/bin/streamlink"; } || true
printf 'cvlc='; command -v cvlc || true
printf 'overlay='; if grep -qw boot=overlay /proc/cmdline; then echo yes; else echo no; fi
printf 'root='; findmnt -n -o SOURCE,FSTYPE,OPTIONS /
printf 'boot=';
if findmnt -n /boot/firmware >/dev/null 2>&1; then
    findmnt -n -o SOURCE,FSTYPE,OPTIONS /boot/firmware
elif findmnt -n /boot >/dev/null 2>&1; then
    findmnt -n -o SOURCE,FSTYPE,OPTIONS /boot
else
    echo 'not separately mounted'
fi
''',
        timeout=30,
    )


def stop_stream(node: dict) -> None:
    kill_script = SCRIPTS_DIR / "kill.sh"
    if not kill_script.is_file():
        print(f"[{node_name(node)}] no scripts/kill.sh; skipping stream stop.", flush=True)
        return

    code, _ = run_local_script(node, kill_script)
    if code != 0:
        print(
            f"[{node_name(node)}] warning: kill.sh returned rc={code}; continuing.",
            flush=True,
        )


def disable_overlay_and_reboot(node: dict) -> None:
    run_ssh(
        node,
        r'''set -eu
sudo -n raspi-config nonint disable_overlayfs
sudo -n sync
echo 'OverlayFS configured OFF; reboot required.'
''',
        timeout=120,
    )
    reboot_and_wait(node)


def verify_root_writable(node: dict) -> None:
    """Verify updates will hit the real SD filesystem, not a temporary overlay."""
    run_ssh(
        node,
        r'''set -eu
if grep -qw boot=overlay /proc/cmdline; then
    echo 'ERROR: boot=overlay is still present after reboot.' >&2
    exit 20
fi

ROOT_TYPE="$(findmnt -n -o FSTYPE /)"
if [ "$ROOT_TYPE" = overlay ]; then
    echo 'ERROR: root filesystem is still OverlayFS.' >&2
    exit 21
fi

TEST=/var/lib/.stream-master-write-test
printf '%s\n' "$(date +%s)" | sudo -n tee "$TEST" >/dev/null
sudo -n sync
sudo -n rm -f "$TEST"

echo "Root filesystem is writable: $(findmnt -n -o SOURCE,FSTYPE,OPTIONS /)"
echo 'Boot read-only setting is left untouched.'
''',
        timeout=30,
    )


def update_vlc_and_streamlink(node: dict) -> None:
    run_ssh(
        node,
        r'''set -eu
export DEBIAN_FRONTEND=noninteractive
export LC_ALL=C

echo '===== apt update ====='
sudo -n apt-get \
    -o Acquire::Retries=3 \
    -o Acquire::http::Timeout=30 \
    -o Acquire::https::Timeout=30 \
    update

echo '===== VLC only-upgrade ====='
sudo -n apt-get \
    -y \
    -o Acquire::Retries=3 \
    -o Acquire::http::Timeout=30 \
    -o Acquire::https::Timeout=30 \
    -o Dpkg::Options::=--force-confold \
    install --only-upgrade vlc

echo '===== pipx / Streamlink ====='
PIPX="$(command -v pipx 2>/dev/null || true)"
if [ -z "$PIPX" ] && [ -x /usr/bin/pipx ]; then
    PIPX=/usr/bin/pipx
fi
if [ -z "$PIPX" ]; then
    echo 'ERROR: pipx is not installed.' >&2
    exit 30
fi

# Streamlink belongs to the normal Pi user's pipx environment. Do NOT sudo this.
# Give pipx its own hard ceiling so a broken network cannot hang maintenance forever.
if command -v timeout >/dev/null 2>&1; then
    timeout --signal=TERM --kill-after=30s 900 "$PIPX" upgrade streamlink
else
    "$PIPX" upgrade streamlink
fi

echo '===== versions after update ====='
STREAMLINK="$(command -v streamlink 2>/dev/null || true)"
if [ -z "$STREAMLINK" ] && [ -x "$HOME/.local/bin/streamlink" ]; then
    STREAMLINK="$HOME/.local/bin/streamlink"
fi
[ -n "$STREAMLINK" ] || {
    echo 'ERROR: streamlink executable not found after upgrade.' >&2
    exit 31
}

STREAMLINK_VERSION="$("$STREAMLINK" --version 2>/dev/null | head -n 1)"
VLC_VERSION="$(dpkg-query -W -f='${Version}' vlc 2>/dev/null || true)"
[ -n "$VLC_VERSION" ] || VLC_VERSION="$(cvlc --version 2>&1 | head -n 1 || true)"
OS_NAME="$(. /etc/os-release; printf '%s' "$PRETTY_NAME")"
UPDATED_UTC="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"

echo "$STREAMLINK_VERSION"
echo "vlc package $VLC_VERSION"

echo '===== persist update metadata ====='
sudo -n mkdir -p /var/lib/stream-master
{
    printf 'LAST_UPDATE_UTC=%s\n' "$UPDATED_UTC"
    printf 'STREAMLINK_VERSION=%s\n' "$STREAMLINK_VERSION"
    printf 'VLC_VERSION=%s\n' "$VLC_VERSION"
    printf 'OS=%s\n' "$OS_NAME"
    printf 'UPDATE_KIND=vlc+streamlink\n'
} | sudo -n tee /var/lib/stream-master/update-info >/dev/null
sudo -n chmod 0644 /var/lib/stream-master/update-info
sudo -n sync

echo "last_update_utc=$UPDATED_UTC"
''',
        timeout=1200,
        live=True,
    )


def enable_overlay_and_reboot(node: dict) -> None:
    run_ssh(
        node,
        r'''set -eu
sudo -n raspi-config nonint enable_overlayfs
sudo -n sync
echo 'OverlayFS configured ON; reboot required.'
''',
        timeout=180,
        live=True,
    )
    reboot_and_wait(node)


def verify_locked(node: dict) -> None:
    """Verify OverlayFS is active again and boot stayed read-only."""
    run_ssh(
        node,
        r'''set -eu
FAIL=0

echo '===== final state ====='

# On current Raspberry Pi OS/Trixie, /proc/cmdline is not a reliable
# indicator for raspi-config OverlayFS. The mounted root filesystem is.
ROOT_SOURCE="$(findmnt -n -o SOURCE / 2>/dev/null || echo unknown)"
ROOT_TYPE="$(findmnt -n -o FSTYPE / 2>/dev/null || echo unknown)"
echo "root_source=$ROOT_SOURCE"
echo "root_fstype=$ROOT_TYPE"
if [ "$ROOT_TYPE" = overlay ]; then
    echo 'overlay=enabled'
else
    echo 'overlay=DISABLED'
    echo 'ERROR: root filesystem is not OverlayFS.'
    FAIL=1
fi

BOOT=''
if findmnt -n /boot/firmware >/dev/null 2>&1; then
    BOOT=/boot/firmware
elif findmnt -n /boot >/dev/null 2>&1; then
    BOOT=/boot
fi

if [ -n "$BOOT" ]; then
    OPTS="$(findmnt -n -o OPTIONS "$BOOT")"
    echo "boot=$(findmnt -n -o SOURCE,FSTYPE,OPTIONS "$BOOT")"
    case ",$OPTS," in
        *,ro,*) ;;
        *)
            echo "ERROR: $BOOT is not read-only. This updater did not change bootro."
            FAIL=1
            ;;
    esac
else
    echo 'boot=not separately mounted'
fi

STREAMLINK="$(command -v streamlink 2>/dev/null || true)"
if [ -z "$STREAMLINK" ] && [ -x "$HOME/.local/bin/streamlink" ]; then
    STREAMLINK="$HOME/.local/bin/streamlink"
fi

printf 'streamlink='
if [ -n "$STREAMLINK" ]; then
    "$STREAMLINK" --version
else
    echo MISSING
    FAIL=1
fi

printf 'vlc='
if command -v cvlc >/dev/null 2>&1; then
    cvlc --version 2>&1 | head -n 1
else
    echo MISSING
    FAIL=1
fi

printf 'last_update_utc='
if [ -r /var/lib/stream-master/update-info ]; then
    VALUE="$(grep '^LAST_UPDATE_UTC=' /var/lib/stream-master/update-info 2>/dev/null | tail -n 1 || true)"
    [ -n "$VALUE" ] && echo "${VALUE#LAST_UPDATE_UTC=}" || echo unknown
else
    echo unknown
fi

exit "$FAIL"
''',
        timeout=60,
    )


def start_stream(node: dict) -> None:
    if not str(node.get("url", "")).strip():
        print(f"[{node_name(node)}] no stream URL configured; leaving stream stopped.", flush=True)
        return
    start_script = SCRIPTS_DIR / "start.sh"
    if not start_script.is_file():
        print(f"[{node_name(node)}] no scripts/start.sh; not restarting stream.", flush=True)
        return

    args = [
        str(node.get("url", "")),
        str(node.get("quality", "max480")),
        str(node.get("connector", "HDMI-A-1")),
    ]
    code, output = run_local_script(node, start_script, args)
    if code != 0:
        raise UpdateError(
            f"{node_name(node)}: start.sh failed with rc={code}"
            + (f"\n{output}" if output else "")
        )


def update_node(node: dict, *, start_after: bool) -> None:
    name = node_name(node)
    print("\n" + "=" * 72, flush=True)
    print(f"UPDATING {name} ({node.get('target')})", flush=True)
    print("=" * 72, flush=True)

    preflight(node)
    stop_stream(node)

    maintenance_open = False
    original_error: Exception | None = None

    try:
        print(f"\n[{name}] STEP 1/5: disable OverlayFS and reboot", flush=True)
        maintenance_open = True
        write_guard(node, stage="disabling OverlayFS", maintenance_open=True, start_after=start_after)
        disable_overlay_and_reboot(node)

        print(f"\n[{name}] STEP 2/5: update VLC and Streamlink", flush=True)
        write_guard(node, stage="root writable; updating packages", maintenance_open=True, start_after=start_after)
        verify_root_writable(node)
        update_vlc_and_streamlink(node)

        print(f"\n[{name}] STEP 3/5: enable OverlayFS and reboot", flush=True)
        write_guard(node, stage="re-enabling OverlayFS", maintenance_open=True, start_after=start_after)
        enable_overlay_and_reboot(node)

        print(f"\n[{name}] STEP 4/5: verify locked state", flush=True)
        verify_locked(node)
        maintenance_open = False
        write_guard(node, stage="lock verified", maintenance_open=False, start_after=start_after)

        print(f"\n[{name}] STEP 5/5: {'restart stream' if start_after else 'leave stream stopped'}", flush=True)
        if start_after:
            start_stream(node)

        clear_guard()
        print(f"\n[{name}] UPDATE COMPLETE", flush=True)
        return

    except Exception as exc:
        original_error = exc
        write_guard(node, stage="update failed; recovery pending", maintenance_open=maintenance_open, start_after=start_after, note=str(exc))
        print(f"\n[{name}] UPDATE ERROR: {exc}", file=sys.stderr, flush=True)

    recovery_errors: list[str] = []
    if maintenance_open:
        print(f"\n[{name}] RECOVERY: attempting to re-enable OverlayFS and reboot", flush=True)
        try:
            write_guard(node, stage="automatic recovery: re-enabling OverlayFS", maintenance_open=True, start_after=start_after, note=str(original_error or ""))
            enable_overlay_and_reboot(node)
            verify_locked(node)
            maintenance_open = False
            write_guard(node, stage="automatic recovery: lock verified", maintenance_open=False, start_after=start_after, note=str(original_error or ""))
            print(f"[{name}] RECOVERY: OverlayFS restored and verified.", flush=True)
        except Exception as exc:
            recovery_errors.append(f"relock failed: {exc}")
            write_guard(node, stage="automatic recovery failed", maintenance_open=True, start_after=start_after, note=str(exc))
            print(f"[{name}] RECOVERY ERROR: {exc}", file=sys.stderr, flush=True)

    if start_after and not maintenance_open:
        print(f"[{name}] RECOVERY: attempting to restart stream", flush=True)
        try:
            start_stream(node)
            print(f"[{name}] RECOVERY: stream restart command succeeded.", flush=True)
        except Exception as exc:
            recovery_errors.append(f"stream restart failed: {exc}")
            write_guard(node, stage="filesystem protected; stream restart failed", maintenance_open=False, start_after=start_after, note=str(exc))
            print(f"[{name}] RECOVERY ERROR: {exc}", file=sys.stderr, flush=True)

    if not maintenance_open and not recovery_errors:
        clear_guard()

    detail = str(original_error) if original_error is not None else "unknown update failure"
    if recovery_errors:
        detail += " | recovery: " + " | ".join(recovery_errors)
    raise UpdateError(detail)


def recover_guard() -> None:
    guard = read_guard()
    node = guard["node"]
    start_after = bool(guard.get("start_after", True))
    name = node_name(node)
    print("\n" + "=" * 72, flush=True)
    print(f"RECOVERING {name} ({node.get('target')})", flush=True)
    print("=" * 72, flush=True)
    write_guard(node, stage="recovery checking node", maintenance_open=bool(guard.get("maintenance_open", True)), start_after=start_after, note=str(guard.get("note", "")))

    code, output = run_ssh(node, "findmnt -n -o FSTYPE /", timeout=20, check=False, quiet=True)
    if code != 0:
        raise UpdateError(f"{name}: cannot reach node for recovery: {output}")
    root_type = output.strip().splitlines()[-1] if output.strip() else "unknown"

    if root_type != "overlay":
        print(f"[{name}] RECOVERY: root is {root_type}; re-enabling OverlayFS and rebooting", flush=True)
        write_guard(node, stage="re-enabling OverlayFS", maintenance_open=True, start_after=start_after)
        enable_overlay_and_reboot(node)
    else:
        print(f"[{name}] RECOVERY: OverlayFS is already active; verifying lock", flush=True)

    verify_locked(node)
    write_guard(node, stage="lock verified", maintenance_open=False, start_after=start_after)
    if start_after:
        print(f"[{name}] RECOVERY: starting configured stream", flush=True)
        start_stream(node)
    clear_guard()
    print(f"[{name}] RECOVERY COMPLETE", flush=True)


def select_nodes(nodes: list[dict], selectors: list[str], all_nodes: bool) -> list[dict]:
    if all_nodes:
        return nodes

    if not selectors:
        raise UpdateError("Choose --all or at least one --node NAME_OR_TARGET")

    selected: list[dict] = []
    for selector in selectors:
        matches = [
            n for n in nodes
            if selector == str(n.get("name", ""))
            or selector == str(n.get("target", ""))
            or selector == str(n.get("target", "")).split("@")[-1]
        ]
        if not matches:
            raise UpdateError(f"No node matches {selector!r}")
        for node in matches:
            if node not in selected:
                selected.append(node)
    return selected


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Update only VLC + Streamlink on OverlayFS-protected Raspberry Pis."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--all", action="store_true", help="update every node sequentially")
    group.add_argument("--recover-guard", action="store_true", help="recover the node recorded in runtime/update_guard.json")
    group.add_argument(
        "--node",
        action="append",
        default=[],
        metavar="NAME_OR_TARGET",
        help="update one node; repeat for multiple nodes",
    )
    parser.add_argument(
        "--start-after",
        action="store_true",
        help="run scripts/start.sh after the final reboot",
    )
    parser.add_argument(
        "--continue-on-error",
        action="store_true",
        help="continue to later nodes if one node fails",
    )
    args = parser.parse_args()

    if args.recover_guard:
        try:
            recover_guard()
            return 0
        except (UpdateError, OSError, subprocess.SubprocessError) as exc:
            print(f"RECOVERY FAILED: {exc}", file=sys.stderr, flush=True)
            return 1

    try:
        nodes = read_nodes()
        selected = select_nodes(nodes, args.node, args.all)
    except (OSError, json.JSONDecodeError, UpdateError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    print(f"Nodes file: {NODES_FILE}")
    print(f"Selected: {', '.join(node_name(n) for n in selected)}")
    if SSH_PASSWORD_FILE.is_file():
        print(f"SSH auth: key first, password fallback from {SSH_PASSWORD_FILE}")
    else:
        print("SSH auth: key only (no password fallback file found)")
    print("Update scope: VLC + Streamlink ONLY. No apt full-upgrade. bootro is untouched.")
    print("Multiple nodes are updated SEQUENTIALLY.")

    failures: list[tuple[str, str]] = []

    for node in selected:
        try:
            update_node(node, start_after=args.start_after)
        except (UpdateError, OSError, subprocess.SubprocessError) as exc:
            name = node_name(node)
            failures.append((name, str(exc)))
            print(f"\n[{name}] FAILED: {exc}", file=sys.stderr, flush=True)
            print(
                f"[{name}] IMPORTANT: inspect this Pi before assuming OverlayFS is enabled again.",
                file=sys.stderr,
                flush=True,
            )
            if not args.continue_on_error:
                break

    print("\n" + "=" * 72)
    if failures:
        print("UPDATE FINISHED WITH FAILURES")
        for name, error in failures:
            print(f"- {name}: {error}")
        return 1

    print("ALL SELECTED NODES UPDATED SUCCESSFULLY")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
