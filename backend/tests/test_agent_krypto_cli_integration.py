"""Offline, real-handler integration tests for the agent-krypto CLI (#101 review).

These exercise the *actual* HANDLERS (no mocking) so a wiring regression
(e.g. request routed through the wrong gate, ingest bypassing #102, evaluate
skipping real holdout computation) fails here instead of only in a mocked
unit test.
"""

from __future__ import annotations

import io
import json
import sqlite3
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

import agent_krypto_cli
from services.crypto_data_lake import DATA_KINDS, SYMBOLS, TIMEFRAMES, CryptoDataLake
from services.crypto_experiment_runner import ExperimentConfig, ExperimentRunner, LocalFeatureMomentumEngine
from services.crypto_research import ResearchToolGate

ROOT = Path(__file__).resolve().parents[2]


class _FakeSink:
    def record(self, **kwargs):
        return "experiment-offline-eval-1"


class _FakeNotes:
    def append(self, note_id, document, metadata):
        pass


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


def _run(argv) -> dict:
    buf = io.StringIO()
    with redirect_stdout(buf):
        agent_krypto_cli.main(argv)
    lines = [line for line in buf.getvalue().splitlines() if line.strip()]
    assert len(lines) == 1
    return json.loads(lines[0])


def _fixture_records(n=40):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    records = []
    for index in range(n):
        available = start + timedelta(minutes=15 * (index + 1))
        close = 100.0 + index * 0.1
        records.append({
            "symbol": "BTC-USDT-SWAP", "timeframe": "15m", "data_kind": "ohlcv",
            "observed_at": (available - timedelta(minutes=15)).isoformat(),
            "available_at": available.isoformat(),
            "source": "offline-fixture",
            "open": close - 0.05, "high": close + 0.1, "low": close - 0.1,
            "close": close, "volume": 10.0,
        })
    return records


def _ready_fixture_records(as_of: datetime):
    """A complete fixture (every SYMBOLS x TIMEFRAMES ohlcv pair plus every
    non-ohlcv DATA_KINDS stream) fresh as of ``as_of``, so
    ``require_ready_dataset`` accepts it — narrower fixtures are the
    stale/incomplete case exercised in the dedicated readiness test below."""
    records = []
    for symbol in SYMBOLS:
        for timeframe in TIMEFRAMES:
            records.append({
                "symbol": symbol, "timeframe": timeframe, "data_kind": "ohlcv",
                "observed_at": (as_of - timedelta(minutes=1)).isoformat(),
                "available_at": as_of.isoformat(),
                "source": "offline-fixture",
                "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "volume": 10.0,
            })
        for kind in DATA_KINDS:
            if kind == "ohlcv":
                continue
            records.append({
                "symbol": symbol, "timeframe": "1m", "data_kind": kind,
                "observed_at": (as_of - timedelta(minutes=1)).isoformat(),
                "available_at": as_of.isoformat(),
                "source": "offline-fixture",
                "value": 1.0,
            })
    return records


def test_ingest_uses_market_ingestor_not_raw_publish(tmp_path):
    config_path = _write_config(tmp_path / "config.json")
    as_of = datetime(2026, 1, 1, tzinfo=timezone.utc)
    fixture_path = tmp_path / "fixture.json"
    fixture_path.write_text(json.dumps({"records": _ready_fixture_records(as_of)}))

    result = _run([
        "ingest", "--config-version", "v1", "--symbol", "BTC-USDT-SWAP",
        "--source-path", str(fixture_path),
        "--data-root", str(tmp_path / "lake"),
        "--as-of", as_of.isoformat(), "--max-age-minutes", "5",
        "--config-path", str(config_path), "--run-db", str(tmp_path / "runs.db"),
    ])
    assert result["status"] == "DONE"
    dataset_id = result["result"]["dataset_id"]

    # Retrying with the exact same request (same run_id/fingerprint) is the
    # idempotent path and must return the already-published version without
    # re-ingesting.
    second = _run([
        "ingest", "--config-version", "v1", "--symbol", "BTC-USDT-SWAP",
        "--source-path", str(fixture_path),
        "--data-root", str(tmp_path / "lake"),
        "--as-of", as_of.isoformat(), "--max-age-minutes", "5",
        "--config-path", str(config_path), "--run-db", str(tmp_path / "runs.db"),
    ])
    assert second["status"] == "DONE"
    assert second["result"]["dataset_id"] == dataset_id

    # Re-ingesting on top of the published version (an incremental merge, the
    # signature of CryptoMarketIngestor.ingest rather than a bare
    # CryptoDataLake.publish call) must dedup identical rows without raising
    # a conflict error.
    incremental = _run([
        "ingest", "--config-version", "v1", "--symbol", "BTC-USDT-SWAP",
        "--source-path", str(fixture_path), "--base-dataset-id", dataset_id,
        "--data-root", str(tmp_path / "lake"),
        "--as-of", as_of.isoformat(), "--max-age-minutes", "5",
        "--config-path", str(config_path), "--run-db", str(tmp_path / "runs2.db"),
    ])
    assert incremental["status"] == "DONE"
    assert incremental["result"]["dataset_id"]


