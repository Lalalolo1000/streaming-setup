#!/usr/bin/env python3
"""Stream Master: minimal HTTP UI + SSH controller for Raspberry Pi stream nodes."""

from __future__ import annotations

import argparse
import fcntl
import os
import json
import math
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
from contextlib import contextmanager, nullcontext
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
DESIRED_STATE_FILE = RUNTIME_DIR / "desired_streams.json"
MAINTENANCE_LOCK_FILE = RUNTIME_DIR / "maintenance.lock"
SSH_PASSWORD_FILE = Path.home() / ".config" / "stream-master" / "ssh-password"
YOUTUBE_COOKIE_FILE = APP_DIR / "youtube-cookies.txt"
REMOTE_YOUTUBE_COOKIE_FILE = "/tmp/stream-master/youtube-cookies.txt"
YOUTUBE_COOKIE_MAX_BYTES = 2 * 1024 * 1024
MASTER_IP = os.environ.get("STREAM_MASTER_MASTER_IP", "192.168.0.101")

SSH_CONNECT_TIMEOUT = 6
REBOOT_DOWN_TIMEOUT = 60
REBOOT_UP_TIMEOUT = 300
UPDATE_PROCESS_INACTIVITY_TIMEOUT = 1300
UPDATE_RECOVERY_GRACE = 720
STARTUP_AUTOSTART_TIMEOUT = int(os.environ.get("STREAM_MASTER_AUTOSTART_TIMEOUT", "900"))
STARTUP_AUTOSTART_RETRY = int(os.environ.get("STREAM_MASTER_AUTOSTART_RETRY", "30"))
STARTUP_AUTOSTART_STAGGER = float(os.environ.get("STREAM_MASTER_AUTOSTART_STAGGER", "5"))
# Give all Pis time to finish a real OS boot before the first Start request.
# The web UI itself is already available during this settle period.
STARTUP_AUTOSTART_INITIAL_DELAY = float(os.environ.get("STREAM_MASTER_AUTOSTART_INITIAL_DELAY", "60"))
RECOVERY_MONITOR_INTERVAL = float(os.environ.get("STREAM_MASTER_RECOVERY_INTERVAL", "30"))
RECOVERY_MONITOR_INITIAL_DELAY = float(os.environ.get("STREAM_MASTER_RECOVERY_INITIAL_DELAY", "30"))
RECOVERY_BOOT_SETTLE = float(os.environ.get("STREAM_MASTER_RECOVERY_BOOT_SETTLE", "15"))
RECOVERY_TCP_TIMEOUT = float(os.environ.get("STREAM_MASTER_RECOVERY_TCP_TIMEOUT", "1.0"))
RECOVERY_SAFETY_AUDIT_INTERVAL = float(os.environ.get("STREAM_MASTER_RECOVERY_AUDIT_INTERVAL", "300"))
POST_REBOOT_STREAM_SETTLE = float(os.environ.get("STREAM_MASTER_POST_REBOOT_SETTLE", "20"))
FLEET_REBOOT_BATCH_SIZE = max(1, int(os.environ.get("STREAM_MASTER_REBOOT_BATCH_SIZE", "4")))
FLEET_REBOOT_BATCH_PAUSE = float(os.environ.get("STREAM_MASTER_REBOOT_BATCH_PAUSE", "8"))

SCRIPT_ACTIONS = {"start", "check", "probe", "logs", "kill", "reboot", "shutdown"}
NORMAL_ACTIONS = {"start", "check", "probe", "logs", "kill"}

UPDATE_LOCK = threading.RLock()
CONFIG_LOCK = threading.RLock()
NODE_LOCKS_LOCK = threading.RLock()
JOBS_LOCK = threading.RLock()
FLEET_LOCK = threading.RLock()
DESIRED_LOCK = threading.RLock()
RECOVERY_LOCK = threading.RLock()
HEALTH_LOCK = threading.RLock()
NODE_LOCKS: dict[str, threading.Lock] = {}
NODE_JOBS: dict[str, dict] = {}
FLEET_JOB: dict = {"running": False, "kind": None, "stage": None, "message": None, "started_at": None, "finished_at": None, "ok": None}
DESIRED_STREAMS: dict[str, str] = {}
RECOVERY_NODES: dict[str, dict] = {}
HEALTH_CACHE: dict[str, dict] = {}
UPDATE_PERSIST_LAST = 0.0
STARTUP_FIRST_PASS_DONE = threading.Event()
JOB_MAINTENANCE_HOLD_LOCK = threading.RLock()
JOB_MAINTENANCE_HOLD_FD: int | None = None
JOB_MAINTENANCE_HOLD_COUNT = 0

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


@contextmanager
def maintenance_lock(*, blocking: bool = False):
    """Cross-process lock shared with the Git updater and manual updater.

    Long maintenance operations hold this lock so Git cannot fast-forward the
    checkout while an OverlayFS update or fleet power operation is in flight.
    """
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(MAINTENANCE_LOCK_FILE, os.O_CREAT | os.O_RDWR, 0o600)
    flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
    acquired = False
    try:
        try:
            fcntl.flock(fd, flags)
            acquired = True
        except BlockingIOError as exc:
            raise NodeBusy("another maintenance/Git operation owns the global lock") from exc
        yield fd
    finally:
        # Close rather than issuing LOCK_UN explicitly. If this fd was passed to
        # a child updater, Linux keeps the flock attached to the shared open-file
        # description until the last inherited fd closes. That protects recovery
        # even if the controller disappears first.
        os.close(fd)


def acquire_job_maintenance_hold() -> None:
    """Hold the cross-process lock while one or more standalone power jobs run.

    Multiple node jobs in this Python process share a single flock, so two manual
    reboots do not fail merely because each other is active. Git still sees one
    exclusive lock for the whole period. Fleet/update paths own their own lock.
    """
    global JOB_MAINTENANCE_HOLD_FD, JOB_MAINTENANCE_HOLD_COUNT
    with JOB_MAINTENANCE_HOLD_LOCK:
        if JOB_MAINTENANCE_HOLD_COUNT == 0:
            RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
            fd = os.open(MAINTENANCE_LOCK_FILE, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                os.close(fd)
                raise NodeBusy("another maintenance/Git operation owns the global lock") from exc
            JOB_MAINTENANCE_HOLD_FD = fd
        JOB_MAINTENANCE_HOLD_COUNT += 1


def release_job_maintenance_hold() -> None:
    global JOB_MAINTENANCE_HOLD_FD, JOB_MAINTENANCE_HOLD_COUNT
    with JOB_MAINTENANCE_HOLD_LOCK:
        if JOB_MAINTENANCE_HOLD_COUNT <= 0:
            return
        JOB_MAINTENANCE_HOLD_COUNT -= 1
        if JOB_MAINTENANCE_HOLD_COUNT == 0 and JOB_MAINTENANCE_HOLD_FD is not None:
            fd = JOB_MAINTENANCE_HOLD_FD
            JOB_MAINTENANCE_HOLD_FD = None
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)


