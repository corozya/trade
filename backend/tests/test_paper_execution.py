import sqlite3

import pytest

from services.paper_execution import (
    ExecutionGate,
    PaperExecutionError,
    record_paper_decision,
    record_research_loop_result,
    derive_point_in_time_decisions,
    validate_demo_config,
)


def test_paper_round_trip_persists_versions_and_pnl():
    conn = sqlite3.connect(":memory:")
    record_paper_decision(
        conn,
        {"symbol": "BTC-USDT-SWAP", "decision": "OPEN", "side": "BUY", "qty": 2,
         "stop_loss_price": 90, "take_profit_price": 120},
        market_price=100, run_id="run-1", model_version="m1", policy_version="p1",
        dataset_version="d1",
    )
    result = record_paper_decision(
        conn, {"symbol": "BTC-USDT-SWAP", "decision": "CLOSE", "qty": 2},
        market_price=110, run_id="run-2", model_version="m1", policy_version="p1",
        dataset_version="d1",
    )
    assert result["pnl"] > 0
    row = conn.execute("SELECT entry_price, exit_price, model_version, dataset_version "
                       "FROM paper_execution_ledger WHERE decision='OPEN'").fetchone()
    assert tuple(row) == (100.0, 110.0, "m1", "d1")


def test_paper_never_accepts_invalid_decision_or_missing_position():
    conn = sqlite3.connect(":memory:")
    with pytest.raises(PaperExecutionError):
        record_paper_decision(conn, {"symbol": "BTC", "decision": "CLOSE", "qty": 1}, market_price=1)


def test_research_loop_result_is_adapted_end_to_end_without_execution():
    conn = sqlite3.connect(":memory:")
    result = record_research_loop_result(
        conn,
        {"run_id": "research-run-1", "decisions": [{"trade_intent": {
            "symbol": "BTC-USDT-SWAP", "decision": "OPEN", "side": "BUY", "qty": 1,
            "stop_loss_price": 90, "take_profit_price": 120,
        }}]},
        market_prices={"BTC-USDT-SWAP": 100}, model_version="m1",
        policy_version="p1", dataset_version="d1",
    )
    assert result[0]["run_id"] == "research-run-1"
    assert conn.execute("SELECT COUNT(*) FROM paper_execution_ledger").fetchone()[0] == 1


def test_point_in_time_signal_opens_then_closes_with_pnl():
    conn = sqlite3.connect(":memory:")
    rows = [
        {"symbol": "BTC", "available_at": "1", "bb_percent_b": 0.40},
        {"symbol": "BTC", "available_at": "2", "bb_percent_b": 0.60},
    ]
    intents = derive_point_in_time_decisions(
        conn, feature_rows=rows, market_prices={"BTC": 100}, threshold=0.01
    )
    assert intents[0]["decision"] == "OPEN"
    record_research_loop_result(
        conn, {"run_id": "r1", "decisions": [{"trade_intent": intents[0]}]},
        market_prices={"BTC": 100},
    )
    rows[-1] = {**rows[-1], "available_at": "3", "bb_percent_b": 0.40}
    intents = derive_point_in_time_decisions(
        conn, feature_rows=rows, market_prices={"BTC": 110}, threshold=0.01
    )
    assert intents[0]["decision"] == "CLOSE"
    result = record_research_loop_result(
        conn, {"run_id": "r2", "decisions": [{"trade_intent": intents[0]}]},
        market_prices={"BTC": 110},
    )
    assert result[0]["pnl"] > 0


@pytest.mark.parametrize("config", [
    {},
    {"environment": "production", "endpoint": "https://sandbox.example", "credential_alias": "okx-demo-x", "manual_approval": True},
    {"environment": "sandbox", "endpoint": "https://api.example", "credential_alias": "okx-demo-x", "manual_approval": True},
    {"environment": "sandbox", "endpoint": "https://sandbox.example", "credential_alias": "okx-demo-x"},
])
def test_demo_gate_fails_closed(config):
    with pytest.raises(PaperExecutionError):
        validate_demo_config(config)


def test_kill_switch_defaults_to_stopped():
    gate = ExecutionGate()
    with pytest.raises(PaperExecutionError):
        gate.assert_running()
    gate.resume_after_approval()
    gate.assert_running()
    gate.stop()
    with pytest.raises(PaperExecutionError):
        gate.assert_running()
