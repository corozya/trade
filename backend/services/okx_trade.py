"""#72: futures long/short z dźwignią (portfel kind='game', exchange='okx',
execution_mode='trading') — market order realnie wysyłany na OKX X-Perps
(demo/simulated_trading=True), jedyny punkt wejścia dla tej kombinacji
kind/exchange/execution_mode (decyzja usera 2026-07-21).

Zakres:
- execute_okx_futures_order(...): futures long/short z dźwignią, wołany z
  services.game.execute_trade (patrz dispatch tam).
- WYŁĄCZNIE market orders (limit orders poza scope).
- ALLOWED_OKX_FUTURES_BASES: twarda allowlist symboli bazowych — instId
  resolved dynamicznie (patrz _resolve_futures_instrument).
- MAX_FUTURES_MARGIN_USDC: twardy limit bezpieczeństwa dla jawnego/manualnego
  qty, sprawdzany PRZED place_order. Automatyczne wejścia ``qty=None`` mają
  osobny cel/limit 10% dostępnego equity USDC jako notional.
- Błąd OKX (OkxError) podczas składania zlecenia -> propaguje jako
  ValidationError.

Brak lokalnego ledgera (decyzja usera 2026-07-22, patrz docstring
execute_okx_futures_order): portfel trading+okx opiera się WYŁĄCZNIE na
wartości konta zsynchronizowanej z OKX — żadna transakcja/pozycja nie jest
zapisywana do bazy, więc nie ma tu już idempotencji ani FIFO/ledger do
utrzymania (usunięte razem z execute_okx_market_order, martwym kodem — nic
w dispatchu produkcyjnym go nie wołało, patrz services/game.py::execute_trade).

Bezpieczeństwo: żaden sekret nie trafia do logów/wyjątków — ta sama zasada
co services/okx_client.py i services/okx_sync.py (#66/#67).
"""
from __future__ import annotations

import logging
import sqlite3
import time
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from typing import Any, Optional

from services.db import get_conn
from services.okx_client import OkxClient, OkxError
from services.pricing import load_fx

logger = logging.getLogger(__name__)

# Race condition odkryty manualnie w e2e (2026-07-21): GET /api/v5/trade/order
# zaraz po POST place_order czasem trafia moment PRZED faktycznym wypełnieniem
# market ordera na koncie demo OKX (state='live', accFillSz='0') — zlecenie
# i tak wykona się na giełdzie chwilę później, ale bez retry lokalny zapis
# widzi fill_qty=0 i _record_trade odrzuca transakcję (ValidationError
# "quantity musi być większe od 0"), mimo że pozycja na OKX już się zmieniła.
_ORDER_FILL_POLL_ATTEMPTS = 5
_ORDER_FILL_POLL_INTERVAL_SECONDS = 0.3
_TERMINAL_ORDER_STATES = frozenset({"filled", "canceled", "partially_filled"})


def _get_order_until_terminal(client: "OkxClient", inst_id: str, ord_id: str) -> Any:
    """GET /api/v5/trade/order z krótkim pollingiem do stanu terminalnego
    (filled/partially_filled/canceled) — zamiast pojedynczego strzału zaraz
    po place_order, który czasem trafia stan 'live' (jeszcze niewypełnione)."""
    last_payload = None
    for attempt in range(_ORDER_FILL_POLL_ATTEMPTS):
        last_payload = client.get_order(inst_id, ord_id)
        data = last_payload.get("data") if isinstance(last_payload, dict) else None
        state = data[0].get("state") if isinstance(data, list) and data and isinstance(data[0], dict) else None
        if state in _TERMINAL_ORDER_STATES:
            return last_payload
        if attempt < _ORDER_FILL_POLL_ATTEMPTS - 1:
            time.sleep(_ORDER_FILL_POLL_INTERVAL_SECONDS)
    return last_payload