def durable_json_write(path: Path, value: object, mode: int = 0o600) -> None:
    """Atomic + fsynced write for configuration/safety-critical state."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    tmp.chmod(mode)
    os.replace(tmp, path)
    try:
        dir_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass


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
    durable_json_write(NODES_FILE, value, 0o600)


def read_nodes() -> list[dict]:
    ensure_local_nodes_file()
    try:
        value = json.loads(NODES_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    if not isinstance(value, list):
        raise ValueError("nodes.json must contain a JSON list")
    # Validate even when nodes.json was edited manually. Locks, desired state and
    # jobs are keyed by target, so accepting a malformed/duplicate target here
    # would be much more dangerous than rejecting the configuration early.
    normalized = [validate_node(item) for item in value]
    validate_nodes_collection(normalized)
    return normalized


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


def validate_nodes_collection(nodes: list[dict]) -> None:
    targets = [node_key(n) for n in nodes]
    duplicates = sorted({t for t in targets if targets.count(t) > 1})
    if duplicates:
        raise ValueError("duplicate SSH target(s): " + ", ".join(duplicates))
    masters = [n for n in nodes if is_master_node(n)]
    if len(masters) != 1:
        raise ValueError(f"configuration must contain exactly one master node; found {len(masters)}")


def write_nodes(nodes: list[dict]) -> None:
    validate_nodes_collection(nodes)
    if NODES_FILE.is_file():
        try:
            backup = NODES_FILE.with_name(NODES_FILE.name + ".bak")
            shutil.copy2(NODES_FILE, backup)
        except OSError:
            pass
    durable_json_write(NODES_FILE, nodes, 0o600)


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


def load_desired_states() -> None:
    value = read_json_file(DESIRED_STATE_FILE, {})
    with DESIRED_LOCK:
        DESIRED_STREAMS.clear()
        if isinstance(value, dict):
            for target, state in value.items():
                if isinstance(target, str) and state in {"running", "stopped"}:
                    DESIRED_STREAMS[target] = state


def save_desired_states() -> None:
    with DESIRED_LOCK:
        durable_json_write(DESIRED_STATE_FILE, DESIRED_STREAMS, 0o600)


def set_desired_states_bulk(states: dict[str, str]) -> None:
    with DESIRED_LOCK:
        for target, state in states.items():
            if state not in {"running", "stopped"}:
                raise ValueError("invalid desired stream state")
            DESIRED_STREAMS[target] = state
        durable_json_write(DESIRED_STATE_FILE, DESIRED_STREAMS, 0o600)


def set_desired_state(node: dict, state: str) -> None:
    if state not in {"running", "stopped"}:
        raise ValueError("invalid desired stream state")
    target = node_key(node)
    with DESIRED_LOCK:
        DESIRED_STREAMS[target] = state
        durable_json_write(DESIRED_STATE_FILE, DESIRED_STREAMS, 0o600)


def remove_desired_state(target: str) -> None:
    with DESIRED_LOCK:
        if target in DESIRED_STREAMS:
            DESIRED_STREAMS.pop(target, None)
            durable_json_write(DESIRED_STATE_FILE, DESIRED_STREAMS, 0o600)


def desired_state(node: dict) -> str:
    target = node_key(node)
    with DESIRED_LOCK:
        state = DESIRED_STREAMS.get(target)
    if state in {"running", "stopped"}:
        return state
    return "running" if str(node.get("url", "")).strip() else "stopped"


def desired_snapshot() -> dict[str, str]:
    with DESIRED_LOCK:
        return dict(DESIRED_STREAMS)


def cache_health(node: dict, data: dict[str, str], *, source: str = "probe") -> None:
    item = dict(data)
    item["CACHE_SOURCE"] = source
    item["CACHE_UPDATED_AT"] = str(now())
    with HEALTH_LOCK:
        HEALTH_CACHE[node_key(node)] = item


def cache_health_failure(node: dict, failure: str | None, output: str = "") -> None:
    reason = {
        "node_unreachable": "Node could not be checked",
        "ssh_auth_failed": "SSH authentication failed",
        "ssh_host_key_changed": "Saved SSH host key no longer matches this Pi",
        "ssh_failed": "SSH failed",
    }.get(failure or "", "Remote check failed")
    cache_health(node, {
        "STATUS": "stopped",
        "STREAM_HEALTH": failure or "remote_command_failed",
        "STREAM_REASON": reason,
        "STREAM_SOURCE": "unknown",
        "SELECTED_STREAM": "unknown",
        "STREAM_RETRY_IN": "0",
    }, source="error")


def health_snapshot() -> dict[str, dict]:
    with HEALTH_LOCK:
        return {k: dict(v) for k, v in HEALTH_CACHE.items()}


def fleet_running() -> bool:
    with FLEET_LOCK:
        return bool(FLEET_JOB.get("running"))


def save_fleet_job() -> None:
    with FLEET_LOCK:
        durable_json_write(FLEET_JOB_FILE, FLEET_JOB, 0o600)


def set_fleet_job(**changes) -> dict:
    with FLEET_LOCK:
        FLEET_JOB.update(changes)
        snapshot = dict(FLEET_JOB)
        durable_json_write(FLEET_JOB_FILE, FLEET_JOB, 0o600)
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
        durable_json_write(NODE_JOBS_FILE, NODE_JOBS, 0o600)


def set_job(target: str, **changes) -> dict:
    with JOBS_LOCK:
        job = NODE_JOBS.setdefault(target, {"target": target})
        job.update(changes)
        snapshot = dict(job)
        durable_json_write(NODE_JOBS_FILE, NODE_JOBS, 0o600)
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


def save_update_state(*, force: bool = False) -> None:
    # Update subprocesses can emit hundreds of lines. Keep the live UI state in
    # memory and persist at most every 5s; the separate safety guard is fsynced.
    global UPDATE_PERSIST_LAST
    t = time.monotonic()
    if not force and t - UPDATE_PERSIST_LAST < 5.0:
        return
    with UPDATE_LOCK:
        state = {k: v for k, v in UPDATE_STATE.items() if k != "lines"}
        state["lines"] = list(UPDATE_STATE.get("lines", []))[-500:]
    atomic_json_write(UPDATE_UI_FILE, state)
    UPDATE_PERSIST_LAST = t


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
        "-o", "LogLevel=ERROR",
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


def _run_local_script_unlocked(node: dict, action: str, *, quiet: bool = False) -> tuple[int, str, str | None]:
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
            failure = None if done.returncode == 0 else "remote_command_failed"
            if done.returncode == 0 and action == "start":
                cache_health(node, {"STATUS":"running","STREAM_HEALTH":"starting","STREAM_REASON":"start requested","STREAM_SOURCE":"unknown","SELECTED_STREAM":"unknown","STREAM_RETRY_IN":"0"}, source="action")
            elif done.returncode == 0 and action == "kill":
                cache_health(node, {"STATUS":"stopped","STREAM_HEALTH":"stopped","STREAM_REASON":"stream is not running","STREAM_SOURCE":"unknown","SELECTED_STREAM":"unknown","STREAM_RETRY_IN":"0"}, source="action")
            return done.returncode, output, failure

    script_path = SCRIPTS_DIR / f"{action}.sh"
    script = script_path.read_bytes()
    args: list[str] = []
    if action == "start":
        if not str(node.get("url", "")).strip():
            return 2, "Kein Stream-Link konfiguriert.", "remote_command_failed"
        args = [str(node.get("url", "")), str(node.get("quality", "max480")), str(node.get("connector", "HDMI-A-1")), str(YOUTUBE_COOKIE_FILE)]
    command = ["bash", "-s", "--", *args]
    timeout = 30 if action == "start" else 20
    name = str(node.get("name") or node_key(node))
    if not quiet:
        print(f"\n[{name}] LOCAL {action}.sh", flush=True)
    try:
        local_env = dict(os.environ)
        local_env.setdefault("STREAM_MASTER_WORKDIR", "/dev/shm/stream-master")
        done = subprocess.run(
            command,
            input=script,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
            cwd=APP_DIR,
            env=local_env,
        )
        output = done.stdout.decode("utf-8", errors="replace").strip()
        if not quiet:
            print(output or "(no output)", flush=True)
            print(f"[{name}] local rc={done.returncode}", flush=True)
        failure = None if done.returncode == 0 else "remote_command_failed"
        if action in {"check", "probe"}:
            if done.returncode == 0:
                cache_health(node, parse_machine_output(output), source=action)
            else:
                cache_health_failure(node, failure, output)
        return done.returncode, output, failure
    except subprocess.TimeoutExpired as exc:
        output = "Local script timed out"
        if exc.stdout:
            try:
                output = exc.stdout.decode("utf-8", errors="replace").strip() + "\n" + output
            except AttributeError:
                pass
        return 124, output.strip(), "remote_command_failed"


def _run_script_unlocked(node: dict, action: str, *, quiet: bool = False) -> tuple[int, str, str | None]:
    if action not in SCRIPT_ACTIONS:
        raise ValueError("unknown action")
    if is_master_node(node):
        return _run_local_script_unlocked(node, action, quiet=quiet)
    script_path = SCRIPTS_DIR / f"{action}.sh"
    script = script_path.read_bytes()
    payload = script
    args: list[str] = []
    if action == "start":
        url = str(node.get("url", "")).strip()
        if not url:
            return 2, "Kein Stream-Link konfiguriert.", "remote_command_failed"
        args = [url, str(node.get("quality", "max480")), str(node.get("connector", "HDMI-A-1")), REMOTE_YOUTUBE_COOKIE_FILE]

        # youtube-cookies.txt is a LOCAL, Git-ignored secret on the master. For
        # YouTube starts it is prepended to the same SSH stdin payload as start.sh
        # and written only to /tmp on the worker. This works with OverlayFS, does
        # not require SCP, and never puts cookie contents in command-line args.
        is_youtube = bool(re.search(r"(?:youtube\.com|youtu\.be)/", url, re.I))
        prelude = bytearray(b"umask 077\nmkdir -p /tmp/stream-master\n")
        if is_youtube and YOUTUBE_COOKIE_FILE.is_file():
            cookie = YOUTUBE_COOKIE_FILE.read_bytes()
            if len(cookie) > YOUTUBE_COOKIE_MAX_BYTES:
                return 2, f"youtube-cookies.txt ist größer als {YOUTUBE_COOKIE_MAX_BYTES} Bytes.", "remote_command_failed"
            marker = f"__STREAMING_SETUP_COOKIE_{os.getpid()}_{time.time_ns()}__"
            while marker.encode() in cookie:
                marker += "X"
            prelude.extend(f"cat > {shlex.quote(REMOTE_YOUTUBE_COOKIE_FILE)} <<'{marker}'\n".encode())
            prelude.extend(cookie)
            if cookie and not cookie.endswith(b"\n"):
                prelude.extend(b"\n")
            prelude.extend(f"{marker}\nchmod 600 {shlex.quote(REMOTE_YOUTUBE_COOKIE_FILE)}\n".encode())
        else:
            # Remove a cookie from a previous YouTube configuration if the local
            # secret was deleted or this node is now assigned a different source.
            prelude.extend(f"rm -f {shlex.quote(REMOTE_YOUTUBE_COOKIE_FILE)}\n".encode())
        payload = bytes(prelude) + script
    remote = "bash -s --" + (" " + " ".join(shlex.quote(x) for x in args) if args else "")
    command = ssh_base(node) + [remote]
    timeout = 30 if action == "start" else 20
    name = str(node.get("name") or node_key(node))
    if not quiet:
        print(f"\n[{name}] {action}.sh", flush=True)
    try:
        done = subprocess.run(
            command,
            input=payload,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
        output = done.stdout.decode("utf-8", errors="replace").strip()
        if not quiet:
            print(output or "(no output)", flush=True)
            print(f"[{name}] rc={done.returncode}", flush=True)
        failure = classify_ssh_failure(output, done.returncode)
        if action in {"check", "probe"}:
            if done.returncode == 0:
                cache_health(node, parse_machine_output(output), source=action)
            else:
                cache_health_failure(node, failure, output)
        elif action == "start" and done.returncode == 0:
            cache_health(node, {"STATUS":"running","STREAM_HEALTH":"starting","STREAM_REASON":"start requested","STREAM_SOURCE":"unknown","SELECTED_STREAM":"unknown","STREAM_RETRY_IN":"0"}, source="action")
        elif action == "kill" and done.returncode == 0:
            cache_health(node, {"STATUS":"stopped","STREAM_HEALTH":"stopped","STREAM_REASON":"stream is not running","STREAM_SOURCE":"unknown","SELECTED_STREAM":"unknown","STREAM_RETRY_IN":"0"}, source="action")
        return done.returncode, output, failure
    except subprocess.TimeoutExpired as exc:
        output = "SSH/script timed out"
        if exc.stdout:
            try:
                output = exc.stdout.decode("utf-8", errors="replace").strip() + "\n" + output
            except AttributeError:
                pass
        if action in {"check", "probe"}:
            cache_health_failure(node, "node_unreachable", output)
        return 124, output.strip(), "node_unreachable"


def _run_script_guarded(node: dict, action: str, *, wait_for_lock: float = 0.0, quiet: bool = False) -> tuple[int, str, str | None]:
    """Run one node action while serializing short master-side commands.

    Normal UI actions stay non-blocking and return NodeBusy if another command
    is already in flight. Config application is different: changing a stream URL
    must win over a background status check/start request, so it may wait briefly
    for this lock and then replace the watcher with the freshly saved config.
    Long-lived reboot/shutdown/update/fleet jobs are still never interrupted.
    """
    with UPDATE_LOCK:
        if UPDATE_STATE["running"]:
            raise RuntimeError("software update is currently running")
    if fleet_running():
        raise RuntimeError("fleet power operation is currently running")
    target = node_key(node)
    if active_node_job_for(target):
        raise NodeBusy("node operation already in progress")
    lock = node_lock(node)
    acquired = lock.acquire(timeout=max(0.0, wait_for_lock)) if wait_for_lock > 0 else lock.acquire(blocking=False)
    if not acquired:
        raise NodeBusy("node is busy")
    try:
        # Re-check everything after taking the node lock. A reboot/update may
        # have started while a config-save request was waiting for a status check.
        with UPDATE_LOCK:
            if UPDATE_STATE["running"]:
                raise RuntimeError("software update is currently running")
        if fleet_running():
            raise RuntimeError("fleet power operation is currently running")
        if active_node_job_for(target):
            raise NodeBusy("node operation already in progress")
        return _run_script_unlocked(node, action, quiet=quiet)
    finally:
        lock.release()


def run_script(node: dict, action: str) -> tuple[int, str, str | None]:
    return _run_script_guarded(node, action, wait_for_lock=0.0)


def run_script_after_config_change(node: dict, action: str) -> tuple[int, str, str | None]:
    # A status check or an already-issued Start request can occupy the per-node
    # lock for a few seconds. URL edits should not fail with "node busy" because
    # of that. Wait long enough for the current SSH command to finish, then run
    # start.sh, which kills/replaces the old supervisor + Streamlink + VLC group.
    return _run_script_guarded(node, action, wait_for_lock=45.0)


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
            if code != 0:
                raise RuntimeError("Master rebooted, but final stream check failed: " + (output or failure or "unknown error"))
            checked = parse_machine_output(output)
            if str(node.get("url", "")).strip() and checked.get("STATUS") != "running":
                raise RuntimeError("Master rebooted, but its supervisor is not running")
            health = checked.get("STREAM_HEALTH", "unknown")
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

            set_job(target, stage="boot settle", message=f"Pi is back; waiting {POST_REBOOT_STREAM_SETTLE:g}s before starting the stream")
            if POST_REBOOT_STREAM_SETTLE > 0:
                time.sleep(POST_REBOOT_STREAM_SETTLE)
            set_job(target, stage="starting stream", message="Pi is ready; starting configured stream")
            if str(node.get("url", "")).strip():
                code, output, failure = _run_script_unlocked(node, "start")
                if code != 0:
                    raise RuntimeError("Pi rebooted, but stream start failed: " + (output or failure or "unknown error"))
                time.sleep(2)
            code, output, failure = _run_script_unlocked(node, "check")
            if code != 0:
                raise RuntimeError("Pi rebooted, but final stream check failed: " + (output or failure or "unknown error"))
            checked = parse_machine_output(output)
            if str(node.get("url", "")).strip() and checked.get("STATUS") != "running":
                raise RuntimeError("Pi rebooted, but its supervisor is not running")
            health = checked.get("STREAM_HEALTH", "unknown")
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
    if kind == "reboot":
        set_desired_state(node, "running" if str(node.get("url", "")).strip() else "stopped")
    else:
        set_desired_state(node, "stopped")
    with UPDATE_LOCK:
        if UPDATE_STATE["running"]:
            raise RuntimeError("software update is currently running")
        if fleet_running() and not _from_fleet:
            raise RuntimeError("fleet power operation is currently running")
        if active_node_job_for(target):
            raise NodeBusy("node operation already in progress")
        set_job(target, running=True, ok=None, kind=kind, stage="queued", message=f"{kind} queued", started_at=now(), finished_at=None, name=node.get("name"))
    worker = _reboot_worker if kind == "reboot" else _shutdown_worker

    def run_job() -> None:
        # Fleet power already owns the cross-process maintenance lock. Standalone
        # power jobs share one process-level hold so Git cannot fast-forward while
        # any reboot/shutdown is still being supervised.
        held = False
        try:
            if not _from_fleet:
                acquire_job_maintenance_hold()
                held = True
            worker(dict(node), False) if kind == "reboot" else worker(dict(node))
        except NodeBusy as exc:
            set_job(target, running=False, ok=False, stage="failed", message=str(exc), finished_at=now())
        finally:
            if held:
                release_job_maintenance_hold()

    threading.Thread(target=run_job, daemon=True, name=f"node-{kind}-{target}").start()
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
            def resume_reboot(n=dict(node), t=target):
                held = False
                try:
                    acquire_job_maintenance_hold()
                    held = True
                    _reboot_worker(n, True)
                except NodeBusy as exc:
                    set_job(t, running=False, ok=False, stage="failed", message=str(exc), finished_at=now())
                finally:
                    if held:
                        release_job_maintenance_hold()
            threading.Thread(target=resume_reboot, daemon=True, name=f"resume-reboot-{target}").start()
        elif is_master_node(node) and job.get("kind") == "shutdown":
            set_job(target, running=False, ok=True, stage="complete", message="Master was previously shut down and has now booted again", finished_at=now())
        else:
            set_job(target, running=False, ok=False, stage="interrupted", message="master restarted during shutdown request; check node state", finished_at=now())


def _wait_for_targets_success(targets: list[str], timeout: int) -> tuple[bool, list[str]]:
    """Wait until jobs finish and require every job to report ok=True."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with JOBS_LOCK:
            running = [t for t in targets if NODE_JOBS.get(t, {}).get("running")]
            if not running:
                failed = [t for t in targets if NODE_JOBS.get(t, {}).get("ok") is not True]
                return not failed, failed
        time.sleep(2)
    with JOBS_LOCK:
        running = [t for t in targets if NODE_JOBS.get(t, {}).get("running")]
    return False, running