def test_request_learning_request_builds_feature_dataset_not_review_required(tmp_path):
    config_path = _write_config(tmp_path / "config.json")
    source = tmp_path / "market.parquet"
    pq.write_table(pa.table({
        "symbol": ["BTC-USDT-SWAP"] * 29,
        "observed_at": [f"2026-01-01T00:{v:02d}:00Z" for v in range(29)],
        "close": [float(v) for v in range(1, 30)],
    }), source)

    request_path = tmp_path / "learning_request.json"
    request_path.write_text(json.dumps({
        "request_id": "req-1", "base_dataset_version": "market-abc",
        "requested_by": "agent-krypto-research", "symbols": ["BTC"],
        "hypothesis": "bollinger width separates trend from compression",
        "features": [{
            "name": "bollinger_bands", "timeframe": "15m",
            "params": {"window": 20, "stddev": 2.0},
            "reason": "test coverage for #101 review point 3",
        }],
    }))

    result = _run([
        "request", "--config-version", "v1", "--symbol", "BTC-USDT-SWAP",
        "--request-file", str(request_path), "--request-kind", "learning",
        "--base-dataset-path", str(source),
        "--data-root", str(tmp_path / "research"),
        "--feature-catalog-path", str(ROOT / "config/agent_krypto_feature_catalog.json"),
        "--update-config", str(config_path),
        "--config-path", str(config_path), "--run-db", str(tmp_path / "runs.db"),
    ])
    assert result["status"] == "DONE"
    assert result["result"]["status"] == "EXECUTED"
    assert result["result"]["feature_dataset_paths"]
    feature_version = result["result"]["feature_schema_versions"][0]
    assert json.loads(config_path.read_text()) == {
        "config_version": "v1",
        "dataset_version": "d1",
        "feature_schema_version": feature_version,
        "promotion_policy_version": "p1",
        "label_config_version": "l1",
        "credential_alias": "okx-demo-1",
    }

    retry = _run([
        "request", "--config-version", "v1", "--symbol", "BTC-USDT-SWAP",
        "--request-file", str(request_path), "--request-kind", "learning",
        "--base-dataset-path", str(source),
        "--data-root", str(tmp_path / "research"),
        "--feature-catalog-path", str(ROOT / "config/agent_krypto_feature_catalog.json"),
        "--config-path", str(config_path), "--run-db", str(tmp_path / "runs-retry.db"),
    ])
    assert retry["result"]["feature_schema_versions"] == [feature_version]


def test_request_tool_request_uses_tool_gate(tmp_path):
    config_path = _write_config(tmp_path / "config.json")
    request_path = tmp_path / "tool_request.json"
    request_path.write_text(json.dumps({
        "request_id": "tool-1", "requested_by": "agent-krypto-research",
        "tool": "walk_forward", "params": {}, "reason": "offline integration test",
    }))

    result = _run([
        "request", "--config-version", "v1", "--symbol", "BTC-USDT-SWAP",
        "--request-file", str(request_path), "--request-kind", "tool",
        "--tool-catalog-path", str(ROOT / "config/agent_krypto_research_tool_catalog.json"),
        "--config-path", str(config_path), "--run-db", str(tmp_path / "runs.db"),
    ])
    assert result["status"] == "DONE"
    assert result["result"]["status"] == "EXECUTED"
    assert result["result"]["tool"] == "walk_forward"


