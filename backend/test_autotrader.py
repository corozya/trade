from __future__ import annotations

from pathlib import Path

import pytest

from autotrader import (
    AutotraderDecision,
    AutotraderState,
    AutonomousTrader,
    DecisionValidationError,
    JsonStateStore,
)


def _open_payload(**overrides):
    payload = {
        "action": "OPEN",
        "side": "LONG",
        "horizon": "SCALP",
        "timeframe": "5m",
        "entry": 100.0,
        "stop_loss": 98.0,
        "take_profit": 104.0,
        "atr14": 1.0,
        "next_check_seconds": 60,
        "strategy_version": "btc-v1",
        "strategy_lifecycle": "CREATE",
        "strategy_task_id": "#300",
        "trade_task_id": "#301",
        "trade_lifecycle": "CREATE",
        "evidence": ["ATR 5m=1"],
        "lessons": ["nie gonić świecy po wybiciu"],
        "reason": "test setup",
    }
    payload.update(overrides)
    return payload


def test_open_contract_requires_strategy_and_trade_tasks():
    decision = AutotraderDecision.from_mapping(_open_payload())
    assert decision.action == "OPEN"
    assert decision.strategy_task_id == "#300"
    assert decision.trade_task_id == "#301"
    assert decision.atr14 == 1.0

    with pytest.raises(DecisionValidationError, match="trade_task_id"):
        AutotraderDecision.from_mapping(_open_payload(trade_task_id=None))


@pytest.mark.parametrize("seconds", [0, 59, 901, 3600])
def test_schedule_is_strictly_bounded(seconds):
    with pytest.raises(DecisionValidationError, match="between 60 and 900"):
        AutotraderDecision.from_mapping(_open_payload(next_check_seconds=seconds))


def test_request_data_requires_created_ats_task():
    base = {
        "action": "REQUEST_DATA",
        "reason": "missing OI 5m",
        "next_check_seconds": 300,
        "strategy_lifecycle": "CONTINUE",
        "strategy_task_id": "#300",
    }
    with pytest.raises(DecisionValidationError, match="ats_task_id"):
        AutotraderDecision.from_mapping(base)
    base["ats_task_id"] = "#302"
    assert AutotraderDecision.from_mapping(base).ats_task_id == "#302"


def _runner(tmp_path: Path, *, decision, risk, execute, symbols=("BTC",)):
    store = JsonStateStore(tmp_path / "state.json")
    store.save(AutotraderState(enabled=True, status="scheduled"))
    decision_for = decision if callable(decision) else (lambda _symbol: decision)
    runner = AutonomousTrader(
        store=store,
        symbols=symbols,
        analyze=lambda symbol, _round_id, _session_id: (decision_for(symbol), "session-1"),
        execute=execute,
        risk_check=lambda: risk,
        log_path=tmp_path / "rounds.jsonl",
        clock=lambda: 1_700_000_000.0,
    )
    return runner, store


def test_kill_switch_turns_open_into_wait(tmp_path):
    executed = []
    runner, store = _runner(
        tmp_path,
        decision=_open_payload(),
        risk={"active": True, "reason": "three losses"},
        execute=lambda _symbol, decision, _round_id: executed.append(decision) or {"ok": True},
    )
    state = runner.run_once()
    assert executed[0].action == "WAIT"
    assert state.last_decision["action"] == "WAIT"
    assert state.mandate("BTC").strategy_task_id == "#300"
    assert store.load().kill_switch["active"] is True


def test_intent_is_persisted_and_failure_is_not_reported_as_success(tmp_path):
    def fail(_symbol, decision, _round_id):
        assert decision.action == "OPEN"
        persisted = store.load()
        assert persisted.status == "executing:BTC"
        assert persisted.mandate("BTC").last_decision["trade_task_id"] == "#301"
        raise RuntimeError("ambiguous exchange response")

    runner, store = _runner(
        tmp_path,
        decision=_open_payload(),
        risk={"active": False},
        execute=fail,
    )
    state = runner.run_once()
    assert state.status == "error"
    assert "ambiguous exchange response" in state.last_error
    assert state.next_run_at is not None
    assert (tmp_path / "rounds.jsonl").read_text().count("ambiguous exchange response") == 1


