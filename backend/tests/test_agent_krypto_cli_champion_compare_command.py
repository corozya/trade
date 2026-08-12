"""Offline tests for the `champion-compare` CLI command (#151).

AC under test: read-only comparison of an already-evaluated challenger
StrategyArtifact against the current champion — NEVER calls
ChampionRegistry.promote/rollback.
"""
from __future__ import annotations

import io
import json
from contextlib import redirect_stdout

import pytest

import agent_krypto_cli
from services.agent_krypto_artifact_registry import _digest
from services.crypto_champion_registry import ChampionRegistry, compare_to_champion


def _run(argv) -> tuple[int, dict]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        exit_code = agent_krypto_cli.main(argv)
    lines = [line for line in buf.getvalue().splitlines() if line.strip()]
    assert len(lines) == 1, f"expected exactly one JSON line on stdout, got: {lines!r}"
    return exit_code, json.loads(lines[0])


def _artifact_payload(*, strategy_version, symbol="BTC-USDT-SWAP", expectancy, passed=True, holdout_evaluated=True):
    fold_metrics = [
        {"expectancy": expectancy, "max_drawdown": 0.01, "profit_factor": 1.5, "trade_count": 10},
    ]
    holdout_result = {"expectancy": expectancy, "trades": 10}
    payload = {
        "artifact_hash": f"hash-{strategy_version}",
        "strategy_version": strategy_version,
        "symbol": symbol,
        "status": "CANDIDATE",
        "dataset_version": "market-1",
        "feature_schema_version": "features-1",
        "promotion_policy_version": "p1",
        "n_folds": 1,
        "valid_until": "2099-01-01T00:00:00Z",
        "fold_metrics": fold_metrics,
        "holdout_evaluated": holdout_evaluated,
        "promotion_decision": {
            "passed": passed, "failures": [], "policy_version": "p1",
            "artifact_policy_version": "p1", "evaluated_at": "2026-01-01T00:00:00Z",
            "fold_count": 1,
            "fold_metrics_hash": _digest(fold_metrics),
            "holdout_result_hash": _digest(holdout_result),
            "oos_trade_count": 10, "costs_included": True,
        },
        "holdout_result": holdout_result,
    }
    return payload


def test_champion_compare_challenger_wins_with_no_registered_champion(tmp_path):
    artifact_path = tmp_path / "challenger.json"
    artifact_path.write_text(json.dumps(_artifact_payload(strategy_version="v1", expectancy=0.01)))

    exit_code, result = _run([
        "champion-compare",
        "--strategy-artifact-file", str(artifact_path),
        "--symbol", "BTC-USDT-SWAP",
        "--champion-registry-db", str(tmp_path / "champion_registry.db"),
    ])

    assert exit_code == 0, result
    assert result["status"] == "DONE"
    body = result["result"]
    assert body["challenger_wins"] is True
    assert body["champion_strategy_version"] is None
    assert body["challenger_strategy_version"] == "v1"


def test_champion_compare_reads_registered_champion_and_beats_it(tmp_path):
    registry_db = tmp_path / "champion_registry.db"
    registry = ChampionRegistry(registry_db)
    champion_artifact = _artifact_payload(strategy_version="v1", expectancy=0.005)
    registry.promote(
        comparison=compare_to_champion(symbol="BTC-USDT-SWAP", challenger_artifact=champion_artifact, champion_artifact=None),
        challenger_artifact=champion_artifact,
        approved_by="pm",
    )

    challenger_path = tmp_path / "challenger.json"
    challenger_path.write_text(json.dumps(_artifact_payload(strategy_version="v2", expectancy=0.02)))

    exit_code, result = _run([
        "champion-compare",
        "--strategy-artifact-file", str(challenger_path),
        "--symbol", "BTC-USDT-SWAP",
        "--champion-registry-db", str(registry_db),
    ])

    body = result["result"]
    assert body["challenger_wins"] is True
    assert body["champion_strategy_version"] == "v1"
    assert body["champion_holdout_expectancy"] == 0.005


def test_champion_compare_challenger_loses_to_registered_champion(tmp_path):
    registry_db = tmp_path / "champion_registry.db"
    registry = ChampionRegistry(registry_db)
    champion_artifact = _artifact_payload(strategy_version="v1", expectancy=0.02)
    registry.promote(
        comparison=compare_to_champion(symbol="BTC-USDT-SWAP", challenger_artifact=champion_artifact, champion_artifact=None),
        challenger_artifact=champion_artifact,
        approved_by="pm",
    )

    challenger_path = tmp_path / "challenger.json"
    challenger_path.write_text(json.dumps(_artifact_payload(strategy_version="v2", expectancy=0.005)))

    exit_code, result = _run([
        "champion-compare",
        "--strategy-artifact-file", str(challenger_path),
        "--symbol", "BTC-USDT-SWAP",
        "--champion-registry-db", str(registry_db),
    ])

    body = result["result"]
    assert body["challenger_wins"] is False


def test_champion_compare_accepts_explicit_champion_artifact_file(tmp_path):
    champion_path = tmp_path / "champion.json"
    champion_path.write_text(json.dumps(_artifact_payload(strategy_version="v1", expectancy=0.005)))
    challenger_path = tmp_path / "challenger.json"
    challenger_path.write_text(json.dumps(_artifact_payload(strategy_version="v2", expectancy=0.02)))

    exit_code, result = _run([
        "champion-compare",
        "--strategy-artifact-file", str(challenger_path),
        "--champion-artifact-file", str(champion_path),
        "--symbol", "BTC-USDT-SWAP",
    ])

    body = result["result"]
    assert body["challenger_wins"] is True
    assert body["champion_strategy_version"] == "v1"


def test_champion_compare_fails_closed_on_incomplete_challenger_artifact(tmp_path):
    artifact_path = tmp_path / "challenger.json"
    artifact_path.write_text(
        json.dumps(_artifact_payload(strategy_version="v1", expectancy=0.01, passed=False))
    )

    exit_code, result = _run([
        "champion-compare",
        "--strategy-artifact-file", str(artifact_path),
        "--symbol", "BTC-USDT-SWAP",
        "--champion-registry-db", str(tmp_path / "champion_registry.db"),
    ])

    assert exit_code != 0
    assert result["status"] == "ERROR"


def test_champion_compare_never_calls_promote_or_rollback():
    """Structural proof: _handle_champion_compare never references
    ChampionRegistry.promote/rollback — only current_champion() (a read)."""
    import ast
    import inspect
    import agent_krypto_cli as cli_module

    function_source = inspect.getsource(cli_module._handle_champion_compare)
    tree = ast.parse(function_source)
    attribute_calls = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    assert "promote" not in attribute_calls
    assert "rollback" not in attribute_calls
    assert "current_champion" in attribute_calls
