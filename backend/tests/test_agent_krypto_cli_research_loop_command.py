"""Offline observation-mode research loop for the agent-krypto CLI (#116/#118).

AC under test: one CLI invocation (`agent_krypto_cli.py research-loop`) drives
ingest -> LearningRequest (bollinger_bands) -> experiment+evaluate
(crypto_multi_symbol_experiment) -> cycle over REAL ingest data (never the
synthetic ``e2e`` fixture), with no network dependency, no promote step, and
a cycle phase that can only ever resolve to WAIT with execution_result=None.
It must also be idempotent per 15-minute run_bucket.
"""

from __future__ import annotations

import io
import json
import socket
from contextlib import redirect_stdout
from datetime import timedelta
from unittest.mock import patch

import agent_krypto_cli
from services.crypto_data_lake import DATA_KINDS, SYMBOLS, TIMEFRAMES


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


def _fixture_records(as_of, *, n_bars: int = 2000):
    """Deterministic multi-symbol/multi-timeframe OHLCV fixture — every
    SYMBOLS x TIMEFRAMES pair plus every non-ohlcv DATA_KINDS stream, with
    enough 15m bars per symbol for purge/embargo/holdout/walk-forward to
    produce real folds (not an in-process ``e2e``-style synthetic dataset:
    this is what a real LocalFeather/OKX ingest adapter would hand off)."""
    records: list[dict] = []
    for symbol_index, symbol in enumerate(SYMBOLS):
        for timeframe in TIMEFRAMES:
            if timeframe == "15m":
                start = as_of - timedelta(minutes=15 * n_bars)
                for index in range(n_bars):
                    available = start + timedelta(minutes=15 * (index + 1))
                    # Alternate drift per symbol so folds see both signs.
                    close = 100.0 + ((-1) ** symbol_index) * index * 0.05
                    records.append({
                        "symbol": symbol, "timeframe": timeframe, "data_kind": "ohlcv",
                        "observed_at": (available - timedelta(minutes=15)).isoformat(),
                        "available_at": available.isoformat(),
                        "source": "offline-research-loop-fixture",
                        "open": close - 0.05, "high": close + 0.1, "low": close - 0.1,
                        "close": close, "volume": 10.0,
                    })
            else:
                records.append({
                    "symbol": symbol, "timeframe": timeframe, "data_kind": "ohlcv",
                    "observed_at": (as_of - timedelta(minutes=1)).isoformat(),
                    "available_at": as_of.isoformat(),
                    "source": "offline-research-loop-fixture",
                    "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "volume": 10.0,
                })
        for kind in DATA_KINDS:
            if kind == "ohlcv":
                continue
            records.append({
                "symbol": symbol, "timeframe": "1m", "data_kind": kind,
                "observed_at": (as_of - timedelta(minutes=1)).isoformat(),
                "available_at": as_of.isoformat(),
                "source": "offline-research-loop-fixture",
                "value": 1.0,
            })
    return {"records": records, "sha256": "offline-research-loop-fixture"}


def _run(argv) -> tuple[int, dict]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        exit_code = agent_krypto_cli.main(argv)
    lines = [line for line in buf.getvalue().splitlines() if line.strip()]
    assert len(lines) == 1, f"expected exactly one JSON line on stdout, got: {lines!r}"
    return exit_code, json.loads(lines[0])


class _NetworkAttempted(AssertionError):
    pass


def _blocked_socket(*args, **kwargs):  # pragma: no cover - only hit on regression
    raise _NetworkAttempted("agent_krypto_cli research-loop attempted a real network socket")


def _write_fixture(tmp_path, now):
    fixture_path = tmp_path / "market.json"
    fixture_path.write_text(json.dumps(_fixture_records(now)))
    return fixture_path


