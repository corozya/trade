import pytest

from services.crypto_champion_registry import (
    ChampionRegistry,
    ChampionRegistryError,
    compare_to_champion,
)


def _artifact(*, strategy_version, symbol="BTC-USDT-SWAP", expectancy, passed=True, failures=None, holdout_evaluated=True):
    return {
        "strategy_version": strategy_version,
        "symbol": symbol,
        "status": "CANDIDATE",
        "holdout_evaluated": holdout_evaluated,
        "holdout_result": {"expectancy": expectancy, "trade_count": 10},
        "promotion_decision": {
            "passed": passed,
            "failures": failures or [],
            "policy_version": "policy-1",
            "evaluated_at": "2026-07-24T10:00:00Z",
        },
    }


# ---------------------------------------------------------------------------
# compare_to_champion
# ---------------------------------------------------------------------------


def test_compare_with_no_incumbent_champion_always_wins():
    challenger = _artifact(strategy_version="v2", expectancy=0.001)
    result = compare_to_champion(symbol="BTC-USDT-SWAP", challenger_artifact=challenger, champion_artifact=None)
    assert result.challenger_wins is True
    assert result.champion_strategy_version is None


def test_compare_challenger_beats_champion_on_higher_expectancy():
    champion = _artifact(strategy_version="v1", expectancy=0.0005)
    challenger = _artifact(strategy_version="v2", expectancy=0.001)
    result = compare_to_champion(symbol="BTC-USDT-SWAP", challenger_artifact=challenger, champion_artifact=champion)
    assert result.challenger_wins is True
    assert result.champion_holdout_expectancy == 0.0005


def test_compare_ties_favor_incumbent_champion():
    champion = _artifact(strategy_version="v1", expectancy=0.001)
    challenger = _artifact(strategy_version="v2", expectancy=0.001)
    result = compare_to_champion(symbol="BTC-USDT-SWAP", challenger_artifact=challenger, champion_artifact=champion)
    assert result.challenger_wins is False


def test_compare_rejects_challenger_without_passed_decision():
    champion = _artifact(strategy_version="v1", expectancy=0.0005)
    challenger = _artifact(strategy_version="v2", expectancy=0.001, passed=False, failures=["bad"])
    with pytest.raises(ChampionRegistryError):
        compare_to_champion(symbol="BTC-USDT-SWAP", challenger_artifact=challenger, champion_artifact=champion)


def test_compare_rejects_challenger_without_completed_holdout():
    challenger = _artifact(strategy_version="v2", expectancy=0.001, holdout_evaluated=False)
    with pytest.raises(ChampionRegistryError):
        compare_to_champion(symbol="BTC-USDT-SWAP", challenger_artifact=challenger, champion_artifact=None)


def test_compare_rejects_symbol_mismatch():
    challenger = _artifact(strategy_version="v2", expectancy=0.001, symbol="ETH-USDT-SWAP")
    with pytest.raises(ChampionRegistryError):
        compare_to_champion(symbol="BTC-USDT-SWAP", challenger_artifact=challenger, champion_artifact=None)


def test_compare_rejects_same_strategy_version_as_champion():
    artifact = _artifact(strategy_version="v1", expectancy=0.001)
    with pytest.raises(ChampionRegistryError):
        compare_to_champion(symbol="BTC-USDT-SWAP", challenger_artifact=artifact, champion_artifact=artifact)


# ---------------------------------------------------------------------------
# ChampionRegistry.promote — no auto-promotion, manual gate
# ---------------------------------------------------------------------------


def test_promote_requires_approved_by(tmp_path):
    registry = ChampionRegistry(tmp_path / "registry.db")
    challenger = _artifact(strategy_version="v1", expectancy=0.001)
    comparison = compare_to_champion(symbol="BTC-USDT-SWAP", challenger_artifact=challenger, champion_artifact=None)
    with pytest.raises(ChampionRegistryError):
        registry.promote(comparison=comparison, challenger_artifact=challenger, approved_by="")


def test_promote_rejects_losing_challenger(tmp_path):
    registry = ChampionRegistry(tmp_path / "registry.db")
    champion = _artifact(strategy_version="v1", expectancy=0.002)
    challenger = _artifact(strategy_version="v2", expectancy=0.001)
    comparison = compare_to_champion(symbol="BTC-USDT-SWAP", challenger_artifact=challenger, champion_artifact=champion)
    with pytest.raises(ChampionRegistryError):
        registry.promote(comparison=comparison, challenger_artifact=challenger, approved_by="pm")


