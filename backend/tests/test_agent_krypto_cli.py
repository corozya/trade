from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from unittest.mock import MagicMock, patch

import pytest

import agent_krypto_cli
from services.agent_krypto_run_store import RunStore


def test_default_storage_paths_stay_inside_split_repository():
    repo_root = agent_krypto_cli._REPO_ROOT
    assert agent_krypto_cli.DEFAULT_DATA_ROOT == str(repo_root / "data" / "lake")
    assert agent_krypto_cli.DEFAULT_RUNTIME_ROOT == repo_root / "data" / "runtime"
    assert agent_krypto_cli.DEFAULT_RUN_DB == str(
        repo_root / "data" / "runtime" / "runs" / "orchestrator_runs.db"
    )
    assert "research/agent-krypto" not in agent_krypto_cli.DEFAULT_RUN_DB


def _write_config(path, **overrides):
    payload = {
        "config_version": "v1",
        "dataset_version": "d1",
        "feature_schema_version": "f1",
        "promotion_policy_version": "p1",
        "label_config_version": "l1",
        "credential_alias": "okx-demo-1",
    }
    payload.update(overrides)
    path.write_text(json.dumps(payload))
    return path


def _run(argv) -> tuple[int, dict]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = agent_krypto_cli.main(argv)
    lines = [line for line in buf.getvalue().splitlines() if line.strip()]
    assert len(lines) == 1, f"expected exactly one JSON line, got: {lines}"
    return code, json.loads(lines[0])


def test_status_is_read_only_and_creates_no_run(tmp_path):
    run_db = tmp_path / "runs.db"
    code, result = _run([
        "status", "--phase", "EXPERIMENT", "--symbol", "BTC-USDT-SWAP",
        "--run-db", str(run_db),
    ])
    assert code == 0
    assert result["command"] == "status"
    store = RunStore(db_path=run_db)
    assert store.find_resumable(phase="EXPERIMENT", symbol="BTC-USDT-SWAP") is None


def test_cycle_waits_when_required_versions_are_missing(tmp_path):
    config_path = _write_config(tmp_path / "config.json", dataset_version="")
    with patch.object(agent_krypto_cli, "HANDLERS", {**agent_krypto_cli.HANDLERS}):
        cycle_handler = MagicMock()
        agent_krypto_cli.HANDLERS["cycle"] = cycle_handler
        code, result = _run([
            "cycle", "--config-version", "v1", "--symbol", "BTC-USDT-SWAP",
            "--strategy-artifact-file", str(tmp_path / "missing.json"),
            "--config-path", str(config_path), "--run-db", str(tmp_path / "runs.db"),
        ])
    assert result["status"] == "WAIT"
    cycle_handler.assert_not_called()


def test_cycle_bad_config_returns_error_without_execution(tmp_path):
    config_path = _write_config(tmp_path / "config.json", extra_field="nope")
    with patch.object(agent_krypto_cli, "HANDLERS", {**agent_krypto_cli.HANDLERS}):
        cycle_handler = MagicMock()
        agent_krypto_cli.HANDLERS["cycle"] = cycle_handler
        code, result = _run([
            "cycle", "--config-version", "v1", "--symbol", "BTC-USDT-SWAP",
            "--strategy-artifact-file", str(tmp_path / "missing.json"),
            "--config-path", str(config_path), "--run-db", str(tmp_path / "runs.db"),
        ])
    assert code == 1
    assert result["status"] == "ERROR"
    cycle_handler.assert_not_called()


