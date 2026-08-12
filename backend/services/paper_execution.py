"""Offline paper execution and the fail-closed demo promotion gate.

Paper execution is deliberately independent from the exchange clients: it only
persists decisions and calculates a deterministic ledger from supplied prices.
The demo gate validates configuration and approval but never places an order.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping


class PaperExecutionError(ValueError):
    """Invalid paper intent or unsafe execution configuration."""


def derive_point_in_time_decisions(
    conn: sqlite3.Connection,
    *,
    feature_rows: list[Mapping[str, Any]],
    market_prices: Mapping[str, Any],
    lookback: int = 1,
    threshold: float = 0.01,
    signal_feature: str = "bb_percent_b",
    qty: Any = 1,
    stop_loss_pct: float = 0.01,
    take_profit_pct: float = 0.02,
) -> list[dict[str, Any]]:
    """Create live point-in-time intents from the approved momentum rule.

    This mirrors ``LocalFeatureMomentumEngine``: current feature minus the
    value ``lookback`` bars ago determines LONG/SHORT/WAIT.  Existing paper
    positions are stateful in the ledger; an opposite signal closes them and
    opens the new side in the same cycle.  No experiment/evaluate status is
    consulted and no exchange client is reachable from this function.
    """
    ensure_paper_schema(conn)
    if lookback < 1 or threshold < 0:
        raise PaperExecutionError("niepoprawne parametry sygnału")
    by_symbol: dict[str, list[Mapping[str, Any]]] = {}
    for row in feature_rows:
        symbol = str(row.get("symbol", "")).strip()
        if symbol:
            by_symbol.setdefault(symbol, []).append(row)
    decisions: list[dict[str, Any]] = []
    for symbol, rows in by_symbol.items():
        rows.sort(key=lambda row: str(row.get("available_at", row.get("decision_at", ""))))
        price = market_prices.get(symbol)
        if price is None or len(rows) <= lookback:
            continue
        current = rows[-1].get(signal_feature)
        reference = rows[-1 - lookback].get(signal_feature)
        if current is None or reference is None:
            direction = "WAIT"
        else:
            momentum = float(current) - float(reference)
            direction = "LONG" if momentum > threshold else "SHORT" if momentum < -threshold else "WAIT"
        open_position = _open_position(conn, symbol)
        if open_position is not None:
            side = str(open_position["side"] or "")
            opposite = (direction == "LONG" and side == "SELL") or (direction == "SHORT" and side == "BUY")
            stop = open_position["stop_loss_price"]
            target = open_position["take_profit_price"]
            hit = (side == "BUY" and ((stop is not None and float(price) <= float(stop)) or (target is not None and float(price) >= float(target)))) or (side == "SELL" and ((stop is not None and float(price) >= float(stop)) or (target is not None and float(price) <= float(target))))
            if opposite or hit:
                decisions.append({"symbol": symbol, "decision": "CLOSE", "qty": float(open_position["qty"])})
                open_position = None
        if open_position is None and direction in {"LONG", "SHORT"}:
            entry = float(price)
            side = "BUY" if direction == "LONG" else "SELL"
            decisions.append({
                "symbol": symbol, "decision": "OPEN", "side": side, "qty": qty,
                "stop_loss_price": entry * (1 - stop_loss_pct if side == "BUY" else 1 + stop_loss_pct),
                "take_profit_price": entry * (1 + take_profit_pct if side == "BUY" else 1 - take_profit_pct),
            })
        elif open_position is None:
            # No position (never opened, or just closed above) and no fresh
            # LONG/SHORT signal: an explicit WAIT observation.
            decisions.append({"symbol": symbol, "decision": "WAIT", "qty": 0})
        else:
            # Position held: signal has not reversed and SL/TP not hit.
            # Record an explicit WAIT so every symbol/cycle has a ledger row
            # (otherwise a held position silently produces no decision at all).
            decisions.append({"symbol": symbol, "decision": "WAIT", "qty": 0})
    return decisions


def _number(value: Any, field: str, *, positive: bool = False) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise PaperExecutionError(f"{field} musi być liczbą") from exc
    if not result.is_finite() or (positive and result <= 0):
        raise PaperExecutionError(f"{field} musi być dodatnie")
    return result


def ensure_paper_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS paper_execution_ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            symbol TEXT NOT NULL,
            decision TEXT NOT NULL,
            side TEXT,
            qty REAL NOT NULL,
            entry_price REAL,
            exit_price REAL,
            stop_loss_price REAL,
            take_profit_price REAL,
            fee REAL NOT NULL,
            pnl REAL,
            model_version TEXT NOT NULL,
            policy_version TEXT NOT NULL,
            dataset_version TEXT NOT NULL,
            event_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(run_id, symbol, decision)
        )"""
    )
    conn.commit()


def _open_position(conn: sqlite3.Connection, symbol: str) -> sqlite3.Row | None:
    conn.row_factory = sqlite3.Row
    return conn.execute(
        "SELECT * FROM paper_execution_ledger WHERE symbol=? AND decision='OPEN' "
        "AND exit_price IS NULL ORDER BY id DESC LIMIT 1", (symbol,)
    ).fetchone()