def test_evaluate_replays_the_accepted_trial_not_a_raw_market_proxy(tmp_path):
    """evaluate must reload the exact accepted trial (#103) — same
    strategy_params/costs/feature_version/BacktestEngine — and score only the
    holdout partition with it, never synthesize a naive long-only metric from
    raw closes (the #101 review-round-2 P0 finding).

    The trial itself is produced with ``ExperimentRunner`` directly (fake
    sink/notes, exactly like #103's own offline tests) rather than through
    the CLI's ``experiment`` handler, so this test does not depend on mlflow/
    chromadb being installed in the environment — only ``evaluate``'s own
    handler (the thing under review here) is exercised through the real,
    unmocked CLI path below.
    """
    config_path = _write_config(tmp_path / "config.json")
    data_root = tmp_path / "research"
    lake = CryptoDataLake(root=data_root)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    n = 200
    records = []
    for index in range(n):
        available = start + timedelta(minutes=15 * (index + 1))
        close = 100.0 + index * 0.5  # strong, steady uptrend
        records.append({
            "symbol": "BTC-USDT-SWAP", "timeframe": "15m", "data_kind": "ohlcv",
            "observed_at": (available - timedelta(minutes=15)).isoformat(),
            "available_at": available.isoformat(),
            "source": "offline-fixture",
            "open": close - 0.05, "high": close + 0.1, "low": close - 0.1,
            "close": close, "volume": 10.0,
        })
    version = lake.publish(records, lineage={"sources": ["fixture"]})

    # Feature dataset: a monotonically increasing signal so the momentum
    # engine's LONG trades track the uptrend and clear costs consistently.
    rows = lake.read_version(version.dataset_id).to_pylist()
    feature_dir = data_root / "datasets" / "features-eval-1"
    feature_dir.mkdir(parents=True, exist_ok=True)
    feature_rows = [{**row, "signal": float(index) * 0.1} for index, row in enumerate(rows)]
    pq.write_table(pa.Table.from_pylist(feature_rows), feature_dir / "features.parquet")
    (feature_dir / "manifest.json").write_text(json.dumps({
        "dataset_version": "features-eval-1",
        "lineage": {"base_dataset_version": version.dataset_id},
    }))

    experiment_config = ExperimentConfig(
        dataset_version=version.dataset_id,
        feature_version="features-eval-1",
        strategy_version="sv-eval-1",
        symbol="BTC-USDT-SWAP",
        timeframe="15m",
        seed=42,
        strategy_params={"lookback": 3, "threshold": 0.01, "signal_feature": "signal"},
        costs={"fee_bps": 0.1, "spread_bps": 0.1, "slippage_bps": 0.1},
        tool_request={
            "request_id": "tool-eval-1", "requested_by": "agent-krypto-research",
            "tool": "walk_forward", "params": {}, "reason": "offline eval integration test",
        },
    )
    output_root = tmp_path / "experiments"
    runner = ExperimentRunner(
        lake, feature_root=data_root, output_root=output_root,
        experiment_sink=_FakeSink(), research_notes=_FakeNotes(),
        tool_gate=ResearchToolGate(ROOT / "config/agent_krypto_research_tool_catalog.json"),
        backtest_engine=LocalFeatureMomentumEngine(),
    )
    trial = runner.run(experiment_config)
    assert trial["accepted"] is True
    trial_id = trial["trial_id"]

    artifact_path = tmp_path / "artifact.json"
    artifact_path.write_text(json.dumps({
        "strategy_version": "sv-eval-1", "symbol": "BTC-USDT-SWAP",
        "dataset_version": version.dataset_id, "feature_schema_version": "features-eval-1",
        "label_config": {"base_timeframe": "15m", "horizon": 1, "label_type": "log_return", "threshold": 0.0},
        "promotion_policy_version": "p1", "n_folds": 5, "embargo_bars": 3,
        "holdout_fraction": 0.2, "seed": 42, "costs": {"fee_bps": 1.0, "spread_bps": 0.5, "slippage_bps": 0.5},
        "status": "CANDIDATE",
        "fold_metrics": [
            {"expectancy": 0.01, "max_drawdown": 0.01, "profit_factor": 1.5}
            for _ in range(5)
        ],
    }))
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps({
        "min_folds_required": 1, "min_oos_trades": 0,
        "min_positive_fold_fraction": 0.0, "max_drawdown": 1.0,
        "min_profit_factor": 0.0, "require_positive_holdout_expectancy": False,
    }))

    result = _run([
        "evaluate", "--config-version", "v1", "--symbol", "BTC-USDT-SWAP",
        "--strategy-artifact-file", str(artifact_path),
        "--promotion-policy-file", str(policy_path),
        "--holdout-claim-db", str(tmp_path / "claims.db"),
        "--data-root", str(data_root),
        "--experiment-output-root", str(tmp_path / "experiments"),
        "--trial-id", trial_id,
        "--config-path", str(config_path), "--run-db", str(tmp_path / "runs_eval.db"),
    ])
    assert result["status"] == "DONE"
    holdout_result = result["result"]["artifact"]["holdout_result"]
    assert holdout_result is not None
    assert holdout_result["trade_count"] > 0
    assert result["result"]["artifact"]["holdout_evaluated"] is True


