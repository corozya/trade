"""Offline tests for the `candidate-cycle` CLI command (#147-149).

AC under test: an INDEPENDENT path from `research-loop` — CandidateGenerator
(#140) -> ExperimentRunner (#96) for exactly one candidate per invocation,
using the durable CandidateCursor (#148) for idempotency. Never promotes,
never places a TradeIntent, never touches ChampionRegistry.
"""
from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone

import pytest
import pyarrow as pa
import pyarrow.parquet as pq

import agent_krypto_cli
from services.crypto_data_lake import CryptoDataLake


def _run(argv) -> tuple[int, dict]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        exit_code = agent_krypto_cli.main(argv)
    lines = [line for line in buf.getvalue().splitlines() if line.strip()]
    assert len(lines) == 1, f"expected exactly one JSON line on stdout, got: {lines!r}"
    return exit_code, json.loads(lines[0])


def _seed_workspace(tmp_path):
    data_root = tmp_path / "research" / "agent-krypto"
    closes = [100 + index * 0.2 + (index % 7) * 0.05 for index in range(120)]
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    records = []
    for index, close in enumerate(closes):
        available = start + timedelta(minutes=15 * (index + 1))
        records.append({
            "symbol": "BTC-USDT-SWAP", "timeframe": "15m", "data_kind": "ohlcv",
            "observed_at": available - timedelta(minutes=15), "available_at": available,
            "source": "offline", "open": close - 0.1, "high": close + 0.2,
            "low": close - 0.2, "close": close, "volume": 10.0,
        })
    lake = CryptoDataLake(data_root)
    version = lake.publish(records, lineage={"sources": [{"name": "offline"}]})
    dataset_id = version.dataset_id

    feature_version = "features-v1"
    feature_dir = data_root / "datasets" / feature_version
    feature_dir.mkdir(parents=True, exist_ok=True)
    rows = lake.read_version(dataset_id).to_pylist()
    feature_rows = [{**row, "bb_percent_b": float(index % 11) / 10} for index, row in enumerate(rows)]
    pq.write_table(pa.Table.from_pylist(feature_rows), feature_dir / "features.parquet")
    (feature_dir / "manifest.json").write_text(json.dumps({
        "dataset_version": feature_version,
        "lineage": {"base_dataset_version": dataset_id},
    }))

    search_space_path = tmp_path / "search_space.json"
    search_space_path.write_text(json.dumps({
        "dataset_version": dataset_id,
        "feature_version": feature_version,
        "lookback": {"low": 2, "high": 4, "step": 1},
        "threshold": {"low": 0.0001, "high": 0.0002, "step": 0.0001},
        "signal_feature": {"choices": ["bb_percent_b"]},
        "model_variant": {"choices": ["local-feature-momentum-v1"]},
    }))
    return data_root, search_space_path


TOOL_CATALOG = (
    __import__("pathlib").Path(__file__).parents[2] / "config" / "agent_krypto_research_tool_catalog.json"
)


def _base_argv(data_root, search_space_path, *, seed=1, run_bucket="bucket-1", cursor_db=None):
    return [
        "candidate-cycle",
        "--search-space-file", str(search_space_path),
        "--seed", str(seed),
        "--symbol", "BTC-USDT-SWAP",
        "--run-bucket", run_bucket,
        "--data-root", str(data_root),
        "--experiment-output-root", str(data_root / "experiments"),
        "--tool-catalog-path", str(TOOL_CATALOG),
        "--candidate-cursor-db", str(cursor_db or (data_root / "candidate_cursor.db")),
        # Always isolated per data_root/tmp_path — the default relative path
        # would otherwise be shared across test runs in the same CWD and
        # collide on InsightReportStore's run_id uniqueness constraint.
        "--insight-reports-db", str(data_root / "insight_reports.db"),
    ]


def test_candidate_cycle_runs_one_candidate_and_trial(tmp_path):
    data_root, search_space_path = _seed_workspace(tmp_path)
    exit_code, result = _run(_base_argv(data_root, search_space_path))

    assert exit_code == 0, result
    assert result["status"] == "DONE"
    body = result["result"]
    assert body["candidate"]["trial_index"] == 0
    assert body["cursor"]["trial_index"] == 0
    assert body["cursor"]["replayed"] is False
    assert body["trial"]["trial_id"]
    assert body["trial"]["lineage"]["holdout"]["accessed"] is False


def test_candidate_cycle_advances_across_invocations(tmp_path):
    data_root, search_space_path = _seed_workspace(tmp_path)
    cursor_db = data_root / "candidate_cursor.db"

    _, first = _run(_base_argv(data_root, search_space_path, run_bucket="bucket-1", cursor_db=cursor_db))
    _, second = _run(_base_argv(data_root, search_space_path, run_bucket="bucket-2", cursor_db=cursor_db))

    assert first["result"]["candidate"]["trial_index"] == 0
    assert second["result"]["candidate"]["trial_index"] == 1
    assert first["result"]["candidate"]["candidate_id"] != second["result"]["candidate"]["candidate_id"]


