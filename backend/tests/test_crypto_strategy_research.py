from datetime import datetime, timedelta, timezone

import pytest

from services.crypto_strategy_research import (
    ArtifactRejected,
    Fold,
    HoldoutAlreadyClaimedError,
    HoldoutClaimStore,
    HoldoutSplit,
    LabelConfig,
    PromotionPolicy,
    StrategyArtifact,
    StrategyResearchError,
    TrialRecord,
    TrialRegistry,
    build_point_in_time_labels,
    chronological_holdout,
    join_higher_timeframe_features,
    purged_expanding_walk_forward,
    require_promoted_artifact,
)


def _bars(n, *, start="2026-01-01T00:00:00Z", step_minutes=15, base_close=100.0, drift=0.0):
    start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
    bars = []
    close = base_close
    for i in range(n):
        close = close * (1 + drift)
        bars.append(
            {
                "close_time": (start_dt + timedelta(minutes=step_minutes * i)).isoformat().replace("+00:00", "Z"),
                "close": close,
            }
        )
    return bars


# ---------------------------------------------------------------------------
# Labels: point-in-time, no lookahead
# ---------------------------------------------------------------------------


def test_labels_use_only_next_closed_bar_for_horizon_1():
    bars = _bars(5, drift=0.01)
    config = LabelConfig(base_timeframe="15m", horizon=1, label_type="log_return")

    labels = build_point_in_time_labels(bars, config=config)

    assert len(labels) == 3  # signal bar needs both an entry bar and a target bar ahead
    assert labels[0]["decision_at"] == bars[0]["close_time"]
    assert labels[0]["entry_at"] == bars[1]["close_time"]
    assert labels[0]["entry_price"] == bars[1]["close"]
    assert labels[0]["label_end"] == bars[2]["close_time"]
    assert labels[0]["raw_log_return"] > 0


def test_entry_return_is_measured_from_entry_price_not_signal_bar():
    bars = _bars(4, drift=0.01)
    config = LabelConfig(base_timeframe="15m", horizon=1, label_type="log_return")

    labels = build_point_in_time_labels(bars, config=config)

    import math

    expected_return = math.log(bars[2]["close"] / bars[1]["close"])
    assert labels[0]["raw_log_return"] == pytest.approx(expected_return)


def test_changing_future_bars_does_not_change_earlier_labels():
    bars = _bars(7, drift=0.01)
    config = LabelConfig(base_timeframe="15m", horizon=1)
    labels_before = build_point_in_time_labels(bars, config=config)

    mutated = list(bars)
    mutated[6] = {**mutated[6], "close": mutated[6]["close"] * 5}  # only the last (future) bar changes
    labels_after = build_point_in_time_labels(mutated, config=config)

    # Every label except the one that reads bar[6] as its target is untouched.
    assert labels_before[:4] == labels_after[:4]


def test_entry_never_fills_on_the_signal_bar():
    bars = _bars(4, drift=0.01)
    config = LabelConfig(base_timeframe="15m", horizon=1)

    labels = build_point_in_time_labels(bars, config=config)

    for label in labels:
        assert label["entry_at"] != label["decision_at"]


def test_direction_label_is_deterministic_projection_of_log_return():
    bars = _bars(3, drift=0.05)
    config = LabelConfig(base_timeframe="15m", horizon=1, label_type="direction", threshold=0.001)

    labels = build_point_in_time_labels(bars, config=config)

    assert all(label["value"] == 1.0 for label in labels)


def test_label_config_is_versioned_and_changes_with_params():
    a = LabelConfig(base_timeframe="15m", horizon=1)
    b = LabelConfig(base_timeframe="15m", horizon=2)
    assert a.version != b.version


# ---------------------------------------------------------------------------
# Higher timeframe join: closed-only
# ---------------------------------------------------------------------------


def test_higher_tf_row_is_invisible_until_its_candle_closes():
    base_rows = [{"decision_at": "2026-01-01T01:00:00Z"}]
    higher_tf_rows = [
        {"available_at": "2026-01-01T01:00:01Z", "regime": "trend"},  # closes just after decision
    ]

    joined = join_higher_timeframe_features(base_rows, higher_tf_rows)

    assert joined[0]["higher_tf"] is None