# Symbole bazowe dozwolone dla futures — analogicznie do ALLOWED_OKX_INSTRUMENTS
# (spot), ale tu to prefiks (BTC/ETH/DOGE/SOL/XRP), bo instId futures ma
# zmienny sufiks daty (np. BTC-USD_UM_XPERP-310328) resolved dynamicznie, nie
# stały string. DOGE dodany jako jedyny altcoin z realną płynnością wśród
# instrumentów X-Perps dostępnych dla kont EEA (patrz
# _FUTURES_INST_FAMILY_SUFFIX) — reszta puli X-Perps (UNI, GRASS, ID, XPL, BZ,
# CAP) ma wolumen rzędu pojedynczych-set USD/24h, nienadający się do handlu
# (sprawdzone manualnie 2026-07-22, ranking volCcy24h*last na
# public/instruments+market/ticker).
# SOL i XRP (research-loop paper mode emituje 5 symboli: BTC/ETH/DOGE/SOL/XRP,
# #133/#134) zostały PRÓBOWANE w #136, ale COFNIĘTE 2026-07-24 po realnym
# teście na koncie OKX Demo: SOL-USD_UM_XPERP i XRP-USD_UM_XPERP nie istnieją
# jako instFamily na tym koncie (OKX API error code=51000 "Parameter
# instFamily error", zweryfikowane bezpośrednim get_instruments, nie tylko
# fake/mockowanym clientem). Oba symbole mają wyłącznie futures Z TERMINEM
# WYGAŚNIĘCIA (np. instFamily="SOL-USD_UM"/instId="SOL-USD_UM-260724" itd.,
# NIE perpetual) — inny typ kontraktu i inny profil ryzyka (rollover) niż
# BTC/ETH/DOGE poniżej. XRP dodatkowo ma ctValCcy="USD" (nie "XRP"), co i tak
# złamałoby istniejący check `ct_val_ccy != base` w _resolve_futures_instrument
# nawet gdyby instFamily się zgadzał. Rozszerzenie na futures-z-wygaśnięciem
# wymaga osobnej, przemyślanej decyzji (patrz ATS #136 blocker comment), nie
# jest to prosty dopisek do tego seta — dopóki taka decyzja nie zapadnie,
# demo_execution.py bezpiecznie pomija SOL/XRP jako "unsupported by execution
# boundary" (audytowane SKIPPED), tak jak przed #136.
# XRP added 2026-08-07 (#198) — re-verified directly against demo_main_full:
# XRP-USD_UM_XPERP-310801 exists as a live perpetual with ctValCcy=XRP,
# unlike when SOL/XRP were both rejected in #136 (2026-07-24). SOL is NOT
# re-added — still only has dated-expiry SOL-USD_UM-YYMMDD contracts on this
# account (verified again 2026-08-07: 6 live instruments, none _XPERP/
# perpetual), same problem as #136 documented above. WLD is NOT included —
# it doesn't exist as a FUTURES instrument on demo_main_full at all (0
# results across all 120 instruments, verified 2026-08-07); the WLD position
# crypto-dashboard's /api/okx_position (#190) shows lives on the REAL
# account (real_main), a completely different account than this demo one.
# SOL added 2026-08-09 (#240) — re-verified directly against demo_main_full a
# THIRD time: SOL-USD_UM_XPERP-310404 now exists as a live perpetual with
# ctVal=0.01, ctValCcy=SOL, lotSz=1, minSz=1, identical shape to BTC/ETH/DOGE/
# XRP above. This reverses the two earlier rejections (#136 2026-07-24 and
# the 2026-08-07 re-check documented above): OKX apparently added the
# perpetual instrument for this account sometime between 2026-08-07 and
# 2026-08-09. This is NOT evidence the earlier documentation was wrong — both
# prior checks were real, direct API verifications (code=51000 / dated-expiry
# only) at the time they were made. It demonstrates that OKX's available
# instrument set on demo_main_full can change over a ~2-day window, so any
# future re-exclusion/re-inclusion decision for a previously-rejected symbol
# should re-verify live via _resolve_futures_instrument rather than trusting
# older comments here. LTC added 2026-08-09 (#240) as a brand-new addition
# (never previously tried/rejected in this project) — verified live the same
# way: LTC-USD_UM_XPERP-310404 exists with ctVal=0.1, ctValCcy=LTC, lotSz=1,
# minSz=1. WLD's exclusion above is unaffected by this and remains current.
ALLOWED_OKX_FUTURES_BASES = frozenset({"BTC", "ETH", "DOGE", "XRP", "SOL", "LTC"})

