#!/usr/bin/env python3
"""Semantic validation of a schema-valid Claude/Codex trading report."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone

from log_agent_krypto_decision import _report_from_payload

REQUIRED_SYMBOLS = {"BTC", "ETH", "DOGE"}
UNCERTAIN_EXECUTION_STATES = {"live", "partial", "canceled", "unknown", "timeout"}


def _parse_utc(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _validate_decision(decision: object, now: datetime) -> tuple[str | None, str | None]:
    if not isinstance(decision, dict):
        return None, "każda decyzja musi być obiektem"
    if set(decision) != {"trade_intent", "execution_result"}:
        return None, "decyzja może zawierać wyłącznie trade_intent i execution_result"

    intent = decision.get("trade_intent")
    result = decision.get("execution_result")
    if not isinstance(intent, dict):
        return None, "brak poprawnego TradeIntent"
    intent_fields = {
        "symbol",
        "decision",
        "side",
        "qty",
        "take_profit_price",
        "stop_loss_price",
        "reason",
        "strategy_artifact",
    }
    if set(intent) != intent_fields:
        return None, (
            "TradeIntent nie może deklarować wykonania ani stanu pozycji "
            "(niepoprawny zestaw pól)"
        )

    symbol = intent.get("symbol")
    if not isinstance(symbol, str):
        return None, "każdy TradeIntent musi zawierać symbol"
    reason = intent.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        return symbol, f"{symbol}: TradeIntent wymaga niepustego reason"

    action = intent.get("decision")
    trade_fields = ("side", "qty", "take_profit_price", "stop_loss_price")
    if action == "WAIT":
        non_null = [field for field in trade_fields if intent.get(field) is not None]
        if non_null or intent.get("strategy_artifact") is not None or result is not None:
            return symbol, (
                f"{symbol}: WAIT wymaga null dla pól transakcyjnych, "
                "strategy_artifact i execution_result"
            )
        return symbol, None

    if action != "TRADE":
        return symbol, f"{symbol}: nieznana decyzja {action!r}"
    if intent.get("side") not in {"LONG", "SHORT"}:
        return symbol, f"{symbol}: TRADE wymaga side LONG albo SHORT"
    for field in ("qty", "take_profit_price", "stop_loss_price"):
        value = intent.get(field)
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
            return symbol, f"{symbol}: TRADE wymaga dodatniego {field}"

    artifact = intent.get("strategy_artifact")
    if not isinstance(artifact, dict) or artifact.get("status") != "PROMOTED":
        return symbol, f"{symbol}: brak promowanego StrategyArtifact wymusza WAIT"
    for field in ("strategy_version", "dataset_version", "feature_schema_version"):
        if not isinstance(artifact.get(field), str) or not artifact[field].strip():
            return symbol, f"{symbol}: niepełny StrategyArtifact wymusza WAIT"
    expires_at = _parse_utc(artifact.get("expires_at"))
    if expires_at is None or expires_at <= now:
        return symbol, f"{symbol}: przeterminowany StrategyArtifact wymusza WAIT"

    if not isinstance(result, dict):
        return symbol, f"{symbol}: TRADE wymaga autorytatywnego ExecutionResult"
    if set(result) != {
        "state",
        "order_id",
        "filled_qty",
        "reconciliation_required",
    }:
        return symbol, f"{symbol}: niepoprawny zestaw pól ExecutionResult"
    state = result.get("state")
    if state in UNCERTAIN_EXECUTION_STATES:
        return symbol, f"{symbol}: ExecutionResult state={state} nie może dać completed"
    if state != "filled":
        return symbol, f"{symbol}: nieznany ExecutionResult state={state!r}"
    order_id = result.get("order_id")
    filled_qty = result.get("filled_qty")
    if not isinstance(order_id, str) or not order_id.strip():
        return symbol, f"{symbol}: filled wymaga niepustego order_id"
    if (
        not isinstance(filled_qty, (int, float))
        or isinstance(filled_qty, bool)
        or filled_qty <= 0
    ):
        return symbol, f"{symbol}: filled wymaga dodatniego filled_qty"
    if filled_qty > intent["qty"]:
        return symbol, f"{symbol}: filled_qty nie może przekraczać qty z TradeIntent"
    if not isinstance(result.get("reconciliation_required"), bool):
        return symbol, f"{symbol}: ExecutionResult wymaga reconciliation_required"
    return symbol, None


def validate(
    payload: object,
    provider: str,
    *,
    now: datetime | None = None,
) -> str | None:
    if not isinstance(payload, dict):
        return "odpowiedź JSON nie jest obiektem"

    report = _report_from_payload(payload, provider)
    if report is None:
        return "brak technicznego raportu w odpowiedzi providera"

    status = report.get("status")
    if status == "failed":
        return "agent zwrócił status=failed"
    if status != "completed":
        return f"niepoprawny status={status!r}; wymagany completed"

    round_id = report.get("round_id")
    if not isinstance(round_id, int) or isinstance(round_id, bool):
        return "completed wymaga nie-null całkowitego round_id"

    decisions = report.get("decisions")
    if not isinstance(decisions, list) or not decisions:
        return "completed wymaga decyzji dla BTC, ETH i DOGE"

    validation_time = now or datetime.now(timezone.utc)
    symbols: list[str] = []
    for decision in decisions:
        symbol, error = _validate_decision(decision, validation_time)
        if error:
            return error
        assert symbol is not None
        symbols.append(symbol)

    duplicates = sorted({symbol for symbol in symbols if symbols.count(symbol) > 1})
    if duplicates:
        return f"zduplikowane decyzje dla: {', '.join(duplicates)}"

    actual = set(symbols)
    if actual != REQUIRED_SYMBOLS:
        missing = sorted(REQUIRED_SYMBOLS - actual)
        extra = sorted(actual - REQUIRED_SYMBOLS)
        details = []
        if missing:
            details.append(f"brak: {', '.join(missing)}")
        if extra:
            details.append(f"nadmiarowe: {', '.join(extra)}")
        return "niepełny komplet decyzji (" + "; ".join(details) + ")"
    return None


def main() -> int:
    provider = sys.argv[1] if len(sys.argv) > 1 else "claude"
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        print(f"niepoprawny JSON: {exc}", file=sys.stderr)
        return 1

    error = validate(payload, provider)
    if error:
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