def test_higher_tf_row_is_visible_once_closed_at_or_before_decision():
    base_rows = [{"decision_at": "2026-01-01T01:00:00Z"}]
    higher_tf_rows = [
        {"available_at": "2026-01-01T00:00:00Z", "regime": "range"},
        {"available_at": "2026-01-01T01:00:00Z", "regime": "trend"},
    ]

    joined = join_higher_timeframe_features(base_rows, higher_tf_rows)

    assert joined[0]["higher_tf"]["regime"] == "trend"


def test_partial_higher_tf_candle_never_leaks_forward():
    base_rows = [{"decision_at": "2026-01-01T00:59:59Z"}]
    higher_tf_rows = [{"available_at": "2026-01-01T01:00:00Z", "regime": "trend"}]

    joined = join_higher_timeframe_features(base_rows, higher_tf_rows)

    assert joined[0]["higher_tf"] is None


# ---------------------------------------------------------------------------
# Purged expanding walk-forward
# ---------------------------------------------------------------------------


def test_five_folds_expanding_window_are_produced():
    folds = purged_expanding_walk_forward(500, label_horizon=4, n_folds=5)

    assert len(folds) == 5
    assert all(isinstance(fold, Fold) for fold in folds)
    # Expanding: later folds' test blocks strictly follow earlier ones.
    for earlier, later in zip(folds, folds[1:]):
        assert max(earlier.test_idx) < min(later.test_idx)


def test_purge_removes_samples_whose_label_window_overlaps_test():
    label_horizon = 5
    folds = purged_expanding_walk_forward(200, label_horizon=label_horizon, n_folds=5)

    for fold in folds:
        test_start = min(fold.test_idx)
        # No train sample's label window [i, i+horizon) may reach into test.
        for i in fold.train_idx:
            assert i + label_horizon < test_start


def test_embargo_additionally_removes_boundary_samples():
    folds = purged_expanding_walk_forward(300, label_horizon=2, n_folds=5, embargo_bars=10)

    for fold in folds:
        if not fold.embargo_idx:
            continue
        test_start = min(fold.test_idx)
        assert max(fold.embargo_idx) < test_start
        assert all(i not in fold.train_idx for i in fold.embargo_idx)


def test_default_embargo_uses_max_of_horizon_and_one_percent_rule():
    # 1000 samples -> 1% == 10 > label_horizon(3), so embargo should be 10.
    folds = purged_expanding_walk_forward(1000, label_horizon=3, n_folds=5)
    # Reconstruct implied embargo from a fold with a non-trivial boundary.
    fold = folds[-1]
    test_start = min(fold.test_idx)
    purge_cut = test_start - 3
    if fold.embargo_idx:
        embargo_span = purge_cut - min(fold.embargo_idx)
        assert embargo_span == 10


def test_train_and_test_never_overlap_across_all_folds():
    folds = purged_expanding_walk_forward(400, label_horizon=3, n_folds=5)
    for fold in folds:
        assert not set(fold.train_idx) & set(fold.test_idx)
        assert not set(fold.purged_idx) & set(fold.train_idx)


def test_rejects_when_purge_and_embargo_consume_entire_train_set():
    with pytest.raises(StrategyResearchError):
        purged_expanding_walk_forward(10, label_horizon=1, n_folds=5, embargo_bars=100)


def test_every_fold_has_a_non_empty_train_and_test_set_including_the_first():
    folds = purged_expanding_walk_forward(500, label_horizon=4, n_folds=5)

    assert len(folds) == 5
    for fold in folds:
        assert len(fold.train_idx) > 0
        assert len(fold.test_idx) > 0


def test_explicit_min_train_size_reserves_a_warmup_prefix():
    folds = purged_expanding_walk_forward(300, label_horizon=2, n_folds=5, min_train_size=50)

    assert min(folds[0].test_idx) >= 50
    assert len(folds[0].train_idx) > 0


