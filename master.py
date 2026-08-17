#!/usr/bin/env python3
"""Stream Master: minimal HTTP UI + SSH controller for Raspberry Pi stream nodes."""

from __future__ import annotations

import argparse
import concurrent.futures
import os
import json
import mimetypes
import queue
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

APP_DIR = Path(__file__).resolve().parent
WEB_DIR = APP_DIR / "web"
NODES_FILE = APP_DIR / "nodes.json"
NODES_DEFAULT_FILE = APP_DIR / "nodes.default.json"
SCRIPTS_DIR = APP_DIR / "scripts"
UPDATE_SCRIPT = APP_DIR / "update_pis.py"
MASTER_LOCAL_UPDATE_SCRIPT = APP_DIR / "update_master_local.sh"
LOCAL_STREAM_SERVICE = os.environ.get("STREAM_MASTER_LOCAL_STREAM_SERVICE", "streaming-setup-local-stream.service")
RUNTIME_DIR = APP_DIR / "runtime"
UPDATE_UI_FILE = RUNTIME_DIR / "update_ui_state.json"
UPDATE_GUARD_FILE = RUNTIME_DIR / "update_guard.json"
NODE_JOBS_FILE = RUNTIME_DIR / "node_jobs.json"
FLEET_JOB_FILE = RUNTIME_DIR / "fleet_job.json"
STARTUP_STATE_FILE = RUNTIME_DIR / "startup_autostart.json"
STARTUP_BOOT_ID_FILE = RUNTIME_DIR / "startup_boot_id"
SSH_PASSWORD_FILE = Path.home() / ".config" / "stream-master" / "ssh-password"
MASTER_IP = os.environ.get("STREAM_MASTER_MASTER_IP", "192.168.0.101")

SSH_CONNECT_TIMEOUT = 6
REBOOT_DOWN_TIMEOUT = 60
REBOOT_UP_TIMEOUT = 300
UPDATE_PROCESS_INACTIVITY_TIMEOUT = 1300
UPDATE_RECOVERY_GRACE = 720
STARTUP_AUTOSTART_TIMEOUT = int(os.environ.get("STREAM_MASTER_AUTOSTART_TIMEOUT", "900"))
STARTUP_AUTOSTART_RETRY = int(os.environ.get("STREAM_MASTER_AUTOSTART_RETRY", "15"))
STARTUP_AUTOSTART_WORKERS = int(os.environ.get("STREAM_MASTER_AUTOSTART_WORKERS", "8"))

SCRIPT_ACTIONS = {"start", "check", "logs", "kill", "reboot", "shutdown"}
NORMAL_ACTIONS = {"start", "check", "logs", "kill"}

UPDATE_LOCK = threading.RLock()
CONFIG_LOCK = threading.RLock()
NODE_LOCKS_LOCK = threading.RLock()
JOBS_LOCK = threading.RLock()
FLEET_LOCK = threading.RLock()
NODE_LOCKS: dict[str, threading.Lock] = {}
NODE_JOBS: dict[str, dict] = {}
FLEET_JOB: dict = {"running": False, "kind": None, "stage": None, "message": None, "started_at": None, "finished_at": None, "ok": None}

UPDATE_STATE: dict = {
    "running": False,
    "mode": None,
    "requested_node": None,
    "current_node": None,
    "stage": None,
    "step": 0,
    "total_steps": 5,
    "started_at": None,
    "finished_at": None,
    "returncode": None,
    "lines": [],
}


class NodeBusy(RuntimeError):
    pass


def now() -> float:
    return time.time()