# Rodzina instrumentów X-Perps (MiCA-regulated, jedyne futures dostępne dla
# kont EEA/Polska na my.okx.com — standardowe SWAP/FUTURES zwracają code=51155
# "local compliance restrictions", zweryfikowane manualnie 2026-07-21).
# Wariant "_XPERP" (kontrakt z terminem 2031, bez cotygodniowego rolowania,
# max dźwignia 10x) wybrany zamiast weekly/quarterly "_UM" (bez sufiksu) —
# decyzja usera: ETH ma TYLKO wariant _XPERP (brak weekly/quarterly), więc
# _XPERP daje spójną strukturę dla obu instrumentów i unika logiki rolowania.
_FUTURES_INST_FAMILY_SUFFIX = "-USD_UM_XPERP"

# Decyzja usera (2026-07-21): dźwignia x10, isolated margin.
FUTURES_LEVERAGE = 10
FUTURES_MARGIN_MODE = "isolated"

# Legacy/manual safety limit. Automatic risk-sized entries use 10% available
# USDC equity as notional instead; explicit qty remains capped here.
MAX_FUTURES_MARGIN_USDC = 100.0
FUTURES_RISK_PER_TRADE_PCT = Decimal("0.0025")
FUTURES_TARGET_NOTIONAL_PCT = Decimal("0.10")


def _positive_decimal(value: Any, field: str) -> Decimal:
    """Konwertuje wymagane dodatnie pole metadanych OKX na Decimal."""
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"nieprawidłowe {field}={value!r}") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError(f"nieprawidłowe {field}={value!r}")
    return parsed


def _available_usdc_equity(balance_payload: Any) -> Decimal:
    """Extract authoritative, currently available USDC equity from OKX."""
    data = balance_payload.get("data") if isinstance(balance_payload, dict) else None
    first = data[0] if isinstance(data, list) and data else None
    details = first.get("details") if isinstance(first, dict) else None
    if not isinstance(details, list):
        raise ValueError("saldo OKX nie zawiera details")
    for row in details:
        if isinstance(row, dict) and str(row.get("ccy") or "").upper() == "USDC":
            return _positive_decimal(row.get("availEq"), "USDC availEq")
    raise ValueError("saldo OKX nie zawiera dostępnego equity USDC")


def calculate_okx_futures_entry_size(
    *,
    available_usdc_equity: Any,
    entry_price: Any,
    stop_loss_price: Any,
    side: str,
    instrument: dict[str, Any],
    max_position_pct: Any,
) -> dict[str, Decimal | str]:
    """Calculate a fail-closed X-Perp entry size, rounded down to ``lotSz``."""
    equity = _positive_decimal(available_usdc_equity, "available_usdc_equity")
    entry = _positive_decimal(entry_price, "entry_price")
    stop = _positive_decimal(stop_loss_price, "stop_loss_price")
    direction = str(side or "").upper().strip()
    if direction == "BUY" and stop >= entry:
        raise ValueError("LONG wymaga stop_loss_price poniżej entry_price")
    if direction == "SELL" and stop <= entry:
        raise ValueError("SHORT wymaga stop_loss_price powyżej entry_price")
    if direction not in {"BUY", "SELL"}:
        raise ValueError("side musi być BUY/SELL")

    ct_val = _positive_decimal(instrument.get("ctVal"), "ctVal")
    lot_sz = _positive_decimal(instrument.get("lotSz"), "lotSz")
    min_sz = _positive_decimal(instrument.get("minSz"), "minSz")
    mandate_pct = _positive_decimal(max_position_pct, "max_position_pct")
    if mandate_pct > 100:
        raise ValueError("max_position_pct nie może przekraczać 100")

    risk_budget = equity * FUTURES_RISK_PER_TRADE_PCT
    target_notional = equity * FUTURES_TARGET_NOTIONAL_PCT
    loss_per_contract = abs(entry - stop) * ct_val
    notional_per_contract = entry * ct_val
    risk_qty = risk_budget / loss_per_contract
    target_notional_qty = target_notional / notional_per_contract
    mandate_qty = (equity * mandate_pct / Decimal("100")) / notional_per_contract
    raw_qty = min(risk_qty, target_notional_qty, mandate_qty)
    qty = (raw_qty / lot_sz).to_integral_value(rounding=ROUND_DOWN) * lot_sz
    if qty < min_sz:
        raise ValueError(
            f"wyliczone qty={_decimal_to_okx_size(qty)} jest mniejsze niż minSz={min_sz}"
        )
    return {
        "qty": qty,
        "available_usdc_equity": equity,
        "entry_price": entry,
        "stop_loss_price": stop,
        "risk_budget_usdc": risk_budget,
        "target_notional_usdc": target_notional,
        "risk_qty": risk_qty,
        "target_notional_qty": target_notional_qty,
        "mandate_qty": mandate_qty,
        "binding_cap": min(
            (("risk", risk_qty), ("target_notional", target_notional_qty), ("mandate", mandate_qty)),
            key=lambda item: item[1],
        )[0],
    }