def record_paper_decision(
    conn: sqlite3.Connection,
    intent: Mapping[str, Any],
    *,
    market_price: Any,
    run_id: str | None = None,
    model_version: str = "unknown",
    policy_version: str = "unknown",
    dataset_version: str = "unknown",
    fee_rate: Any = "0.0005",
) -> dict[str, Any]:
    """Record one simulated decision; this function has no exchange/client path."""
    ensure_paper_schema(conn)
    symbol = str(intent.get("symbol", "")).strip()
    decision = str(intent.get("decision", intent.get("action", "WAIT"))).upper()
    side = str(intent.get("side", "") or "").upper() or None
    if not symbol or decision not in {"WAIT", "OPEN", "CLOSE", "INCREASE", "REDUCE"}:
        raise PaperExecutionError("niepoprawna decyzja paper")
    price = _number(market_price, "market_price", positive=True)
    qty = _number(intent.get("qty", 0) or 0, "qty")
    if decision != "WAIT" and qty <= 0:
        raise PaperExecutionError("qty musi być dodatnie dla decyzji transakcyjnej")
    if decision in {"OPEN", "INCREASE"} and side not in {"BUY", "SELL"}:
        raise PaperExecutionError("side musi być BUY albo SELL")
    if decision in {"CLOSE", "REDUCE"} and _open_position(conn, symbol) is None:
        raise PaperExecutionError("brak otwartej pozycji paper")
    run = run_id or f"paper-{uuid.uuid4().hex}"
    fee = abs(qty * price) * _number(fee_rate, "fee_rate")
    entry = price if decision in {"OPEN", "INCREASE"} else None
    exit_price = price if decision in {"CLOSE", "REDUCE"} else None
    pnl = None
    if exit_price is not None:
        prior = _open_position(conn, symbol)
        prior_entry = Decimal(str(prior["entry_price"]))
        prior_side = prior["side"]
        gross = (price - prior_entry) * qty
        pnl = gross if prior_side == "BUY" else -gross
        pnl -= fee
        conn.execute("UPDATE paper_execution_ledger SET exit_price=?, pnl=? WHERE id=?",
                     (float(price), float(pnl), prior["id"]))
    now = datetime.now(timezone.utc).isoformat()
    result = {
        "run_id": run, "symbol": symbol, "decision": decision, "side": side,
        "qty": float(qty), "entry_price": float(entry) if entry else None,
        "exit_price": float(exit_price) if exit_price else None,
        "stop_loss_price": intent.get("stop_loss_price"),
        "take_profit_price": intent.get("take_profit_price"), "fee": float(fee),
        "pnl": float(pnl) if pnl is not None else None,
        "model_version": model_version, "policy_version": policy_version,
        "dataset_version": dataset_version,
    }
    conn.execute(
        """INSERT OR IGNORE INTO paper_execution_ledger
        (run_id,symbol,decision,side,qty,entry_price,exit_price,stop_loss_price,
         take_profit_price,fee,pnl,model_version,policy_version,dataset_version,event_json,created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (run, symbol, decision, side, float(qty), result["entry_price"], result["exit_price"],
         result["stop_loss_price"], result["take_profit_price"], float(fee), result["pnl"],
         model_version, policy_version, dataset_version, json.dumps(result, sort_keys=True), now),
    )
    conn.commit()
    return result


def record_research_loop_result(
    conn: sqlite3.Connection,
    research_result: Mapping[str, Any],
    *,
    market_prices: Mapping[str, Any],
    model_version: str = "unknown",
    policy_version: str = "unknown",
    dataset_version: str = "unknown",
    fee_rate: Any = "0.0005",
) -> list[dict[str, Any]]:
    """Adapt one research-loop result into paper decisions.

    The adapter only consumes serialized output (``run_id`` and ``decisions``)
    and supplied prices. It deliberately has no execution/client dependency.
    """
    run_id = str(research_result.get("run_id", "")).strip()
    if not run_id:
        raise PaperExecutionError("research-loop wymaga run_id")
    decisions = research_result.get("decisions")
    if decisions is None and isinstance(research_result.get("phases"), Mapping):
        cycle = research_result["phases"].get("cycle")
        decisions = cycle.get("decisions") if isinstance(cycle, Mapping) else None
    if not isinstance(decisions, list):
        raise PaperExecutionError("research-loop nie zawiera decisions")
    recorded: list[dict[str, Any]] = []
    for item in decisions:
        if not isinstance(item, Mapping):
            raise PaperExecutionError("niepoprawna decyzja research-loop")
        intent = item.get("trade_intent", item)
        if not isinstance(intent, Mapping):
            raise PaperExecutionError("brak trade_intent")
        symbol = str(intent.get("symbol", "")).strip()
        if symbol not in market_prices:
            raise PaperExecutionError(f"brak market price dla {symbol}")
        recorded.append(record_paper_decision(
            conn, intent, market_price=market_prices[symbol], run_id=run_id,
            model_version=model_version, policy_version=policy_version,
            dataset_version=dataset_version, fee_rate=fee_rate,
        ))
    return recorded


def validate_demo_config(config: Mapping[str, Any]) -> None:
    """Fail closed unless an explicitly sandbox-only, manually approved config exists."""
    if config.get("environment") != "sandbox":
        raise PaperExecutionError("demo wymaga environment=sandbox")
    endpoint = str(config.get("endpoint", ""))
    if not endpoint.startswith("https://") or "sandbox" not in endpoint.lower():
        raise PaperExecutionError("demo endpoint nie jest jawnie sandbox")
    if not str(config.get("credential_alias", "")).startswith("okx-demo-"):
        raise PaperExecutionError("brak aliasu klucza demo")
    if config.get("manual_approval") is not True:
        raise PaperExecutionError("demo wymaga ręcznej akceptacji paper→demo")
    if config.get("kill_switch", False) is True:
        raise PaperExecutionError("kill-switch jest aktywny")


class ExecutionGate:
    """Process-local stop/rollback gate; default state is stopped."""

    def __init__(self) -> None:
        self.stopped = True

    def stop(self) -> None:
        self.stopped = True

    def resume_after_approval(self) -> None:
        self.stopped = False

    def assert_running(self) -> None:
        if self.stopped:
            raise PaperExecutionError("execution zatrzymane przez kill-switch")
