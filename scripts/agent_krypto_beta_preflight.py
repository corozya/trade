#!/usr/bin/env python3
"""Fail-closed beta/local preflight for the #100 agent-krypto bundle.

The repository may contain unrelated dirty work.  Only paths matching the
manifest's narrow scope patterns are treated as bundle candidates; every such
candidate must be explicitly allowlisted.  The script never reads OKX
credentials, imports an OKX client, enables cron, or constructs a TradeIntent.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "config" / "agent_krypto_beta_bundle.json"
DEFAULT_CONFIG = ROOT / "config" / "agent_krypto_orchestrator_config.json"
DEFAULT_REGISTRY = ROOT / "research" / "agent-krypto" / "artifacts" / "registry.db"


class PreflightError(RuntimeError):
    pass


def canonical_hash(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def load_manifest(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text())
    files = payload.get("files")
    if not isinstance(files, list) or not files or files != sorted(set(files)):
        raise PreflightError("manifest files must be a non-empty sorted unique list")
    forbidden = tuple(payload.get("forbidden_suffixes", ()))
    for relative in files:
        candidate = Path(relative)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise PreflightError(f"unsafe manifest path: {relative}")
        if relative.endswith(forbidden):
            raise PreflightError(f"forbidden artifact in manifest: {relative}")
        if not (ROOT / candidate).is_file():
            raise PreflightError(f"manifest file does not exist: {relative}")
    return payload


def scoped_dirty_paths(manifest: dict[str, Any]) -> list[str]:
    proc = subprocess.run(
        ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    entries = proc.stdout.decode(errors="strict").split("\0")
    paths: list[str] = []
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if not entry:
            continue
        status, path = entry[:2], entry[3:]
        if status[0] in {"R", "C"} and index < len(entries):
            path = entries[index]
            index += 1
        paths.append(path)
    patterns = tuple(manifest["scope_patterns"])
    return sorted(path for path in paths if path.startswith(patterns))


def validate_allowlist(manifest: dict[str, Any]) -> dict[str, Any]:
    allowed = set(manifest["files"])
    candidates = scoped_dirty_paths(manifest)
    unexpected = sorted(set(candidates) - allowed)
    if unexpected:
        raise PreflightError(f"unexpected scoped dirty files: {unexpected}")
    return {
        "candidate_count": len(candidates),
        "manifest_file_count": len(allowed),
        "manifest_sha256": canonical_hash(manifest),
        "unexpected": [],
    }


def validate_config(config_path: Path) -> dict[str, str]:
    config = json.loads(config_path.read_text())
    required = (
        "dataset_version",
        "feature_schema_version",
        "label_config_version",
        "promotion_policy_version",
    )
    missing = [key for key in required if not config.get(key) or config[key] == "unset"]
    if missing:
        raise PreflightError(f"unresolved config versions: {missing}")
    return {key: str(config[key]) for key in required}


def active_compatible_promoted(registry_path: Path, versions: dict[str, str]) -> int:
    if not registry_path.exists():
        return 0
    with sqlite3.connect(f"file:{registry_path}?mode=ro", uri=True) as conn:
        table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='active_strategy_artifacts'"
        ).fetchone()
        if not table:
            return 0
        rows = conn.execute(
            """
            SELECT a.payload_json
            FROM active_strategy_artifacts x
            JOIN strategy_artifacts a ON a.artifact_hash=x.artifact_hash
            WHERE a.status='PROMOTED'
            """
        ).fetchall()
    count = 0
    for (encoded,) in rows:
        payload = json.loads(encoded)
        if all(payload.get(key) == value for key, value in versions.items()
               if key != "label_config_version"):
            count += 1
    return count


def run_checks(backend: Path) -> dict[str, Any]:
    python = ROOT / ".venv" / "bin" / "python"
    if not python.is_file():
        raise PreflightError(f"project Python missing: {python}")
    tests = [
        "tests/test_agent_krypto_cli_e2e_command.py",
        "tests/test_agent_krypto_backup.py",
        "tests/test_agent_krypto_cycle_e2e.py",
        "tests/test_agent_krypto_orchestrator_lock.py",
        "tests/test_agent_krypto_orchestrator_state_machine.py",
        "tests/test_agent_krypto_run_store.py",
        "../scripts/test_agent_krypto_beta_preflight.py",
    ]
    proc = subprocess.run(
        [str(python), "-m", "pytest", "-q", *tests],
        cwd=backend,
        text=True,
        capture_output=True,
    )
    if proc.returncode:
        raise PreflightError(f"targeted tests failed:\n{proc.stdout}\n{proc.stderr}")
    return {"targeted_tests": proc.stdout.strip()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--run-tests", action="store_true")
    args = parser.parse_args(argv)
    try:
        manifest = load_manifest(args.manifest.resolve())
        allowlist = validate_allowlist(manifest)
        versions = validate_config(args.config.resolve())
        active = active_compatible_promoted(args.registry.resolve(), versions)
        if active:
            raise PreflightError(
                f"active compatible real PROMOTED artifacts: {active}; beta is NO-GO"
            )
        result: dict[str, Any] = {
            "decision": "READY",
            "mode": "beta-local-no-trade",
            "allowlist": allowlist,
            "versions": versions,
            "active_compatible_promoted": active,
            "cron_activated": False,
            "network_used": False,
            "credentials_read": False,
            "trade_intent_created": False,
        }
        if args.run_tests:
            result.update(run_checks(ROOT / "backend"))
    except (OSError, ValueError, subprocess.SubprocessError, PreflightError) as exc:
        print(json.dumps({"decision": "NO-GO", "reason": str(exc)}, sort_keys=True))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
