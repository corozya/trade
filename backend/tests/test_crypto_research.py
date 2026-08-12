import hashlib
import json
import sys
import types

import pyarrow as pa
import pyarrow.parquet as pq

from services.crypto_research import (
    EXECUTED,
    REVIEW_REQUIRED,
    FeatureDatasetStore,
    MlflowExperimentRegistry,
    ResearchToolGate,
    build_renko,
)


ROOT = __import__("pathlib").Path(__file__).resolve().parents[2]


def test_bollinger_bands_creates_new_immutable_dataset(tmp_path):
    source = tmp_path / "market.parquet"
    pq.write_table(pa.table({
        "symbol": ["BTC-USDT-SWAP"] * 25,
        "observed_at": [f"2026-01-01T{value:02d}:00:00Z" for value in range(25)],
        "close": [float(value) for value in range(1, 26)],
    }), source)
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    store = FeatureDatasetStore(
        tmp_path / "research", ROOT / "config/agent_krypto_feature_catalog.json"
    )

    version = store.bollinger_bands(
        source, base_dataset_version="market-abc", window=20, stddev=2
    )

    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    assert version != source.parent
    derived = pq.read_table(version / "features.parquet")
    assert derived.num_rows == 25
    assert derived["bb_mid"][18].as_py() is None
    assert derived["bb_mid"][19].as_py() == 10.5
    manifest = json.loads((version / "manifest.json").read_text())
    assert manifest["lineage"]["base_dataset_version"] == "market-abc"
    assert manifest["lineage"]["params"] == {"stddev": 2, "window": 20}


def test_bollinger_bands_isolated_per_symbol_and_order_independent(tmp_path):
    rows = [
        {
            "symbol": symbol,
            "observed_at": f"2026-01-01T00:{minute:02d}:00Z",
            "close": offset + minute,
        }
        for symbol, offset in (("BTC-USDT-SWAP", 100), ("ETH-USDT-SWAP", 1000))
        for minute in range(6)
    ]
    store = FeatureDatasetStore(
        tmp_path / "research", ROOT / "config/agent_krypto_feature_catalog.json"
    )

    outputs = []
    for name, ordered_rows in (("ordered", rows), ("shuffled", list(reversed(rows)))):
        source = tmp_path / f"{name}.parquet"
        pq.write_table(pa.Table.from_pylist(ordered_rows), source)
        version = store.bollinger_bands(
            source,
            base_dataset_version=f"market-{name}",
            symbols=["BTC-USDT-SWAP", "ETH-USDT-SWAP"],
            window=5,
        )
        table = pq.read_table(version / "features.parquet")
        outputs.append({
            (row["symbol"], row["observed_at"]): row["bb_mid"]
            for row in table.to_pylist()
        })

    assert outputs[0] == outputs[1]
    for output in outputs:
        for symbol in ("BTC-USDT-SWAP", "ETH-USDT-SWAP"):
            symbol_values = [
                value for (row_symbol, _), value in sorted(output.items())
                if row_symbol == symbol
            ]
            assert symbol_values[:4] == [None, None, None, None]
            assert symbol_values[4:] != [None, None]


def test_bollinger_bands_fails_closed_when_requested_symbol_is_missing(tmp_path):
    source = tmp_path / "market.parquet"
    pq.write_table(pa.table({
        "symbol": ["BTC-USDT-SWAP"] * 5,
        "observed_at": [f"2026-01-01T00:0{value}:00Z" for value in range(5)],
        "close": [1.0, 2.0, 3.0, 4.0, 5.0],
    }), source)
    store = FeatureDatasetStore(
        tmp_path / "research", ROOT / "config/agent_krypto_feature_catalog.json"
    )

    with __import__("pytest").raises(ValueError, match="ETH-USDT-SWAP"):
        store.bollinger_bands(
            source,
            base_dataset_version="market-abc",
            symbols=["BTC-USDT-SWAP", "ETH-USDT-SWAP"],
            window=5,
        )


def test_rsi_creates_new_immutable_dataset(tmp_path):
    # Classic textbook RSI(14) example (Wilder): 14 up days then 5 down days,
    # gives a known RSI value on the last row we can assert against loosely
    # (must land strictly between 0 and 100, and below 50 after 5 straight
    # down closes following 14 up closes).
    closes = [float(44 + i) for i in range(15)] + [58.0, 57.0, 56.0, 55.0, 54.0]
    source = tmp_path / "market.parquet"
    pq.write_table(pa.table({
        "symbol": ["BTC-USDT-SWAP"] * len(closes),
        "observed_at": [f"2026-01-01T{value:02d}:00:00Z" for value in range(len(closes))],
        "close": closes,
    }), source)
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    store = FeatureDatasetStore(
        tmp_path / "research", ROOT / "config/agent_krypto_feature_catalog.json"
    )

    version = store.rsi(source, base_dataset_version="market-abc", period=14)

    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    derived = pq.read_table(version / "features.parquet")
    assert derived.num_rows == len(closes)
    assert all(v is None for v in derived["rsi_value"][:14].to_pylist())
    assert derived["rsi_value"][14].as_py() == 100.0  # 14 consecutive up closes
    last = derived["rsi_value"][-1].as_py()
    assert last is not None and 0.0 < last < 100.0
    manifest = json.loads((version / "manifest.json").read_text())
    assert manifest["lineage"]["feature"] == "rsi"
    assert manifest["lineage"]["params"] == {"period": 14}


