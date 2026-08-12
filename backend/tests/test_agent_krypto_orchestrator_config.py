from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from services.agent_krypto_orchestrator import (
    OrchestratorConfigError,
    default_cycle_bucket,
    dispatch,
    load_orchestrator_config,
)
from services.agent_krypto_run_store import RunStore


def _write_config(path, payload):
    path.write_text(json.dumps(payload))


def test_missing_required_field_raises(tmp_path):
    config_path = tmp_path / "config.json"
    _write_config(config_path, {
        "config_version": "v1",
        "feature_schema_version": "f1",
        "promotion_policy_version": "p1",
        "label_config_version": "l1",
        "credential_alias": "okx-demo-1",
    })  # dataset_version missing
    with pytest.raises(OrchestratorConfigError):
        load_orchestrator_config("v1", config_path=config_path)


def test_unknown_field_raises(tmp_path):
    config_path = tmp_path / "config.json"
    _write_config(config_path, {
        "config_version": "v1", "dataset_version": "d1",
        "feature_schema_version": "f1", "promotion_policy_version": "p1",
        "label_config_version": "l1", "credential_alias": "okx-demo-1",
        "api_key": "should-not-be-here",
    })
    with pytest.raises(OrchestratorConfigError):
        load_orchestrator_config("v1", config_path=config_path)


def test_unknown_config_version_raises(tmp_path):
    config_path = tmp_path / "config.json"
    _write_config(config_path, {
        "config_version": "v2", "dataset_version": "d1",
        "feature_schema_version": "f1", "promotion_policy_version": "p1",
        "label_config_version": "l1", "credential_alias": "okx-demo-1",
    })
    with pytest.raises(OrchestratorConfigError):
        load_orchestrator_config("v1", config_path=config_path)


def test_dispatch_returns_error_for_bad_config_without_touching_run_store(tmp_path):
    config_path = tmp_path / "config.json"
    _write_config(config_path, {
        "config_version": "v1", "dataset_version": "d1",
        "feature_schema_version": "f1", "promotion_policy_version": "p1",
        "label_config_version": "l1", "credential_alias": "okx-demo-1",
        "extra": "field",
    })
    run_store = RunStore(db_path=tmp_path / "runs.db")
    handler = MagicMock()
    result = dispatch(
        "ingest", {"config_version": "v1", "symbol": "BTC-USDT-SWAP"},
        run_store=run_store, handler=handler, config_path=config_path,
    )
    assert result["status"] == "ERROR"
    handler.assert_not_called()
    assert run_store.find_resumable(phase="INGEST", symbol="BTC-USDT-SWAP") is None


def test_dispatch_waits_on_missing_required_version_without_calling_handler(tmp_path):
    config_path = tmp_path / "config.json"
    _write_config(config_path, {
        "config_version": "v1", "dataset_version": "",
        "feature_schema_version": "f1", "promotion_policy_version": "p1",
        "label_config_version": "l1", "credential_alias": "okx-demo-1",
    })
    run_store = RunStore(db_path=tmp_path / "runs.db")
    handler = MagicMock()
    result = dispatch(
        "experiment", {"config_version": "v1", "symbol": "BTC-USDT-SWAP"},
        run_store=run_store, handler=handler, config_path=config_path,
    )
    assert result["status"] == "WAIT"
    assert "dataset_version" in result["reason"]
    handler.assert_not_called()


@pytest.mark.parametrize("placeholder", ["unset", "todo", "tbd", "changeme", "None", "NULL", "  "])
def test_dispatch_waits_on_sentinel_placeholder_version(tmp_path, placeholder):
    config_path = tmp_path / "config.json"
    _write_config(config_path, {
        "config_version": "v1", "dataset_version": placeholder,
        "feature_schema_version": "f1", "promotion_policy_version": "p1",
        "label_config_version": "l1", "credential_alias": "okx-demo-1",
    })
    run_store = RunStore(db_path=tmp_path / "runs.db")
    handler = MagicMock()
    result = dispatch(
        "experiment", {"config_version": "v1", "symbol": "BTC-USDT-SWAP"},
        run_store=run_store, handler=handler, config_path=config_path,
    )
    assert result["status"] == "WAIT"
    handler.assert_not_called()