def _fleet_power_worker(kind: str, resume: bool = False) -> None:
    try:
        # On resume after the master reboot/power cycle the old process lock is
        # already gone; completion only needs to observe the persisted master job.
        lock_context = maintenance_lock(blocking=False) if not resume else nullcontext()
        with lock_context:
            nodes = read_nodes()
            master = master_node(nodes)
            workers = [(i, n) for i, n in enumerate(nodes) if not is_master_node(n)]

            if not resume:
                set_fleet_job(running=True, kind=kind, stage="workers", message=f"{kind}: worker nodes first", started_at=now(), finished_at=None, ok=None)
                if kind == "reboot":
                    # Small batches prevent 23 identical Pis from returning and
                    # opening 23 source streams at nearly the same moment.
                    for batch_start in range(0, len(workers), FLEET_REBOOT_BATCH_SIZE):
                        batch = workers[batch_start:batch_start + FLEET_REBOOT_BATCH_SIZE]
                        batch_no = batch_start // FLEET_REBOOT_BATCH_SIZE + 1
                        total_batches = math.ceil(len(workers) / FLEET_REBOOT_BATCH_SIZE)
                        set_fleet_job(stage="workers", message=f"reboot: worker batch {batch_no}/{total_batches}")
                        for i, node in batch:
                            start_node_job(i, kind, _from_fleet=True)
                            time.sleep(0.5)
                        targets = [node_key(n) for _, n in batch]
                        ok, failed = _wait_for_targets_success(targets, 600)
                        if not ok:
                            raise RuntimeError("worker reboot failed; master stays online: " + ", ".join(failed))
                        if batch_start + FLEET_REBOOT_BATCH_SIZE < len(workers) and FLEET_REBOOT_BATCH_PAUSE > 0:
                            time.sleep(FLEET_REBOOT_BATCH_PAUSE)
                else:
                    # Shutdown can be issued quickly, but the master is powered off
                    # only after every worker was positively observed offline.
                    for i, node in workers:
                        start_node_job(i, kind, _from_fleet=True)
                        time.sleep(0.5)
                    targets = [node_key(n) for _, n in workers]
                    ok, failed = _wait_for_targets_success(targets, 240)
                    if not ok:
                        raise RuntimeError("worker shutdown not confirmed; master stays online: " + ", ".join(failed))

                set_fleet_job(stage="master", message=f"{kind}: all workers succeeded; master is last")
                if master is None:
                    set_fleet_job(running=False, ok=True, stage="complete", message="fleet power operation complete", finished_at=now())
                    return
                mi = next(i for i, n in enumerate(nodes) if is_master_node(n))
                start_node_job(mi, kind, _from_fleet=True)
                return

            mt = master_node(nodes)
            if mt:
                ok, failed = _wait_for_targets_success([node_key(mt)], 180)
                if not ok:
                    raise RuntimeError("master power job did not complete successfully: " + ", ".join(failed))
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
    snapshot = fleet_snapshot()
    # Only the intentional final master power action is resumed automatically.
    # If the controller crashed during the worker phase, resumed per-node jobs may
    # still complete, but the master deliberately stays online for inspection.
    if snapshot.get("stage") != "master":
        set_fleet_job(
            running=False, ok=False, stage="interrupted",
            message="controller restarted during worker fleet operation; master left online",
            finished_at=now(),
        )
        return
    threading.Thread(
        target=_fleet_power_worker,
        kwargs={"kind": str(snapshot.get("kind") or "reboot"), "resume": True},
        daemon=True,
        name="resume-fleet-power",
    ).start()


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
            validate_nodes_collection(nodes)
            write_nodes(nodes)

        if old is not None and node_key(old) != node_key(node):
            remove_desired_state(node_key(old))
        # Saving a non-empty URL with Apply means the operator wants this stream
        # running. Clearing the URL means it must remain stopped. A rename-only
        # edit preserves the existing desired state.
        if apply and (old is None or any(old.get(key) != node.get(key) for key in ("target", "port", "url", "quality", "connector"))):
            set_desired_state(node, "running" if node["url"] else "stopped")
        elif old is None:
            set_desired_state(node, "running" if node["url"] else "stopped")

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
                    old_code, old_output, _ = run_script_after_config_change(old, "kill")
                    if old_code != 0:
                        old_stop_warning = old_output or "old target could not be stopped"
                except Exception as exc:
                    old_stop_warning = str(exc)
            if not node["url"]:
                # Clearing a URL means "no stream configured". Stop the current
                # stream on the still-selected target instead of trying to start
                # an empty URL.
                code, output, failure = run_script_after_config_change(node, "kill")
            else:
                code, output, failure = run_script_after_config_change(node, "start")
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


