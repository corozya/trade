from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

import main
from autotrader import AutotraderDecision


class _Conn:
    def close(self):
        pass


class _Client:
    def get_ticker(self, _inst_id):
        return {"data": [{"last": "100"}]}


def _decision(action="REDUCE", **overrides):
    payload = {
        "action": action,
        "reason": "test",
        "next_check_seconds": 300,
        "strategy_lifecycle": "CONTINUE",
        "strategy_task_id": "#300",
    }
    if action == "REDUCE":
        payload["reduce_fraction"] = 0.25
    payload.update(overrides)
    return AutotraderDecision.from_mapping(payload)


def test_strict_position_read_propagates_unavailability(monkeypatch):
    class Broken:
        def get_positions(self, **_kwargs):
            raise RuntimeError("network down")

    monkeypatch.setattr(main, "_demo_client", lambda: Broken())
    monkeypatch.setattr(
        main, "resolve_futures_instrument",
        lambda *_args: {"instId": "BTC-USD_UM_XPERP"},
    )
    with pytest.raises(RuntimeError, match="network down"):
        main._strict_demo_position("BTC")


def test_okx_positions_does_not_mask_resolve_failure_as_flat(monkeypatch):
    """#319 regression: OKX 51014 "Index doesn't exist" on LTC/SOL (this
    demo account's authenticated account/instruments catalog is missing
    those instFamilies, even though the public catalog lists them as
    state=live — confirmed live against OKX Demo, not a client-side bug)
    must surface as `{"unavailable": ...}`, never as `None` — `None` means
    confirmed flat and is indistinguishable from "couldn't check" if a
    resolve failure is swallowed into it."""
    monkeypatch.setattr(main, "_demo_client", lambda: _Client2(positions=[]))

    def fake_resolve(base, _client):
        if base == "LTC":
            raise RuntimeError("OKX API error (code=51014): Index doesn't exist.")
        return {"instId": f"{base}-USD_UM_XPERP"}

    monkeypatch.setattr(main, "resolve_futures_instrument", fake_resolve)
    result = main.okx_positions()
    assert result["LTC"] == {"unavailable": "OKX API error (code=51014): Index doesn't exist."}
    assert result["BTC"] is None  # genuinely flat bases still report None


class _Client2:
    def __init__(self, positions):
        self._positions = positions

    def get_positions(self, **_kwargs):
        return {"data": self._positions}


def test_reduce_uses_safe_execution_and_rounds_down(monkeypatch):
    captured = {}
    monkeypatch.setattr(main, "_demo_client", lambda: _Client())
    monkeypatch.setattr(
        main, "resolve_futures_instrument",
        lambda *_args: {
            "instId": "BTC-USD_UM_XPERP", "ctVal": Decimal("1"),
            "lotSz": Decimal("1"), "minSz": Decimal("1"),
        },
    )
    class Client:
        def submit_trade_intent(self, portfolio_id, intent):
            captured.update(intent)
            captured["portfolio_id"] = portfolio_id
            return {"ok": True}

    monkeypatch.setattr(main, "PortfolioClient", Client)
    result = main._safe_trade_intent(
        "BTC", _decision(), "round-1", {"side": "long", "qty": 10.0}
    )
    assert result["ok"] is True
    assert captured["action"] == "REDUCE"
    assert captured["side"] == "SELL"
    assert captured["qty"] == "2"


def test_manage_cannot_widen_long_stop(monkeypatch):
    monkeypatch.setattr(
        main, "_strict_demo_position",
        lambda _base: {"side": "long", "qty": 2, "stop_loss": 99.0},
    )
    called = []
    monkeypatch.setattr(main, "update_position", lambda _payload: called.append(True))
    decision = _decision("MANAGE", stop_loss=98.0)
    with pytest.raises(RuntimeError, match="cannot be moved farther"):
        main._autotrader_execute("BTC", decision, "round-2")
    assert called == []