# ---------------------------------------------------------------------------
# Chronological holdout: last 20%, single evaluation
# ---------------------------------------------------------------------------


def test_holdout_is_last_chronological_20_percent():
    split = chronological_holdout(100, fraction=0.2)

    assert len(split.holdout_idx) == 20
    assert split.holdout_idx == tuple(range(80, 100))
    assert split.research_idx == tuple(range(0, 80))
    assert split.holdout_fraction == pytest.approx(0.2)


def test_holdout_never_overlaps_research_range():
    split = chronological_holdout(137, fraction=0.2)
    assert not set(split.holdout_idx) & set(split.research_idx)
    assert max(split.research_idx) < min(split.holdout_idx)


def _make_artifact(**overrides):
    defaults = dict(
        strategy_version="strat-v1",
        symbol="BTC-USDT-SWAP",
        dataset_version="market-abc",
        feature_schema_version="features-abc",
        label_config=LabelConfig(base_timeframe="15m", horizon=1),
        promotion_policy_version="policy-v1",
        n_folds=5,
        embargo_bars=10,
        holdout_fraction=0.2,
        seed=42,
        costs={"taker_fee": 0.0005, "slippage_bps": 2.0},
    )
    defaults.update(overrides)
    return StrategyArtifact(**defaults)


def test_final_evaluation_is_single_shot_per_strategy_version():
    artifact = _make_artifact()

    artifact.run_final_evaluation(
        {"expectancy": 0.01, "trades": 40},
        claim_store=None,
        allow_unclaimed_for_tests_only=True,
    )

    with pytest.raises(StrategyResearchError):
        artifact.run_final_evaluation(
            {"expectancy": 0.02, "trades": 41},
            claim_store=None,
            allow_unclaimed_for_tests_only=True,
        )


def test_final_evaluation_requires_a_claim_store_by_default():
    artifact = _make_artifact()
    with pytest.raises(StrategyResearchError, match="claim_store is required"):
        artifact.run_final_evaluation({"expectancy": 0.01, "trades": 40}, claim_store=None)


def test_holdout_claim_store_persists_a_durable_once_only_claim(tmp_path):
    store = HoldoutClaimStore(tmp_path / "holdout_claims.db")
    artifact = _make_artifact()

    artifact.run_final_evaluation({"expectancy": 0.01, "trades": 40}, claim_store=store)

    assert store.get(strategy_version=artifact.strategy_version) == {
        "expectancy": 0.01,
        "trades": 40,
    }


def test_holdout_claim_store_rejects_reconstruction_after_restart(tmp_path):
    db_path = tmp_path / "holdout_claims.db"
    store = HoldoutClaimStore(db_path)
    artifact = _make_artifact()
    artifact.run_final_evaluation({"expectancy": 0.01, "trades": 40}, claim_store=store)

    # Simulate a process restart: same strategy_version/config, brand new in-memory
    # StrategyArtifact instance (holdout_evaluated=False) and a fresh store handle
    # pointed at the same durable db file.
    reconstructed = _make_artifact()
    reopened_store = HoldoutClaimStore(db_path)
    with pytest.raises(HoldoutAlreadyClaimedError):
        reconstructed.run_final_evaluation(
            {"expectancy": 0.02, "trades": 41}, claim_store=reopened_store
        )


def test_holdout_claim_store_allows_a_new_strategy_version_to_claim_independently(tmp_path):
    store = HoldoutClaimStore(tmp_path / "holdout_claims.db")
    artifact = _make_artifact(strategy_version="strat-v1")
    artifact.run_final_evaluation({"expectancy": 0.01, "trades": 40}, claim_store=store)

    other = _make_artifact(strategy_version="strat-v2")
    other.run_final_evaluation({"expectancy": 0.02, "trades": 50}, claim_store=store)  # does not raise


