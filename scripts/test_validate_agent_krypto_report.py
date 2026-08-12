from datetime import datetime, timezone

import pytest

from scripts.validate_agent_krypto_report import validate


NOW = datetime(2026, 7, 23, 12, 0, tzinfo=timezone.utc)


def wait(symbol: str) -> dict:
    return {
        "trade_intent": {
            "symbol": symbol,
            "decision": "WAIT",
            "side": None,
            "qty": None,
            "take_profit_price": None,
            "stop_loss_price": None,
            "reason": "no promoted edge",
            "strategy_artifact": None,
        },
        "execution_result": None,
    }


def trade(symbol: str, state: str = "filled") -> dict:
    return {
        "trade_intent": {
            "symbol": symbol,
            "decision": "TRADE",
            "side": "LONG",
            "qty": 2,
            "take_profit_price": 110,
            "stop_loss_price": 95,
            "reason": "promoted momentum signal",
            "strategy_artifact": {
                "strategy_version": "strategy-v1",
                "dataset_version": "dataset-v3",
                "feature_schema_version": "features-v2",
                "status": "PROMOTED",
                "expires_at": "2026-07-30T12:00:00Z",
            },
        },
        "execution_result": {
            "state": state,
            "order_id": "okx-123" if state == "filled" else None,
            "filled_qty": 2 if state == "filled" else 0,
            "reconciliation_required": False,
        },
    }


def report(*decisions: dict) -> dict:
    return {"status": "completed", "round_id": 17, "decisions": list(decisions)}


def test_wait_only_report_is_valid():
    assert validate(report(wait("BTC"), wait("ETH"), wait("DOGE")), "codex", now=NOW) is None


def test_filled_trade_uses_separate_intent_and_authoritative_result():
    assert validate(report(trade("BTC"), wait("ETH"), wait("DOGE")), "codex", now=NOW) is None


@pytest.mark.parametrize("state", ["live", "partial", "canceled", "unknown", "timeout"])
def test_non_filled_execution_never_gives_completed(state):
    error = validate(report(trade("BTC", state), wait("ETH"), wait("DOGE")), "codex", now=NOW)
    assert error == f"BTC: ExecutionResult state={state} nie może dać completed"


def test_trade_without_artifact_is_fail_closed_to_wait():
    decision = trade("BTC")
    decision["trade_intent"]["strategy_artifact"] = None
    assert "wymusza WAIT" in validate(
        report(decision, wait("ETH"), wait("DOGE")), "codex", now=NOW
    )


def test_trade_with_expired_artifact_is_fail_closed_to_wait():
    decision = trade("BTC")
    decision["trade_intent"]["strategy_artifact"]["expires_at"] = "2026-07-22T12:00:00Z"
    assert "przeterminowany StrategyArtifact wymusza WAIT" in validate(
        report(decision, wait("ETH"), wait("DOGE")), "codex", now=NOW
    )


def test_wait_cannot_claim_execution_or_position_state():
    decision = wait("BTC")
    decision["trade_intent"]["order_id"] = "forbidden"
    assert "TradeIntent nie może deklarować wykonania ani stanu pozycji" in validate(
        report(decision, wait("ETH"), wait("DOGE")), "codex", now=NOW
    )


def test_filled_requires_real_order_id_and_quantity():
    decision = trade("BTC")
    decision["execution_result"]["order_id"] = None
    assert "filled wymaga niepustego order_id" in validate(
        report(decision, wait("ETH"), wait("DOGE")), "codex", now=NOW
    )


def test_legacy_flat_report_is_rejected():
    legacy = {"symbol": "BTC", "decision": "WAIT", "reason": "legacy"}
    assert "trade_intent i execution_result" in validate(
        report(legacy, wait("ETH"), wait("DOGE")), "codex", now=NOW
    )