def test_promote_rejects_artifact_not_matching_comparison(tmp_path):
    registry = ChampionRegistry(tmp_path / "registry.db")
    challenger = _artifact(strategy_version="v1", expectancy=0.001)
    other = _artifact(strategy_version="v-other", expectancy=0.002)
    comparison = compare_to_champion(symbol="BTC-USDT-SWAP", challenger_artifact=challenger, champion_artifact=None)
    with pytest.raises(ChampionRegistryError):
        registry.promote(comparison=comparison, challenger_artifact=other, approved_by="pm")


def test_promote_records_new_champion_and_history(tmp_path):
    registry = ChampionRegistry(tmp_path / "registry.db")
    challenger = _artifact(strategy_version="v1", expectancy=0.001)
    comparison = compare_to_champion(symbol="BTC-USDT-SWAP", challenger_artifact=challenger, champion_artifact=None)
    registry.promote(comparison=comparison, challenger_artifact=challenger, approved_by="pm")

    current = registry.current_champion(symbol="BTC-USDT-SWAP")
    assert current["strategy_version"] == "v1"

    history = registry.history(symbol="BTC-USDT-SWAP")
    assert len(history) == 1
    assert history[0]["event_type"] == "PROMOTE"
    assert history[0]["approved_by"] == "pm"
    assert history[0]["previous_strategy_version"] is None


def test_promote_twice_replaces_champion_and_appends_history(tmp_path):
    registry = ChampionRegistry(tmp_path / "registry.db")
    champion_v1 = _artifact(strategy_version="v1", expectancy=0.001)
    comparison_1 = compare_to_champion(symbol="BTC-USDT-SWAP", challenger_artifact=champion_v1, champion_artifact=None)
    registry.promote(comparison=comparison_1, challenger_artifact=champion_v1, approved_by="pm")

    challenger_v2 = _artifact(strategy_version="v2", expectancy=0.002)
    comparison_2 = compare_to_champion(
        symbol="BTC-USDT-SWAP", challenger_artifact=challenger_v2, champion_artifact=champion_v1
    )
    registry.promote(comparison=comparison_2, challenger_artifact=challenger_v2, approved_by="pm2")

    current = registry.current_champion(symbol="BTC-USDT-SWAP")
    assert current["strategy_version"] == "v2"
    history = registry.history(symbol="BTC-USDT-SWAP")
    assert [event["event_type"] for event in history] == ["PROMOTE", "PROMOTE"]
    assert history[1]["previous_strategy_version"] == "v1"


def test_promote_rejects_stale_comparison_after_concurrent_promotion(tmp_path):
    registry = ChampionRegistry(tmp_path / "registry.db")
    champion_v1 = _artifact(strategy_version="v1", expectancy=0.001)
    comparison_1 = compare_to_champion(symbol="BTC-USDT-SWAP", challenger_artifact=champion_v1, champion_artifact=None)
    registry.promote(comparison=comparison_1, challenger_artifact=champion_v1, approved_by="pm")

    # comparison computed against "no champion" is now stale since v1 exists.
    challenger_v2 = _artifact(strategy_version="v2", expectancy=0.002)
    stale_comparison = compare_to_champion(symbol="BTC-USDT-SWAP", challenger_artifact=challenger_v2, champion_artifact=None)
    with pytest.raises(ChampionRegistryError):
        registry.promote(comparison=stale_comparison, challenger_artifact=challenger_v2, approved_by="pm2")


def test_registry_is_per_symbol(tmp_path):
    registry = ChampionRegistry(tmp_path / "registry.db")
    btc = _artifact(strategy_version="v1", expectancy=0.001, symbol="BTC-USDT-SWAP")
    eth = _artifact(strategy_version="v1", expectancy=0.001, symbol="ETH-USDT-SWAP")
    registry.promote(
        comparison=compare_to_champion(symbol="BTC-USDT-SWAP", challenger_artifact=btc, champion_artifact=None),
        challenger_artifact=btc,
        approved_by="pm",
    )
    registry.promote(
        comparison=compare_to_champion(symbol="ETH-USDT-SWAP", challenger_artifact=eth, champion_artifact=None),
        challenger_artifact=eth,
        approved_by="pm",
    )
    assert registry.current_champion(symbol="BTC-USDT-SWAP")["symbol"] == "BTC-USDT-SWAP"
    assert registry.current_champion(symbol="ETH-USDT-SWAP")["symbol"] == "ETH-USDT-SWAP"