def atomic_json_write(path: Path, value: object, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.chmod(mode)
    tmp.replace(path)


def read_json_file(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def ensure_local_nodes_file() -> None:
    """Create mutable local config from the tracked default on first use."""
    if NODES_FILE.exists():
        return
    if not NODES_DEFAULT_FILE.is_file():
        return
    value = json.loads(NODES_DEFAULT_FILE.read_text(encoding="utf-8"))
    if not isinstance(value, list):
        raise ValueError("nodes.default.json must contain a JSON list")
    atomic_json_write(NODES_FILE, value)


def read_nodes() -> list[dict]:
    ensure_local_nodes_file()
    try:
        value = json.loads(NODES_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    if not isinstance(value, list):
        raise ValueError("nodes.json must contain a JSON list")
    return value


def validate_node(value: object) -> dict:
    if not isinstance(value, dict):
        raise ValueError("node must be a JSON object")

    def text(key: str, default: str = "", max_len: int = 2048) -> str:
        raw = value.get(key, default)
        if not isinstance(raw, str):
            raise ValueError(f"{key} must be a string")
        result = raw.strip()
        if "\n" in result or "\r" in result:
            raise ValueError(f"{key} must be one line")
        if len(result) > max_len:
            raise ValueError(f"{key} is too long")
        return result

    name = text("name", max_len=100)
    target = text("target", max_len=255)
    url = text("url", max_len=4096)
    quality = text("quality", "max480", 100) or "max480"
    connector = text("connector", "HDMI-A-1", 100) or "HDMI-A-1"
    role = text("role", "node", 20).lower() or "node"
    if role not in {"node", "master"}:
        raise ValueError("role must be node or master")
    if not name:
        raise ValueError("name is required")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+@[A-Za-z0-9_.-]+", target):
        raise ValueError("target must look like pi@192.168.0.101 or pi@hostname")
    # An empty URL is allowed so a freshly generated 24-node configuration can
    # be named/addressed before the stream links are entered. Start will reject it.
    if any(ch.isspace() for ch in connector):
        raise ValueError("connector may not contain spaces")
    try:
        port = int(value.get("port", 22))
    except (TypeError, ValueError) as exc:
        raise ValueError("port must be an integer") from exc
    if not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535")
    return {"name": name, "target": target, "port": port, "url": url, "quality": quality, "connector": connector, "role": role}


def write_nodes(nodes: list[dict]) -> None:
    atomic_json_write(NODES_FILE, nodes)


def node_key(node: dict) -> str:
    return str(node.get("target") or node.get("name") or "node")


def is_master_node(node: dict) -> bool:
    """The one machine that hosts this controller and is also a stream node.

    role=master is preferred. The configured master IP is also recognized so an
    existing local nodes.json does not need to be overwritten by Git.
    """
    if str(node.get("role", "node")).lower() == "master":
        return True
    try:
        host = str(node.get("target", "")).rsplit("@", 1)[1]
    except IndexError:
        return False
    return host == MASTER_IP


def master_node(nodes: list[dict] | None = None) -> dict | None:
    candidates = [n for n in (nodes if nodes is not None else read_nodes()) if is_master_node(n)]
    if len(candidates) > 1:
        raise ValueError("only one node may have role=master")
    return candidates[0] if candidates else None


def fleet_running() -> bool:
    with FLEET_LOCK:
        return bool(FLEET_JOB.get("running"))


def save_fleet_job() -> None:
    with FLEET_LOCK:
        atomic_json_write(FLEET_JOB_FILE, FLEET_JOB)


def set_fleet_job(**changes) -> dict:
    with FLEET_LOCK:
        FLEET_JOB.update(changes)
        snapshot = dict(FLEET_JOB)
        atomic_json_write(FLEET_JOB_FILE, FLEET_JOB)
        return snapshot


def fleet_snapshot() -> dict:
    with FLEET_LOCK:
        item = dict(FLEET_JOB)
    if item.get("started_at"):
        end = item.get("finished_at") or now()
        item["elapsed_seconds"] = int(max(0, end - item["started_at"]))
    return item


def load_fleet_job() -> None:
    value = read_json_file(FLEET_JOB_FILE, None)
    if isinstance(value, dict):
        with FLEET_LOCK:
            FLEET_JOB.update(value)


def node_lock(node: dict) -> threading.Lock:
    key = node_key(node)
    with NODE_LOCKS_LOCK:
        return NODE_LOCKS.setdefault(key, threading.Lock())


def active_node_job_for(target: str) -> dict | None:
    with JOBS_LOCK:
        job = NODE_JOBS.get(target)
        return dict(job) if job and job.get("running") else None


def any_active_jobs() -> bool:
    with JOBS_LOCK:
        return any(bool(j.get("running")) for j in NODE_JOBS.values())


def persist_jobs() -> None:
    with JOBS_LOCK:
        atomic_json_write(NODE_JOBS_FILE, NODE_JOBS)


def set_job(target: str, **changes) -> dict:
    with JOBS_LOCK:
        job = NODE_JOBS.setdefault(target, {"target": target})
        job.update(changes)
        snapshot = dict(job)
        atomic_json_write(NODE_JOBS_FILE, NODE_JOBS)
        return snapshot


def jobs_snapshot() -> dict[str, dict]:
    with JOBS_LOCK:
        result: dict[str, dict] = {}
        for key, value in NODE_JOBS.items():
            item = dict(value)
            if item.get("started_at"):
                end = item.get("finished_at") or now()
                item["elapsed_seconds"] = int(max(0, end - item["started_at"]))
            result[key] = item
        return result


def load_jobs() -> None:
    value = read_json_file(NODE_JOBS_FILE, {})
    if isinstance(value, dict):
        with JOBS_LOCK:
            NODE_JOBS.clear()
            for key, job in value.items():
                if isinstance(key, str) and isinstance(job, dict):
                    NODE_JOBS[key] = job


def update_guard_snapshot() -> dict | None:
    value = read_json_file(UPDATE_GUARD_FILE, None)
    return value if isinstance(value, dict) else None


def save_update_state() -> None:
    with UPDATE_LOCK:
        state = {k: v for k, v in UPDATE_STATE.items() if k != "lines"}
        state["lines"] = list(UPDATE_STATE.get("lines", []))[-500:]
    atomic_json_write(UPDATE_UI_FILE, state)


def load_update_state() -> None:
    value = read_json_file(UPDATE_UI_FILE, None)
    if not isinstance(value, dict):
        return
    with UPDATE_LOCK:
        for key in UPDATE_STATE:
            if key in value:
                UPDATE_STATE[key] = value[key]
        if UPDATE_STATE.get("running"):
            UPDATE_STATE["running"] = False
            UPDATE_STATE["returncode"] = 130
            UPDATE_STATE["finished_at"] = now()
            UPDATE_STATE["stage"] = "master restarted during update; check recovery status"
            UPDATE_STATE.setdefault("lines", []).append("Master restarted while an update was marked running.\n")
    save_update_state()


def ssh_base(node: dict) -> list[str]:
    """Password-only SSH for the isolated installation LAN.

    Host keys are deliberately not persisted or checked, so re-imaged/cloned Pis
    can reuse an IP without known_hosts conflicts. This trades MITM protection for
    operational simplicity and should only be used on a trusted local network.
    """
    target = str(node.get("target", ""))
    if not re.fullmatch(r"[A-Za-z0-9_.-]+@[A-Za-z0-9_.-]+", target):
        raise ValueError(f"invalid SSH target: {target!r}")
    if not SSH_PASSWORD_FILE.is_file():
        raise RuntimeError(f"SSH password file missing: {SSH_PASSWORD_FILE}")
    sshpass = shutil.which("sshpass")
    if not sshpass:
        raise RuntimeError("sshpass is required for password-only SSH")
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


def classify_ssh_failure(output: str, returncode: int) -> str | None:
    if returncode == 0:
        return None
    text = output.lower()
    if "remote host identification has changed" in text or "host key verification failed" in text:
        return "ssh_host_key_changed"
    if "permission denied" in text or "authentication failed" in text:
        return "ssh_auth_failed"
    if any(x in text for x in ("connection timed out", "operation timed out", "no route to host", "network is unreachable", "connection refused", "could not resolve hostname")):
        return "node_unreachable"
    if returncode == 124:
        return "node_unreachable"
    if returncode == 255:
        return "ssh_failed"
    return "remote_command_failed"


def _run_local_script_unlocked(node: dict, action: str) -> tuple[int, str, str | None]:
    """Run master-node actions locally, never through SSH.

    Start/kill use a separate systemd unit when installed so Git restarts of the
    web controller do not kill the local video stream's cgroup.
    """
    if action not in SCRIPT_ACTIONS:
        raise ValueError("unknown action")

    if action in {"start", "kill"}:
        unit_exists = subprocess.run(
            ["systemctl", "cat", LOCAL_STREAM_SERVICE],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode == 0
        if unit_exists:
            if action == "start" and not str(node.get("url", "")).strip():
                command = ["sudo", "-n", "systemctl", "stop", LOCAL_STREAM_SERVICE]
            else:
                command = ["sudo", "-n", "systemctl", "restart" if action == "start" else "stop", LOCAL_STREAM_SERVICE]
            try:
                done = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=30, check=False)
            except subprocess.TimeoutExpired:
                return 124, "Local stream systemd action timed out", "remote_command_failed"
            output = done.stdout.strip() or f"{LOCAL_STREAM_SERVICE}: {action} requested"
            return done.returncode, output, None if done.returncode == 0 else "remote_command_failed"

    script_path = SCRIPTS_DIR / f"{action}.sh"
    script = script_path.read_bytes()
    args: list[str] = []
    if action == "start":
        if not str(node.get("url", "")).strip():
            return 2, "Kein Stream-Link konfiguriert.", "remote_command_failed"
        args = [str(node.get("url", "")), str(node.get("quality", "max480")), str(node.get("connector", "HDMI-A-1"))]
    command = ["bash", "-s", "--", *args]
    timeout = 30 if action == "start" else 20
    name = str(node.get("name") or node_key(node))
    print(f"\n[{name}] LOCAL {action}.sh", flush=True)
    try:
        done = subprocess.run(
            command,
            input=script,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
            cwd=APP_DIR,
        )
        output = done.stdout.decode("utf-8", errors="replace").strip()
        print(output or "(no output)", flush=True)
        print(f"[{name}] local rc={done.returncode}", flush=True)
        failure = None if done.returncode == 0 else "remote_command_failed"
        return done.returncode, output, failure
    except subprocess.TimeoutExpired as exc:
        output = "Local script timed out"
        if exc.stdout:
            try:
                output = exc.stdout.decode("utf-8", errors="replace").strip() + "\n" + output
            except AttributeError:
                pass
        return 124, output.strip(), "remote_command_failed"


def _run_script_unlocked(node: dict, action: str) -> tuple[int, str, str | None]:
    if action not in SCRIPT_ACTIONS:
        raise ValueError("unknown action")
    if is_master_node(node):
        return _run_local_script_unlocked(node, action)
    script_path = SCRIPTS_DIR / f"{action}.sh"
    script = script_path.read_bytes()
    args: list[str] = []
    if action == "start":
        if not str(node.get("url", "")).strip():
            return 2, "Kein Stream-Link konfiguriert.", "remote_command_failed"
        args = [str(node.get("url", "")), str(node.get("quality", "max480")), str(node.get("connector", "HDMI-A-1"))]
    remote = "bash -s --" + (" " + " ".join(shlex.quote(x) for x in args) if args else "")
    command = ssh_base(node) + [remote]
    timeout = 30 if action == "start" else 20
    name = str(node.get("name") or node_key(node))
    print(f"\n[{name}] {action}.sh", flush=True)
    try:
        done = subprocess.run(
            command,
            input=script,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
        output = done.stdout.decode("utf-8", errors="replace").strip()
        print(output or "(no output)", flush=True)
        print(f"[{name}] rc={done.returncode}", flush=True)
        return done.returncode, output, classify_ssh_failure(output, done.returncode)
    except subprocess.TimeoutExpired as exc:
        output = "SSH/script timed out"
        if exc.stdout:
            try:
                output = exc.stdout.decode("utf-8", errors="replace").strip() + "\n" + output
            except AttributeError:
                pass
        return 124, output.strip(), "node_unreachable"


def run_script(node: dict, action: str) -> tuple[int, str, str | None]:
    with UPDATE_LOCK:
        if UPDATE_STATE["running"]:
            raise RuntimeError("software update is currently running")
    if fleet_running():
        raise RuntimeError("fleet power operation is currently running")
    if active_node_job_for(node_key(node)):
        raise NodeBusy("node operation already in progress")
    lock = node_lock(node)
    if not lock.acquire(blocking=False):
        raise NodeBusy("node is busy")
    try:
        # Re-check after taking the node lock. This closes the small race where
        # an update starts while an already-requested check/start was waiting.
        with UPDATE_LOCK:
            if UPDATE_STATE["running"]:
                raise RuntimeError("software update is currently running")
        return _run_script_unlocked(node, action)
    finally:
        lock.release()


def parse_machine_output(output: str) -> dict[str, str]:
    data: dict[str, str] = {}
    for line in output.splitlines():
        match = re.match(r"^([A-Z][A-Z0-9_]*)=(.*)$", line)
        if match:
            data[match.group(1)] = match.group(2)
    return data


def host_port(node: dict) -> tuple[str, int]:
    return str(node["target"]).rsplit("@", 1)[1], int(node.get("port", 22))


def tcp_up(node: dict, timeout: float = 2.0) -> bool:
    host, port = host_port(node)
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def ssh_authenticated(node: dict) -> bool:
    try:
        done = subprocess.run(
            ssh_base(node) + ["true"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
        return done.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def wait_for_node(node: dict, online: bool, timeout: int) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        up = tcp_up(node)
        if online:
            if up and ssh_authenticated(node):
                return True
        elif not up:
            return True
        time.sleep(2)
    return False


def _local_power_command(kind: str) -> None:
    command = ["sudo", "-n", "systemctl", "reboot" if kind == "reboot" else "poweroff", "--no-block"]
    done = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=15, check=False)
    if done.returncode != 0:
        raise RuntimeError(done.stdout.strip() or f"local {kind} command failed")


def _finish_master_reboot(node: dict) -> None:
    """Complete a master reboot job after systemd has started this service again."""
    target = node_key(node)
    lock = node_lock(node)
    with lock:
        try:
            set_job(target, running=True, kind="reboot", stage="starting stream", message="Master is back; starting configured stream")
            if str(node.get("url", "")).strip():
                code, output, failure = _run_local_script_unlocked(node, "start")
                if code != 0:
                    raise RuntimeError("Master rebooted, but stream start failed: " + (output or failure or "unknown error"))
                time.sleep(2)
            code, output, failure = _run_local_script_unlocked(node, "check")
            health = parse_machine_output(output).get("STREAM_HEALTH", "unknown") if code == 0 else "unknown"
            set_job(target, running=False, ok=True, stage="complete", message=f"reboot complete; stream state: {health}", finished_at=now())
        except Exception as exc:
            set_job(target, running=False, ok=False, stage="failed", message=str(exc), finished_at=now())


def _reboot_worker(node: dict, resume: bool = False) -> None:
    target = node_key(node)
    if is_master_node(node):
        if resume:
            _finish_master_reboot(node)
            return
        try:
            set_job(target, running=True, kind="reboot", stage="rebooting master", message="Master will reboot last", started_at=now(), finished_at=None)
            # Give the HTTP response/job state time to reach the browser before systemd stops us.
            time.sleep(1.0)
            _local_power_command("reboot")
            # Do not mark complete: the next service start resumes this job.
        except Exception as exc:
            set_job(target, running=False, ok=False, stage="failed", message=str(exc), finished_at=now())
        return

    lock = node_lock(node)
    with lock:
        try:
            if not resume:
                set_job(target, running=True, kind="reboot", stage="sending reboot", message="sending reboot command")
                code, output, failure = _run_script_unlocked(node, "reboot")
                if code != 0:
                    raise RuntimeError(output or failure or "reboot command failed")
                set_job(target, stage="waiting offline", message="waiting for the Pi to shut down")
                if not wait_for_node(node, online=False, timeout=REBOOT_DOWN_TIMEOUT):
                    set_job(target, stage="waiting online", message="reboot sent; shutdown was not observed, waiting for SSH")
                else:
                    set_job(target, stage="waiting online", message="Pi is offline; waiting for SSH")
            else:
                set_job(target, running=True, kind="reboot", stage="resuming reboot", message="master restarted; waiting for the Pi")

            if not wait_for_node(node, online=True, timeout=REBOOT_UP_TIMEOUT):
                raise RuntimeError("Pi did not return to SSH after reboot")

            set_job(target, stage="starting stream", message="Pi is back; starting configured stream")
            if str(node.get("url", "")).strip():
                code, output, failure = _run_script_unlocked(node, "start")
                if code != 0:
                    raise RuntimeError("Pi rebooted, but stream start failed: " + (output or failure or "unknown error"))
                time.sleep(2)
            code, output, failure = _run_script_unlocked(node, "check")
            health = parse_machine_output(output).get("STREAM_HEALTH", "unknown") if code == 0 else "unknown"
            set_job(target, running=False, ok=True, stage="complete", message=f"reboot complete; stream state: {health}", finished_at=now())
        except Exception as exc:
            set_job(target, running=False, ok=False, stage="failed", message=str(exc), finished_at=now())


def _shutdown_worker(node: dict) -> None:
    target = node_key(node)
    if is_master_node(node):
        try:
            set_job(target, running=True, kind="shutdown", stage="shutting down master", message="Master will power off last", started_at=now(), finished_at=None)
            time.sleep(1.0)
            _local_power_command("shutdown")
            # The process will disappear. On the next boot this job is closed as successful.
        except Exception as exc:
            set_job(target, running=False, ok=False, stage="failed", message=str(exc), finished_at=now())
        return

    lock = node_lock(node)
    with lock:
        try:
            set_job(target, running=True, kind="shutdown", stage="shutting down", message="sending shutdown command")
            code, output, failure = _run_script_unlocked(node, "shutdown")
            if code != 0:
                raise RuntimeError(output or failure or "shutdown command failed")
            set_job(target, stage="waiting offline", message="waiting for Pi to go offline")
            offline = wait_for_node(node, online=False, timeout=60)
            set_job(target, running=False, ok=offline, stage="complete" if offline else "sent", message="Pi is shut down" if offline else "shutdown command sent; offline state not observed", finished_at=now())
        except Exception as exc:
            set_job(target, running=False, ok=False, stage="failed", message=str(exc), finished_at=now())


def start_node_job(index: int, kind: str, _from_fleet: bool = False) -> dict:
    nodes = read_nodes()
    if not 0 <= index < len(nodes):
        raise ValueError("invalid node index")
    node = nodes[index]
    target = node_key(node)
    with UPDATE_LOCK:
        if UPDATE_STATE["running"]:
            raise RuntimeError("software update is currently running")
        if fleet_running() and not _from_fleet:
            raise RuntimeError("fleet power operation is currently running")
        if active_node_job_for(target):
            raise NodeBusy("node operation already in progress")
        set_job(target, running=True, ok=None, kind=kind, stage="queued", message=f"{kind} queued", started_at=now(), finished_at=None, name=node.get("name"))
    worker = _reboot_worker if kind == "reboot" else _shutdown_worker
    threading.Thread(target=worker, args=(dict(node),), daemon=True, name=f"node-{kind}-{target}").start()
    return jobs_snapshot()[target]


def resume_interrupted_jobs() -> None:
    nodes = read_nodes()
    by_target = {node_key(n): n for n in nodes}
    with JOBS_LOCK:
        pending = [(target, dict(job)) for target, job in NODE_JOBS.items() if job.get("running")]
    for target, job in pending:
        node = by_target.get(target)
        if not node:
            set_job(target, running=False, ok=False, stage="failed", message="node removed from config while operation was pending", finished_at=now())
            continue
        if job.get("kind") == "reboot":
            threading.Thread(target=_reboot_worker, args=(dict(node), True), daemon=True, name=f"resume-reboot-{target}").start()
        elif is_master_node(node) and job.get("kind") == "shutdown":
            set_job(target, running=False, ok=True, stage="complete", message="Master was previously shut down and has now booted again", finished_at=now())
        else:
            set_job(target, running=False, ok=False, stage="interrupted", message="master restarted during shutdown request; check node state", finished_at=now())


def _wait_for_targets_finished(targets: list[str], timeout: int) -> tuple[bool, list[str]]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with JOBS_LOCK:
            running = [t for t in targets if NODE_JOBS.get(t, {}).get("running")]
        if not running:
            return True, []
        time.sleep(2)
    with JOBS_LOCK:
        running = [t for t in targets if NODE_JOBS.get(t, {}).get("running")]
    return False, running


def _fleet_power_worker(kind: str, resume: bool = False) -> None:
    try:
        nodes = read_nodes()
        master = master_node(nodes)
        workers = [n for n in nodes if not is_master_node(n)]

        if not resume:
            set_fleet_job(running=True, kind=kind, stage="workers", message=f"{kind}: worker nodes first", started_at=now(), finished_at=None, ok=None)
            # Start all non-master power jobs. They are allowed to finish/recover
            # while the controller stays online.
            for i, node in enumerate(nodes):
                if is_master_node(node):
                    continue
                try:
                    start_node_job(i, kind, _from_fleet=True)
                except NodeBusy:
                    pass
            targets = [node_key(n) for n in workers]
            timeout = 600 if kind == "reboot" else 180
            finished, remaining = _wait_for_targets_finished(targets, timeout)
            if not finished:
                raise RuntimeError("timed out waiting for worker nodes: " + ", ".join(remaining))
            set_fleet_job(stage="master", message=f"{kind}: worker nodes done; master is last")
            if master is None:
                set_fleet_job(running=False, ok=True, stage="complete", message="fleet power operation complete", finished_at=now())
                return
            mi = next(i for i,n in enumerate(nodes) if is_master_node(n))
            start_node_job(mi, kind, _from_fleet=True)
            # reboot/shutdown now intentionally kills this service. Completion is
            # recorded by resume_fleet_job() on the next master boot.
            return

        # We are back after the master itself rebooted or after a later power-on.
        mt = master_node(nodes)
        if mt:
            _wait_for_targets_finished([node_key(mt)], 120)
        set_fleet_job(running=False, ok=True, stage="complete", message="fleet power operation complete", finished_at=now())
    except Exception as exc:
        set_fleet_job(running=False, ok=False, stage="failed", message=str(exc), finished_at=now())


def start_fleet_power(kind: str) -> dict:
    if kind not in {"reboot", "shutdown"}:
        raise ValueError("invalid fleet power action")
    with UPDATE_LOCK:
        if UPDATE_STATE["running"]:
            raise RuntimeError("software update is currently running")
    if fleet_running():
        raise NodeBusy("fleet power operation already in progress")
    if any_active_jobs():
        raise NodeBusy("finish current node operations first")
    set_fleet_job(running=True, kind=kind, stage="queued", message=f"{kind} queued", started_at=now(), finished_at=None, ok=None)
    threading.Thread(target=_fleet_power_worker, args=(kind, False), daemon=True, name=f"fleet-{kind}").start()
    return fleet_snapshot()


def resume_fleet_job() -> None:
    if not fleet_running():
        return
    threading.Thread(target=_fleet_power_worker, kwargs={"kind": str(FLEET_JOB.get("kind") or "reboot"), "resume": True}, daemon=True, name="resume-fleet-power").start()


def save_node_config(index: int | None, value: object, apply: bool = False) -> dict:
    if not isinstance(value, dict):
        raise ValueError("node must be a JSON object")
    # The UI normally sends role back. If an older UI does not, preserve the
    # existing role so Stream 01 cannot silently stop being the master.
    if index is not None and "role" not in value:
        current = read_nodes()
        if isinstance(index, int) and 0 <= index < len(current):
            value = dict(value)
            value["role"] = current[index].get("role", "node")
    node = validate_node(value)
    with UPDATE_LOCK:
        if UPDATE_STATE["running"]:
            raise RuntimeError("cannot change config while an update is running")
        with CONFIG_LOCK:
            nodes = read_nodes()
            old = None
            if index is None:
                index = len(nodes)
                nodes.append(node)
            else:
                if not isinstance(index, int) or not 0 <= index < len(nodes):
                    raise ValueError("invalid node index")
                old = dict(nodes[index])
                if active_node_job_for(node_key(old)):
                    raise NodeBusy("cannot edit this node while reboot/shutdown is running")
                nodes[index] = node
            write_nodes(nodes)

        url_changed = old is not None and str(old.get("url", "")) != node["url"]
        target_changed = old is not None and str(old.get("target", "")) != node["target"]
        runtime_changed = old is not None and any(
            old.get(key) != node.get(key) for key in ("target", "port", "url", "quality", "connector")
        )
        apply_result = None
        old_stop_warning = None
        if apply and runtime_changed:
            if target_changed and old is not None:
                try:
                    old_code, old_output, _ = run_script(old, "kill")
                    if old_code != 0:
                        old_stop_warning = old_output or "old target could not be stopped"
                except Exception as exc:
                    old_stop_warning = str(exc)
            if not node["url"]:
                # Clearing a URL means "no stream configured". Stop the current
                # stream on the still-selected target instead of trying to start
                # an empty URL.
                code, output, failure = run_script(node, "kill")
            else:
                code, output, failure = run_script(node, "start")
            apply_result = {"ok": code == 0, "returncode": code, "output": output, "failure": failure, "old_stop_warning": old_stop_warning}
        return {"ok": True, "index": index, "node": node, "url_changed": url_changed, "target_changed": target_changed, "runtime_changed": runtime_changed, "apply": apply_result}


def _update_snapshot() -> dict:
    with UPDATE_LOCK:
        state = {k: v for k, v in UPDATE_STATE.items() if k != "lines"}
        lines = list(UPDATE_STATE["lines"])
    if state.get("started_at"):
        end = state.get("finished_at") or now()
        state["elapsed_seconds"] = int(max(0, end - state["started_at"]))
    else:
        state["elapsed_seconds"] = None
    state["output"] = "".join(lines)
    return state


def _append_update_line(line: str) -> None:
    print("[update]", line, end="" if line.endswith("\n") else "\n", flush=True)
    clean = line.strip()
    with UPDATE_LOCK:
        UPDATE_STATE["lines"].append(line)
        if len(UPDATE_STATE["lines"]) > 4000:
            del UPDATE_STATE["lines"][:1000]
        m = re.search(r"^UPDATING (.+?) \(", clean)
        if m:
            UPDATE_STATE["current_node"] = m.group(1)
            UPDATE_STATE["stage"] = "preflight"
            UPDATE_STATE["step"] = 0
        m = re.search(r"STEP (\d+)/(\d+):\s*(.+)$", clean)
        if m:
            UPDATE_STATE["step"] = int(m.group(1))
            UPDATE_STATE["total_steps"] = int(m.group(2))
            UPDATE_STATE["stage"] = m.group(3)
        if "waiting for SSH to go offline" in clean:
            UPDATE_STATE["stage"] = "rebooting · waiting offline"
        elif "waiting for SSH to go online" in clean:
            UPDATE_STATE["stage"] = "rebooting · waiting for SSH"
        elif "apt update" in clean.lower():
            UPDATE_STATE["stage"] = "apt update"
        elif "VLC only-upgrade" in clean:
            UPDATE_STATE["stage"] = "updating VLC"
        elif "pipx / Streamlink" in clean:
            UPDATE_STATE["stage"] = "updating Streamlink"
        elif "RECOVERY:" in clean:
            UPDATE_STATE["stage"] = "recovering after failure"
        elif "RECOVERY COMPLETE" in clean:
            UPDATE_STATE["stage"] = "recovery complete"
        elif "UPDATE COMPLETE" in clean:
            UPDATE_STATE["stage"] = "complete"
        elif "FAILED:" in clean or "UPDATE ERROR:" in clean:
            UPDATE_STATE["stage"] = "failed"
    save_update_state()


def _run_master_local_package_update() -> int:
    with UPDATE_LOCK:
        UPDATE_STATE["current_node"] = "Master (.101)"
        UPDATE_STATE["stage"] = "updating master locally"
        UPDATE_STATE["step"] = 0
    save_update_state()
    if not MASTER_LOCAL_UPDATE_SCRIPT.is_file():
        _append_update_line("MASTER LOCAL UPDATE ERROR: update_master_local.sh is missing.\n")
        return 1
    _append_update_line("\nMASTER LOCAL UPDATE: VLC + Streamlink on controller (no OverlayFS toggle, no reboot).\n")
    try:
        proc = subprocess.Popen(
            ["/bin/bash", str(MASTER_LOCAL_UPDATE_SCRIPT)],
            cwd=APP_DIR,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            _append_update_line("[master-local] " + line)
        return proc.wait()
    except Exception as exc:
        _append_update_line(f"MASTER LOCAL UPDATE ERROR: {exc}\n")
        return 1


def _run_update_process(command: list[str]) -> None:
    rc = 1
    proc: subprocess.Popen[str] | None = None
    try:
        proc = subprocess.Popen(
            command,
            cwd=APP_DIR,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        assert proc.stdout is not None
        line_queue: queue.Queue[str | None] = queue.Queue()

        def reader() -> None:
            try:
                for line in proc.stdout:
                    line_queue.put(line)
            finally:
                line_queue.put(None)

        threading.Thread(target=reader, daemon=True, name="updater-output").start()
        last_output = time.monotonic()
        reader_finished = False
        interrupt_deadline: float | None = None

        while True:
            try:
                item = line_queue.get(timeout=0.5)
            except queue.Empty:
                item = "__NO_LINE__"
            if item is None:
                reader_finished = True
            elif item != "__NO_LINE__":
                _append_update_line(item)
                last_output = time.monotonic()
            if proc.poll() is not None and reader_finished and line_queue.empty():
                rc = proc.wait()
                break

            if interrupt_deadline is None and time.monotonic() - last_output > UPDATE_PROCESS_INACTIVITY_TIMEOUT:
                _append_update_line("MASTER WATCHDOG: updater stalled; requesting graceful interruption/recovery.\n")
                proc.send_signal(signal.SIGINT)
                interrupt_deadline = time.monotonic() + UPDATE_RECOVERY_GRACE
                last_output = time.monotonic()
            elif interrupt_deadline is not None and time.monotonic() > interrupt_deadline:
                _append_update_line("MASTER WATCHDOG: recovery grace expired; killing updater. MANUAL INSPECTION REQUIRED.\n")
                proc.kill()
                rc = proc.wait()
                if rc == 0:
                    rc = 124
                break

        # In "Update all", worker Pis use the OverlayFS updater while the
        # controller master is updated locally without rebooting or toggling
        # OverlayFS. This keeps the web controller alive.
        with UPDATE_LOCK:
            mode = UPDATE_STATE.get("mode")
        if mode == "all":
            local_rc = _run_master_local_package_update()
            if local_rc != 0 and rc == 0:
                rc = local_rc
    except Exception as exc:
        _append_update_line(f"MASTER UPDATE ERROR: {exc}\n")
        rc = 1
        if proc is not None and proc.poll() is None:
            try:
                proc.send_signal(signal.SIGINT)
                proc.wait(timeout=UPDATE_RECOVERY_GRACE)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
    finally:
        with UPDATE_LOCK:
            UPDATE_STATE["running"] = False
            UPDATE_STATE["returncode"] = rc
            UPDATE_STATE["finished_at"] = now()
            UPDATE_STATE["stage"] = "complete" if rc == 0 else "failed"
        save_update_state()


def _run_update_after_barrier(command: list[str], barrier_nodes: list[dict]) -> None:
    try:
        # UPDATE_STATE is already marked running, so no new UI actions can begin.
        # Wait for any action that started just before the update request to finish.
        for node in barrier_nodes:
            lock = node_lock(node)
            if not lock.acquire(timeout=45):
                raise RuntimeError(f"timed out waiting for active node action on {node_key(node)}")
            lock.release()
    except Exception as exc:
        _append_update_line(f"MASTER UPDATE ERROR: {exc}\n")
        with UPDATE_LOCK:
            UPDATE_STATE["running"] = False
            UPDATE_STATE["returncode"] = 1
            UPDATE_STATE["finished_at"] = now()
            UPDATE_STATE["stage"] = "failed before updater launch"
        save_update_state()
        return
    _run_update_process(command)


def start_update(*, node_index: int | None = None, all_nodes: bool = False, recover: bool = False) -> dict:
    if fleet_running():
        raise RuntimeError("finish fleet reboot/shutdown before starting an update")
    if any_active_jobs():
        raise RuntimeError("finish reboot/shutdown operations before starting an update")
    if not UPDATE_SCRIPT.is_file():
        raise ValueError(f"missing updater: {UPDATE_SCRIPT}")
    nodes = read_nodes()

    if recover:
        if not update_guard_snapshot():
            raise ValueError("no interrupted-update recovery guard exists")
        command = [sys.executable, "-u", str(UPDATE_SCRIPT), "--recover-guard"]
        mode = "recovery"
        guard = update_guard_snapshot() or {}
        requested_node = str(guard.get("name") or guard.get("target") or "guarded node")
        barrier_nodes = [guard.get("node")] if isinstance(guard.get("node"), dict) else []
    elif all_nodes:
        if update_guard_snapshot():
            raise RuntimeError("an interrupted-update recovery guard exists; recover it before starting another update")
        workers = [n for n in nodes if not is_master_node(n)]
        if not workers:
            raise ValueError("no worker nodes are configured for OverlayFS updates")
        command = [sys.executable, "-u", str(UPDATE_SCRIPT), "--all", "--start-after", "--continue-on-error"]
        mode = "all"
        requested_node = None
        barrier_nodes = workers
    else:
        if update_guard_snapshot():
            raise RuntimeError("an interrupted-update recovery guard exists; recover it before starting another update")
        if node_index is None or not 0 <= node_index < len(nodes):
            raise ValueError("invalid node index")
        node = nodes[node_index]
        if is_master_node(node):
            raise ValueError("the master node stays writable and is intentionally excluded from OverlayFS node updates")
        selector = str(node.get("target", ""))
        command = [sys.executable, "-u", str(UPDATE_SCRIPT), "--node", selector, "--start-after"]
        mode = "one"
        requested_node = str(node.get("name") or selector)
        barrier_nodes = [node]

    with UPDATE_LOCK:
        if UPDATE_STATE["running"]:
            raise RuntimeError("an update/recovery is already running")
        if any_active_jobs():
            raise RuntimeError("finish reboot/shutdown operations before starting an update")
        UPDATE_STATE.update({
            "running": True,
            "mode": mode,
            "requested_node": requested_node,
            "current_node": requested_node,
            "stage": "starting recovery" if recover else "starting updater",
            "step": 0,
            "total_steps": 1 if recover else 5,
            "started_at": now(),
            "finished_at": None,
            "returncode": None,
            "lines": ["$ " + shlex.join(command) + "\n"],
        })
    save_update_state()
    threading.Thread(target=_run_update_after_barrier, args=(command, barrier_nodes), daemon=True, name="pi-updater").start()
    return _update_snapshot()


def current_boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
    except OSError:
        # Fallback is intentionally process-independent only when boot_id is unavailable.
        return "unknown-boot"


def _write_startup_state(**changes) -> None:
    state = read_json_file(STARTUP_STATE_FILE, {})
    if not isinstance(state, dict):
        state = {}
    state.update(changes)
    atomic_json_write(STARTUP_STATE_FILE, state)


def _autostart_one_node(node: dict, deadline: float) -> dict:
    name = str(node.get("name") or node_key(node))
    target = node_key(node)
    if not str(node.get("url", "")).strip():
        return {"name": name, "target": target, "result": "skipped_no_url"}
    attempts = 0
    last_failure = None
    last_output = ""
    while now() < deadline:
        attempts += 1
        try:
            code, output, failure = run_script(node, "start")
        except (NodeBusy, RuntimeError) as exc:
            code, output, failure = 1, str(exc), "busy"
        last_failure, last_output = failure, output
        if code == 0:
            return {"name": name, "target": target, "result": "started", "attempts": attempts}
        # Power-on race: retry nodes that simply are not reachable yet. An auth
        # or software/configuration error will not become better by hammering it.
        if failure not in {"node_unreachable", "ssh_failed", "busy"}:
            break
        remaining = deadline - now()
        if remaining <= 0:
            break
        time.sleep(min(STARTUP_AUTOSTART_RETRY, max(1, remaining)))
    return {
        "name": name,
        "target": target,
        "result": "failed",
        "attempts": attempts,
        "failure": last_failure,
        "output": last_output[-300:],
    }


def startup_autostart_once_per_boot() -> None:
    """Start every configured stream once after a fresh MASTER boot.

    Git-triggered service restarts during the same OS boot do not restart all
    video streams. If this worker itself is interrupted before completion, the
    boot marker is not written, so systemd's next service start retries safely.
    """
    if os.environ.get("STREAM_MASTER_AUTOSTART", "1") not in {"1", "yes", "true", "on"}:
        print("Startup autostart disabled by STREAM_MASTER_AUTOSTART.")
        return
    boot_id = current_boot_id()
    try:
        if STARTUP_BOOT_ID_FILE.read_text(encoding="utf-8").strip() == boot_id:
            print("Startup autostart already completed during this OS boot; skipping.")
            return
    except OSError:
        pass

    nodes = read_nodes()
    configured = [n for n in nodes if str(n.get("url", "")).strip()]
    _write_startup_state(
        running=True,
        boot_id=boot_id,
        started_at=now(),
        finished_at=None,
        configured=len(configured),
        total=len(nodes),
        results=[],
    )
    print(f"Daily startup: {len(configured)}/{len(nodes)} configured streams; waiting up to {STARTUP_AUTOSTART_TIMEOUT}s for Pis.")
    deadline = now() + STARTUP_AUTOSTART_TIMEOUT
    results: list[dict] = []
    if configured:
        workers = max(1, min(STARTUP_AUTOSTART_WORKERS, len(configured)))
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers, thread_name_prefix="daily-start") as pool:
            futures = [pool.submit(_autostart_one_node, node, deadline) for node in configured]
            for future in concurrent.futures.as_completed(futures):
                try:
                    result = future.result()
                except Exception as exc:
                    result = {"result": "failed", "failure": "master_exception", "output": str(exc)}
                results.append(result)
                _write_startup_state(results=results)
                print(f"Daily startup: {result}")
    # No URL is a valid, intentionally unconfigured node and does not prevent
    # completion of the once-per-boot marker.
    _write_startup_state(running=False, finished_at=now(), results=results)
    STARTUP_BOOT_ID_FILE.write_text(boot_id + "\n", encoding="utf-8")
    print("Daily startup pass completed for this OS boot.")


def launch_startup_autostart() -> None:
    threading.Thread(target=startup_autostart_once_per_boot, daemon=True, name="daily-autostart").start()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        print("[http]", fmt % args)

    def send_bytes(self, body: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, value: object, status: int = 200) -> None:
        self.send_bytes(json.dumps(value, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8", status)

    def read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        if length > 64 * 1024:
            raise ValueError("request body too large")
        value = json.loads(self.rfile.read(length))
        if not isinstance(value, dict):
            raise ValueError("JSON body must be an object")
        return value

    def serve_static(self, path: str) -> bool:
        mapping = {"/": "index.html", "/index.html": "index.html", "/app.js": "app.js", "/style.css": "style.css"}
        name = mapping.get(path)
        if not name:
            return False
        file = WEB_DIR / name
        content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        if name.endswith(".js"):
            content_type = "text/javascript; charset=utf-8"
        elif name.endswith(".css"):
            content_type = "text/css; charset=utf-8"
        elif name.endswith(".html"):
            content_type = "text/html; charset=utf-8"
        self.send_bytes(file.read_bytes(), content_type)
        return True

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        try:
            if self.serve_static(path):
                return
            if path == "/api/nodes":
                self.send_json({"nodes": read_nodes()})
            elif path == "/api/state":
                self.send_json({"update": _update_snapshot(), "jobs": jobs_snapshot(), "fleet": fleet_snapshot(), "recovery_guard": update_guard_snapshot()})
            elif path == "/api/update/status":
                self.send_json(_update_snapshot())
            else:
                self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            if path == "/api/config/node":
                data = self.read_json()
                index = data.get("index")
                if index is not None and not isinstance(index, int):
                    raise ValueError("index must be an integer or null")
                result = save_node_config(index, data.get("node"), bool(data.get("apply")))
                self.send_json(result)
                return

            if path == "/api/update/start":
                data = self.read_json()
                if data.get("all") is True:
                    self.send_json(start_update(all_nodes=True))
                else:
                    index = data.get("index")
                    if not isinstance(index, int):
                        raise ValueError("index must be an integer")
                    self.send_json(start_update(node_index=index))
                return

            if path == "/api/recovery/start":
                self.send_json(start_update(recover=True))
                return

            fleet_match = re.fullmatch(r"/api/fleet/(reboot|shutdown)", path)
            if fleet_match:
                self.send_json({"ok": True, "accepted": True, "fleet": start_fleet_power(fleet_match.group(1))}, HTTPStatus.ACCEPTED)
                return

            match = re.fullmatch(r"/api/nodes/(\d+)/(start|check|logs|kill|reboot|shutdown)", path)
            if not match:
                self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
                return
            nodes = read_nodes()
            index = int(match.group(1))
            if not 0 <= index < len(nodes):
                raise ValueError("invalid node index")
            action = match.group(2)
            if action in {"reboot", "shutdown"}:
                self.send_json({"ok": True, "accepted": True, "job": start_node_job(index, action)}, HTTPStatus.ACCEPTED)
                return
            code, output, failure = run_script(nodes[index], action)
            self.send_json({"ok": code == 0, "returncode": code, "output": output, "failure": failure})
        except NodeBusy as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.CONFLICT)
        except RuntimeError as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.CONFLICT)
        except (IndexError, ValueError, OSError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    ensure_local_nodes_file()
    required = [WEB_DIR / "index.html", WEB_DIR / "app.js", WEB_DIR / "style.css", UPDATE_SCRIPT, MASTER_LOCAL_UPDATE_SCRIPT, APP_DIR / "local_stream_service.sh"]
    required.extend(SCRIPTS_DIR / f"{name}.sh" for name in SCRIPT_ACTIONS)
    missing = [str(p) for p in required if not p.is_file()]
    if missing:
        print("Missing files:\n" + "\n".join(missing), file=sys.stderr)
        return 1

    load_jobs()
    load_update_state()
    load_fleet_job()
    resume_interrupted_jobs()
    resume_fleet_job()

    server = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    server.daemon_threads = True
    print(f"Streaming Setup listening on 0.0.0.0:{args.port}")
    print(f"Web:     {WEB_DIR}")
    print(f"Nodes:   {NODES_FILE}")
    print(f"Scripts: {SCRIPTS_DIR}")
    print(f"Updater: {UPDATE_SCRIPT}")
    if SSH_PASSWORD_FILE.is_file():
        print(f"SSH auth: password only; host keys ignored; password file: {SSH_PASSWORD_FILE}")
    else:
        print(f"WARNING: required SSH password file missing: {SSH_PASSWORD_FILE}")
    if update_guard_snapshot():
        print("WARNING: interrupted-update recovery guard exists; open Admin view and recover before another update.")

    # Start the daily node pass in the background so the web UI is available
    # immediately even while the Pis themselves are still booting.
    launch_startup_autostart()

    def stop_handler(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop_handler)
    signal.signal(signal.SIGINT, stop_handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping Stream Master.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