def _run_master_local_package_update(maintenance_fd: int | None = None) -> int:
    with UPDATE_LOCK:
        UPDATE_STATE["current_node"] = "Master (.101)"
        UPDATE_STATE["stage"] = "updating master locally"
        UPDATE_STATE["step"] = 0
    save_update_state()
    if not MASTER_LOCAL_UPDATE_SCRIPT.is_file():
        _append_update_line("MASTER LOCAL UPDATE ERROR: update_master_local.sh is missing.\n")
        return 1
    _append_update_line("\nMASTER LOCAL UPDATE: stop Stream 01 if active, update VLC + Streamlink, then restore it.\n")
    proc: subprocess.Popen[str] | None = None
    try:
        pass_fds: tuple[int, ...] = (maintenance_fd,) if maintenance_fd is not None else ()
        proc = subprocess.Popen(
            ["/bin/bash", str(MASTER_LOCAL_UPDATE_SCRIPT)],
            cwd=APP_DIR, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, start_new_session=True, pass_fds=pass_fds,
        )
        assert proc.stdout is not None
        q: queue.Queue[str | None] = queue.Queue()
        def reader() -> None:
            try:
                for line in proc.stdout:
                    q.put(line)
            finally:
                q.put(None)
        threading.Thread(target=reader, daemon=True, name="master-local-update-output").start()
        last_output = time.monotonic()
        reader_done = False
        while True:
            try:
                item = q.get(timeout=0.5)
            except queue.Empty:
                item = "__NO_LINE__"
            if item is None:
                reader_done = True
            elif item != "__NO_LINE__":
                _append_update_line("[master-local] " + item)
                last_output = time.monotonic()
            if proc.poll() is not None and reader_done and q.empty():
                return proc.wait()
            if time.monotonic() - last_output > 1200:
                _append_update_line("MASTER LOCAL WATCHDOG: no output for 20 minutes; terminating update.\n")
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    return proc.wait(timeout=60) or 124
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()
                    return 124
    except Exception as exc:
        _append_update_line(f"MASTER LOCAL UPDATE ERROR: {exc}\n")
        if proc is not None and proc.poll() is None:
            try: os.killpg(proc.pid, signal.SIGTERM)
            except Exception: pass
        return 1