def test_research_loop_runs_full_chain_over_real_ingest_no_network(tmp_path):
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    config_path = _write_config(tmp_path / "config.json")
    fixture_path = _write_fixture(tmp_path, now)

    with patch("socket.socket", side_effect=_blocked_socket):
        exit_code, result = _run([
            "research-loop", "--config-version", "v1",
            "--data-root", str(tmp_path / "research" / "agent-krypto"),
            "--config-path", str(config_path),
            "--run-db", str(tmp_path / "research" / "agent-krypto" / "runs" / "orchestrator_runs.db"),
            "--source-path", str(fixture_path),
            "--as-of", now.isoformat(),
            "--max-age-minutes", str(60 * 24 * 365),
            "--run-bucket", "test-bucket-1",
        ])

    assert exit_code == 0, result
    assert result["status"] == "DONE", result
    phases = result["result"]["phases"]
    assert phases["ingest"]["dataset_id"]
    assert set(phases["ingest"]["symbols"]) == set(SYMBOLS)
    assert phases["request"]["feature_version"]
    assert phases["experiment"]["status"] in ("accepted", "rejected")
    assert phases["evaluate"]["status"] in ("ACCEPTED", "REJECTED")
    # No TradeIntent was ever supplied — this command never places an order;
    # the cycle phase must gate to WAIT, not COMPLETED, and evaluate must
    # never itself trigger a PROMOTED artifact.
    assert phases["cycle"]["status"] == "WAIT"
    assert phases["cycle"]["execution_result"] is None
    assert phases["paper"]["ledger_path"]
    assert len(phases["paper"]["decisions"]) == len(SYMBOLS)
    assert all(item["decision"] == "WAIT" for item in phases["paper"]["decisions"])
    assert "promote" not in phases


def test_research_loop_is_idempotent_for_the_same_run_bucket(tmp_path):
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    config_path = _write_config(tmp_path / "config.json")
    fixture_path = _write_fixture(tmp_path, now)
    data_root = tmp_path / "research" / "agent-krypto"
    run_db = data_root / "runs" / "orchestrator_runs.db"
    argv = [
        "research-loop", "--config-version", "v1", "--data-root", str(data_root),
        "--config-path", str(config_path), "--run-db", str(run_db),
        "--source-path", str(fixture_path), "--as-of", now.isoformat(),
        "--max-age-minutes", str(60 * 24 * 365),
        "--run-bucket", "idempotent-bucket",
    ]

    first_exit, first = _run(argv)
    second_exit, second = _run(argv)

    assert first_exit == 0 and second_exit == 0
    assert first["run_id"] == second["run_id"]
    assert first["result"] == second["result"]
    assert second["reason"] == "ok"


def test_research_loop_never_reaches_okx_execution_or_trade_intent(tmp_path):
    """No TradeIntent path exists to reach: this command's cycle call never
    receives --trade-intent-file/--portfolio-id/--tracker-db-path, so
    services.portfolio_client.PortfolioClient can never be instantiated,
    let alone called, from this code path."""
    import sys

    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    config_path = _write_config(tmp_path / "config.json")
    fixture_path = _write_fixture(tmp_path, now)

    sys.modules.pop("services.portfolio_client", None)
    _, result = _run([
        "research-loop", "--config-version", "v1",
        "--data-root", str(tmp_path / "research" / "agent-krypto"),
        "--config-path", str(config_path),
        "--run-db", str(tmp_path / "research" / "agent-krypto" / "runs" / "orchestrator_runs.db"),
        "--source-path", str(fixture_path), "--as-of", now.isoformat(),
        "--max-age-minutes", str(60 * 24 * 365),
        "--run-bucket", "no-execution-bucket",
    ])

    assert result["status"] == "DONE"
    assert result["result"]["phases"]["cycle"]["execution_result"] is None
    # portfolio_client is imported lazily inside _handle_cycle's local
    # `_execute` closure, which is never invoked without a trade_intent —
    # a regression that started importing/calling it would show up here.
    assert "services.portfolio_client" not in sys.modules


def test_research_loop_fails_closed_on_stale_data(tmp_path):
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    config_path = _write_config(tmp_path / "config.json")
    stale_records = _fixture_records(now - timedelta(days=10))
    fixture_path = tmp_path / "stale.json"
    fixture_path.write_text(json.dumps(stale_records))

    exit_code, result = _run([
        "research-loop", "--config-version", "v1",
        "--data-root", str(tmp_path / "research" / "agent-krypto"),
        "--config-path", str(config_path),
        "--run-db", str(tmp_path / "research" / "agent-krypto" / "runs" / "orchestrator_runs.db"),
        "--source-path", str(fixture_path), "--as-of", now.isoformat(),
        "--max-age-minutes", "20",
        "--run-bucket", "stale-bucket",
    ])

    assert exit_code == 1
    assert result["status"] == "ERROR"
    assert "stale" in result["reason"]
    assert result["result"] is None
