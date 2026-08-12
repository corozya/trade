"""Read-only OKX futures instrument metadata used by Crypto Agent."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any


ALLOWED_OKX_FUTURES_BASES = frozenset({"BTC", "ETH", "DOGE", "XRP", "SOL", "LTC"})
FUTURES_LEVERAGE = 10
MAX_FUTURES_MARGIN_USDC = 100.0
_FUTURES_INST_FAMILY_SUFFIX = "-USD_UM_XPERP"


def _positive_decimal(value: Any, field: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"nieprawidłowe {field}={value!r}") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError(f"nieprawidłowe {field}={value!r}")
    return parsed


def _get_live_futures_instrument(base_symbol: str, client: Any) -> tuple[str, dict[str, Any]]:
    base = (base_symbol or "").upper().strip()
    if base not in ALLOWED_OKX_FUTURES_BASES:
        raise ValueError(f"symbol {base} nie jest dozwolony dla futures demo OKX")
    inst_family = f"{base}{_FUTURES_INST_FAMILY_SUFFIX}"
    if getattr(client, "simulated_trading", False):
        payload = client.get_account_instruments("FUTURES", inst_family=inst_family)
    else:
        payload = client.get_instruments("FUTURES", inst_family=inst_family)
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list) or not data:
        raise ValueError(f"brak instrumentu futures dla instFamily={inst_family}")
    live = [row for row in data if isinstance(row, dict) and row.get("state") == "live"]
    if not live:
        raise ValueError(f"brak aktywnego instrumentu futures dla {inst_family}")
    return base, live[0]


def resolve_futures_instrument(base_symbol: str, client: Any) -> dict[str, Any]:
    base, instrument = _get_live_futures_instrument(base_symbol, client)
    inst_id = instrument.get("instId")
    ct_val_ccy = str(instrument.get("ctValCcy") or "").upper().strip()
    if not isinstance(inst_id, str) or not inst_id.strip():
        raise ValueError(f"instrument {base} nie zawiera poprawnego instId")
    if ct_val_ccy != base:
        raise ValueError(f"nieobsługiwana waluta ctValCcy={ct_val_ccy!r}; oczekiwano {base}")
    return {
        "instId": inst_id,
        "ctVal": _positive_decimal(instrument.get("ctVal"), "ctVal"),
        "ctValCcy": ct_val_ccy,
        "lotSz": _positive_decimal(instrument.get("lotSz"), "lotSz"),
        "minSz": _positive_decimal(instrument.get("minSz"), "minSz"),
    }


def resolve_futures_inst_id(base_symbol: str, client: Any) -> str:
    _base, instrument = _get_live_futures_instrument(base_symbol, client)
    inst_id = instrument.get("instId")
    if not isinstance(inst_id, str) or not inst_id.strip():
        raise ValueError("instrument futures nie zawiera poprawnego instId")
    return inst_id