def test_current_champion_is_none_when_never_promoted(tmp_path):
    registry = ChampionRegistry(tmp_path / "registry.db")
    assert registry.current_champion(symbol="BTC-USDT-SWAP") is None


# ---------------------------------------------------------------------------
# ChampionRegistry.rollback
# ---------------------------------------------------------------------------


def test_rollback_without_prior_promotion_raises(tmp_path):
    registry = ChampionRegistry(tmp_path / "registry.db")
    v1 = _artifact(strategy_version="v1", expectancy=0.001)
    registry.promote(
        comparison=compare_to_champion(symbol="BTC-USDT-SWAP", challenger_artifact=v1, champion_artifact=None),
        challenger_artifact=v1,
        approved_by="pm",
    )
    with pytest.raises(ChampionRegistryError):
        registry.rollback(symbol="BTC-USDT-SWAP", approved_by="pm", reason="bad live PnL")


def test_rollback_requires_approved_by_and_reason(tmp_path):
    registry = ChampionRegistry(tmp_path / "registry.db")
    v1 = _artifact(strategy_version="v1", expectancy=0.001)
    v2 = _artifact(strategy_version="v2", expectancy=0.002)
    registry.promote(
        comparison=compare_to_champion(symbol="BTC-USDT-SWAP", challenger_artifact=v1, champion_artifact=None),
        challenger_artifact=v1,
        approved_by="pm",
    )
    registry.promote(
        comparison=compare_to_champion(symbol="BTC-USDT-SWAP", challenger_artifact=v2, champion_artifact=v1),
        challenger_artifact=v2,
        approved_by="pm",
    )
    with pytest.raises(ChampionRegistryError):
        registry.rollback(symbol="BTC-USDT-SWAP", approved_by="", reason="bad live PnL")
    with pytest.raises(ChampionRegistryError):
        registry.rollback(symbol="BTC-USDT-SWAP", approved_by="pm", reason="")


def test_rollback_restores_previous_champion_and_appends_history(tmp_path):
    registry = ChampionRegistry(tmp_path / "registry.db")
    v1 = _artifact(strategy_version="v1", expectancy=0.001)
    v2 = _artifact(strategy_version="v2", expectancy=0.002)
    registry.promote(
        comparison=compare_to_champion(symbol="BTC-USDT-SWAP", challenger_artifact=v1, champion_artifact=None),
        challenger_artifact=v1,
        approved_by="pm",
    )
    registry.promote(
        comparison=compare_to_champion(symbol="BTC-USDT-SWAP", challenger_artifact=v2, champion_artifact=v1),
        challenger_artifact=v2,
        approved_by="pm",
    )

    restored = registry.rollback(symbol="BTC-USDT-SWAP", approved_by="ops", reason="bad live PnL")
    assert restored["strategy_version"] == "v1"
    assert registry.current_champion(symbol="BTC-USDT-SWAP")["strategy_version"] == "v1"

    history = registry.history(symbol="BTC-USDT-SWAP")
    assert [event["event_type"] for event in history] == ["PROMOTE", "PROMOTE", "ROLLBACK"]
    assert history[-1]["approved_by"] == "ops"
    assert history[-1]["reason"] == "bad live PnL"
    assert history[-1]["strategy_version"] == "v1"
    assert history[-1]["previous_strategy_version"] == "v2"


def test_rollback_does_not_delete_promotion_history(tmp_path):
    registry = ChampionRegistry(tmp_path / "registry.db")
    v1 = _artifact(strategy_version="v1", expectancy=0.001)
    v2 = _artifact(strategy_version="v2", expectancy=0.002)
    registry.promote(
        comparison=compare_to_champion(symbol="BTC-USDT-SWAP", challenger_artifact=v1, champion_artifact=None),
        challenger_artifact=v1,
        approved_by="pm",
    )
    registry.promote(
        comparison=compare_to_champion(symbol="BTC-USDT-SWAP", challenger_artifact=v2, champion_artifact=v1),
        challenger_artifact=v2,
        approved_by="pm",
    )
    registry.rollback(symbol="BTC-USDT-SWAP", approved_by="ops", reason="regression")
    history = registry.history(symbol="BTC-USDT-SWAP")
    assert len(history) == 3
