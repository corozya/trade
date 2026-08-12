"""Offline E2E for Plane #94: immutable data through safe execution."""

from datetime import datetime, timedelta, timezone

import pyarrow.parquet as pq
import pytest

from services.agent_krypto_cycle import run_agent_krypto_cycle
from services.crypto_data_lake import CryptoDataLake
from services.crypto_research import FeatureDatasetStore
from services.crypto_strategy_research import (
    HoldoutClaimStore,
    LabelConfig,
    PromotionPolicy,
    StrategyArtifact,
    build_point_in_time_labels,
    chronological_holdout,
    purged_expanding_walk_forward,
)


ROOT = __import__("pathlib").Path(__file__).resolve().parents[2]
SYMBOL = "BTC-USDT-SWAP"


def _market_rows(count=120):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = []
    for index in range(count):
        observed = start + timedelta(minutes=15 * index)
        close_time = observed + timedelta(minutes=15)
        rows.append({
            "symbol": SYMBOL,
            "timeframe": "15m",
            "data_kind": "ohlcv",
            "observed_at": observed.isoformat().replace("+00:00", "Z"),
            "available_at": close_time.isoformat().replace("+00:00", "Z"),
            "close_time": close_time.isoformat().replace("+00:00", "Z"),
            "source": "offline-fixture",
            "open": 100.0 + index,
            "high": 102.0 + index,
            "low": 99.0 + index,
            "close": 101.0 + index,
            "volume": 10.0,
        })
    return rows


def _promoted_artifact(tmp_path, dataset_version, feature_version):
    labels = build_point_in_time_labels(
        _market_rows(), config=LabelConfig(base_timeframe="15m", horizon=1)
    )
    split = chronological_holdout(len(labels))
    folds = purged_expanding_walk_forward(
        len(split.research_idx), label_horizon=1, n_folds=5, min_train_size=20
    )
    policy = PromotionPolicy(
        min_folds_required=5,
        min_oos_trades=30,
        min_positive_fold_fraction=0.6,
        max_drawdown=0.15,
        min_profit_factor=1.2,
    )
    artifact = StrategyArtifact(
        strategy_version="e2e-v1",
        symbol=SYMBOL,
        dataset_version=dataset_version,
        feature_schema_version=feature_version,
        label_config=LabelConfig(base_timeframe="15m", horizon=1),
        promotion_policy_version=policy.version,
        n_folds=len(folds),
        embargo_bars=1,
        holdout_fraction=split.holdout_fraction,
        seed=42,
        costs={"taker_fee": 0.0005, "slippage_bps": 2.0},
    )
    artifact.record_fold_metrics([
        {"expectancy": 0.01, "max_drawdown": 0.05, "profit_factor": 1.3}
        for _ in folds
    ])
    artifact.run_final_evaluation(
        {"expectancy": 0.02, "trades": 40},
        claim_store=HoldoutClaimStore(tmp_path / "holdout-claims.sqlite"),
    )
    artifact.transition("CANDIDATE", reason="walk-forward complete")
    assert artifact.evaluate_promotion(
        policy, oos_trade_count=40, costs_included=True, trial_count=1
    )["passed"]
    for status in ("PAPER", "DEMO", "PROMOTED"):
        artifact.transition(status, reason="offline E2E gate passed")
    artifact.set_valid_until(base_timeframe="15m")
    return artifact, policy


def _intent():
    return {
        "idempotency_key": "e2e-offline-btc",
        "symbol": "BTC",
        "side": "BUY",
        "action": "OPEN",
        "qty": 1,
        "atr14": 10,
        "stop_loss_price": 490,
        "take_profit_price": 515,
    }


def test_offline_happy_path_data_to_execution_result(tmp_path):
    lake = CryptoDataLake(tmp_path / "lake")
    version = lake.publish(
        _market_rows(),
        lineage={"sources": [{"name": "offline-fixture"}]},
    )
    feature_store = FeatureDatasetStore(
        tmp_path / "research", ROOT / "config/agent_krypto_feature_catalog.json"
    )
    source_path = tmp_path / "market_data.parquet"
    pq.write_table(lake.read_version(version.dataset_id), source_path)
    feature_path = feature_store.bollinger_bands(
        source_path,
        base_dataset_version=version.dataset_id,
        window=20,
    )
    assert pq.read_table(feature_path / "features.parquet").num_rows == 120
    feature_version = feature_path.name
    artifact, policy = _promoted_artifact(
        tmp_path, version.dataset_id, feature_version
    )
    submitted = []

    def submit_to_portfolio(intent):
        submitted.append(intent)
        return {
            "contractVersion": "1.0",
            "ok": True,
            "state": "filled",
            "exchangeOrderId": "OFFLINE-ORDER",
        }

    result = run_agent_krypto_cycle(
        artifact=artifact,
        symbol=SYMBOL,
        expected_dataset_version=version.dataset_id,
        expected_feature_schema_version=feature_version,
        expected_promotion_policy_version=policy.version,
        trade_intent=_intent(),
        execute=submit_to_portfolio,
    )

    assert result["status"] == "COMPLETED"
    assert result["execution_result"]["ok"] is True
    assert result["execution_result"]["state"] == "filled"
    assert submitted == [_intent()]


