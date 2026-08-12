"""Fail-closed risk and execution engine for OKX Demo X-Perps.

This is the server-side boundary for agent ``TradeIntent`` objects.  It keeps
the legacy order helper out of the safety decision: current exposure and order
state always come from OKX, while SQLite only stores idempotency/reconciliation
metadata.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Optional

from services.okx_client import OkxApiError, OkxClient, OkxError
from services.okx_trade import (
    ALLOWED_OKX_FUTURES_BASES,
    FUTURES_LEVERAGE,
    FUTURES_MARGIN_MODE,
    MAX_FUTURES_MARGIN_USDC,
    _decimal_to_okx_size,
    _extract_order_fill,
    _extract_place_order_result,
    _extract_ticker_last_price,
    _get_order_until_terminal,
    _resolve_futures_instrument,
)

UNCERTAIN_STATES = frozenset({"live", "partially_filled", "canceled", "unknown"})
TERMINAL_SUCCESS_STATE = "filled"
FLAT_CONFIRM_ATTEMPTS = 5
ORDER_NOT_FOUND_CODES = frozenset({"51603"})


def _decimal(value: Any, field: str, *, positive: bool = False) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field} musi być poprawną liczbą") from exc
    if not result.is_finite() or (positive and result <= 0):
        raise ValueError(f"{field} musi być dodatnie")
    return result


def _ensure_store(conn: Any) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS okx_execution_requests (
            portfolio_id INTEGER NOT NULL,
            idempotency_key TEXT NOT NULL,
            request_fingerprint TEXT NOT NULL,
            cl_ord_id TEXT NOT NULL,
            state TEXT NOT NULL,
            exchange_order_id TEXT,
            result_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (portfolio_id, idempotency_key),
            UNIQUE (cl_ord_id)
        )
        """
    )