def _run_update_process(command: list[str], maintenance_fd: int | None = None) -> None:
    rc = 1
    proc: subprocess.Popen[str] | None = None
    try:
        child_env = dict(os.environ)
        pass_fds: tuple[int, ...] = ()
        if maintenance_fd is not None:
            # The updater inherits the already-locked file description. If the
            # controller itself is stopped/crashes mid-update, the child keeps the
            # flock while its signal handler relocks/verifies the worker.
            child_env["STREAM_MASTER_MAINTENANCE_LOCK_FD"] = str(maintenance_fd)
            pass_fds = (maintenance_fd,)
        proc = subprocess.Popen(
            command,
            cwd=APP_DIR,
            env=child_env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,
            pass_fds=pass_fds,
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
            if update_guard_snapshot():
                _append_update_line("MASTER LOCAL UPDATE SKIPPED: a worker recovery guard is still open; recover that worker first.\n")
            else:
                local_rc = _run_master_local_package_update(maintenance_fd)
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
        _finish_update_state(rc)


def _finish_update_state(rc: int) -> None:
    with UPDATE_LOCK:
        UPDATE_STATE["running"] = False
        UPDATE_STATE["returncode"] = rc
        UPDATE_STATE["finished_at"] = now()
        UPDATE_STATE["stage"] = "complete" if rc == 0 else "failed"
    save_update_state(force=True)


def _run_master_only_after_barrier(master_node: dict) -> None:
    rc = 1
    try:
        # A master-only package update is local, but it must obey the same node
        # action and cross-process maintenance barriers as worker maintenance.
        lock = node_lock(master_node)
        if not lock.acquire(timeout=45):
            raise RuntimeError(f"timed out waiting for active node action on {node_key(master_node)}")
        lock.release()
        with maintenance_lock(blocking=False) as maintenance_fd:
            rc = _run_master_local_package_update(maintenance_fd)
    except Exception as exc:
        _append_update_line(f"MASTER LOCAL UPDATE ERROR: {exc}\n")
        rc = 1
    finally:
        _finish_update_state(rc)


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
        save_update_state(force=True)
        return
    try:
        with maintenance_lock(blocking=False) as maintenance_fd:
            _run_update_process(command, maintenance_fd)
    except NodeBusy as exc:
        _append_update_line(f"MASTER UPDATE ERROR: {exc}\n")
        with UPDATE_LOCK:
            UPDATE_STATE["running"] = False
            UPDATE_STATE["returncode"] = 1
            UPDATE_STATE["finished_at"] = now()
            UPDATE_STATE["stage"] = "failed before updater launch"
        save_update_state(force=True)


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
        command = [sys.executable, "-u", str(UPDATE_SCRIPT), "--all", "--start-after", "--respect-desired-state", "--continue-on-error"]
        mode = "all"
        requested_node = None
        barrier_nodes = workers
    else:
        if update_guard_snapshot():
            raise RuntimeError("an interrupted-update recovery guard exists; recover it before starting another update")
        if node_index is None or not 0 <= node_index < len(nodes):
            raise ValueError("invalid node index")
        node = nodes[node_index]
        selector = str(node.get("target", ""))
        if is_master_node(node):
            # The master stays writable permanently. Its per-node button uses
            # only the local VLC/Streamlink updater: no OverlayFS toggle and no
            # reboot. Preserve the operator's desired stream state.
            command = None
            mode = "master"
            requested_node = str(node.get("name") or "Master (.101)")
            barrier_nodes = [node]
        else:
            command = [sys.executable, "-u", str(UPDATE_SCRIPT), "--node", selector, "--start-after", "--respect-desired-state"]
            mode = "one"
            requested_node = str(node.get("name") or selector)
            barrier_nodes = [node]

    # Software maintenance preserves the operator's persisted desired stream
    # state. The worker updater receives --respect-desired-state; the master-local
    # updater restores Stream 01 only when it was active before maintenance.

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
            "stage": "starting recovery" if recover else ("starting master-local update" if mode == "master" else "starting updater"),
            "step": 0,
            "total_steps": 1 if mode == "master" or recover else 5,
            "started_at": now(),
            "finished_at": None,
            "returncode": None,
            "lines": (["$ " + shlex.join(command) + "\n"] if command is not None else ["$ ./update_master_local.sh\n"]),
        })
    save_update_state(force=True)
    if mode == "master":
        threading.Thread(target=_run_master_only_after_barrier, args=(barrier_nodes[0],), daemon=True, name="master-local-updater").start()
    else:
        assert command is not None
        threading.Thread(target=_run_update_after_barrier, args=(command, barrier_nodes), daemon=True, name="pi-updater").start()
    return _update_snapshot()