def test_promote_illegal_transition_returns_error(tmp_path):
    config_path = _write_config(tmp_path / "config.json")
    artifact_path = tmp_path / "artifact.json"
    artifact_path.write_text(json.dumps({
        "strategy_version": "sv1", "symbol": "BTC-USDT-SWAP",
        "dataset_version": "d1", "feature_schema_version": "f1",
        "label_config": {
            "base_timeframe": "15m", "horizon": 1,
            "label_type": "log_return", "threshold": 0.0,
        },
        "promotion_policy_version": "p1", "n_folds": 5, "embargo_bars": 3,
        "holdout_fraction": 0.2, "seed": 0, "costs": {},
        "status": "DRAFT",
    }))
    code, result = _run([
        "promote", "--config-version", "v1", "--symbol", "BTC-USDT-SWAP",
        "--strategy-artifact-file", str(artifact_path),
        "--target-status", "PROMOTED",  # illegal: DRAFT can only go to CANDIDATE
        "--config-path", str(config_path), "--run-db", str(tmp_path / "runs.db"),
    ])
    assert code == 1
    assert result["status"] == "ERROR"


def test_resume_continues_run_instead_of_restarting_handler(tmp_path):
    config_path = _write_config(tmp_path / "config.json")
    run_db = tmp_path / "runs.db"
    calls = []

    def flaky_handler(args, config):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("simulated crash mid-phase")
        return {"dataset_id": "market-ok"}

    with patch.object(agent_krypto_cli, "HANDLERS", {**agent_krypto_cli.HANDLERS, "ingest": flaky_handler}):
        first_code, first = _run([
            "ingest", "--config-version", "v1", "--symbol", "BTC-USDT-SWAP",
            "--source-path", str(tmp_path / "fixture.json"),
            "--config-path", str(config_path), "--run-db", str(run_db),
            "--run-bucket", "fixed-bucket",
        ])
        assert first["status"] == "ERROR"

        second_code, second = _run([
            "ingest", "--config-version", "v1", "--symbol", "BTC-USDT-SWAP",
            "--source-path", str(tmp_path / "fixture.json"),
            "--config-path", str(config_path), "--run-db", str(run_db),
            "--run-bucket", "fixed-bucket",
        ])
    assert second["status"] == "DONE"
    assert second["run_id"] == first["run_id"]
    assert len(calls) == 2


def test_no_secrets_in_stdout_or_reason(tmp_path):
    config_path = _write_config(tmp_path / "config.json")
    secret_like = "abcdefghij0123456789ABCDEFGHIJ0123456789"

    def leaky_handler(args, config):
        raise RuntimeError(f"upstream failed with key {secret_like}")

    with patch.object(agent_krypto_cli, "HANDLERS", {**agent_krypto_cli.HANDLERS, "ingest": leaky_handler}):
        code, result = _run([
            "ingest", "--config-version", "v1", "--symbol", "BTC-USDT-SWAP",
            "--source-path", str(tmp_path / "fixture.json"),
            "--config-path", str(config_path), "--run-db", str(tmp_path / "runs.db"),
        ])
    raw = json.dumps(result)
    assert secret_like not in raw
    assert "[REDACTED]" in result["reason"]


def test_no_secrets_persisted_in_run_store_result_or_history(tmp_path):
    """#101 review-round-2 P1: redaction must happen before persistence, not
    only in the stdout envelope — a secret nested in a handler's ``result``
    must never reach ``orchestrator_runs.result_json`` or the history table."""
    config_path = _write_config(tmp_path / "config.json")
    run_db = tmp_path / "runs.db"
    secret_like = "0123456789abcdefFEDCBA98765432100123456789a"

    def leaky_result_handler(args, config):
        return {"nested": {"api_key": secret_like}, "ok": True}

    with patch.object(agent_krypto_cli, "HANDLERS", {**agent_krypto_cli.HANDLERS, "ingest": leaky_result_handler}):
        code, result = _run([
            "ingest", "--config-version", "v1", "--symbol", "BTC-USDT-SWAP",
            "--source-path", str(tmp_path / "fixture.json"),
            "--config-path", str(config_path), "--run-db", str(run_db),
        ])
    assert result["status"] == "DONE"
    assert secret_like not in json.dumps(result)

    run_store = RunStore(db_path=run_db)
    row = run_store.get(run_id=result["run_id"])
    assert secret_like not in (row["result_json"] or "")
    for entry in run_store.history(run_id=result["run_id"]):
        assert secret_like not in json.dumps(entry)