def test_three_unique_losses_activate_guard(monkeypatch):
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    history = [
        {"closed_at": str(now_ms - index), "entry": 100 + index, "exit": 99, "pnl": -1.0}
        for index in range(3)
    ]
    monkeypatch.setattr(main, "_demo_equity", lambda: 1000.0)
    monkeypatch.setattr(main, "okx_position_history", lambda **_kwargs: history)
    guard = main._autotrader_risk_check()
    assert guard["active"] is True
    assert guard["consecutive_losses"] == 3


def test_previous_day_losses_do_not_keep_daily_guard_active(monkeypatch):
    old_ms = int((datetime.now(timezone.utc) - timedelta(days=1)).timestamp() * 1000)
    history = [
        {"closed_at": str(old_ms - index), "entry": 100 + index, "exit": 99, "pnl": -1.0}
        for index in range(3)
    ]
    monkeypatch.setattr(main, "_demo_equity", lambda: 1000.0)
    monkeypatch.setattr(main, "okx_position_history", lambda **_kwargs: history)
    guard = main._autotrader_risk_check()
    assert guard["active"] is False
    assert guard["consecutive_losses"] == 0


def test_wait_does_not_touch_demo_account(monkeypatch):
    monkeypatch.setattr(
        main, "_strict_demo_position",
        lambda _base: pytest.fail("WAIT must not read or mutate the exchange"),
    )
    result = main._autotrader_execute("BTC", _decision("WAIT"), "round-3")
    assert result == {"ok": True, "skipped": "WAIT"}


def test_context_surfaces_missing_data_to_agent_instead_of_crashing(monkeypatch):
    monkeypatch.setattr(main, "available", lambda: {"freshness": {}})
    monkeypatch.setattr(main, "_read_series", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("missing lake")))
    monkeypatch.setattr(main, "_strict_demo_position", lambda *_args: (_ for _ in ()).throw(RuntimeError("position unavailable")))
    monkeypatch.setattr(main, "_strict_pending_orders", lambda *_args: (_ for _ in ()).throw(RuntimeError("orders unavailable")))
    monkeypatch.setattr(main, "okx_position_history", lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("history unavailable")))
    monkeypatch.setattr(main, "_autotrader_risk_check", lambda: {"active": True, "reason": "risk unavailable"})
    context = main._autotrader_context("BTC")
    assert context["recent_ohlcv"]["1m"]["unavailable"] == "missing lake"
    assert context["position"]["unavailable"] == "position unavailable"
    assert context["risk_guard"]["active"] is True