def _find_node_by_target(target: str) -> dict | None:
    for node in read_nodes():
        if node_key(node) == target:
            return node
    return None


def _recovery_set(target: str, **changes) -> None:
    with RECOVERY_LOCK:
        item = RECOVERY_NODES.setdefault(target, {})
        item.update(changes)


def recovery_snapshot() -> dict[str, dict]:
    with RECOVERY_LOCK:
        return {k: dict(v) for k, v in RECOVERY_NODES.items()}


def _powerloss_recovery_worker(target: str, boot_settle: bool = True) -> None:
    """Recover one worker after a real offline -> online transition.

    We deliberately do not restart a healthy retrying supervisor. probe.sh first
    verifies whether the supervisor survived. Only STATUS!=running causes a
    redeploy, and only when the persisted desired state is running.
    """
    _recovery_set(target, recovering=True, pending=True, stage="boot settling" if boot_settle else "safety audit", updated_at=now())
    if boot_settle:
        time.sleep(max(0.0, RECOVERY_BOOT_SETTLE))
    try:
        node = _find_node_by_target(target)
        if node is None:
            _recovery_set(target, recovering=False, pending=False, stage="node removed", updated_at=now())
            return
        if not STARTUP_FIRST_PASS_DONE.is_set():
            _recovery_set(target, recovering=False, pending=True, stage="waiting for startup first pass", updated_at=now())
            return
        if desired_state(node) != "running" or not str(node.get("url", "")).strip():
            _recovery_set(target, recovering=False, pending=False, stage="intentionally stopped", updated_at=now())
            return
        with UPDATE_LOCK:
            updating = bool(UPDATE_STATE.get("running"))
        if updating or fleet_running() or active_node_job_for(target):
            _recovery_set(target, recovering=False, pending=True, stage="waiting for maintenance", updated_at=now())
            return

        _recovery_set(target, stage="checking supervisor", updated_at=now())
        try:
            code, output, failure = _run_script_guarded(node, "probe", wait_for_lock=15.0, quiet=True)
        except (NodeBusy, RuntimeError) as exc:
            _recovery_set(target, recovering=False, pending=True, stage=f"check deferred: {exc}", updated_at=now())
            return
        data = parse_machine_output(output) if code == 0 else {}
        if code != 0:
            # A transport failure shortly after boot is worth retrying on the next
            # monitor pass. Persistent auth/remote-script failures are not: retry
            # those only on the normal five-minute audit to avoid SSH hammering.
            quick_retry = failure in {"node_unreachable", "ssh_failed"}
            _recovery_set(
                target, recovering=False, pending=quick_retry,
                stage=(f"SSH not ready: {failure or code}" if quick_retry else f"probe failed; next safety audit: {failure or code}"),
                last_audit_at=now(), updated_at=now(),
            )
            return
        if data.get("STATUS") == "running":
            _recovery_set(target, recovering=False, pending=False, stage="supervisor already running", recovered_at=now(), last_audit_at=now(), updated_at=now())
            return

        _recovery_set(target, stage="restarting stream", updated_at=now())
        try:
            code, output, failure = _run_script_guarded(node, "start", wait_for_lock=15.0)
        except (NodeBusy, RuntimeError) as exc:
            _recovery_set(target, recovering=False, pending=True, stage=f"start deferred: {exc}", updated_at=now())
            return
        if code == 0:
            print(f"Power-loss recovery: {target} returned without a supervisor; stream restarted.", flush=True)
            _recovery_set(target, recovering=False, pending=False, stage="stream restarted", recovered_at=now(), last_audit_at=now(), updated_at=now())
        else:
            quick_retry = failure in {"node_unreachable", "ssh_failed"}
            _recovery_set(
                target, recovering=False, pending=quick_retry,
                stage=(f"restart transport failed: {failure or code}" if quick_retry else f"restart failed; next safety audit: {failure or code}"),
                last_audit_at=now(), updated_at=now(),
            )
    except Exception as exc:
        print(f"Power-loss recovery error for {target}: {exc}", flush=True)
        _recovery_set(target, recovering=False, pending=True, stage=f"error: {exc}", updated_at=now())