def test_unpromoted_strategy_waits_without_execution(tmp_path):
    artifact, policy = _promoted_artifact(tmp_path, "market-e2e", "features-e2e")
    artifact.status = "DEMO"
    calls = []

    result = run_agent_krypto_cycle(
        artifact=artifact,
        symbol=SYMBOL,
        expected_dataset_version="market-e2e",
        expected_feature_schema_version="features-e2e",
        expected_promotion_policy_version=policy.version,
        trade_intent=_intent(),
        execute=lambda intent: calls.append(intent),
    )

    assert result["status"] == "WAIT"
    assert calls == []


def test_agent_wait_intent_never_reaches_execution_boundary(tmp_path):
    artifact, policy = _promoted_artifact(tmp_path, "market-e2e", "features-e2e")
    calls = []

    result = run_agent_krypto_cycle(
        artifact=artifact,
        symbol=SYMBOL,
        expected_dataset_version="market-e2e",
        expected_feature_schema_version="features-e2e",
        expected_promotion_policy_version=policy.version,
        trade_intent={
            "decision": "WAIT",
            "symbol": "BTC",
            "reason": "no confirmed edge",
        },
        execute=lambda intent: calls.append(intent),
    )

    assert result == {
        "status": "WAIT",
        "reason": "no confirmed edge",
        "execution_result": None,
    }
    assert calls == []


@pytest.mark.parametrize("decision", ["FILLED", "UNKNOWN", ""])
def test_unknown_agent_decision_is_fail_closed(tmp_path, decision):
    artifact, policy = _promoted_artifact(tmp_path, "market-e2e", "features-e2e")
    calls = []

    result = run_agent_krypto_cycle(
        artifact=artifact,
        symbol=SYMBOL,
        expected_dataset_version="market-e2e",
        expected_feature_schema_version="features-e2e",
        expected_promotion_policy_version=policy.version,
        trade_intent={"decision": decision, "symbol": "BTC", "reason": "bad"},
        execute=lambda intent: calls.append(intent),
    )

    assert result["status"] == "WAIT"
    assert calls == []


@pytest.mark.parametrize("failed_gate", ["sync_ok", "position_ok", "dataset_ok"])
def test_upstream_uncertainty_is_fail_closed_without_execution(tmp_path, failed_gate):
    artifact, policy = _promoted_artifact(tmp_path, "market-e2e", "features-e2e")
    calls = []
    gates = {"sync_ok": True, "position_ok": True, "dataset_ok": True}
    gates[failed_gate] = False

    result = run_agent_krypto_cycle(
        artifact=artifact,
        symbol=SYMBOL,
        expected_dataset_version="market-e2e",
        expected_feature_schema_version="features-e2e",
        expected_promotion_policy_version=policy.version,
        trade_intent=_intent(),
        execute=lambda intent: calls.append(intent),
        **gates,
    )

    assert result["status"] == "WAIT"
    assert calls == []


@pytest.mark.parametrize(
    "execution",
    [
        lambda _intent: {"ok": False, "state": "unknown"},
        lambda _intent: (_ for _ in ()).throw(TimeoutError("offline timeout")),
    ],
)
def test_execution_uncertainty_is_fail_closed(tmp_path, execution):
    artifact, policy = _promoted_artifact(tmp_path, "market-e2e", "features-e2e")

    result = run_agent_krypto_cycle(
        artifact=artifact,
        symbol=SYMBOL,
        expected_dataset_version="market-e2e",
        expected_feature_schema_version="features-e2e",
        expected_promotion_policy_version=policy.version,
        trade_intent=_intent(),
        execute=execution,
    )

    assert result["status"] == "WAIT"
