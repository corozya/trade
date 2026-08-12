import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
import pyarrow as pa
import pyarrow.parquet as pq

from services.crypto_data_lake import CryptoDataLake
from services.crypto_experiment_runner import (
    ExperimentConfig,
    ExperimentError,
    ExperimentRunner,
    LocalFeatureMomentumEngine,
    main,
)
from services.crypto_research import ResearchToolGate


TOOL_CATALOG = (
    __import__("pathlib").Path(__file__).parents[2]
    / "config"
    / "agent_krypto_research_tool_catalog.json"
)


class _Sink:
    def __init__(self):
        self.calls = 0

    def record(self, **kwargs):
        self.calls += 1
        return "experiment-offline-1"


class _Notes:
    def __init__(self):
        self.items = []

    def append(self, note_id, document, metadata):
        self.items.append((note_id, document, metadata))


def _lake(tmp_path, *, closes=None):
    closes = closes or [100 + index * 0.2 + (index % 7) * 0.05 for index in range(120)]
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    records = []
    for index, close in enumerate(closes):
        available = start + timedelta(minutes=15 * (index + 1))
        records.append(
            {
                "symbol": "BTC-USDT-SWAP",
                "timeframe": "15m",
                "data_kind": "ohlcv",
                "observed_at": available - timedelta(minutes=15),
                "available_at": available,
                "source": "offline",
                "open": close - 0.1,
                "high": close + 0.2,
                "low": close - 0.2,
                "close": close,
                "volume": 10.0,
            }
        )
    lake = CryptoDataLake(tmp_path / "lake")
    version = lake.publish(records, lineage={"sources": [{"name": "offline"}]})
    return lake, version.dataset_id


def _config(dataset_version, **overrides):
    payload = {
        "dataset_version": dataset_version,
        "feature_version": "features-v1",
        "strategy_version": "momentum-v1",
        "symbol": "BTC-USDT-SWAP",
        "timeframe": "15m",
        "seed": 42,
        "strategy_params": {
            "lookback": 3,
            "threshold": 0.0001,
            "signal_feature": "signal",
        },
        "costs": {"fee_bps": 1.0, "spread_bps": 0.5, "slippage_bps": 0.5},
        "bootstrap_samples": 200,
    }
    payload.update(overrides)
    return ExperimentConfig(**payload)


def _runner(tmp_path, lake, dataset_version, *, sink=None, notes=None):
    rows = lake.read_version(dataset_version).to_pylist()
    feature_dir = tmp_path / "features" / "datasets" / "features-v1"
    feature_dir.mkdir(parents=True, exist_ok=True)
    feature_rows = []
    for index, row in enumerate(rows):
        feature_rows.append({**row, "signal": float(index % 11) / 10})
    pq.write_table(pa.Table.from_pylist(feature_rows), feature_dir / "features.parquet")
    (feature_dir / "manifest.json").write_text(
        json.dumps(
            {
                "dataset_version": "features-v1",
                "lineage": {"base_dataset_version": dataset_version},
            }
        )
    )
    return ExperimentRunner(
        lake,
        tmp_path / "features",
        tmp_path / "out",
        experiment_sink=sink or _Sink(),
        research_notes=notes or _Notes(),
        tool_gate=ResearchToolGate(TOOL_CATALOG),
        backtest_engine=LocalFeatureMomentumEngine(),
    )


def _tool():
    return {
        "request_id": "tool-1",
        "requested_by": "agent-krypto-research",
        "tool": "walk_forward",
        "params": {},
        "reason": "offline test",
    }


def test_runner_records_complete_cost_aware_trial_without_holdout_access(tmp_path):
    lake, dataset_version = _lake(tmp_path)
    result = _runner(tmp_path, lake, dataset_version).run(
        _config(dataset_version, tool_request=_tool())
    )

    assert len(result["fold_metrics"]) == 5
    assert result["lineage"]["dataset_version"] == dataset_version
    assert result["lineage"]["feature_version"] == "features-v1"
    assert result["lineage"]["seed"] == 42
    assert result["lineage"]["holdout"]["accessed"] is False
    assert result["lineage"]["holdout"]["sample_count"] > 0
    assert set(result["symbol_metrics"]["BTC-USDT-SWAP"]) == {
        "expectancy",
        "max_drawdown",
        "profit_factor",
        "trade_count",
    }
    assert result["bootstrap_ci"]["confidence"] == 0.95
    assert any(key.startswith("direction:") for key in result["dimension_metrics"])
    assert any(key.startswith("regime:") for key in result["dimension_metrics"])
    assert all(
        "fold:" in key and "|symbol:" in key and "|direction:" in key and "|regime:" in key
        for key in result["slice_metrics"]
    )
    saved = json.loads(
        (tmp_path / "out" / "trials" / f"{result['trial_id']}.json").read_text()
    )
    assert saved == result


def test_same_configuration_is_deterministic_offline(tmp_path):
    lake, dataset_version = _lake(tmp_path)
    runner = _runner(tmp_path, lake, dataset_version)
    config = _config(dataset_version, tool_request=_tool())
    first = runner.run(config)
    second = _runner(tmp_path, lake, dataset_version).run(config)

    assert first == second


def test_parallel_retry_calls_external_sinks_exactly_once(tmp_path):
    lake, dataset_version = _lake(tmp_path)
    sink = _Sink()
    notes = _Notes()
    config = _config(dataset_version, tool_request=_tool())
    runners = [
        _runner(tmp_path, lake, dataset_version, sink=sink, notes=notes)
        for _ in range(2)
    ]

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda runner: runner.run(config), runners))

    assert results[0] == results[1]
    assert sink.calls == 1
    assert len(notes.items) == 1