def test_two_distinct_requests_same_symbol_same_day_get_separate_runs(tmp_path):
    """#101 review-round-2 P0: run_id must be derived from the request
    identity, not just phase/symbol/config/day, or two different
    LearningRequests for the same symbol on the same day collapse onto one
    run_id and the second one fails as a fingerprint mismatch instead of
    getting its own run."""
    config_path = tmp_path / "config.json"
    _write_config(config_path, {
        "config_version": "v1", "dataset_version": "d1",
        "feature_schema_version": "f1", "promotion_policy_version": "p1",
        "label_config_version": "l1", "credential_alias": "okx-demo-1",
    })
    run_store = RunStore(db_path=tmp_path / "runs.db")
    handler = MagicMock(side_effect=[{"result": "first"}, {"result": "second"}])

    first = dispatch(
        "request",
        {"config_version": "v1", "symbol": "BTC-USDT-SWAP", "request_file": "req-a.json"},
        run_store=run_store, handler=handler, config_path=config_path,
    )
    second = dispatch(
        "request",
        {"config_version": "v1", "symbol": "BTC-USDT-SWAP", "request_file": "req-b.json"},
        run_store=run_store, handler=handler, config_path=config_path,
    )
    assert first["status"] == "DONE"
    assert second["status"] == "DONE"
    assert first["run_id"] != second["run_id"]
    assert handler.call_count == 2


def test_identical_request_retried_is_idempotent_same_run_id(tmp_path):
    config_path = tmp_path / "config.json"
    _write_config(config_path, {
        "config_version": "v1", "dataset_version": "d1",
        "feature_schema_version": "f1", "promotion_policy_version": "p1",
        "label_config_version": "l1", "credential_alias": "okx-demo-1",
    })
    run_store = RunStore(db_path=tmp_path / "runs.db")
    handler = MagicMock(return_value={"result": "only-once"})

    args = {"config_version": "v1", "symbol": "BTC-USDT-SWAP", "request_file": "req-a.json"}
    first = dispatch("request", args, run_store=run_store, handler=handler, config_path=config_path)
    second = dispatch("request", args, run_store=run_store, handler=handler, config_path=config_path)

    assert first["run_id"] == second["run_id"]
    assert first["status"] == second["status"] == "DONE"
    handler.assert_called_once()


def test_default_cycle_bucket_floors_to_15_minutes():
    assert default_cycle_bucket(datetime(2026, 7, 23, 12, 0, tzinfo=timezone.utc)) == \
        default_cycle_bucket(datetime(2026, 7, 23, 12, 14, 59, tzinfo=timezone.utc))
    assert default_cycle_bucket(datetime(2026, 7, 23, 12, 14, 59, tzinfo=timezone.utc)) != \
        default_cycle_bucket(datetime(2026, 7, 23, 12, 15, 0, tzinfo=timezone.utc))


def test_cycle_same_quarter_hour_reuses_run_id_next_quarter_gets_new_one(tmp_path):
    """#101 review-round-3 P0: `cycle` must bucket by 15-minute UTC window,
    not by calendar day — otherwise every 15-minute cron cycle after the
    first in a day is a no-op cache hit against the first cycle's stale
    DONE result."""
    config_path = tmp_path / "config.json"
    _write_config(config_path, {
        "config_version": "v1", "dataset_version": "d1",
        "feature_schema_version": "f1", "promotion_policy_version": "p1",
        "label_config_version": "l1", "credential_alias": "okx-demo-1",
    })
    run_store = RunStore(db_path=tmp_path / "runs.db")
    handler = MagicMock(side_effect=[{"status": "COMPLETED"}, {"status": "COMPLETED"}])

    args = {
        "config_version": "v1", "symbol": "BTC-USDT-SWAP",
        "dataset_version": "d1", "feature_schema_version": "f1",
        "promotion_policy_version": "p1",
    }
    same_quarter_a = dispatch(
        "cycle", args, run_store=run_store, handler=handler, config_path=config_path,
        now=datetime(2026, 7, 23, 12, 3, tzinfo=timezone.utc),
    )
    same_quarter_b = dispatch(
        "cycle", args, run_store=run_store, handler=handler, config_path=config_path,
        now=datetime(2026, 7, 23, 12, 13, tzinfo=timezone.utc),
    )
    assert same_quarter_a["run_id"] == same_quarter_b["run_id"]
    handler.assert_called_once()  # second call within the same quarter is a cache hit

    next_quarter = dispatch(
        "cycle", args, run_store=run_store, handler=handler, config_path=config_path,
        now=datetime(2026, 7, 23, 12, 16, tzinfo=timezone.utc),
    )
    assert next_quarter["run_id"] != same_quarter_a["run_id"]
    assert handler.call_count == 2