def test_context_is_symbol_scoped_and_isolated(monkeypatch):
    """Each symbol's context must carry its own mandate, not another
    symbol's strategy/trade task ids."""
    monkeypatch.setattr(main, "available", lambda: {"freshness": {}})
    monkeypatch.setattr(main, "_read_series", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(main, "_strict_demo_position", lambda *_args: None)
    monkeypatch.setattr(main, "_strict_pending_orders", lambda *_args: [])
    monkeypatch.setattr(main, "okx_position_history", lambda **_kwargs: [])
    monkeypatch.setattr(main, "_autotrader_risk_check", lambda: {"active": False})

    from autotrader import AutotraderState

    state = AutotraderState()
    btc_mandate = state.mandate("BTC")
    btc_mandate.strategy_task_id = "#300"
    btc_mandate.trade_task_id = "#301"
    state.set_mandate("BTC", btc_mandate)
    eth_mandate = state.mandate("ETH")
    eth_mandate.strategy_task_id = "#310"
    state.set_mandate("ETH", eth_mandate)
    monkeypatch.setattr(main, "_autotrader_store", type("S", (), {"load": staticmethod(lambda: state)})())

    btc_context = main._autotrader_context("BTC")
    eth_context = main._autotrader_context("ETH")
    assert btc_context["active_strategy_task_id"] == "#300"
    assert btc_context["active_trade_task_id"] == "#301"
    assert eth_context["active_strategy_task_id"] == "#310"
    assert eth_context["active_trade_task_id"] is None


def test_autotrader_claude_success_does_not_call_codex(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="uvicorn.error")
    monkeypatch.setattr(main, "_autotrader_context", lambda _base: {})
    monkeypatch.setattr(
        main, "_run_claude",
        lambda *_args, **_kwargs: ('{"action":"WAIT"}', "claude-session"),
    )
    monkeypatch.setattr(
        main, "_run_codex_autotrader",
        lambda *_args: pytest.fail("Codex must not run after Claude succeeds"),
    )

    decision, sid = main._autotrader_analyze("BTC", "round-claude", None)

    assert decision["action"] == "WAIT"
    assert sid == "claude-session"
    assert "autotrader agent start provider=claude symbol=BTC round_id=round-claude" in caplog.text
    assert "provider=codex" not in caplog.text


def test_codex_autotrader_uses_role_dynamic_prompt_and_read_only_output_file(monkeypatch, tmp_path):
    role_path = tmp_path / "analyst.md"
    role_path.write_text("ANALYST ROLE")
    captured = {}

    def run(args, **kwargs):
        captured["args"] = args
        captured["input"] = kwargs["input"]
        output_path = args[args.index("--output-last-message") + 1]
        main.Path(output_path).write_text('{"action":"WAIT"}')
        return main.subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(main, "_CRYPTO_DASHBOARD_AGENT_PATH", role_path)
    monkeypatch.setattr(main.subprocess, "run", run)

    reply = main._run_codex_autotrader("DYNAMIC ROUND", 10)

    assert reply == '{"action":"WAIT"}'
    assert captured["args"][:2] == ["codex", "exec"]
    assert "--sandbox" in captured["args"]
    assert captured["args"][captured["args"].index("--sandbox") + 1] == "read-only"
    assert "--ephemeral" in captured["args"]
    assert "--output-last-message" in captured["args"]
    assert "ANALYST ROLE" in captured["input"]
    assert "DYNAMIC ROUND" in captured["input"]


@pytest.mark.parametrize(
    "claude_error",
    [
        OSError("claude not found"),
        main.HTTPException(status_code=504, detail="agent timeout"),
        main.HTTPException(status_code=502, detail="claude CLI failed"),
    ],
)
def test_autotrader_claude_unavailable_falls_back_to_codex(monkeypatch, caplog, claude_error):
    caplog.set_level(logging.INFO, logger="uvicorn.error")
    monkeypatch.setattr(main, "_autotrader_context", lambda _base: {})

    def fail_claude(*_args, **_kwargs):
        raise claude_error

    monkeypatch.setattr(main, "_run_claude", fail_claude)
    monkeypatch.setattr(main, "_run_codex_autotrader", lambda *_args: '{"action":"WAIT"}')

    decision, sid = main._autotrader_analyze("BTC", "round-codex", "existing-claude-session")

    assert decision["action"] == "WAIT"
    assert sid == "existing-claude-session"
    claude_log = "autotrader agent start provider=claude symbol=BTC round_id=round-codex"
    codex_log = "autotrader agent start provider=codex symbol=BTC round_id=round-codex"
    assert claude_log in caplog.text
    assert codex_log in caplog.text
    assert caplog.text.index(claude_log) < caplog.text.index(codex_log)


def test_autotrader_codex_failure_is_reported_clearly(monkeypatch):
    monkeypatch.setattr(main, "_autotrader_context", lambda _base: {})

    def fail_claude(*_args, **_kwargs):
        raise main.HTTPException(status_code=502, detail="claude CLI failed")

    def fail_codex(*_args):
        raise RuntimeError("Codex fallback zakończył się błędem: auth failed")

    monkeypatch.setattr(main, "_run_claude", fail_claude)
    monkeypatch.setattr(main, "_run_codex_autotrader", fail_codex)

    with pytest.raises(RuntimeError, match="Claude jest niedostępny.*Codex fallback.*auth failed"):
        main._autotrader_analyze("BTC", "round-error", "existing-claude-session")