def quote_okx_futures_entry_size(
    *,
    portfolio_id: int,
    symbol: str,
    side: str,
    entry_price: Any,
    stop_loss_price: Any,
    conn: sqlite3.Connection,
    okx_client_factory=OkxClient,
) -> dict[str, Any]:
    """Read-only live quote used by agent-krypto before an OPEN."""
    from services.game import NotFoundError, ValidationError, get_portfolio_row

    pf = get_portfolio_row(portfolio_id, conn)
    if pf.get("archived"):
        raise NotFoundError(f"portfolio {portfolio_id} nie jest aktywne")
    if not (
        pf.get("kind") == "game"
        and pf.get("exchange") == "okx"
        and pf.get("execution_mode") == "trading"
    ):
        raise ValidationError("risk sizing jest dostępny wyłącznie dla OKX Demo futures")
    base = (symbol or "").upper().strip()
    if base not in ALLOWED_OKX_FUTURES_BASES:
        raise ValidationError(f"symbol {base} nie jest dozwolony dla futures demo OKX")
    try:
        client = okx_client_factory(pf.get("exchange_credential_alias"), simulated_trading=True)
        instrument = _resolve_futures_instrument(base, client)
        balance = client.get_balance("USDC")
        available = _available_usdc_equity(balance)
        result = calculate_okx_futures_entry_size(
            available_usdc_equity=available,
            entry_price=entry_price,
            stop_loss_price=stop_loss_price,
            side=side,
            instrument=instrument,
            max_position_pct=pf.get("strategy_profile", {}).get("max_position_pct"),
        )
    except (OkxError, ValueError) as exc:
        raise ValidationError(f"nie można wyliczyć qty futures: {exc}") from exc
    return {
        key: _decimal_to_okx_size(value) if isinstance(value, Decimal) else value
        for key, value in result.items()
    } | {"symbol": base, "instId": instrument["instId"]}


def _get_live_futures_instrument(base_symbol: str, client: "OkxClient") -> tuple[str, dict[str, Any]]:
    """Pobiera metadane X-Perp z katalogu właściwego dla trybu klienta.

    Demo musi korzystać z uwierzytelnionego ``account/instruments``: publiczny
    katalog może wskazywać inne instId niż faktycznie tradowalne na koncie
    symulowanym. Brak wyniku konta jest celowo fail-closed — bez fallbacku do
    katalogu publicznego. Odczyty real/public zachowują dotychczasowe źródło.
    """
    base = (base_symbol or "").upper().strip()
    inst_family = f"{base}{_FUTURES_INST_FAMILY_SUFFIX}"
    if getattr(client, "simulated_trading", False):
        payload = client.get_account_instruments("FUTURES", inst_family=inst_family)
    else:
        payload = client.get_instruments("FUTURES", inst_family=inst_family)
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list) or not data:
        raise ValueError(f"brak instrumentu futures dla instFamily={inst_family}")
    live = [d for d in data if isinstance(d, dict) and d.get("state") == "live"]
    if not live:
        raise ValueError(f"brak aktywnego (state=live) instrumentu futures dla {inst_family}")
    return base, live[0]