def test_reserved_trial_is_resumed_but_ambiguous_sink_crash_fails_closed(tmp_path):
    lake, dataset_version = _lake(tmp_path)
    config = _config(dataset_version, tool_request=_tool())
    trial_dir = tmp_path / "out" / "trials"
    trial_dir.mkdir(parents=True)
    state_path = trial_dir / f"{config.trial_id}.state.json"
    reservation = {
        "schema_version": 1,
        "trial_id": config.trial_id,
        "config_hash": config.config_hash,
        "state": "RESERVED",
    }
    state_path.write_text(json.dumps(reservation))
    sink = _Sink()
    notes = _Notes()

    result = _runner(
        tmp_path, lake, dataset_version, sink=sink, notes=notes
    ).run(config)

    assert result["trial_id"] == config.trial_id
    assert sink.calls == 1
    assert len(notes.items) == 1

    result_path = trial_dir / f"{config.trial_id}.json"
    result_path.unlink()
    reservation["state"] = "MLFLOW_IN_FLIGHT"
    state_path.write_text(json.dumps(reservation))
    with pytest.raises(ExperimentError, match="operator recovery"):
        _runner(tmp_path, lake, dataset_version).run(config)


def test_rejected_trial_is_still_persisted_with_lineage_and_costs(tmp_path):
    closes = [100 + ((-1) ** index) * index * 0.02 for index in range(120)]
    lake, dataset_version = _lake(tmp_path, closes=closes)
    runner = _runner(tmp_path, lake, dataset_version)
    result = runner.run(
        _config(
            dataset_version,
            strategy_params={"lookback": 2, "threshold": 0.5, "signal_feature": "signal"},
            tool_request=_tool(),
        )
    )

    assert result["accepted"] is False
    assert result["reason"]
    assert result["costs"]["fee_bps"] == 1.0
    assert runner.registry.rejected()[0].trial_id == result["trial_id"]


def test_cli_requires_operational_adapters(tmp_path):
    _, dataset_version = _lake(tmp_path)
    config_path = tmp_path / "experiment.json"
    config_path.write_text(json.dumps(as_payload(_config(dataset_version))))

    with pytest.raises(SystemExit):
        main(["--lake-root", str(tmp_path / "lake"), "--config", str(config_path)])


def test_invalid_or_too_small_configuration_fails_closed(tmp_path):
    lake, dataset_version = _lake(tmp_path, closes=[100 + index for index in range(20)])
    with pytest.raises(ExperimentError, match="at least 30"):
        _runner(tmp_path, lake, dataset_version).run(
            _config(dataset_version, tool_request=_tool())
        )


def test_request_lineage_is_validated_and_recorded(tmp_path):
    lake, dataset_version = _lake(tmp_path)
    learning = {
        "request_id": "learning-1",
        "base_dataset_version": dataset_version,
        "requested_by": "agent-krypto-research",
        "symbols": ["BTC"],
        "hypothesis": "Momentum remains positive after transaction costs.",
        "features": [{"name": "returns", "timeframe": "15m", "params": {}, "reason": "signal"}],
    }
    tool = {
        "request_id": "tool-1",
        "requested_by": "agent-krypto-research",
        "tool": "walk_forward",
        "params": {},
        "reason": "Validate the frozen strategy offline",
    }
    result = _runner(tmp_path, lake, dataset_version).run(
        _config(dataset_version, learning_request=learning, tool_request=tool)
    )
    assert result["lineage"]["learning_request_id"] == "learning-1"
    assert result["lineage"]["tool_request_id"] == "tool-1"

    with pytest.raises(ExperimentError, match="dataset version"):
        _config(dataset_version, learning_request={**learning, "base_dataset_version": "wrong"})


def test_feature_version_mismatch_and_unapproved_tool_fail_closed(tmp_path):
    lake, dataset_version = _lake(tmp_path)
    runner = _runner(tmp_path, lake, dataset_version)
    manifest = tmp_path / "features" / "datasets" / "features-v1" / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "dataset_version": "features-v1",
                "lineage": {"base_dataset_version": "market-wrong"},
            }
        )
    )
    with pytest.raises(ExperimentError, match="base version mismatch"):
        runner.run(_config(dataset_version, tool_request=_tool()))

    runner = _runner(tmp_path, lake, dataset_version)
    with pytest.raises(ExperimentError, match="not executable"):
        runner.run(
            _config(
                dataset_version,
                tool_request={**_tool(), "tool": "shell_execute"},
            )
        )


def test_trial_write_is_immutable_on_collision(tmp_path):
    lake, dataset_version = _lake(tmp_path)
    runner = _runner(tmp_path, lake, dataset_version)
    config = _config(dataset_version, tool_request=_tool())
    destination = tmp_path / "out" / "trials" / f"{config.trial_id}.json"
    destination.parent.mkdir(parents=True)
    destination.write_text("{}\n")
    with pytest.raises(ExperimentError, match="immutable trial collision"):
        runner.run(config)


def as_payload(config):
    return {
        "dataset_version": config.dataset_version,
        "feature_version": config.feature_version,
        "strategy_version": config.strategy_version,
        "symbol": config.symbol,
        "timeframe": config.timeframe,
        "seed": config.seed,
        "strategy_params": dict(config.strategy_params),
        "costs": dict(config.costs),
        "learning_request": (
            dict(config.learning_request) if config.learning_request is not None else None
        ),
        "tool_request": dict(config.tool_request) if config.tool_request is not None else None,
        "n_folds": config.n_folds,
        "holdout_fraction": config.holdout_fraction,
        "bootstrap_samples": config.bootstrap_samples,
        "code_version": config.code_version,
    }
