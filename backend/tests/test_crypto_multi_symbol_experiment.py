import json
import socket

import pytest

from services.crypto_research_policy import (
    atomic_update_versions,
    canonical_version,
    corrected_expectancy_threshold,
)
from services.crypto_strategy_research import LabelConfig, StrategyResearchError, build_point_in_time_labels, purged_expanding_walk_forward


def test_more_trials_never_loosen_multiple_testing_gate_and_rejected_are_not_excluded():
    one = corrected_expectancy_threshold(base_threshold=0, trial_count=1, oos_trade_count=100)
    ten_including_rejected = corrected_expectancy_threshold(
        base_threshold=0, trial_count=10, oos_trade_count=100
    )
    assert ten_including_rejected > one


def test_triple_barrier_never_reads_after_horizon_and_tie_is_stop_loss():
    bars = [
        {"close_time": f"2026-01-01T00:{minute:02d}:00Z", "close": 100.0, "high": high, "low": low}
        for minute, high, low in [
            (0, 100, 100), (1, 100, 100), (2, 102, 98), (3, 999, 1), (4, 999, 1)
        ]
    ]
    config = LabelConfig(
        base_timeframe="1m", horizon=1, label_type="triple_barrier",
        take_profit=0.01, stop_loss=0.01, tie_break="stop_loss",
    )
    labels = build_point_in_time_labels(bars, config=config)
    assert labels[0]["barrier_hit"] == "stop_loss"
    assert labels[0]["value"] == -1
    mutated = [dict(row) for row in bars]
    mutated[3].update(high=10_000, low=0.01)
    assert build_point_in_time_labels(mutated, config=config)[0] == labels[0]


def test_embargo_cannot_be_shorter_than_max_label_horizon():
    with pytest.raises(StrategyResearchError, match="embargo_bars"):
        purged_expanding_walk_forward(100, label_horizon=16, embargo_bars=15)


def test_versions_are_content_addressed_and_config_update_is_atomic(tmp_path):
    config = {
        "dataset_version": "market-real",
        "feature_schema_version": "features-real",
        "label_config_version": "unset",
        "promotion_policy_version": "unset",
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    label_version = canonical_version("labels", {"horizon": 16})
    policy_version = canonical_version("policy", {"min_trades": 100})
    updated = atomic_update_versions(
        path, label_version=label_version, policy_version=policy_version
    )
    assert updated["dataset_version"] == "market-real"
    assert updated["feature_schema_version"] == "features-real"
    assert updated["label_config_version"] == label_version
    assert updated["promotion_policy_version"] == policy_version


def test_module_has_no_network_side_effect(monkeypatch):
    monkeypatch.setattr(socket, "socket", lambda *args, **kwargs: pytest.fail("network attempted"))
    assert canonical_version("policy", {"offline": True}).startswith("policy-")