def _fingerprint(intent: Mapping[str, Any]) -> str:
    encoded = json.dumps(intent, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def deterministic_cl_ord_id(portfolio_id: int, idempotency_key: str) -> str:
    """Stable OKX-compatible client order id (max 32 alphanumeric chars)."""
    digest = hashlib.sha256(f"{portfolio_id}:{idempotency_key}".encode()).hexdigest()
    return f"ak{digest[:30]}"


def _position_for_instrument(payload: Any, inst_id: str) -> Decimal:
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        raise ValueError("OKX nie zwrócił autorytatywnej listy pozycji")
    matching = [row for row in data if isinstance(row, dict) and row.get("instId") == inst_id]
    if not matching:
        return Decimal("0")
    try:
        return sum((_decimal(row.get("pos", "0"), "position") for row in matching), Decimal("0"))
    except ValueError as exc:
        raise ValueError("OKX zwrócił niepoprawny stan pozycji") from exc


def _classify(action: str, side: str, qty: Decimal, position: Decimal) -> tuple[Decimal, bool]:
    signed_qty = qty if side == "BUY" else -qty
    target = position + signed_qty
    action = action.upper()
    if action in {"REDUCE", "CLOSE"}:
        if position == 0 or position * signed_qty >= 0:
            raise ValueError(f"{action} musi mieć stronę przeciwną do pozycji")
        if abs(qty) > abs(position) or position * target < 0:
            raise ValueError(f"{action} nie może przejść przez zero")
        if action == "CLOSE" and target != 0:
            raise ValueError("CLOSE musi wyzerować pozycję")
        return target, True
    if action == "REVERSE":
        if position == 0 or position * signed_qty >= 0:
            raise ValueError("REVERSE wymaga istniejącej pozycji przeciwnej do side")
        return signed_qty, False
    if action not in {"OPEN", "INCREASE"}:
        raise ValueError(f"nieznana operacja {action!r}")
    if position != 0 and position * signed_qty < 0:
        raise ValueError(f"{action} nie może niejawnie zmniejszyć ani odwrócić pozycji")
    return target, False


def _validate_open_risk(
    side: str,
    reference: Decimal,
    stop: Any,
    take_profit: Any,
    atr14: Any,
) -> tuple[Decimal, Decimal]:
    sl = _decimal(stop, "stop_loss_price", positive=True)
    tp = _decimal(take_profit, "take_profit_price", positive=True)
    atr = _decimal(atr14, "atr14", positive=True)
    risk = reference - sl if side == "BUY" else sl - reference
    reward = tp - reference if side == "BUY" else reference - tp
    if risk <= 0 or reward <= 0:
        raise ValueError("SL/TP są po złej stronie ceny")
    if risk < atr:
        raise ValueError("odległość SL musi wynosić co najmniej 1×ATR15m")
    if reward / risk < Decimal("1.5"):
        raise ValueError("reward/risk musi wynosić co najmniej 1.5")
    return sl, tp


def _order_result(
    client: Any,
    inst_id: str,
    order_id: Optional[str],
    cl_ord_id: str,
    expected_qty: Decimal,
    *,
    reduce_only: bool,
    phase: Optional[str] = None,
) -> dict[str, Any]:
    if order_id:
        detail = _get_order_until_terminal(client, inst_id, order_id)
    else:
        detail = client.get_order(inst_id, cl_ord_id=cl_ord_id)
        rows = detail.get("data") if isinstance(detail, dict) else None
        if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
            raise ValueError("OKX nie zwrócił zlecenia dla clOrdId")
        order_id = rows[0].get("ordId") or None
    avg_px, fill_qty, state = _extract_order_fill(detail)
    normalized_state = state or "unknown"
    ok = normalized_state == TERMINAL_SUCCESS_STATE and fill_qty == expected_qty
    result = {
        "ok": ok,
        "state": normalized_state,
        "order_id": order_id,
        "cl_ord_id": cl_ord_id,
        "filled_qty": float(fill_qty or 0),
        "fill_price": float(avg_px) if avg_px is not None else None,
        "reconciliation_required": not ok,
        "reduce_only": reduce_only,
        "requested_qty": float(expected_qty),
    }
    if phase:
        result["phase"] = phase
    return result


def _persist_result(
    conn: Any,
    portfolio_id: int,
    key: str,
    result: Mapping[str, Any],
    now: str,
) -> None:
    stored_state = "completed" if result["ok"] else "reconciliation_required"
    conn.execute(
        """UPDATE okx_execution_requests SET state=?,exchange_order_id=?,
           result_json=?,updated_at=? WHERE portfolio_id=? AND idempotency_key=?""",
        (
            stored_state, result.get("order_id"), json.dumps(dict(result)), now,
            portfolio_id, key,
        ),
    )
    conn.commit()


def _execute_reverse_open(
    *,
    client: Any,
    conn: Any,
    portfolio_id: int,
    key: str,
    intent: Mapping[str, Any],
    instrument: Mapping[str, Any],
    qty: Decimal,
    side: str,
    open_cl_ord_id: str,
    now: str,
    reconcile_before_submit: bool = False,
) -> dict[str, Any]:
    """Resume the OPEN half of REVERSE after an authoritative flat check."""
    inst_id = instrument["instId"]
    if _position_for_instrument(client.get_positions("FUTURES"), inst_id) != 0:
        result = {
            "ok": False, "state": "flat_unconfirmed", "order_id": None,
            "cl_ord_id": open_cl_ord_id, "requested_qty": float(qty),
            "reduce_only": False, "phase": "flat_confirmed",
            "reconciliation_required": True,
        }
        _persist_result(conn, portfolio_id, key, result, now)
        return result
    if reconcile_before_submit:
        try:
            existing_open = _order_result(
                client, inst_id, None, open_cl_ord_id, qty,
                reduce_only=False, phase="reverse_open_submitted",
            )
        except OkxApiError as exc:
            if exc.code not in ORDER_NOT_FOUND_CODES:
                raise
        else:
            _persist_result(conn, portfolio_id, key, existing_open, now)
            return existing_open
    flat_result = {
        "ok": False, "state": "flat_confirmed", "order_id": None,
        "cl_ord_id": open_cl_ord_id, "requested_qty": float(qty),
        "reduce_only": False, "phase": "flat_confirmed",
        "reconciliation_required": True,
    }
    conn.execute(
        """UPDATE okx_execution_requests SET state='flat_confirmed',
           exchange_order_id=NULL,cl_ord_id=?,result_json=?,updated_at=?
           WHERE portfolio_id=? AND idempotency_key=?""",
        (open_cl_ord_id, json.dumps(flat_result), now, portfolio_id, key),
    )
    conn.commit()
    ticker = _extract_ticker_last_price(client.get_ticker(inst_id))
    if ticker is None:
        raise ValueError("brak świeżej ceny OKX po CLOSE")
    sl, tp = _validate_open_risk(
        side, ticker, intent.get("stop_loss_price"),
        intent.get("take_profit_price"), intent.get("atr14"),
    )
    target_margin = qty * instrument["ctVal"] * ticker / Decimal(str(FUTURES_LEVERAGE))
    if target_margin > Decimal(str(MAX_FUTURES_MARGIN_USDC)):
        raise ValueError("wynikowa pozycja przekracza 100 USDC margin")
    try:
        payload = client.place_order(
            inst_id=inst_id, td_mode=FUTURES_MARGIN_MODE,
            side="buy" if side == "BUY" else "sell",
            ord_type="market", sz=_decimal_to_okx_size(qty),
            clOrdId=open_cl_ord_id,
            attachAlgoOrds=[{
                "slTriggerPx": str(sl), "slOrdPx": "-1",
                "tpTriggerPx": str(tp), "tpOrdPx": "-1",
            }],
        )
    except OkxError:
        uncertain = dict(
            flat_result, state="unknown", phase="reverse_open_submitted",
        )
        conn.execute(
            """UPDATE okx_execution_requests SET state='reverse_open_submitted',
               result_json=?,updated_at=?
               WHERE portfolio_id=? AND idempotency_key=?""",
            (json.dumps(uncertain), now, portfolio_id, key),
        )
        conn.commit()
        raise
    order_id, code, message = _extract_place_order_result(payload)
    if not order_id or code not in (None, "0", "1"):
        raise ValueError(f"OKX nie potwierdził przyjęcia OPEN dla REVERSE: {message}")
    conn.execute(
        """UPDATE okx_execution_requests SET state='reverse_open_submitted',
           exchange_order_id=?,updated_at=?
           WHERE portfolio_id=? AND idempotency_key=?""",
        (order_id, now, portfolio_id, key),
    )
    conn.commit()
    result = _order_result(
        client, inst_id, order_id, open_cl_ord_id, qty,
        reduce_only=False, phase="reverse_open_submitted",
    )
    _persist_result(conn, portfolio_id, key, result, now)
    return result


def execute_trade_intent(
    *,
    portfolio_id: int,
    intent: Mapping[str, Any],
    conn: Any,
    credential_alias: str,
    okx_client_factory=OkxClient,
) -> dict[str, Any]:
    """Validate, reserve and execute one TradeIntent on OKX Demo.

    Repeated calls with the same key/fingerprint return the persisted result;
    an unfinished request is reconciled by ``clOrdId`` metadata and never
    submitted again.
    """
    from services.game import ValidationError

    key = str(intent.get("idempotency_key") or "").strip()
    if not key:
        raise ValidationError("idempotency_key jest wymagany")
    symbol = str(intent.get("symbol") or "").upper().strip()
    side = str(intent.get("side") or "").upper().strip()
    action = str(intent.get("action") or "").upper().strip()
    if symbol not in ALLOWED_OKX_FUTURES_BASES or side not in {"BUY", "SELL"}:
        raise ValidationError("niedozwolony symbol lub side")
    try:
        qty = _decimal(intent.get("qty"), "qty", positive=True)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc

    fingerprint = _fingerprint(intent)
    cl_ord_id = deterministic_cl_ord_id(portfolio_id, key)
    now = datetime.now(timezone.utc).isoformat()
    _ensure_store(conn)
    existing = conn.execute(
        "SELECT * FROM okx_execution_requests WHERE portfolio_id=? AND idempotency_key=?",
        (portfolio_id, key),
    ).fetchone()
    if existing:
        if existing["request_fingerprint"] != fingerprint:
            raise ValidationError("idempotency_key został użyty dla innego żądania")
        previous = json.loads(existing["result_json"]) if existing["result_json"] else None
        if previous and previous.get("ok"):
            return previous
        client = okx_client_factory(credential_alias, simulated_trading=True)
        try:
            instrument = _resolve_futures_instrument(symbol, client)
            if existing["state"] == "reserved":
                try:
                    reconciled = _order_result(
                        client, instrument["instId"], None, existing["cl_ord_id"],
                        qty, reduce_only=False,
                    )
                except OkxApiError as exc:
                    if exc.code not in ORDER_NOT_FOUND_CODES:
                        raise
                else:
                    _persist_result(conn, portfolio_id, key, reconciled, now)
                    return reconciled
            if action == "REVERSE" and existing["state"] == "flat_confirmed":
                return _execute_reverse_open(
                    client=client, conn=conn, portfolio_id=portfolio_id, key=key,
                    intent=intent, instrument=instrument, qty=qty, side=side,
                    open_cl_ord_id=deterministic_cl_ord_id(portfolio_id, key)[:31] + "o",
                    now=now, reconcile_before_submit=True,
                )
            if existing["state"] != "reserved":
                expected_qty = _decimal(
                    previous.get("requested_qty", intent.get("qty")) if previous else intent.get("qty"),
                    "requested_qty", positive=True,
                )
                reconciled = _order_result(
                    client, instrument["instId"], existing["exchange_order_id"],
                    existing["cl_ord_id"], expected_qty,
                    reduce_only=bool(previous and previous.get("reduce_only")),
                    phase=previous.get("phase") if previous else None,
                )
                if previous and previous.get("phase") == "reverse_close":
                    # A reconciled CLOSE is not completion of the REVERSE intent.
                    if reconciled["ok"] and _position_for_instrument(
                        client.get_positions("FUTURES"), instrument["instId"],
                    ) == 0:
                        return _execute_reverse_open(
                            client=client, conn=conn, portfolio_id=portfolio_id, key=key,
                            intent=intent, instrument=instrument, qty=qty, side=side,
                            open_cl_ord_id=deterministic_cl_ord_id(portfolio_id, key)[:31] + "o",
                            now=now,
                        )
                    reconciled.update(
                        ok=False, state="flat_unconfirmed",
                        reconciliation_required=True,
                    )
                _persist_result(conn, portfolio_id, key, reconciled, now)
                return reconciled
        except (OkxError, ValueError):
            return previous or {
                "ok": False, "state": existing["state"],
                "order_id": existing["exchange_order_id"],
                "cl_ord_id": existing["cl_ord_id"],
                "reconciliation_required": True,
            }
    else:
        conn.execute(
            """INSERT INTO okx_execution_requests
               (portfolio_id,idempotency_key,request_fingerprint,cl_ord_id,state,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?)""",
            (portfolio_id, key, fingerprint, cl_ord_id, "reserved", now, now),
        )
        conn.commit()

    client = okx_client_factory(credential_alias, simulated_trading=True)
    try:
        instrument = _resolve_futures_instrument(symbol, client)
        inst_id = instrument["instId"]
        ticker = _extract_ticker_last_price(client.get_ticker(inst_id))
        if ticker is None:
            raise ValueError("brak świeżej ceny OKX")
        position = _position_for_instrument(client.get_positions("FUTURES"), inst_id)
        target, reduce_only = _classify(action, side, qty, position)
        if qty < instrument["minSz"] or qty % instrument["lotSz"] != 0:
            raise ValueError("qty nie spełnia minSz/lotSz")
        sl = tp = None
        if not reduce_only:
            sl, tp = _validate_open_risk(
                side, ticker, intent.get("stop_loss_price"),
                intent.get("take_profit_price"), intent.get("atr14"),
            )
            target_margin = abs(target) * instrument["ctVal"] * ticker / Decimal(str(FUTURES_LEVERAGE))
            if target_margin > Decimal(str(MAX_FUTURES_MARGIN_USDC)):
                raise ValueError("wynikowa pozycja przekracza 100 USDC margin")
        client.set_leverage(inst_id, str(FUTURES_LEVERAGE), FUTURES_MARGIN_MODE)
        if action == "REVERSE":
            close_qty = abs(position)
            close_side = "sell" if position > 0 else "buy"
            close_cl_ord_id = f"{cl_ord_id[:31]}c"
            close_payload = client.place_order(
                inst_id=inst_id, td_mode=FUTURES_MARGIN_MODE,
                side=close_side, ord_type="market",
                sz=_decimal_to_okx_size(close_qty),
                clOrdId=close_cl_ord_id, reduceOnly=True,
            )
            close_order_id, close_code, close_message = _extract_place_order_result(close_payload)
            if not close_order_id or close_code not in (None, "0", "1"):
                raise ValueError(f"OKX nie potwierdził CLOSE dla REVERSE: {close_message}")
            conn.execute(
                """UPDATE okx_execution_requests SET state='reverse_close_submitted',
                   exchange_order_id=?,cl_ord_id=?,updated_at=?
                   WHERE portfolio_id=? AND idempotency_key=?""",
                (close_order_id, close_cl_ord_id, now, portfolio_id, key),
            )
            conn.commit()
            close_result = _order_result(
                client, inst_id, close_order_id, close_cl_ord_id, close_qty,
                reduce_only=True, phase="reverse_close",
            )
            if not close_result["ok"]:
                _persist_result(conn, portfolio_id, key, close_result, now)
                return close_result
            flat = False
            for _ in range(FLAT_CONFIRM_ATTEMPTS):
                if _position_for_instrument(client.get_positions("FUTURES"), inst_id) == 0:
                    flat = True
                    break
            if not flat:
                close_result.update(
                    ok=False, state="flat_unconfirmed",
                    reconciliation_required=True,
                )
                _persist_result(conn, portfolio_id, key, close_result, now)
                return close_result
            return _execute_reverse_open(
                client=client, conn=conn, portfolio_id=portfolio_id, key=key,
                intent=intent, instrument=instrument, qty=qty, side=side,
                open_cl_ord_id=deterministic_cl_ord_id(portfolio_id, key)[:31] + "o",
                now=now,
            )
        extras: dict[str, Any] = {"clOrdId": cl_ord_id}
        if reduce_only:
            extras["reduceOnly"] = True
        else:
            extras["attachAlgoOrds"] = [{
                "slTriggerPx": str(sl), "slOrdPx": "-1",
                "tpTriggerPx": str(tp), "tpOrdPx": "-1",
            }]
        payload = client.place_order(
            inst_id=inst_id, td_mode=FUTURES_MARGIN_MODE,
            side="buy" if side == "BUY" else "sell",
            ord_type="market", sz=_decimal_to_okx_size(qty), **extras,
        )
        order_id, code, message = _extract_place_order_result(payload)
        if not order_id or code not in (None, "0", "1"):
            raise ValueError(f"OKX nie potwierdził przyjęcia zlecenia: {message}")
        conn.execute(
            """UPDATE okx_execution_requests SET state='submitted',
               exchange_order_id=?,updated_at=? WHERE portfolio_id=? AND idempotency_key=?""",
            (order_id, now, portfolio_id, key),
        )
        conn.commit()
        result = _order_result(
            client, inst_id, order_id, cl_ord_id, qty, reduce_only=reduce_only,
            phase=None,
        )
        _persist_result(conn, portfolio_id, key, result, now)
        return result
    except (OkxError, ValueError) as exc:
        conn.execute(
            """UPDATE okx_execution_requests
               SET state=CASE WHEN state IN ('flat_confirmed','reverse_open_submitted')
                              THEN state ELSE 'reconciliation_required' END,
                   updated_at=?
               WHERE portfolio_id=? AND idempotency_key=?""",
            (now, portfolio_id, key),
        )
        conn.commit()
        raise ValidationError(f"bezpieczne wykonanie odrzucone: {exc}") from exc