def test_holdout_claim_store_rejects_mutated_config_for_same_strategy_version(tmp_path):
    from services.crypto_strategy_research import FrozenConfigViolationError

    store = HoldoutClaimStore(tmp_path / "holdout_claims.db")
    artifact = _make_artifact(strategy_version="strat-v1", seed=42)
    artifact.run_final_evaluation({"expectancy": 0.01, "trades": 40}, claim_store=store)

    # Same strategy_version, but the frozen config changed (seed differs) ->
    # artifact_hash differs -> this must be flagged as a frozen-config
    # violation, not silently treated as a fresh claim or a plain re-run.
    mutated = _make_artifact(strategy_version="strat-v1", seed=99)
    with pytest.raises(FrozenConfigViolationError):
        mutated.run_final_evaluation({"expectancy": 0.05, "trades": 60}, claim_store=store)


def test_fold_metrics_cannot_change_after_final_evaluation():
    artifact = _make_artifact()
    artifact.record_fold_metrics([{"expectancy": 0.01}])
    artifact.run_final_evaluation(
        {"expectancy": 0.01, "trades": 40},
        claim_store=None,
        allow_unclaimed_for_tests_only=True,
    )

    with pytest.raises(StrategyResearchError):
        artifact.record_fold_metrics([{"expectancy": 0.05}])


# ---------------------------------------------------------------------------
# Status transitions
# ---------------------------------------------------------------------------


def test_valid_status_transitions_follow_the_approved_lifecycle():
    artifact = _make_artifact()
    assert artifact.status == "DRAFT"
    artifact.transition("CANDIDATE", reason="walk-forward complete")
    artifact.transition("PAPER", reason="promotion gate passed")
    artifact.transition("DEMO", reason="100+ paper signals")
    artifact.transition("PROMOTED", reason="30+ demo trades, costs verified")
    artifact.transition("RETIRED", reason="superseded")
    assert [entry["status"] for entry in artifact.history] == [
        "DRAFT",
        "CANDIDATE",
        "PAPER",
        "DEMO",
        "PROMOTED",
        "RETIRED",
    ]


def test_illegal_transition_is_rejected():
    artifact = _make_artifact()
    with pytest.raises(StrategyResearchError):
        artifact.transition("PROMOTED", reason="skip gates")


def test_rejected_is_a_terminal_gate_outcome_from_candidate():
    artifact = _make_artifact()
    artifact.transition("CANDIDATE", reason="walk-forward complete")
    artifact.transition("REJECTED", reason="failed promotion gate")
    with pytest.raises(StrategyResearchError):
        artifact.transition("PAPER", reason="retry")


# ---------------------------------------------------------------------------
# TTL / valid_until
# ---------------------------------------------------------------------------


def test_valid_until_defaults_to_7_days_for_intraday_timeframe():
    artifact = _make_artifact()
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    artifact.set_valid_until(base_timeframe="15m", now=now)
    assert artifact.valid_until == "2026-01-08T00:00:00Z"


def test_valid_until_defaults_to_30_days_for_daily_timeframe():
    artifact = _make_artifact()
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    artifact.set_valid_until(base_timeframe="1d", now=now)
    assert artifact.valid_until == "2026-01-31T00:00:00Z"


def test_invalidate_forces_immediate_expiry():
    artifact = _make_artifact()
    artifact.set_valid_until(base_timeframe="15m")
    artifact.invalidate(reason="feature schema drift")
    assert artifact.valid_until <= _iso_now()


def _iso_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Runtime gate: missing/expired/incompatible/not-PROMOTED all reject
# ---------------------------------------------------------------------------


_LIVE_CONTRACT = dict(
    expected_dataset_version="market-abc",
    expected_feature_schema_version="features-abc",
    expected_promotion_policy_version="policy-v1",
)


def _promoted_artifact(*, valid_days=7, symbol="BTC-USDT-SWAP"):
    artifact = _make_artifact(symbol=symbol)
    artifact.record_fold_metrics(
        [{"expectancy": 0.01, "max_drawdown": 0.05, "profit_factor": 1.3} for _ in range(5)]
    )
    artifact.run_final_evaluation(
        {"expectancy": 0.02, "trades": 40},
        claim_store=None,
        allow_unclaimed_for_tests_only=True,
    )
    artifact.transition("CANDIDATE", reason="wf complete")
    policy = PromotionPolicy(
        min_folds_required=5,
        min_oos_trades=30,
        min_positive_fold_fraction=0.6,
        max_drawdown=0.15,
        min_profit_factor=1.2,
    )
    decision = artifact.evaluate_promotion(
        policy, oos_trade_count=40, costs_included=True, trial_count=1
    )
    assert decision["passed"]
    artifact.transition("PAPER", reason="gate passed")
    artifact.transition("DEMO", reason="paper ok")
    artifact.transition("PROMOTED", reason="demo ok")
    artifact.set_valid_until(base_timeframe="15m")
    return artifact