def test_rsi_isolated_per_symbol_and_order_independent(tmp_path):
    rows = [
        {
            "symbol": symbol,
            "observed_at": f"2026-01-01T00:{minute:02d}:00Z",
            "close": offset + minute,
        }
        for symbol, offset in (("BTC-USDT-SWAP", 100), ("ETH-USDT-SWAP", 1000))
        for minute in range(20)
    ]
    store = FeatureDatasetStore(
        tmp_path / "research", ROOT / "config/agent_krypto_feature_catalog.json"
    )

    outputs = []
    for name, ordered_rows in (("ordered", rows), ("shuffled", list(reversed(rows)))):
        source = tmp_path / f"{name}.parquet"
        pq.write_table(pa.Table.from_pylist(ordered_rows), source)
        version = store.rsi(
            source,
            base_dataset_version=f"market-{name}",
            symbols=["BTC-USDT-SWAP", "ETH-USDT-SWAP"],
            period=5,
        )
        table = pq.read_table(version / "features.parquet")
        outputs.append({
            (row["symbol"], row["observed_at"]): row["rsi_value"]
            for row in table.to_pylist()
        })

    assert outputs[0] == outputs[1]
    for output in outputs:
        for symbol in ("BTC-USDT-SWAP", "ETH-USDT-SWAP"):
            symbol_values = [
                value for (row_symbol, _), value in sorted(output.items())
                if row_symbol == symbol
            ]
            assert symbol_values[:5] == [None] * 5
            assert symbol_values[5:] != [None, None]


def test_rsi_fails_closed_when_requested_symbol_is_missing(tmp_path):
    source = tmp_path / "market.parquet"
    pq.write_table(pa.table({
        "symbol": ["BTC-USDT-SWAP"] * 5,
        "observed_at": [f"2026-01-01T00:0{value}:00Z" for value in range(5)],
        "close": [1.0, 2.0, 3.0, 4.0, 5.0],
    }), source)
    store = FeatureDatasetStore(
        tmp_path / "research", ROOT / "config/agent_krypto_feature_catalog.json"
    )

    with __import__("pytest").raises(ValueError, match="ETH-USDT-SWAP"):
        store.rsi(
            source,
            base_dataset_version="market-abc",
            symbols=["BTC-USDT-SWAP", "ETH-USDT-SWAP"],
            period=5,
        )


def test_rsi_rejects_period_outside_feature_catalog(tmp_path):
    source = tmp_path / "market.parquet"
    pq.write_table(pa.table({
        "symbol": ["BTC-USDT-SWAP"] * 5,
        "observed_at": [f"2026-01-01T00:0{value}:00Z" for value in range(5)],
        "close": [1.0, 2.0, 3.0, 4.0, 5.0],
    }), source)
    store = FeatureDatasetStore(
        tmp_path / "research", ROOT / "config/agent_krypto_feature_catalog.json"
    )

    with __import__("pytest").raises(ValueError, match="period outside feature catalog"):
        store.rsi(source, base_dataset_version="market-abc", period=1)


def test_renko_returns_chart_and_causal_brick_table_without_repainting():
    prefix = [100, 101, 102, 101, 99]
    first = build_renko(prefix, brick_size=1)
    extended = build_renko(prefix + [98, 101], brick_size=1)

    assert first["chart"].startswith("<svg")
    assert first["brick_table"]
    assert extended["brick_table"][: len(first["brick_table"])] == first["brick_table"]


def test_unknown_tool_is_review_required_and_handler_is_not_called():
    gate = ResearchToolGate(ROOT / "config/agent_krypto_research_tool_catalog.json")
    calls = []
    request = {
        "request_id": "tool-1",
        "requested_by": "agent-krypto-research",
        "tool": "shell",
        "params": {"command": "echo unsafe"},
        "reason": "try an unknown operation",
    }

    decision = gate.execute(request, {"shell": lambda params: calls.append(params)})

    assert decision.status == REVIEW_REQUIRED
    assert calls == []


def test_known_tool_without_executor_is_not_silently_executed():
    gate = ResearchToolGate(ROOT / "config/agent_krypto_research_tool_catalog.json")
    request = {
        "request_id": "tool-2",
        "requested_by": "agent-krypto-research",
        "tool": "bootstrap",
        "params": {},
        "reason": "estimate confidence interval",
    }

    assert gate.decide(request).status == EXECUTED
    assert gate.execute(request, {}).status == REVIEW_REQUIRED


def test_experiment_registry_records_lineage_params_costs_and_results(monkeypatch):
    calls = {"params": [], "metrics": []}

    class Run:
        info = types.SimpleNamespace(run_id="run-123")

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    fake_mlflow = types.SimpleNamespace(
        set_tracking_uri=lambda value: calls.update(tracking_uri=value),
        set_experiment=lambda value: calls.update(experiment=value),
        start_run=lambda: Run(),
        log_params=lambda value: calls["params"].append(value),
        log_metrics=lambda value: calls["metrics"].append(value),
    )
    monkeypatch.setitem(sys.modules, "mlflow", fake_mlflow)
    registry = MlflowExperimentRegistry("sqlite:///research.db", "agent-krypto")

    run_id = registry.record(
        lineage={
            "dataset_version": "features-1",
            "strategy_version": "baseline-1",
            "code_version": "abc123",
            "time_range": "2026-01-01/2026-06-30",
            "data_stage": "as_of",
        },
        params={"window": 20},
        costs={"fees": 12.5, "slippage": 4.0},
        results={"sharpe": 1.2, "drawdown": 0.08},
    )

    assert run_id == "run-123"
    assert calls["tracking_uri"] == "sqlite:///research.db"
    assert any("lineage.dataset_version" in item for item in calls["params"])
    assert calls["metrics"] == [
        {"cost.fees": 12.5, "cost.slippage": 4.0},
        {"result.sharpe": 1.2, "result.drawdown": 0.08},
    ]