def powerloss_recovery_monitor() -> None:
    """Watch worker reachability with cheap TCP probes, not recurring SSH.

    Polling port 22 every 30 seconds is enough to notice a Pi reboot without
    creating authenticated SSH sessions. A real SSH check is only performed
    after a worker has been observed offline and later online, or while such a
    recovery remains pending.
    """
    if os.environ.get("STREAM_MASTER_POWERLOSS_RECOVERY", "1") not in {"1", "yes", "true", "on"}:
        print("Power-loss recovery monitor disabled by STREAM_MASTER_POWERLOSS_RECOVERY.")
        return
    if RECOVERY_MONITOR_INITIAL_DELAY > 0:
        time.sleep(RECOVERY_MONITOR_INITIAL_DELAY)

    # Baseline existing nodes without triggering recovery. Normal server-start
    # autostart is responsible for the initial boot of the installation.
    # Seed audit timestamps across one full interval so the first authenticated
    # audit sweep is staggered instead of all nodes becoming due at once.
    all_baseline_nodes = read_nodes()
    baseline_now = now()
    baseline_count = max(1, len(all_baseline_nodes))
    for index, node in enumerate(all_baseline_nodes):
        target = node_key(node)
        up = True if is_master_node(node) else tcp_up(node, timeout=RECOVERY_TCP_TIMEOUT)
        stagger_age = RECOVERY_SAFETY_AUDIT_INTERVAL * ((index + 1) / baseline_count)
        _recovery_set(
            target,
            last_up=up,
            seen_down=False,
            pending=False,
            recovering=False,
            stage="online" if up else "offline baseline",
            last_audit_at=baseline_now - stagger_age,
            updated_at=baseline_now,
        )

    print(
        f"Power-loss recovery monitor active: TCP/{int(RECOVERY_MONITOR_INTERVAL)}s, "
        f"SSH after offline→online plus one safety audit per node about every {int(RECOVERY_SAFETY_AUDIT_INTERVAL // 60)} min; "
        f"boot settle {int(RECOVERY_BOOT_SETTLE)}s.",
        flush=True,
    )
    while True:
        cycle_started = time.monotonic()
        try:
            all_nodes = read_nodes()
            nodes = [n for n in all_nodes if not is_master_node(n)]
            known_targets = {node_key(n) for n in all_nodes}
            with RECOVERY_LOCK:
                for stale in list(RECOVERY_NODES):
                    if stale not in known_targets:
                        RECOVERY_NODES.pop(stale, None)

            # Avoid even liveness probing during explicit fleet power operations;
            # those jobs already own reboot/shutdown recovery.
            if not fleet_running():
                for node in nodes:
                    target = node_key(node)
                    up = tcp_up(node, timeout=RECOVERY_TCP_TIMEOUT)
                    with RECOVERY_LOCK:
                        previous = dict(RECOVERY_NODES.get(target) or {})
                    was_up = previous.get("last_up")
                    seen_down = bool(previous.get("seen_down"))
                    pending = bool(previous.get("pending"))
                    recovering = bool(previous.get("recovering"))

                    if not up:
                        if was_up is not False:
                            print(f"Power-loss monitor: {target} is offline.", flush=True)
                            cache_health_failure(node, "node_unreachable")
                        _recovery_set(target, last_up=False, seen_down=True, stage="offline", updated_at=now())
                        continue

                    # Node is reachable on TCP/22.
                    if was_up is False and seen_down:
                        print(f"Power-loss monitor: {target} is reachable again; scheduling one recovery check.", flush=True)
                        pending = True
                        _recovery_set(target, last_up=True, pending=True, stage="returned", updated_at=now())
                    else:
                        _recovery_set(target, last_up=True, updated_at=now())

                    if pending and not recovering and STARTUP_FIRST_PASS_DONE.is_set() and desired_state(node) == "running" and str(node.get("url", "")).strip():
                        _recovery_set(target, recovering=True, pending=True, seen_down=False, stage="recovery queued", updated_at=now())
                        threading.Thread(target=_powerloss_recovery_worker, args=(target,), daemon=True, name=f"powerloss-recovery-{target}").start()
                    elif pending and desired_state(node) != "running":
                        _recovery_set(target, pending=False, seen_down=False, recovering=False, stage="intentionally stopped", updated_at=now())

                # Safety net for the rare case where a very fast reboot happens
                # entirely between two 30-second TCP probes. Every worker whose
                # desired state is running receives an authenticated supervisor
                # audit about once per RECOVERY_SAFETY_AUDIT_INTERVAL (5 minutes
                # by default). Audits are spread across monitor cycles rather than
                # opening SSH sessions to all workers at once.
                audit_now = now()
                due_audits: list[tuple[float, str]] = []
                eligible_count = 0
                with UPDATE_LOCK:
                    updating = bool(UPDATE_STATE.get("running"))
                if not updating and STARTUP_FIRST_PASS_DONE.is_set():
                    for node in all_nodes:
                        target = node_key(node)
                        with RECOVERY_LOCK:
                            st = dict(RECOVERY_NODES.get(target) or {})
                        last_audit = float(st.get("last_audit_at") or 0.0)
                        eligible = (
                            st.get("last_up") is True
                            and not st.get("pending")
                            and not st.get("recovering")
                            and desired_state(node) == "running"
                            and bool(str(node.get("url", "")).strip())
                            and not active_node_job_for(target)
                        )
                        if not eligible:
                            continue
                        eligible_count += 1
                        if audit_now - last_audit >= RECOVERY_SAFETY_AUDIT_INTERVAL:
                            due_audits.append((last_audit, target))

                if due_audits:
                    # Capacity required to audit every eligible worker within the
                    # requested interval while retaining the cheap 30-second TCP
                    # monitor. With 23 workers / 5 minutes this is 3 audits per
                    # cycle, i.e. roughly one SSH check every 10 seconds fleet-wide.
                    interval = max(1.0, RECOVERY_SAFETY_AUDIT_INTERVAL)
                    slots = max(1, math.ceil(eligible_count * RECOVERY_MONITOR_INTERVAL / interval))
                    selected = sorted(due_audits)[:slots]
                    spacing = RECOVERY_MONITOR_INTERVAL / max(1, len(selected))
                    for audit_index, (_last_audit, audit_target) in enumerate(selected):
                        _recovery_set(
                            audit_target,
                            recovering=True,
                            pending=True,
                            stage="safety audit queued",
                            # Reserve this audit slot immediately so a slow SSH
                            # handshake cannot cause the same node to be queued twice.
                            last_audit_at=audit_now,
                            updated_at=audit_now,
                        )

                        def run_staggered_audit(target=audit_target, delay=audit_index * spacing):
                            if delay > 0:
                                time.sleep(delay)
                            _powerloss_recovery_worker(target, False)

                        threading.Thread(
                            target=run_staggered_audit,
                            daemon=True,
                            name=f"powerloss-audit-{audit_target}",
                        ).start()
        except Exception as exc:
            print(f"Power-loss monitor error: {exc}", flush=True)

        elapsed = time.monotonic() - cycle_started
        time.sleep(max(1.0, RECOVERY_MONITOR_INTERVAL - elapsed))


def launch_powerloss_recovery_monitor() -> None:
    threading.Thread(target=powerloss_recovery_monitor, daemon=True, name="powerloss-monitor").start()


def _write_startup_state(**changes) -> None:
    state = read_json_file(STARTUP_STATE_FILE, {})
    if not isinstance(state, dict):
        state = {}
    state.update(changes)
    atomic_json_write(STARTUP_STATE_FILE, state)


def schedule_lightweight_probe(node: dict, delay: float = 12.0) -> None:
    """Refresh cached health once after an explicit automatic start.

    This is a one-shot probe, not recurring browser SSH. It lets the dashboard
    show Live/retrying soon after boot without waiting up to five minutes for the
    regular staggered audit.
    """
    snapshot = dict(node)
    def worker() -> None:
        if delay > 0:
            time.sleep(delay)
        try:
            _run_script_guarded(snapshot, "probe", wait_for_lock=5.0, quiet=True)
        except (NodeBusy, RuntimeError, OSError, subprocess.SubprocessError):
            pass
    threading.Thread(target=worker, daemon=True, name=f"post-start-probe-{node_key(snapshot)}").start()


def _autostart_attempt(node: dict, attempts: int) -> dict:
    """Try to (re)start one configured stream exactly once.

    Startup orchestration deliberately does not sit on one unavailable node.
    Unreachable/busy nodes are marked pending and revisited after the first
    staggered pass across the whole installation.
    """
    name = str(node.get("name") or node_key(node))
    target = node_key(node)
    # Do not spend an SSH timeout on a worker that has not even opened port 22
    # yet. This keeps the first boot pass predictably staggered when several Pis
    # are still booting or physically absent.
    if not is_master_node(node) and not tcp_up(node, timeout=min(1.0, RECOVERY_TCP_TIMEOUT)):
        return {
            "name": name,
            "target": target,
            "result": "pending",
            "attempts": attempts,
            "failure": "node_unreachable",
            "output": "TCP/22 not ready",
        }
    try:
        code, output, failure = run_script(node, "start")
    except (NodeBusy, RuntimeError) as exc:
        code, output, failure = 1, str(exc), "busy"

    if code == 0:
        _recovery_set(target, last_up=True, seen_down=False, pending=False, recovering=False, stage="started by startup pass", updated_at=now())
        schedule_lightweight_probe(node)
        return {
            "name": name,
            "target": target,
            "result": "started",
            "attempts": attempts,
        }

    retryable = failure in {"node_unreachable", "ssh_failed", "busy"}
    return {
        "name": name,
        "target": target,
        "result": "pending" if retryable else "failed",
        "attempts": attempts,
        "failure": failure,
        "output": output[-300:],
    }