def test_runtime_accepts_fresh_promoted_artifact():
    artifact = _promoted_artifact()
    payload = require_promoted_artifact(artifact, symbol="BTC-USDT-SWAP", **_LIVE_CONTRACT)
    assert payload["status"] == "PROMOTED"


def test_runtime_rejects_missing_artifact():
    with pytest.raises(ArtifactRejected):
        require_promoted_artifact({}, symbol="BTC-USDT-SWAP", **_LIVE_CONTRACT)


def test_runtime_rejects_when_expected_contract_context_is_omitted():
    artifact = _promoted_artifact()
    # Fail-closed: omitting any expected_* kwarg must not silently skip the
    # compatibility check — the gate simply cannot be called without it.
    with pytest.raises(TypeError):
        require_promoted_artifact(artifact, symbol="BTC-USDT-SWAP")


def test_runtime_rejects_expired_artifact():
    artifact = _promoted_artifact()
    future = datetime.fromisoformat(artifact.valid_until.replace("Z", "+00:00")) + timedelta(days=1)
    with pytest.raises(ArtifactRejected, match="expired"):
        require_promoted_artifact(artifact, symbol="BTC-USDT-SWAP", now=future, **_LIVE_CONTRACT)


def test_runtime_rejects_non_promoted_status():
    artifact = _make_artifact()
    artifact.set_valid_until(base_timeframe="15m")
    with pytest.raises(ArtifactRejected, match="PROMOTED"):
        require_promoted_artifact(artifact, symbol="BTC-USDT-SWAP", **_LIVE_CONTRACT)


def test_runtime_rejects_incompatible_symbol():
    artifact = _promoted_artifact(symbol="BTC-USDT-SWAP")
    with pytest.raises(ArtifactRejected, match="symbol mismatch"):
        require_promoted_artifact(artifact, symbol="ETH-USDT-SWAP", **_LIVE_CONTRACT)


def test_runtime_rejects_artifact_without_valid_until():
    artifact = _promoted_artifact()
    artifact.valid_until = None
    with pytest.raises(ArtifactRejected, match="valid_until"):
        require_promoted_artifact(artifact, symbol="BTC-USDT-SWAP", **_LIVE_CONTRACT)


def test_runtime_rejects_artifact_with_stale_dataset_version():
    artifact = _promoted_artifact()
    with pytest.raises(ArtifactRejected, match="dataset_version"):
        require_promoted_artifact(
            artifact,
            symbol="BTC-USDT-SWAP",
            expected_dataset_version="market-xyz-newer",
            expected_feature_schema_version="features-abc",
            expected_promotion_policy_version="policy-v1",
        )


def test_runtime_rejects_artifact_with_stale_feature_schema_version():
    artifact = _promoted_artifact()
    with pytest.raises(ArtifactRejected, match="feature_schema_version"):
        require_promoted_artifact(
            artifact,
            symbol="BTC-USDT-SWAP",
            expected_dataset_version="market-abc",
            expected_feature_schema_version="features-newer",
            expected_promotion_policy_version="policy-v1",
        )


def test_runtime_rejects_artifact_with_stale_promotion_policy_version():
    artifact = _promoted_artifact()
    with pytest.raises(ArtifactRejected, match="promotion_policy_version"):
        require_promoted_artifact(
            artifact,
            symbol="BTC-USDT-SWAP",
            expected_dataset_version="market-abc",
            expected_feature_schema_version="features-abc",
            expected_promotion_policy_version="policy-v2",
        )