def test_evaluate_rejects_trial_dataset_mismatch(tmp_path):
    """A trial frozen against a different dataset_version than the artifact
    claims must fail closed, not silently score the mismatched trial."""
    config_path = _write_config(tmp_path / "config.json")
    output_root = tmp_path / "experiments"
    trial_dir = output_root / "trials"
    trial_dir.mkdir(parents=True)
    (trial_dir / "sv-mismatch.json").write_text(json.dumps({
        "trial_id": "sv-mismatch", "accepted": True, "reason": None,
        "lineage": {
            "dataset_version": "some-other-dataset", "feature_version": "f1",
            "strategy_version": "sv-mismatch", "seed": 0,
        },
        "params": {"lookback": 3, "threshold": 0.01, "signal_feature": "signal"},
        "costs": {"fee_bps": 1.0, "spread_bps": 0.5, "slippage_bps": 0.5},
    }))
    artifact_path = tmp_path / "artifact.json"
    artifact_path.write_text(json.dumps({
        "strategy_version": "sv-mismatch", "symbol": "BTC-USDT-SWAP",
        "dataset_version": "d1", "feature_schema_version": "f1",
        "label_config": {"base_timeframe": "15m", "horizon": 1, "label_type": "log_return", "threshold": 0.0},
        "promotion_policy_version": "p1", "n_folds": 5, "embargo_bars": 3,
        "holdout_fraction": 0.2, "seed": 0, "costs": {},
        "status": "CANDIDATE",
    }))
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps({
        "min_folds_required": 1, "min_oos_trades": 0,
        "min_positive_fold_fraction": 0.0, "max_drawdown": 1.0,
    }))

    result = _run([
        "evaluate", "--config-version", "v1", "--symbol", "BTC-USDT-SWAP",
        "--strategy-artifact-file", str(artifact_path),
        "--promotion-policy-file", str(policy_path),
        "--holdout-claim-db", str(tmp_path / "claims.db"),
        "--experiment-output-root", str(output_root), "--trial-id", "sv-mismatch",
        "--config-path", str(config_path), "--run-db", str(tmp_path / "runs.db"),
    ])
    assert result["status"] == "ERROR"