def _startup_sleep(seconds: float, deadline: float) -> None:
    remaining = deadline - now()
    if remaining > 0 and seconds > 0:
        time.sleep(min(seconds, remaining))


def startup_autostart_on_server_start() -> None:
    """Restart every configured stream whenever the master server starts.

    The first pass is intentionally gentle: the master node is started first,
    then each configured worker is started five seconds after the previous one
    by default. This avoids a 24-node burst of SSH, Streamlink and source-site
    requests. Nodes which are still booting do not block the first pass; they
    are revisited afterwards until STARTUP_AUTOSTART_TIMEOUT expires.
    """
    if os.environ.get("STREAM_MASTER_AUTOSTART", "1") not in {"1", "yes", "true", "on"}:
        print("Startup autostart disabled by STREAM_MASTER_AUTOSTART.")
        STARTUP_FIRST_PASS_DONE.set()
        return

    nodes = read_nodes()
    # A server start is an explicit installation-wide restart request. It also
    # establishes the desired state used by unexpected power-loss recovery.
    set_desired_states_bulk({
        node_key(n): ("running" if str(n.get("url", "")).strip() else "stopped")
        for n in nodes
    })
    configured = [n for n in nodes if str(n.get("url", "")).strip()]
    # Python's sort is stable, so worker order remains the nodes.json order.
    configured.sort(key=lambda node: 0 if is_master_node(node) else 1)

    _write_startup_state(
        running=True,
        mode="every_server_start_staggered",
        stage="waiting_for_nodes",
        started_at=now(),
        finished_at=None,
        configured=len(configured),
        total=len(nodes),
        initial_delay_seconds=STARTUP_AUTOSTART_INITIAL_DELAY,
        stagger_seconds=STARTUP_AUTOSTART_STAGGER,
        results=[],
    )
    print(
        f"Server startup: controller is ready; waiting {STARTUP_AUTOSTART_INITIAL_DELAY:g}s "
        f"before the first stream Start so the Pis can finish booting."
    )
    if STARTUP_AUTOSTART_INITIAL_DELAY > 0:
        time.sleep(STARTUP_AUTOSTART_INITIAL_DELAY)

    _write_startup_state(stage="starting_streams")
    print(
        f"Server startup: restarting {len(configured)}/{len(nodes)} configured streams "
        f"sequentially with {STARTUP_AUTOSTART_STAGGER:g}s spacing; "
        f"waiting up to {STARTUP_AUTOSTART_TIMEOUT}s for late Pis after the settle delay."
    )

    # The initial boot-settle delay is intentionally outside the retry timeout.
    # Once starts begin, late/unreachable Pis still receive the full retry window.
    deadline = now() + STARTUP_AUTOSTART_TIMEOUT
    results_by_target: dict[str, dict] = {}
    pending: list[tuple[dict, int]] = []

    # First pass: exactly one attempt per node, spaced out. An offline node is
    # queued for later instead of delaying every node after it.
    for pos, node in enumerate(configured):
        if now() >= deadline:
            break
        result = _autostart_attempt(node, 1)
        target = str(result.get("target") or node_key(node))
        results_by_target[target] = result
        if result.get("result") == "pending":
            pending.append((node, 1))
        _write_startup_state(results=list(results_by_target.values()))
        print(f"Server startup: {result}")
        if pos + 1 < len(configured):
            _startup_sleep(STARTUP_AUTOSTART_STAGGER, deadline)

    # The recovery monitor may now audit/recover nodes. It was explicitly gated
    # until this point so it cannot bypass the 60s settle + staggered first pass.
    STARTUP_FIRST_PASS_DONE.set()

    # Retry only Pis which were unreachable/busy. Each retry round starts no
    # sooner than STARTUP_AUTOSTART_RETRY after the previous pass and still
    # spaces individual starts, so recovery cannot turn into another burst.
    round_no = 0
    while pending and now() < deadline:
        round_no += 1
        _startup_sleep(STARTUP_AUTOSTART_RETRY, deadline)
        if now() >= deadline:
            break
        print(f"Server startup: retry round {round_no} for {len(pending)} pending node(s).")
        next_pending: list[tuple[dict, int]] = []
        current_pending = pending
        for pos, (node, attempts) in enumerate(current_pending):
            if now() >= deadline:
                next_pending.extend(current_pending[pos:])
                break
            result = _autostart_attempt(node, attempts + 1)
            target = str(result.get("target") or node_key(node))
            results_by_target[target] = result
            if result.get("result") == "pending":
                next_pending.append((node, attempts + 1))
            _write_startup_state(results=list(results_by_target.values()))
            print(f"Server startup: {result}")
            if pos + 1 < len(current_pending):
                _startup_sleep(STARTUP_AUTOSTART_STAGGER, deadline)
        pending = next_pending

    # Convert any timed-out pending entries to a final failure state.
    for node, attempts in pending:
        target = node_key(node)
        previous = dict(results_by_target.get(target) or {})
        previous.update({
            "name": str(node.get("name") or target),
            "target": target,
            "result": "failed",
            "attempts": attempts,
            "failure": previous.get("failure") or "startup_timeout",
        })
        results_by_target[target] = previous

    results = list(results_by_target.values())
    _write_startup_state(running=False, finished_at=now(), results=results)
    print("Server startup stream restart pass completed.")

def launch_startup_autostart() -> None:
    def runner() -> None:
        try:
            startup_autostart_on_server_start()
        except Exception as exc:
            print(f"Startup autostart error: {exc}", flush=True)
        finally:
            # Fail open for recovery if startup orchestration itself crashes.
            STARTUP_FIRST_PASS_DONE.set()
    threading.Thread(target=runner, daemon=True, name="startup-autostart").start()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        path = urlparse(self.path).path
        if self.command == "GET" and path in {"/", "/index.html", "/app.js", "/style.css", "/api/state", "/api/nodes"}:
            return
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
                self.send_json({"update": _update_snapshot(), "jobs": jobs_snapshot(), "fleet": fleet_snapshot(), "recovery_guard": update_guard_snapshot(), "desired": desired_snapshot(), "powerloss_recovery": recovery_snapshot(), "health": health_snapshot()})
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
            if action == "start":
                set_desired_state(nodes[index], "running")
            elif action == "kill":
                set_desired_state(nodes[index], "stopped")
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
    try:
        RUNTIME_DIR.chmod(0o700)
    except OSError:
        pass
    ensure_local_nodes_file()
    try:
        NODES_FILE.chmod(0o600)
    except OSError:
        pass
    validate_nodes_collection(read_nodes())
    required = [WEB_DIR / "index.html", WEB_DIR / "app.js", WEB_DIR / "style.css", UPDATE_SCRIPT, MASTER_LOCAL_UPDATE_SCRIPT, APP_DIR / "local_stream_service.sh"]
    required.extend(SCRIPTS_DIR / f"{name}.sh" for name in SCRIPT_ACTIONS)
    missing = [str(p) for p in required if not p.is_file()]
    if missing:
        print("Missing files:\n" + "\n".join(missing), file=sys.stderr)
        return 1

    load_jobs()
    load_update_state()
    load_fleet_job()
    load_desired_states()
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

    # Restart every configured stream once in the background on each server
    # start, while keeping the web UI available during Pi boot/retry delays.
    launch_startup_autostart()
    launch_powerloss_recovery_monitor()

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