def test_runtime_accepts_artifact_matching_expected_contract():
    artifact = _promoted_artifact()
    payload = require_promoted_artifact(artifact, symbol="BTC-USDT-SWAP", **_LIVE_CONTRACT)
    assert payload["status"] == "PROMOTED"


# ---------------------------------------------------------------------------
# PromotionPolicy gates
# ---------------------------------------------------------------------------


def test_promotion_policy_thresholds_are_not_hardcoded_and_versioned():
    lenient = PromotionPolicy(
        min_folds_required=1, min_oos_trades=1, min_positive_fold_fraction=0.1, max_drawdown=0.5
    )
    strict = PromotionPolicy(
        min_folds_required=5, min_oos_trades=30, min_positive_fold_fraction=0.6, max_drawdown=0.15
    )
    assert lenient.version != strict.version


def test_promotion_fails_without_full_folds_costs_trades_and_holdout():
    policy = PromotionPolicy(
        min_folds_required=5, min_oos_trades=30, min_positive_fold_fraction=0.6, max_drawdown=0.15
    )
    passed, failures = policy.evaluate(
        fold_metrics=[{"expectancy": 0.01, "max_drawdown": 0.05, "profit_factor": 1.3}],
        oos_trade_count=5,
        costs_included=False,
        holdout_metrics=None,
        trial_count=1,
    )
    assert not passed
    assert any("folds" in f for f in failures)
    assert any("costs" in f for f in failures)
    assert any("OOS trades" in f for f in failures)
    assert any("holdout" in f for f in failures)


def test_promotion_fails_when_degradation_vs_train_exceeds_limit():
    policy = PromotionPolicy(
        min_folds_required=1,
        min_oos_trades=1,
        min_positive_fold_fraction=0.1,
        max_drawdown=0.5,
        max_degradation_vs_train=0.3,
    )
    passed, failures = policy.evaluate(
        fold_metrics=[{"expectancy": 0.01, "max_drawdown": 0.05, "profit_factor": 1.3}],
        oos_trade_count=10,
        costs_included=True,
        holdout_metrics={"expectancy": 0.001},
        train_expectancy=0.01,
        trial_count=1,
    )
    assert not passed
    assert any("degradation vs train" in f for f in failures)


def test_promotion_fails_when_bootstrap_ci_lower_bound_is_negative():
    policy = PromotionPolicy(
        min_folds_required=1,
        min_oos_trades=1,
        min_positive_fold_fraction=0.1,
        max_drawdown=0.5,
        min_bootstrap_ci_lower_bound=0.0,
    )
    passed, failures = policy.evaluate(
        fold_metrics=[{"expectancy": 0.01, "max_drawdown": 0.05, "profit_factor": 1.3}],
        oos_trade_count=10,
        costs_included=True,
        holdout_metrics={"expectancy": 0.01},
        bootstrap_ci_lower_bound=-0.002,
        trial_count=1,
    )
    assert not passed
    assert any("bootstrap 95% CI" in f for f in failures)


def test_promotion_fails_when_oos_trades_per_symbol_direction_are_insufficient():
    policy = PromotionPolicy(
        min_folds_required=1,
        min_oos_trades=1,
        min_positive_fold_fraction=0.1,
        max_drawdown=0.5,
        min_oos_trades_per_symbol_direction=30,
    )
    passed, failures = policy.evaluate(
        fold_metrics=[{"expectancy": 0.01, "max_drawdown": 0.05, "profit_factor": 1.3}],
        oos_trade_count=100,
        costs_included=True,
        holdout_metrics={"expectancy": 0.01},
        oos_trades_per_symbol_direction={"BTC-USDT-SWAP:long": 40, "BTC-USDT-SWAP:short": 12},
        trial_count=1,
    )
    assert not passed
    assert any("per symbol/direction" in f for f in failures)


