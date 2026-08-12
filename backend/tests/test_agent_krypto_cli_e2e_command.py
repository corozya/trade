"""Offline, single-command E2E for the agent-krypto CLI (#105).

AC under test: "Offline E2E uruchamia pełną pętlę jedną komendą bez sieci" —
one CLI invocation (`agent_krypto_cli.py e2e`) must drive the whole loop
(ingest -> LearningRequest -> experiment -> evaluate -> promote -> cycle)
using only an in-process synthetic fixture, with no fixture file and no
network dependency. It must also be idempotent (replaying the same run_bucket
returns the cached DONE result rather than re-executing).
"""

from __future__ import annotations

import io
import json
import socket
from contextlib import redirect_stdout
from unittest.mock import patch

import pytest

import agent_krypto_cli


def _write_config(path, **overrides):
    payload = {
        "config_version": "v1",
        "dataset_version": "unset",
        "feature_schema_version": "unset",
        "promotion_policy_version": "unset",
        "label_config_version": "unset",
        "credential_alias": "okx-demo-1",
    }
    payload.update(overrides)
    path.write_text(json.dumps(payload))
    return path


def _run(argv) -> dict:
    buf = io.StringIO()
    with redirect_stdout(buf):
        exit_code = agent_krypto_cli.main(argv)
    lines = [line for line in buf.getvalue().splitlines() if line.strip()]
    assert len(lines) == 1, f"expected exactly one JSON line on stdout, got: {lines!r}"
    return exit_code, json.loads(lines[0])


class _NetworkAttempted(AssertionError):
    pass


def _blocked_socket(*args, **kwargs):  # pragma: no cover - only hit on regression
    raise _NetworkAttempted("agent_krypto_cli e2e attempted a real network socket")


def test_e2e_runs_full_loop_with_one_command_no_network(tmp_path):
    config_path = _write_config(tmp_path / "config.json")

    # Fail loudly if the e2e path ever tries to open a real network socket —
    # the AC requires this command to be network-free end to end.
    with patch("socket.socket", side_effect=_blocked_socket):
        exit_code, result = _run([
            "e2e", "--config-version", "v1",
            "--data-root", str(tmp_path / "research" / "agent-krypto"),
            "--config-path", str(config_path),
            "--run-db", str(tmp_path / "research" / "agent-krypto" / "runs" / "orchestrator_runs.db"),
            "--run-bucket", "test-bucket-1",
        ])

    assert exit_code == 0, result
    assert result["status"] == "DONE", result
    phases = result["result"]["phases"]
    assert phases["ingest"]["dataset_id"]
    assert phases["request"]["feature_version"]
    assert phases["experiment"]["accepted"] is True
    assert phases["evaluate"]["promotion_decision"]["passed"] is True
    assert phases["promote"] == {"PAPER": "PAPER", "DEMO": "DEMO", "PROMOTED": "PROMOTED"}
    # No TradeIntent was supplied — this offline command never places an
    # order; the cycle phase must gate to WAIT, not COMPLETED.
    assert phases["cycle"]["status"] == "WAIT"
    assert phases["cycle"]["execution_result"] is None


def test_e2e_is_idempotent_for_the_same_run_bucket(tmp_path):
    config_path = _write_config(tmp_path / "config.json")
    data_root = tmp_path / "research" / "agent-krypto"
    run_db = data_root / "runs" / "orchestrator_runs.db"
    argv = [
        "e2e", "--config-version", "v1", "--data-root", str(data_root),
        "--config-path", str(config_path), "--run-db", str(run_db),
        "--run-bucket", "idempotent-bucket",
    ]

    first_exit, first = _run(argv)
    second_exit, second = _run(argv)

    assert first_exit == 0 and second_exit == 0
    assert first["run_id"] == second["run_id"]
    assert first["result"] == second["result"]
    assert second["reason"] == "ok"


def test_e2e_activates_a_promoted_artifact_in_the_durable_registry(tmp_path):
    config_path = _write_config(tmp_path / "config.json")
    data_root = tmp_path / "research" / "agent-krypto"

    _, result = _run([
        "e2e", "--config-version", "v1", "--data-root", str(data_root),
        "--config-path", str(config_path),
        "--run-db", str(data_root / "runs" / "orchestrator_runs.db"),
        "--run-bucket", "registry-bucket",
    ])

    assert result["status"] == "DONE"
    registry_db = result["result"]["artifact_registry_db"]

    import sqlite3
    conn = sqlite3.connect(registry_db)
    try:
        rows = conn.execute(
            "SELECT status FROM strategy_artifacts WHERE symbol='BTC-USDT-SWAP'"
        ).fetchall()
    finally:
        conn.close()
    assert ("PROMOTED",) in rows