def _resolve_futures_instrument(base_symbol: str, client: "OkxClient") -> dict[str, Any]:
    """Zwraca aktywny X-Perp wraz z metadanymi potrzebnymi do sizingu.

    ``qty`` na OKX oznacza liczbę kontraktów. Wartość jednego kontraktu
    pochodzi z ``ctVal`` i nie może być zastępowana jednostką instrumentu
    bazowego (np. dla ETH ctVal=0.1, a dla DOGE ctVal=1000).
    """
    base, instrument = _get_live_futures_instrument(base_symbol, client)
    inst_family = f"{base}{_FUTURES_INST_FAMILY_SUFFIX}"
    inst_id = instrument.get("instId")
    ct_val_ccy = str(instrument.get("ctValCcy") or "").upper().strip()
    if not isinstance(inst_id, str) or not inst_id.strip():
        raise ValueError(f"instrument {inst_family} nie zawiera poprawnego instId")
    if ct_val_ccy != base:
        raise ValueError(
            f"nieobsługiwana waluta ctValCcy={ct_val_ccy!r} dla {inst_family}; oczekiwano {base}"
        )
    return {
        "instId": inst_id,
        "ctVal": _positive_decimal(instrument.get("ctVal"), "ctVal"),
        "ctValCcy": ct_val_ccy,
        "lotSz": _positive_decimal(instrument.get("lotSz"), "lotSz"),
        "minSz": _positive_decimal(instrument.get("minSz"), "minSz"),
    }


def _resolve_futures_inst_id(base_symbol: str, client: "OkxClient") -> str:
    """Kompatybilny helper dla odczytowych konsumentów potrzebujących tylko instId."""
    base, instrument = _get_live_futures_instrument(base_symbol, client)
    inst_id = instrument.get("instId")
    if not isinstance(inst_id, str) or not inst_id.strip():
        raise ValueError(f"instrument {base}{_FUTURES_INST_FAMILY_SUFFIX} nie zawiera poprawnego instId")
    return inst_id


def _decimal_to_okx_size(value: Decimal) -> str:
    """Zapis Decimal bez notacji naukowej i zbędnych zer końcowych."""
    rendered = format(value, "f")
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


def _extract_ticker_last_price(payload: Any) -> Optional[Decimal]:
    """Wyciąga 'last' (ostatnia cena rynkowa) z GET /api/v5/market/ticker."""
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list) or not data:
        return None
    first = data[0]
    if not isinstance(first, dict):
        return None
    raw = first.get("last")
    if raw is None:
        return None
    try:
        return Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return None


