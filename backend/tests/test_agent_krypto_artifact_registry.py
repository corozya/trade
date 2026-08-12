from __future__ import annotations

import sqlite3
import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest

from services.agent_krypto_artifact_registry import (
    ArtifactRegistryError,
    StrategyArtifactRegistry,
)
from services.crypto_strategy_research import (
    ArtifactRejected,
    HoldoutAlreadyClaimedError,
    HoldoutClaimStore,
    LabelConfig,
    StrategyArtifact,
)


SYMBOL = "BTC-USDT-SWAP"
CONTRACT = {
    "expected_dataset_version": "dataset-v1",
    "expected_feature_schema_version": "features-v1",
    "expected_promotion_policy_version": "policy-v1",
}


def _artifact(version: str, *, status: str = "PROMOTED") -> dict:
    fold_metrics = [
        {
            "expectancy": 0.01, "max_drawdown": 0.02,
            "profit_factor": 1.3, "trade_count": 10,
        }
        for _ in range(5)
    ]
    holdout_result = {"expectancy": 0.01}

    def digest(value):
        return hashlib.sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    artifact = StrategyArtifact(
        strategy_version=version,
        symbol=SYMBOL,
        dataset_version="dataset-v1",
        feature_schema_version="features-v1",
        label_config=LabelConfig(base_timeframe="15m"),
        promotion_policy_version="policy-v1",
        n_folds=5,
        embargo_bars=1,
        holdout_fraction=0.2,
        seed=42,
        costs={"fee_bps": 1, "spread_bps": 1, "slippage_bps": 1},
        status=status,
        fold_metrics=fold_metrics,
        holdout_result=holdout_result,
        holdout_evaluated=True,
        promotion_decision={
            "policy_version": "policy-v1",
            "passed": True,
            "failures": [],
            "evaluated_at": datetime.now(timezone.utc).isoformat(),
            "artifact_policy_version": "policy-v1",
            "fold_count": len(fold_metrics),
            "fold_metrics_hash": digest(fold_metrics),
            "holdout_result_hash": digest(holdout_result),
            "oos_trade_count": 50,
            "costs_included": True,
        },
    )
    if status == "PROMOTED":
        artifact.history = [
            {"status": "DEMO", "at": artifact.created_at, "reason": "demo passed"},
            {"status": "PROMOTED", "at": artifact.created_at, "reason": "promotion"},
        ]
    artifact.valid_until = (
        datetime.now(timezone.utc) + timedelta(days=7)
    ).isoformat().replace("+00:00", "Z")
    return artifact.to_dict()


def _claim(path, artifact):
    store = HoldoutClaimStore(path)
    store.claim(
        strategy_version=artifact["strategy_version"],
        config_hash=artifact["artifact_hash"],
        holdout_result=artifact["holdout_result"],
    )
    return store


def test_restart_keeps_exactly_one_active_artifact_and_audit(tmp_path):
    path = tmp_path / "registry.db"
    first = _artifact("strategy-v1")
    claim_store = _claim(tmp_path / "claims.db", first)
    StrategyArtifactRegistry(path).activate(
        first, claim_store=claim_store, reason="offline policy PASS", **CONTRACT
    )

    restarted = StrategyArtifactRegistry(path)
    assert restarted.get_active(symbol=SYMBOL, **CONTRACT)["strategy_version"] == "strategy-v1"
    assert [event["action"] for event in restarted.audit(symbol=SYMBOL)] == ["ACTIVATE"]
    assert restarted.audit(symbol=SYMBOL)[0]["from_status"] == "DEMO"


def test_atomic_activation_retires_previous_artifact(tmp_path):
    registry = StrategyArtifactRegistry(tmp_path / "registry.db")
    first = _artifact("strategy-v1")
    second = _artifact("strategy-v2")
    claim_store = _claim(tmp_path / "claims.db", first)
    _claim(tmp_path / "claims.db", second)
    registry.activate(first, claim_store=claim_store, reason="first PASS", **CONTRACT)
    registry.activate(second, claim_store=claim_store, reason="second PASS", **CONTRACT)

    assert registry.get_active(symbol=SYMBOL, **CONTRACT)["strategy_version"] == "strategy-v2"
    assert [event["action"] for event in registry.audit(symbol=SYMBOL)] == [
        "ACTIVATE",
        "RETIRE",
        "ACTIVATE",
    ]
    assert registry.audit(symbol=SYMBOL)[-1]["from_status"] == "DEMO"
    conn = sqlite3.connect(tmp_path / "registry.db")
    try:
        statuses = dict(conn.execute(
            "SELECT strategy_version,status FROM strategy_artifacts"
        ).fetchall())
    finally:
        conn.close()
    assert statuses == {"strategy-v1": "RETIRED", "strategy-v2": "PROMOTED"}