def test_evaluate_gives_the_first_holdout_bar_its_research_tail_warmup(tmp_path):
    """#101 review-round-3 P1: the frozen strategy must see `lookback` bars
    of history *before* the first holdout bar to compute momentum there. A
    signal that only becomes distinctive starting exactly at the first
    holdout index — requiring a lookback read into the research-tail feature
    row at (first_holdout_idx - lookback) — must still produce a trade at
    that first holdout bar. Slicing to holdout-only rows and re-indexing
    from 0 (the pre-fix behavior) would make the engine's `index < lookback`
    guard skip it, silently losing that signal."""
    config_path = _write_config(tmp_path / "config.json")
    data_root = tmp_path / "research"
    lake = CryptoDataLake(root=data_root)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    n = 30
    records = []
    for index in range(n):
        available = start + timedelta(minutes=15 * (index + 1))
        close = 100.0 + index * 0.5
        records.append({
            "symbol": "BTC-USDT-SWAP", "timeframe": "15m", "data_kind": "ohlcv",
            "observed_at": (available - timedelta(minutes=15)).isoformat(),
            "available_at": available.isoformat(),
            "source": "offline-fixture",
            "open": close - 0.05, "high": close + 0.1, "low": close - 0.1,
            "close": close, "volume": 10.0,
        })
    version = lake.publish(records, lineage={"sources": ["fixture"]})

    # Compute the first holdout index the same way the handler will, so the
    # fixture's signal jump lands exactly on the warm-up boundary.
    from services.crypto_strategy_research import (
        LabelConfig, build_point_in_time_labels, chronological_holdout,
    )
    bars = lake.read_version(version.dataset_id).to_pylist()
    bars.sort(key=lambda row: row["available_at"])
    for row in bars:
        row.setdefault("close_time", row["available_at"])
    label_config = LabelConfig(base_timeframe="15m", horizon=1)
    labels = build_point_in_time_labels(bars, config=label_config)
    holdout_fraction = 0.2
    split = chronological_holdout(len(labels), fraction=holdout_fraction)
    first_holdout_idx = split.holdout_idx[0]
    lookback = 3

    rows = lake.read_version(version.dataset_id).to_pylist()
    rows.sort(key=lambda row: row["available_at"])
    # Flat/low signal everywhere, jumping high starting exactly at the first
    # holdout row. lookback bars earlier (in the research tail) it is still
    # low, so momentum at the first holdout bar is only large and positive
    # if the engine actually reads that research-tail row.
    feature_rows = [
        {**row, "signal": 10.0 if index >= first_holdout_idx else 0.0}
        for index, row in enumerate(rows)
    ]
    feature_dir = data_root / "datasets" / "features-warmup-1"
    feature_dir.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(feature_rows), feature_dir / "features.parquet")
    (feature_dir / "manifest.json").write_text(json.dumps({
        "dataset_version": "features-warmup-1",
        "lineage": {"base_dataset_version": version.dataset_id},
    }))

    output_root = tmp_path / "experiments"
    trial_dir = output_root / "trials"
    trial_dir.mkdir(parents=True)
    (trial_dir / "sv-warmup-1.json").write_text(json.dumps({
        "trial_id": "sv-warmup-1", "accepted": True, "reason": None,
        "lineage": {
            "dataset_version": version.dataset_id, "feature_version": "features-warmup-1",
            "strategy_version": "sv-warmup-1", "seed": 0,
        },
        "params": {"lookback": lookback, "threshold": 1.0, "signal_feature": "signal"},
        "costs": {"fee_bps": 0.0, "spread_bps": 0.0, "slippage_bps": 0.0},
    }))

    artifact_path = tmp_path / "artifact.json"
    artifact_path.write_text(json.dumps({
        "strategy_version": "sv-warmup-1", "symbol": "BTC-USDT-SWAP",
        "dataset_version": version.dataset_id, "feature_schema_version": "features-warmup-1",
        "label_config": {"base_timeframe": "15m", "horizon": 1, "label_type": "log_return", "threshold": 0.0},
        "promotion_policy_version": "p1", "n_folds": 5, "embargo_bars": 3,
        "holdout_fraction": holdout_fraction, "seed": 0,
        "costs": {"fee_bps": 0.0, "spread_bps": 0.0, "slippage_bps": 0.0},
        "status": "CANDIDATE",
        "fold_metrics": [{"expectancy": 0.01, "max_drawdown": 0.01, "profit_factor": 1.5}],
    }))
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps({
        "min_folds_required": 1, "min_oos_trades": 0,
        "min_positive_fold_fraction": 0.0, "max_drawdown": 1.0,
        "min_profit_factor": 0.0, "require_positive_holdout_expectancy": False,
    }))

    result = _run([
        "evaluate", "--config-version", "v1", "--symbol", "BTC-USDT-SWAP",
        "--strategy-artifact-file", str(artifact_path),
        "--promotion-policy-file", str(policy_path),
        "--holdout-claim-db", str(tmp_path / "claims.db"),
        "--data-root", str(data_root),
        "--experiment-output-root", str(output_root), "--trial-id", "sv-warmup-1",
        "--config-path", str(config_path), "--run-db", str(tmp_path / "runs.db"),
    ])
    assert result["status"] == "DONE"
    # The research-tail warm-up must have produced at least the one trade at
    # the first holdout bar; with the pre-fix bug (holdout-only reindex) this
    # would be trade_count == 0.
    assert result["result"]["artifact"]["holdout_result"]["trade_count"] >= 1