def _extract_place_order_result(payload: Any) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Wyciąga (ordId, sCode, sMsg) z odpowiedzi POST /api/v5/trade/order."""
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list) or not data:
        return None, None, None
    first = data[0]
    if not isinstance(first, dict):
        return None, None, None
    return first.get("ordId"), first.get("sCode"), first.get("sMsg")


def _extract_order_fill(payload: Any) -> tuple[Optional[Decimal], Optional[Decimal], Optional[str]]:
    """Wyciąga (avgPx, accFillSz, state) z GET /api/v5/trade/order.

    accFillSz (skumulowane wypełnienie CAŁEGO zlecenia), nie fillSz (rozmiar
    TYLKO ostatniej pojedynczej transzy) — market order może wypełnić się
    kilkoma transakcjami (kilka tradeId), fillSz wtedy zaniża realną ilość
    o rząd wielkości (odkryte w teście e2e: qty=0.001 BTC zażądane, accFillSz
    ~0.001, ale fillSz tylko 0.00000785 — jedna z kilku transz fillu).
    """
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list) or not data:
        return None, None, None
    first = data[0]
    if not isinstance(first, dict):
        return None, None, None
    avg_px = first.get("avgPx")
    fill_sz = first.get("accFillSz") or first.get("fillSz")
    state = first.get("state")
    try:
        avg_px_dec = Decimal(str(avg_px)) if avg_px not in (None, "") else None
    except (InvalidOperation, ValueError):
        avg_px_dec = None
    try:
        fill_sz_dec = Decimal(str(fill_sz)) if fill_sz not in (None, "") else None
    except (InvalidOperation, ValueError):
        fill_sz_dec = None
    return avg_px_dec, fill_sz_dec, state


def execute_okx_futures_order(
    *,
    portfolio_id: int,
    symbol: str,
    side: str,
    qty: Optional[float],
    reason: str,
    take_profit_price: Optional[float] = None,
    stop_loss_price: Optional[float] = None,
    limit_price: Optional[float] = None,
    market: Optional[str] = None,
    exit_level: Optional[float] = None,
    round_id: Optional[int] = None,
    dt: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    commission_pln: float = 0.0,
    source: str = "legacy",
    conn: Optional[sqlite3.Connection] = None,
    okx_client_factory=None,
) -> dict:
    """Futures order (long/short, dźwignia x10 isolated) na X-Perps OKX
    demo dla portfela kind='game'+okx+trading (#72, decyzja usera 2026-07-21).
    Market by default; pass `limit_price` for a LIMIT order (#198,
    2026-08-07 — crypto-dashboard's agent-proposed strategies need a real
    entry price, not immediate market fill).

    symbol: symbol bazowy (np. 'BTC', 'ETH') — instId resolved dynamicznie
    (patrz _resolve_futures_inst_id), NIE pełny instId z sufiksem daty.
    side: BUY otwiera/zwiększa long, SELL otwiera/zwiększa short (konto w
    net_mode — brak posSide, jedna pozycja netto per instrument).
    qty: jawna ilość kontraktów zachowuje legacy limit 100 USDC margin.
    ``None`` uruchamia automatyczny sizing wejścia: 0.25% equity ryzyka na
    SL, 10% equity notionalu i ``max_position_pct`` mandatu.
    take_profit_price/stop_loss_price: opcjonalne, dołączone do zlecenia jako
    attachAlgoOrds (market TP/SL, tpOrdPx/slOrdPx='-1') — jedno zlecenie
    OCO wykonuje się automatycznie na OKX przy przekroczeniu progu, bez
    potrzeby monitorowania przez agenta między uruchomieniami cron.
    limit_price: gdy podane, `ord_type='limit'` z `px=limit_price` zamiast
    market. Zlecenie limit może NIE wypełnić się natychmiast (czeka na
    rynku aż cena dojdzie do poziomu) — w przeciwieństwie do market,
    _get_order_until_terminal poniżej NIE czeka na fill dla limit orderów
    (user decision 2026-08-07: złóż i potwierdź PRZYJĘCIE, nie wypełnienie;
    stan sprawdza się później przez /api/okx_position, #190/#191). `state`
    w zwróconym wyniku będzie 'live' (oczekujące), nie 'filled', i to jest
    poprawny/oczekiwany wynik dla limit, nie błąd.

    Brak lokalnego ledgera (decyzja usera 2026-07-22): portfel trading+okx
    opiera się WYŁĄCZNIE na wartości konta zsynchronizowanej z OKX — żadna
    transakcja/pozycja nie jest zapisywana do bazy. Po złożeniu zlecenia
    funkcja woła sync_trading_okx_portfolio (odświeża total_value/cash z
    salda USDC OKX) i zwraca surowe dane zlecenia (nie rekord z tabeli
    transactions, bo taki rekord nie istnieje).

    KONSEKWENCJE świadomie zaakceptowane: (1) brak idempotency_key na
    poziomie bazy — retry crona (np. po timeoucie) MOŻE złożyć drugie
    zlecenie na OKX, bo nie ma z czym porównać poprzedniego wywołania;
    (2) dla automatycznego wejścia ``max_position_pct`` jest sprawdzany wobec
    dostępnego equity USDC; pozostałe historyczne limity ledgerowe nie mają
    wiarygodnego zastosowania do stanu futures OKX.
    """
    from services.game import ValidationError, NotFoundError, get_portfolio_row

    base_symbol = (symbol or "").upper().strip()
    side = (side or "").upper().strip()

    if okx_client_factory is None:
        okx_client_factory = OkxClient

    own = conn is None
    conn = conn or get_conn()
    try:
        pf = get_portfolio_row(portfolio_id, conn)
        if pf.get("archived"):
            raise NotFoundError(f"portfolio {portfolio_id} nie jest aktywne")

        if base_symbol not in ALLOWED_OKX_FUTURES_BASES:
            raise ValidationError(
                f"symbol {base_symbol} nie jest dozwolony dla futures demo OKX "
                f"(ALLOWED_OKX_FUTURES_BASES={sorted(ALLOWED_OKX_FUTURES_BASES)})"
            )
        if side not in ("BUY", "SELL"):
            raise ValidationError(f"side musi być BUY/SELL, otrzymano: {side}")
        alias = pf.get("exchange_credential_alias")
        client = okx_client_factory(alias, simulated_trading=True)

        try:
            instrument = _resolve_futures_instrument(base_symbol, client)
        except ValueError as exc:
            raise ValidationError(f"nie udało się rozwiązać instrumentu futures dla {base_symbol}: {exc}") from exc
        inst_id = instrument["instId"]
        try:
            ticker_payload = client.get_ticker(inst_id)
        except OkxError as exc:
            raise ValidationError(
                f"nie udało się pobrać ceny rynkowej OKX dla {inst_id}: {exc}"
            ) from exc
        last_price = _extract_ticker_last_price(ticker_payload)
        if last_price is None:
            raise ValidationError(f"odpowiedź OKX ticker dla {inst_id} nie zawiera ceny 'last'")

        limit_px_dec = _positive_decimal(limit_price, "limit_price") if limit_price is not None else None
        pricing_reference = limit_px_dec if limit_px_dec is not None else last_price
        auto_sized_entry = qty is None
        if auto_sized_entry:
            try:
                sizing = calculate_okx_futures_entry_size(
                    available_usdc_equity=_available_usdc_equity(client.get_balance("USDC")),
                    entry_price=pricing_reference,
                    stop_loss_price=stop_loss_price,
                    side=side,
                    instrument=instrument,
                    max_position_pct=pf.get("strategy_profile", {}).get("max_position_pct"),
                )
                qty_contracts = sizing["qty"]
            except (OkxError, ValueError) as exc:
                raise ValidationError(f"automatyczny sizing futures odrzucony: {exc}") from exc
        else:
            try:
                qty_contracts = Decimal(str(qty))
            except (InvalidOperation, TypeError, ValueError) as exc:
                raise ValidationError(f"qty musi być poprawną liczbą kontraktów: {qty!r}") from exc
            if not qty_contracts.is_finite() or qty_contracts <= 0:
                raise ValidationError("qty musi być dodatnie")
            if qty_contracts < instrument["minSz"]:
                raise ValidationError(
                    f"qty={qty} jest mniejsze niż minSz={instrument['minSz']} dla {inst_id}"
                )
            if qty_contracts % instrument["lotSz"] != 0:
                raise ValidationError(
                    f"qty={qty} nie jest wielokrotnością lotSz={instrument['lotSz']} dla {inst_id}"
                )

        # Legacy limit dla jawnego/manualnego qty jest liczony jako MARGIN
        # (notional/leverage). Automatyczny sizing ma osobny cel 10% equity
        # jako notional (około 1% marginu przy x10). Dla
        # limit order liczony z limit_price (#198) — cena po której zlecenie
        # faktycznie się wypełni, jeśli w ogóle, nie z bieżącej ceny rynkowej
        # (last_price), która może być daleko od poziomu wejścia.
        notional_usdc = pricing_reference * qty_contracts * instrument["ctVal"]
        estimated_margin_usdc = notional_usdc / Decimal(str(FUTURES_LEVERAGE))
        if not auto_sized_entry and estimated_margin_usdc > Decimal(str(MAX_FUTURES_MARGIN_USDC)):
            raise ValidationError(
                f"zlecenie {inst_id} {side} qty={_decimal_to_okx_size(qty_contracts)} (ctVal={instrument['ctVal']} "
                f"{instrument['ctValCcy']}) przekracza limit "
                f"{MAX_FUTURES_MARGIN_USDC} USDC margin/pozycję (szacowany margin "
                f"{estimated_margin_usdc:.2f} USDC = notional {notional_usdc:.2f} / "
                f"lever {FUTURES_LEVERAGE})"
            )

        executed_at = dt or datetime.now().isoformat(timespec="seconds")

        try:
            client.set_leverage(inst_id, str(FUTURES_LEVERAGE), FUTURES_MARGIN_MODE)
        except OkxError as exc:
            raise ValidationError(
                f"nie udało się ustawić dźwigni {FUTURES_LEVERAGE}x dla {inst_id}: {exc}"
            ) from exc

        okx_side = "buy" if side == "BUY" else "sell"
        extra_fields: dict[str, Any] = {}
        if take_profit_price is not None or stop_loss_price is not None:
            attach: dict[str, Any] = {}
            if take_profit_price is not None:
                attach["tpTriggerPx"] = str(take_profit_price)
                attach["tpOrdPx"] = "-1"
            if stop_loss_price is not None:
                attach["slTriggerPx"] = str(stop_loss_price)
                attach["slOrdPx"] = "-1"
            extra_fields["attachAlgoOrds"] = [attach]

        is_limit = limit_px_dec is not None
        if is_limit:
            extra_fields["px"] = _decimal_to_okx_size(limit_px_dec)

        try:
            order_payload = client.place_order(
                inst_id=inst_id,
                td_mode=FUTURES_MARGIN_MODE,
                side=okx_side,
                ord_type="limit" if is_limit else "market",
                sz=_decimal_to_okx_size(qty_contracts),
                **extra_fields,
            )
        except OkxError as exc:
            logger.warning(
                "execute_okx_futures_order: OKX odrzucił zlecenie portfolio_id=%s "
                "inst_id=%s side=%s: %s",
                portfolio_id, inst_id, side, exc,
            )
            raise ValidationError(f"OKX odrzucił zlecenie futures {inst_id} {side}: {exc}") from exc

        ord_id, s_code, s_msg = _extract_place_order_result(order_payload)
        if not ord_id or (s_code is not None and s_code not in ("0", "1")):
            raise ValidationError(
                f"OKX place_order (futures) nie zwrócił poprawnego ordId dla {inst_id} "
                f"(sCode={s_code}, sMsg={s_msg})"
            )

        if is_limit:
            # #198: a limit order may sit on the book for hours/days — do NOT
            # wait for a fill like market does below. Confirm ACCEPTANCE only
            # (state should be 'live'); actual fill is checked later via
            # /api/okx_position (#190/#191), not by this call blocking on it.
            try:
                order_detail = client.get_order(inst_id, ord_id)
            except OkxError as exc:
                raise ValidationError(
                    f"zlecenie limit {ord_id} złożone na OKX, ale nie udało się pobrać "
                    f"potwierdzenia przyjęcia: {exc}"
                ) from exc
            avg_px, fill_sz, state = _extract_order_fill(order_detail)
            fill_price = avg_px if avg_px is not None else limit_px_dec
            fill_qty_contracts = float(fill_sz) if fill_sz is not None else 0.0
        else:
            try:
                order_detail = _get_order_until_terminal(client, inst_id, ord_id)
            except OkxError as exc:
                raise ValidationError(
                    f"zlecenie {ord_id} złożone na OKX, ale nie udało się pobrać "
                    f"potwierdzenia wykonania: {exc}"
                ) from exc
            avg_px, fill_sz, state = _extract_order_fill(order_detail)
            fill_price = avg_px if avg_px is not None else last_price
            fill_qty_contracts = float(fill_sz) if fill_sz is not None else float(qty_contracts)

        # Brak lokalnego ledgera (patrz docstring funkcji) — odświeżamy total_value
        # z realnego salda OKX zamiast zapisywać transakcję. Błąd sync NIE cofa
        # zlecenia (już wykonane na giełdzie) — tylko loguje ostrzeżenie, kolejny
        # krok crona (sync_krypto_portfolio_value.py) i tak spróbuje ponownie.
        from services.okx_sync import sync_trading_okx_portfolio
        sync_result = sync_trading_okx_portfolio(portfolio_id, conn=conn, okx_client_factory=okx_client_factory)
        if not sync_result.get("ok"):
            logger.warning(
                "execute_okx_futures_order: zlecenie %s wykonane, ale sync total_value nieudany: %s",
                ord_id, sync_result.get("error"),
            )

        return {
            "ok": True,
            "portfolio_id": portfolio_id,
            "symbol": base_symbol,
            "side": side,
            "inst_id": inst_id,
            "order_id": ord_id,
            "order_state": state,
            "qty_requested": float(qty_contracts),
            "qty_filled": fill_qty_contracts,
            "fill_price": float(fill_price),
            "limit_price": limit_price,
            "reason": reason,
            "take_profit_price": take_profit_price,
            "stop_loss_price": stop_loss_price,
            "executed_at": executed_at,
            "total_value_pln": sync_result.get("total_value_pln"),
        }
    finally:
        if own:
            conn.close()