@pytest.mark.parametrize(
    ("mutation", "match"),
    [
        (lambda artifact: artifact.update(status="DEMO"), "PROMOTED"),
        (lambda artifact: artifact.update(dataset_version="stale"), "dataset_version"),
        (lambda artifact: artifact.update(feature_schema_version="stale"), "feature_schema"),
        (lambda artifact: artifact.update(promotion_policy_version="stale"), "promotion_policy"),
        (lambda artifact: artifact.update(promotion_decision={"passed": False}), "passing"),
        (
            lambda artifact: artifact.update(
                valid_until=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
            ),
            "expired",
        ),
    ],
)
def test_activation_fails_closed_without_changing_active_pointer(
    tmp_path, mutation, match
):
    registry = StrategyArtifactRegistry(tmp_path / "registry.db")
    first = _artifact("strategy-v1")
    claim_store = _claim(tmp_path / "claims.db", first)
    registry.activate(first, claim_store=claim_store, reason="PASS", **CONTRACT)
    rejected = _artifact("strategy-v2")
    mutation(rejected)
    _claim(tmp_path / "claims.db", rejected)

    with pytest.raises((ArtifactRejected, ArtifactRegistryError), match=match):
        registry.activate(
            rejected, claim_store=claim_store, reason="must fail", **CONTRACT
        )
    assert registry.get_active(symbol=SYMBOL, **CONTRACT)["strategy_version"] == "strategy-v1"


def test_missing_or_drifted_active_artifact_requires_wait(tmp_path):
    registry = StrategyArtifactRegistry(tmp_path / "registry.db")
    with pytest.raises(ArtifactRejected, match="exactly one"):
        registry.get_active(symbol=SYMBOL, **CONTRACT)
    artifact = _artifact("strategy-v1")
    claim_store = _claim(tmp_path / "claims.db", artifact)
    registry.activate(
        artifact, claim_store=claim_store, reason="PASS", **CONTRACT
    )
    with pytest.raises(ArtifactRejected, match="dataset_version"):
        registry.get_active(
            symbol=SYMBOL,
            expected_dataset_version="dataset-v2",
            expected_feature_schema_version="features-v1",
            expected_promotion_policy_version="policy-v1",
        )


def test_activation_rejects_holdout_claim_inconsistent_with_artifact(tmp_path):
    artifact = _artifact("strategy-v1")
    claim_store = HoldoutClaimStore(tmp_path / "claims.db")
    claim_store.claim(
        strategy_version=artifact["strategy_version"],
        config_hash=artifact["artifact_hash"],
        holdout_result={"expectancy": -999},
    )
    with pytest.raises(ArtifactRejected, match="claim result"):
        StrategyArtifactRegistry(tmp_path / "registry.db").activate(
            artifact, claim_store=claim_store, reason="forged", **CONTRACT
        )


def test_reactivation_audits_actual_promoted_from_status(tmp_path):
    artifact = _artifact("strategy-v1")
    claim_store = _claim(tmp_path / "claims.db", artifact)
    registry = StrategyArtifactRegistry(tmp_path / "registry.db")
    registry.activate(artifact, claim_store=claim_store, reason="first", **CONTRACT)
    registry.activate(artifact, claim_store=claim_store, reason="idempotent", **CONTRACT)
    assert registry.audit(symbol=SYMBOL)[-1]["from_status"] == "PROMOTED"


def test_holdout_claim_survives_restart_alongside_registry(tmp_path):
    path = tmp_path / "research.db"
    claim = HoldoutClaimStore(path)
    claim.claim(
        strategy_version="strategy-v1",
        config_hash="frozen-config",
        holdout_result={"expectancy": 0.01},
    )
    with pytest.raises(HoldoutAlreadyClaimedError):
        HoldoutClaimStore(path).claim(
            strategy_version="strategy-v1",
            config_hash="frozen-config",
            holdout_result={"expectancy": 0.02},
        )
