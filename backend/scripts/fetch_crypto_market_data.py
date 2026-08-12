#!/usr/bin/env python3
"""#78: krok 1/3 pipeline'u agenta krypto — pobiera surowe dane rynkowe OKX
dla BTC/ETH (X-Perps futures) potrzebne skryptowi analizy (#79).

Zakres danych (spec ustalona z agentem-traderem, #80, 2026-07-21):
- Świece 15m (100-200 wstecz), 5m (50-60 wstecz), 1H i 4H (50-100 wstecz)
- Order book: best bid/ask + głębokość (sz=20 poziomów)
- Funding rate (bieżący + przewidywany)
- Open Interest (poziom bieżący)

NIE liczy żadnych wskaźników (RSI/EMA/ATR/...) — surowe dane wejściowe dla #79.

Zapis: jeden plik JSON per symbol, nadpisywany "latest" (cron co 15 min,
konsument #79 zawsze czyta najświeższy stan) — backend/data/crypto_market/{symbol}_latest.json.

Odporność: pojedynczy failed fetch (candle/orderbook/funding/OI) nie wywala
całego skryptu — błąd per-sekcja jest zapisany w JSON (pole "error"), reszta
danych zapisuje się normalnie. Retry/backoff na odczytach już wbudowany w
OkxClient (services/okx_client.py, _retry_read).

Użycie:
    cd backend
    .venv/bin/python scripts/fetch_crypto_market_data.py [alias]

    # przykład (cron):
    .venv/bin/python scripts/fetch_crypto_market_data.py okx_demo_main_full

Domyślny alias: "demo_main_full" (portfel Claude-krypto, patrz
.claude/skills/agent-krypto/SKILL.md). Dane rynkowe (candles/orderbook/
funding-rate/open-interest) są publiczne — alias wybiera tylko z jakim
kluczem/regionem podpisywać żądania (my.okx.com wymaga podpisu nawet dla
endpointów publicznych w praktyce klienta, patrz OkxClient).

Wyjście: PASS/FAIL per symbol na stdout, exit code 0 gdy przynajmniej jeden
symbol zapisał się bez krytycznego błędu (brak credentials), 1 gdy wszystkie
symbole zawiodły całkowicie.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BACKEND_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = BACKEND_ROOT.parent
sys.path.insert(0, str(BACKEND_ROOT))

from dotenv import load_dotenv

load_dotenv(REPO_ROOT / ".env")

from services.okx_client import OkxClient, OkxCredentialsError, OkxError
from services.okx_trade import ALLOWED_OKX_FUTURES_BASES, _resolve_futures_inst_id

DATA_DIR = BACKEND_ROOT / "data" / "crypto_market"

SYMBOLS: list[str] = sorted(ALLOWED_OKX_FUTURES_BASES)

CANDLE_SPECS: list[tuple[str, str, int]] = [
    # (label, bar, limit)
    ("15m", "15m", 150),
    ("5m", "5m", 60),
    ("1H", "1H", 80),
    ("4H", "4H", 60),
]

ORDERBOOK_DEPTH = 20


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{datetime.now(timezone.utc).microsecond // 1000:03d}Z"


def _safe_call(fn, *args, **kwargs) -> dict[str, Any]:
    """Wywołuje fn i zwraca {"ok": True, "data": ...} albo {"ok": False, "error": "..."}.

    Pojedynczy failed fetch nie ma wywalać całego skryptu (#78 wymaganie
    odporności — cron co 15 min).
    """
    try:
        result = fn(*args, **kwargs)
        return {"ok": True, "data": result}
    except OkxError as exc:
        return {"ok": False, "error": str(exc)}


def fetch_symbol(client: OkxClient, inst_id: str) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "inst_id": inst_id,
        "fetched_at": _now_iso(),
        "candles": {},
        "orderbook": None,
        "funding_rate": None,
        "open_interest": None,
    }

    for label, bar, limit in CANDLE_SPECS:
        payload["candles"][label] = _safe_call(client.get_candles, inst_id, bar=bar, limit=limit)

    payload["orderbook"] = _safe_call(client.get_orderbook, inst_id, sz=ORDERBOOK_DEPTH)
    payload["funding_rate"] = _safe_call(client.get_funding_rate, inst_id)
    payload["open_interest"] = _safe_call(client.get_open_interest, inst_id=inst_id, inst_type="FUTURES")

    return payload


def _pass(step: str, detail: str = "") -> None:
    suffix = f" — {detail}" if detail else ""
    print(f"[PASS] {step}{suffix}")


def _fail(step: str, detail: str = "") -> None:
    suffix = f" — {detail}" if detail else ""
    print(f"[FAIL] {step}{suffix}")


def main() -> int:
    alias = sys.argv[1] if len(sys.argv) > 1 else "demo_main_full"
    print(f"Fetch crypto market data — alias='{alias}'")
    print("-" * 60)

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    client = OkxClient(alias, simulated_trading=False)
    any_success = False

    try:
        for symbol in SYMBOLS:
            try:
                inst_id = _resolve_futures_inst_id(symbol, client)
                payload = fetch_symbol(client, inst_id)
            except OkxCredentialsError as exc:
                _fail(symbol, str(exc))
                continue
            except (OkxError, ValueError) as exc:
                _fail(symbol, f"resolve instId nieudany: {exc}")
                continue

            failed_sections = [
                key for key in ("orderbook", "funding_rate", "open_interest")
                if not payload[key]["ok"]
            ]
            failed_sections += [
                f"candles.{label}" for label, result in payload["candles"].items()
                if not result["ok"]
            ]

            section_results = [
                payload[key] for key in ("orderbook", "funding_rate", "open_interest")
            ] + list(payload["candles"].values())
            symbol_has_data = any(result["ok"] for result in section_results)
            if not symbol_has_data:
                _fail(symbol, f"sekcje z błędem: {', '.join(failed_sections)} (brak danych, pominięto zapis)")
                continue

            out_path = DATA_DIR / f"{symbol}_latest.json"
            out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))

            if failed_sections:
                _fail(symbol, f"sekcje z błędem: {', '.join(failed_sections)} (zapisano częściowo -> {out_path})")
            else:
                _pass(symbol, f"zapisano -> {out_path}")
            any_success = True
    finally:
        client.close()

    print("-" * 60)
    if any_success:
        print("WYNIK: dane zapisane (patrz PASS/FAIL per symbol i pole 'error' w JSON dla szczegółów).")
        return 0
    print("WYNIK: FAIL — żadna sekcja danych nie została pobrana.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