def test_promote_requires_passing_promotion_decision_before_promoted(tmp_path):
    config_path = _write_config(tmp_path / "config.json")
    artifact_path = tmp_path / "artifact.json"
    artifact_path.write_text(json.dumps({
        "strategy_version": "sv-promote-1", "symbol": "BTC-USDT-SWAP",
        "dataset_version": "d1", "feature_schema_version": "f1",
        "label_config": {"base_timeframe": "15m", "horizon": 1, "label_type": "log_return", "threshold": 0.0},
        "promotion_policy_version": "p1", "n_folds": 5, "embargo_bars": 3,
        "holdout_fraction": 0.2, "seed": 0, "costs": {},
        "status": "DEMO", "promotion_decision": None,
    }))

    result = _run([
        "promote", "--config-version", "v1", "--symbol", "BTC-USDT-SWAP",
        "--strategy-artifact-file", str(artifact_path), "--target-status", "PROMOTED",
        "--config-path", str(config_path), "--run-db", str(tmp_path / "runs.db"),
    ])
    assert result["status"] == "ERROR"


def test_promote_rejects_fabricated_pass_without_folds_or_holdout_claim(tmp_path):
    config_path = _write_config(tmp_path / "config.json")
    artifact_path = tmp_path / "forged-artifact.json"
    artifact_path.write_text(json.dumps({
        "strategy_version": "sv-forged", "symbol": "BTC-USDT-SWAP",
        "dataset_version": "d1", "feature_schema_version": "f1",
        "label_config": {
            "base_timeframe": "15m", "horizon": 1,
            "label_type": "log_return", "threshold": 0.0,
        },
        "promotion_policy_version": "p1", "n_folds": 5, "embargo_bars": 3,
        "holdout_fraction": 0.2, "seed": 0, "costs": {},
        "status": "DEMO", "fold_metrics": [],
        "holdout_result": {"expectancy": 999},
        "holdout_evaluated": True,
        "promotion_decision": {
            "passed": True, "failures": [], "policy_version": "forged",
            "artifact_policy_version": "p1",
            "evaluated_at": datetime.now(timezone.utc).isoformat(),
            "fold_count": 0, "fold_metrics_hash": "forged",
            "holdout_result_hash": "forged",
        },
    }))

    result = _run([
        "promote", "--config-version", "v1", "--symbol", "BTC-USDT-SWAP",
        "--strategy-artifact-file", str(artifact_path),
        "--target-status", "PROMOTED",
        "--artifact-registry-db", str(tmp_path / "registry.db"),
        "--holdout-claim-db", str(tmp_path / "claims.db"),
        "--config-path", str(config_path), "--run-db", str(tmp_path / "runs.db"),
    ])
    assert result["status"] == "ERROR"
    assert "fold_metrics" in result["reason"]


def test_promote_persists_transition_in_durable_registry(tmp_path):
    config_path = _write_config(tmp_path / "config.json")
    artifact_path = tmp_path / "artifact.json"
    registry_path = tmp_path / "artifacts.db"
    artifact_path.write_text(json.dumps({
        "strategy_version": "sv-promote-2", "symbol": "BTC-USDT-SWAP",
        "dataset_version": "d1", "feature_schema_version": "f1",
        "label_config": {"base_timeframe": "15m", "horizon": 1, "label_type": "log_return", "threshold": 0.0},
        "promotion_policy_version": "p1", "n_folds": 5, "embargo_bars": 3,
        "holdout_fraction": 0.2, "seed": 0, "costs": {},
        "status": "DRAFT",
    }))

    result = _run([
        "promote", "--config-version", "v1", "--symbol", "BTC-USDT-SWAP",
        "--strategy-artifact-file", str(artifact_path), "--target-status", "CANDIDATE",
        "--artifact-registry-db", str(registry_path),
        "--config-path", str(config_path), "--run-db", str(tmp_path / "runs.db"),
    ])
    assert result["status"] == "DONE"
    assert result["result"]["durable_registry"] == str(registry_path)
    conn = sqlite3.connect(registry_path)
    try:
        persisted = conn.execute(
            "SELECT status FROM strategy_artifacts WHERE strategy_version='sv-promote-2'"
        ).fetchone()
    finally:
        conn.close()
    assert persisted == ("CANDIDATE",)
