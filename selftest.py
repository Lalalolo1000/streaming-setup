#!/usr/bin/env python3
"""Small no-network deployment self-test used by git_update.sh.

This intentionally avoids SSH, package operations, systemd and writes. It catches
configuration/API wiring mistakes that syntax compilation alone cannot catch.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent


def load_module(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> int:
    master = load_module("streaming_setup_master_selftest", ROOT / "master.py")
    updater = load_module("streaming_setup_updater_selftest", ROOT / "update_pis.py")

    defaults = json.loads((ROOT / "nodes.default.json").read_text(encoding="utf-8"))
    require(isinstance(defaults, list), "nodes.default.json must be a list")
    normalized = [master.validate_node(node) for node in defaults]
    master.validate_nodes_collection(normalized)
    require(len(normalized) == 24, "default configuration must contain 24 nodes")
    require(sum(1 for node in normalized if master.is_master_node(node)) == 1, "exactly one master required")
    require(master.is_master_node(normalized[0]), "Stream 01 must be the master in the default configuration")

    # Duplicate targets must be rejected because locks/jobs/desired state are keyed by target.
    duplicate = [dict(node) for node in normalized]
    duplicate[1]["target"] = duplicate[0]["target"]
    try:
        master.validate_nodes_collection(duplicate)
    except ValueError:
        pass
    else:
        raise AssertionError("duplicate node targets were accepted")

    workers = updater.select_nodes(defaults, [], True)
    require(len(workers) == 23, "worker updater must exclude the master")
    require(all(not updater.is_master_node(node) for node in workers), "master leaked into worker updater")

    for name in master.SCRIPT_ACTIONS:
        require((ROOT / "scripts" / f"{name}.sh").is_file(), f"missing scripts/{name}.sh")
    for required in (
        "run_master.sh", "git_update.sh", "git_update_systemd.sh",
        "local_stream_service.sh", "update_master_local.sh", "backup_local_state.sh",
        "web/index.html", "web/app.js", "web/style.css",
    ):
        require((ROOT / required).is_file(), f"missing {required}")

    # Guard against accidentally reintroducing aggressive unattended polling defaults.
    require(master.STARTUP_AUTOSTART_INITIAL_DELAY >= 60, "startup settle delay is unexpectedly short")
    require(master.STARTUP_AUTOSTART_STAGGER >= 5, "startup staggering is unexpectedly short")
    require(240 <= master.RECOVERY_SAFETY_AUDIT_INTERVAL <= 360, "supervisor audit should remain roughly five minutes")
    require(master.RECOVERY_MONITOR_INTERVAL >= 30, "TCP recovery polling is unexpectedly aggressive")

    print("Streaming Setup self-test: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