def test_candidate_cycle_retry_in_same_bucket_is_idempotent(tmp_path):
    data_root, search_space_path = _seed_workspace(tmp_path)
    cursor_db = data_root / "candidate_cursor.db"

    _, first = _run(_base_argv(data_root, search_space_path, run_bucket="bucket-1", cursor_db=cursor_db))
    _, retry = _run(_base_argv(data_root, search_space_path, run_bucket="bucket-1", cursor_db=cursor_db))

    assert retry["result"]["cursor"]["replayed"] is True
    assert retry["result"]["candidate"]["candidate_id"] == first["result"]["candidate"]["candidate_id"]


def test_candidate_cycle_records_insight_report(tmp_path):
    from services.crypto_research_insights import InsightReportStore

    data_root, search_space_path = _seed_workspace(tmp_path)
    exit_code, result = _run(_base_argv(data_root, search_space_path))

    body = result["result"]
    assert body["insight_report_run_id"] == body["trial"]["trial_id"]

    store = InsightReportStore(db_path=data_root / "insight_reports.db")
    saved = store.get(run_id=body["trial"]["trial_id"])
    assert saved is not None
    assert saved["run_id"] == body["trial"]["trial_id"]
    assert saved["schema_version"] == "insights.v1"


def test_candidate_cycle_replay_does_not_duplicate_insight_report(tmp_path):
    data_root, search_space_path = _seed_workspace(tmp_path)
    cursor_db = data_root / "candidate_cursor.db"

    _, first = _run(_base_argv(data_root, search_space_path, run_bucket="bucket-1", cursor_db=cursor_db))
    exit_code, retry = _run(_base_argv(data_root, search_space_path, run_bucket="bucket-1", cursor_db=cursor_db))

    # Replay must not raise/error trying to re-save an already-recorded report.
    assert exit_code == 0
    assert retry["status"] == "DONE"
    assert retry["result"]["insight_report_run_id"] is None
    assert first["result"]["insight_report_run_id"] == first["result"]["trial"]["trial_id"]


def test_candidate_cycle_includes_monitoring_status(tmp_path):
    data_root, search_space_path = _seed_workspace(tmp_path)
    exit_code, result = _run(_base_argv(data_root, search_space_path))

    monitoring = result["result"]["monitoring"]
    assert monitoring["schema_version"] == "monitoring.v1"
    assert monitoring["status"] in {"OK", "WARNING", "CRITICAL"}
    assert any(check["name"] == "data_quality" for check in monitoring["checks"])


def test_candidate_cycle_requires_run_bucket(tmp_path):
    """--run-bucket is a required argparse flag: an invocation without it
    fails closed at the CLI boundary (argparse SystemExit(2)) before the
    handler ever runs — even stronger than a handler-level fail-closed
    check."""
    data_root, search_space_path = _seed_workspace(tmp_path)
    argv = [arg for arg in _base_argv(data_root, search_space_path) if arg not in ("--run-bucket", "bucket-1")]
    with pytest.raises(SystemExit) as excinfo:
        agent_krypto_cli.main(argv)
    assert excinfo.value.code == 2


def test_candidate_cycle_never_imports_champion_registry_promote_path():
    """Structural proof, analogous to
    test_demo_execution_module_is_not_imported_by_research_loop_cli_path: the
    candidate-cycle handler must never reference ChampionRegistry or call
    .promote()/.rollback() — checked at the AST level of the function body
    itself (not string-matching the source, which would also match the
    docstring's own prose about this guarantee).

    Note: the MODULE agent_krypto_cli.py does import ChampionRegistry (#151,
    for the separate champion-compare command) — that import is legitimate
    and expected. This test's guarantee is scoped to _handle_candidate_cycle
    specifically, not "ChampionRegistry is absent from the file".
    """
    import ast
    import inspect
    import agent_krypto_cli as cli_module

    function_source = inspect.getsource(cli_module._handle_candidate_cycle)
    tree = ast.parse(function_source)
    referenced_names = {
        node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
    }
    attribute_calls = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
    }
    assert "ChampionRegistry" not in referenced_names
    assert "promote" not in attribute_calls
    assert "rollback" not in attribute_calls


def test_candidate_cycle_does_not_affect_research_loop_module():
    """research-loop must remain untouched by this addition."""
    import inspect
    import agent_krypto_cli as cli_module

    source = inspect.getsource(cli_module._handle_research_loop)
    assert "candidate" not in source.lower()
    assert "CandidateCursor" not in source