def test_promotion_requires_positive_drawdown_and_profit_factor_gates():
    policy = PromotionPolicy(
        min_folds_required=1,
        min_oos_trades=1,
        min_positive_fold_fraction=0.5,
        max_drawdown=0.10,
        min_profit_factor=1.5,
    )
    passed, failures = policy.evaluate(
        fold_metrics=[{"expectancy": 0.01, "max_drawdown": 0.20, "profit_factor": 1.1}],
        oos_trade_count=10,
        costs_included=True,
        holdout_metrics={"expectancy": 0.01},
        trial_count=1,
    )
    assert not passed
    assert any("drawdown" in f for f in failures)
    assert any("profit factor" in f for f in failures)


def test_multiple_testing_trial_one_is_unpenalized_and_more_trials_never_loosen_gate():
    policy = PromotionPolicy(
        min_folds_required=1,
        min_oos_trades=1,
        min_positive_fold_fraction=0,
        max_drawdown=1,
        min_profit_factor=0,
        require_positive_holdout_expectancy=False,
    )
    inputs = {
        "fold_metrics": [{"expectancy": 0.01, "max_drawdown": 0, "profit_factor": 2}],
        "oos_trade_count": 100,
        "costs_included": True,
        "holdout_metrics": {"expectancy": 0.01},
    }
    baseline, _ = policy.evaluate(**inputs, trial_count=1)
    searched, failures = policy.evaluate(**inputs, trial_count=10)
    assert baseline is True
    assert searched is False
    assert any("OOS expectancy" in failure for failure in failures)


@pytest.mark.parametrize("trial_count", [None, 0, -1])
def test_multiple_testing_missing_or_invalid_trial_count_fails_closed(trial_count):
    policy = PromotionPolicy(
        min_folds_required=1,
        min_oos_trades=1,
        min_positive_fold_fraction=0,
        max_drawdown=1,
        min_profit_factor=0,
        require_positive_holdout_expectancy=False,
    )
    passed, failures = policy.evaluate(
        fold_metrics=[{"expectancy": 1, "max_drawdown": 0, "profit_factor": 2}],
        oos_trade_count=100,
        costs_included=True,
        holdout_metrics={"expectancy": 1},
        trial_count=trial_count,
    )
    assert passed is False
    assert any("trial_count" in failure for failure in failures)


def test_strategy_artifact_promotion_cannot_bypass_registry_trial_count():
    artifact = _make_artifact()
    artifact.record_fold_metrics(
        [{"expectancy": 0.01, "max_drawdown": 0, "profit_factor": 2}]
    )
    artifact.run_final_evaluation(
        {"expectancy": 0.01},
        claim_store=None,
        allow_unclaimed_for_tests_only=True,
    )
    artifact.transition("CANDIDATE", reason="research complete")
    registry = TrialRegistry()
    registry.record(TrialRecord("accepted", "a", (), accepted=True))
    registry.record(TrialRecord("rejected", "b", (), accepted=False, reason="gate"))
    policy = PromotionPolicy(
        min_folds_required=1,
        min_oos_trades=1,
        min_positive_fold_fraction=0,
        max_drawdown=1,
        min_profit_factor=0,
        require_positive_holdout_expectancy=False,
    )
    decision = artifact.evaluate_promotion(
        policy,
        oos_trade_count=100,
        costs_included=True,
        trial_registry=registry,
    )
    assert decision["passed"] is False
    assert decision["trial_count"] == 2
    assert decision["multiple_testing_corrected_threshold"] > 0


# ---------------------------------------------------------------------------
# Trial registry (accepted + rejected trials retained)
# ---------------------------------------------------------------------------


def test_trial_registry_keeps_both_accepted_and_rejected_trials():
    registry = TrialRegistry()
    registry.record(TrialRecord("t1", "hash1", ({"expectancy": 0.01},), accepted=True))
    registry.record(TrialRecord("t2", "hash2", ({"expectancy": -0.01},), accepted=False, reason="negative"))

    assert len(registry.trials) == 2
    assert len(registry.accepted()) == 1
    assert len(registry.rejected()) == 1


def test_trial_registry_rejects_duplicate_trial_ids():
    registry = TrialRegistry()
    registry.record(TrialRecord("t1", "hash1", (), accepted=True))
    with pytest.raises(StrategyResearchError):
        registry.record(TrialRecord("t1", "hash2", (), accepted=False))