def test_invalidated_strategy_is_cleared_from_durable_state(tmp_path):
    decision = {
        "action": "WAIT",
        "reason": "thesis invalidated",
        "next_check_seconds": 300,
        "strategy_lifecycle": "INVALIDATE",
        "strategy_task_id": "#300",
        "strategy_version": "btc-v1",
    }
    runner, store = _runner(
        tmp_path,
        decision=decision,
        risk={"active": False},
        execute=lambda *_args: {"ok": True, "skipped": "WAIT"},
    )
    prior = store.load()
    prior_mandate = prior.mandate("BTC")
    prior_mandate.strategy_task_id = "#300"
    prior.set_mandate("BTC", prior_mandate)
    store.save(prior)
    state = runner.run_once()
    assert state.mandate("BTC").strategy_task_id is None


def test_round_processes_symbols_sequentially_with_isolated_mandates(tmp_path):
    """4-symbol loop: each symbol gets its own analyze/execute call, in list
    order, and its own mandate (strategy/trade task ids) — never shared."""
    order: list[str] = []
    payloads = {
        "BTC": _open_payload(strategy_task_id="#300", trade_task_id="#301"),
        "ETH": {
            "action": "WAIT", "reason": "no setup", "next_check_seconds": 120,
            "strategy_lifecycle": "CONTINUE", "strategy_task_id": "#310",
        },
        "DOGE": {
            "action": "WAIT", "reason": "no setup", "next_check_seconds": 600,
            "strategy_lifecycle": "CONTINUE", "strategy_task_id": "#320",
        },
        "XRP": {
            "action": "WAIT", "reason": "no setup", "next_check_seconds": 300,
            "strategy_lifecycle": "CONTINUE", "strategy_task_id": "#330",
        },
    }

    def execute(symbol, decision, _round_id):
        order.append(symbol)
        return {"ok": True, "skipped": decision.action}

    runner, store = _runner(
        tmp_path,
        decision=lambda symbol: payloads[symbol],
        risk={"active": False},
        execute=execute,
        symbols=("BTC", "ETH", "DOGE", "XRP"),
    )
    state = runner.run_once()
    assert order == ["BTC", "ETH", "DOGE", "XRP"]
    assert state.mandate("BTC").strategy_task_id == "#300"
    assert state.mandate("BTC").trade_task_id == "#301"
    assert state.mandate("ETH").strategy_task_id == "#310"
    assert state.mandate("ETH").trade_task_id is None
    assert state.mandate("DOGE").strategy_task_id == "#320"
    assert state.mandate("XRP").strategy_task_id == "#330"
    # Shared next_run_at reflects the minimum next_check_seconds across the
    # whole round, not a per-symbol schedule.
    assert state.next_run_at == "2023-11-14T22:14:20Z"


def test_kill_switch_activated_mid_round_blocks_open_for_later_symbols(tmp_path):
    """A loss recorded while processing an earlier symbol must be honored by
    the shared/global kill switch for the remaining symbols in the SAME
    round — not just from the next round onward."""
    risk_state = {"active": False}
    executed: list[tuple[str, str]] = []

    def risk_check():
        return dict(risk_state)

    def analyze(symbol, _round_id, _session_id):
        if symbol == "BTC":
            # BTC's CLOSE (processed via execute below) flips the global
            # kill switch on before ETH is analyzed.
            return _open_payload(strategy_task_id="#300", trade_task_id="#301"), "s1"
        return (
            {
                "action": "OPEN", "side": "LONG", "horizon": "SCALP", "timeframe": "5m",
                "entry": 1.0, "stop_loss": 0.9, "take_profit": 1.2, "atr14": 0.01,
                "next_check_seconds": 120, "strategy_lifecycle": "CREATE",
                "strategy_task_id": f"#{symbol}-strat", "trade_task_id": f"#{symbol}-trade",
                "trade_lifecycle": "CREATE", "reason": "setup",
            },
            "s1",
        )

    def execute(symbol, decision, _round_id):
        executed.append((symbol, decision.action))
        if symbol == "BTC":
            risk_state["active"] = True
            risk_state["reason"] = "three losses"
        return {"ok": True, "skipped": decision.action}

    store = JsonStateStore(tmp_path / "state.json")
    store.save(AutotraderState(enabled=True, status="scheduled"))
    runner = AutonomousTrader(
        store=store,
        symbols=("BTC", "ETH", "DOGE", "XRP"),
        analyze=analyze,
        execute=execute,
        risk_check=risk_check,
        log_path=tmp_path / "rounds.jsonl",
        clock=lambda: 1_700_000_000.0,
    )
    state = runner.run_once()
    assert executed[0] == ("BTC", "OPEN")
    # ETH/DOGE/XRP were force-flipped to WAIT because the kill switch went
    # active mid-round, before their execute() call.
    assert executed[1:] == [("ETH", "WAIT"), ("DOGE", "WAIT"), ("XRP", "WAIT")]
    assert state.kill_switch["active"] is True
