"""#67: synchronizacja portfeli real+okx (read_only) z OKX.
#87: rozszerzone o portfele game+okx+trading (futures, np. Claude-krypto) —
lokalny ledger transactions ma znany bug (qty błędnie skalowane przy zapisie,
brak synchronizacji zamknięć SL/TP), więc dla tych portfeli total_value/cash
też pochodzą wyłącznie z OKX, tak jak dla real+okx.

Zakres:
- sync_real_okx_portfolio(portfolio_id): dla portfela kind='real', exchange='okx',
  execution_mode='read_only' — pobiera saldo (OkxClient.get_balance) przez
  OkxClient(alias=exchange_credential_alias, simulated_trading=False), przelicza
  wartość portfela do PLN (kurs USDT/PLN ~ USDPLN z services.pricing.load_fx —
  to jedyny kurs FX dostępny dziś w pricing.py; OKX wycenia saldo w USD-ekwiwalencie
  w polu totalEq), zapisuje snapshot dnia + last_synced_at + sync_status.
- sync_trading_okx_portfolio(portfolio_id): analogicznie dla kind='game',
  exchange='okx', execution_mode='trading' — różnica: simulated_trading=True
  (demo trading). Zakres świadomie ograniczony do total_value/cash (wartość
  konta) — NIE mapuje szczegółów otwartych pozycji do UI, to osobny temat.
- Błąd OKX (wyjątki domenowe z services.okx_client) -> sync_status='error',
  NIE nadpisuje poprzedniego dobrego snapshotu, nie crashuje wywołującego batcha.
- Nie loguje/nie wstawia do wyjątków żadnego sekretu — tylko komunikaty domenowe
  zwrócone przez OkxClient (patrz services/okx_client.py — ta sama zasada z #66).

Ta ścieżka jest CAŁKOWICIE osobna od load_real_accounts()/pricing.py (legacy,
czyta lokalne pliki Stooq dla kont maklerskich niepowiązanych z tabelą
`portfolios`) — real+okx i game+okx+trading nie ufają lokalnemu ledgerowi
transakcji/cash_entries dla total_value/cash, źródłem prawdy jest wyłącznie
odpowiedź OKX (execute_trade nadal zapisuje transactions dla audytu/reason,
ale wycena portfela je ignoruje).
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import date as date_cls, datetime
from typing import Optional

from services.db import get_conn
from services.game import ValidationError, get_portfolio_row
from services.okx_client import OkxClient, OkxError
from services.pricing import load_fx

logger = logging.getLogger(__name__)

# Próg świeżości danych (#67 AC): powyżej tej liczby minut od last_synced_at
# dane uznajemy za "stale". Rekomendacja PM: 15 min, na razie stała — bez UI/API
# do konfiguracji (poza zakresem #67, patrz #70).
STALE_AFTER_MINUTES = 15


def _total_eq_usd(balance_payload: dict) -> Optional[float]:
    """Wyciąga totalEq (USD-ekwiwalent salda) z odpowiedzi OKX get_balance.

    Struktura: {"code": "0", "data": [{"totalEq": "123.45", "details": [...]}]}.
    None gdy struktura nieoczekiwana (traktowane jako błąd syncu wyżej).
    """
    data = balance_payload.get("data") if isinstance(balance_payload, dict) else None
    if not isinstance(data, list) or not data:
        return None
    first = data[0]
    if not isinstance(first, dict):
        return None
    raw = first.get("totalEq")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _usdc_balance_usd(balance_payload: dict) -> Optional[float]:
    """#87: wyciąga saldo USDC (eq = availBal+frozenBal) z details get_balance —
    dla portfeli trading+okx (futures X-Perps rozliczane w USDC, tdMode='isolated',
    ccy='USDC' na każdym zleceniu) totalEq CAŁEGO konta demo jest bezużyteczny:
    konto demo OKX ma domyślny "starter pack" w wielu walutach (100k USD, 1 BTC,
    50k XRP, 10 ETH, ...) niezwiązany z grą — totalEq dałby wartość w setkach
    tysięcy PLN, kompletnie oderwaną od portfela. USDC to jedyna waluta, w
    której nasza gra faktycznie handluje (margin, PnL, fee wszystkie w USDC).

    Zwraca eq (availBal+frozenBal, uwzględnia zablokowany margin w otwartych
    pozycjach) w USD-approx (USDC≈USD 1:1) lub None gdy brak wpisu USDC.
    """
    data = balance_payload.get("data") if isinstance(balance_payload, dict) else None
    if not isinstance(data, list) or not data:
        return None
    first = data[0]
    if not isinstance(first, dict):
        return None
    details = first.get("details")
    if not isinstance(details, list):
        return None
    for entry in details:
        if isinstance(entry, dict) and entry.get("ccy") == "USDC":
            raw = entry.get("eq")
            if raw is None:
                return None
            try:
                return float(raw)
            except (TypeError, ValueError):
                return None
    return None


def _extract_positions(positions_payload: dict) -> list[dict]:
    data = positions_payload.get("data") if isinstance(positions_payload, dict) else None
    if not isinstance(data, list):
        return []
    out = []
    for pos in data:
        if not isinstance(pos, dict):
            continue
        # Zachowujemy wyłącznie pola read-only potrzebne do projekcji w UI.
        # Nie mapujemy ich do transactions ani portfolio_positions.
        out.append({
            "instId": pos.get("instId"),
            "pos": pos.get("pos"),
            "posSide": pos.get("posSide"),
            "avgPx": pos.get("avgPx"),
            "markPx": pos.get("markPx"),
            "notionalUsd": pos.get("notionalUsd"),
            "upl": pos.get("upl"),
            "uplRatio": pos.get("uplRatio"),
            "lever": pos.get("lever"),
            "mgnMode": pos.get("mgnMode"),
            "uTime": pos.get("uTime"),
        })
    return out


def _mark_sync_status(
    portfolio_id: int, status: str, conn: sqlite3.Connection, *, synced_at: Optional[str] = None
) -> None:
    """Zapisuje sync_status i (dla 'ok') last_synced_at. Nie commituje — wołający decyduje."""
    now = synced_at or datetime.now().isoformat(timespec="seconds")
    if status == "ok":
        conn.execute(
            "UPDATE portfolios SET sync_status=?, last_synced_at=? WHERE id=?",
            (status, now, portfolio_id),
        )
    else:
        # last_synced_at NIE jest dotykane przy błędzie — zachowujemy czas
        # ostatniej udanej synchronizacji, żeby "stale" liczyło się poprawnie.
        conn.execute(
            "UPDATE portfolios SET sync_status=? WHERE id=?",
            (status, portfolio_id),
        )


def _sync_okx_portfolio_totals(
    portfolio_id: int,
    *,
    expected_kind: str,
    expected_execution_mode: str,
    simulated_trading: bool,
    caller_name: str,
    balance_field: str = "total_eq",
    conn: Optional[sqlite3.Connection] = None,
    snap_date: Optional[str] = None,
    okx_client_factory=None,
) -> dict:
    """Rdzeń współdzielony przez sync_real_okx_portfolio i sync_trading_okx_portfolio
    (#87) — różnice: expected_kind/execution_mode/simulated_trading/balance_field.

    balance_field: "total_eq" (domyślne, real+okx spot — totalEq całego konta)
    albo "usdc" (trading+okx futures — saldo USDC, bo totalEq miesza demo-starter
    kapitał wielu walut niezwiązany z grą, patrz _usdc_balance_usd).

    Zapisuje WYŁĄCZNIE total_value/cash (wartość konta) — świadomie NIE mapuje
    szczegółów otwartych pozycji (positions_json = [], zgodnie z #67 dla real+okx;
    dla trading+okx pozycje nadal pokazywane są z lokalnego ledgera przez
    get_portfolio_valuation, znany bug #87 qty/sync SL-TP nie jest tu naprawiany).
    """
    if okx_client_factory is None:
        okx_client_factory = OkxClient

    own = conn is None
    conn = conn or get_conn()
    try:
        pf = get_portfolio_row(portfolio_id, conn)
        if pf.get("kind") != expected_kind or pf.get("exchange") != "okx" or pf.get("execution_mode") != expected_execution_mode:
            raise ValidationError(
                f"portfolio {portfolio_id} nie jest portfelem "
                f"kind={expected_kind}+exchange=okx+execution_mode={expected_execution_mode} — "
                f"{caller_name} nie ma zastosowania"
            )
        alias = pf.get("exchange_credential_alias")
        try:
            client = okx_client_factory(alias, simulated_trading=simulated_trading)
            balance_payload = client.get_balance()
            positions_payload = client.get_positions()
        except OkxError as exc:
            # Sekrety nigdy nie trafiają tu ani do logu — OkxError już niesie
            # tylko kod/komunikat OKX (patrz services/okx_client.py::_map_okx_error).
            logger.warning(
                "%s: błąd OKX dla portfolio_id=%s alias=%s: %s",
                caller_name, portfolio_id, alias, exc,
            )
            _mark_sync_status(portfolio_id, "error", conn)
            conn.commit()
            return {
                "ok": False,
                "portfolio_id": portfolio_id,
                "sync_status": "error",
                "error": str(exc),
            }

        if balance_field == "usdc":
            total_eq_usd = _usdc_balance_usd(balance_payload)
            missing_field_error = "odpowiedź OKX get_balance nie zawiera pozycji USDC w details"
        else:
            total_eq_usd = _total_eq_usd(balance_payload)
            missing_field_error = "odpowiedź OKX get_balance nie zawiera totalEq"

        if total_eq_usd is None:
            logger.warning(
                "%s: nieoczekiwana odpowiedź OKX (brak %s) portfolio_id=%s",
                caller_name, balance_field, portfolio_id,
            )
            _mark_sync_status(portfolio_id, "error", conn)
            conn.commit()
            return {
                "ok": False,
                "portfolio_id": portfolio_id,
                "sync_status": "error",
                "error": missing_field_error,
            }

        fx_rate, _fx_date = load_fx()
        total_value_pln = round(total_eq_usd * fx_rate, 2)
        positions = _extract_positions(positions_payload)
        # Uproszczenie świadome (#67 — brak w scope rozbicia cash vs pozycje):
        # snapshots.cash_pln = total_value_pln (cały portfel jako jedna liczba),
        # bo OKX get_balance().data[0].totalEq nie rozróżnia jednoznacznie
        # stablecoin/cash od wycenionych pozycji bez dodatkowego mapowania
        # per-ccy z pola details, którego kontrakt nie był ustalony w #66.
        # Podział cash/positions to temat na #69/#70 jeśli UI/ranking go potrzebuje.

        snap_date = snap_date or date_cls.today().isoformat()
        conn.execute(
            "INSERT INTO snapshots (portfolio_id, date, total_value_pln, cash_pln, positions_json) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(portfolio_id, date) DO UPDATE SET total_value_pln=excluded.total_value_pln, "
            "cash_pln=excluded.cash_pln, positions_json=excluded.positions_json",
            (portfolio_id, snap_date, total_value_pln, total_value_pln, json.dumps(positions, ensure_ascii=False)),
        )
        _mark_sync_status(portfolio_id, "ok", conn)
        conn.commit()
        return {
            "ok": True,
            "portfolio_id": portfolio_id,
            "sync_status": "ok",
            "total_value_pln": total_value_pln,
            "date": snap_date,
        }
    except BaseException:
        if own:
            conn.rollback()
        raise
    finally:
        if own:
            conn.close()


def sync_real_okx_portfolio(
    portfolio_id: int,
    conn: Optional[sqlite3.Connection] = None,
    snap_date: Optional[str] = None,
    okx_client_factory=None,
) -> dict:
    """Synchronizuje pojedynczy portfel real+okx (read_only): sync_status +
    last_synced_at + snapshot (total_value/cash z OKX get_balance().totalEq).

    okx_client_factory: injection point dla testów (domyślnie None -> rozwiązywany
    dynamicznie na moduł-poziomowy OkxClient PRZY WYWOŁANIU, nie przy definicji
    funkcji — tak żeby monkeypatch services.okx_sync.OkxClient w testach/
    take_snapshot_all też działał, a nie tylko jawne przekazanie parametru).
    Testy podają fake/mock zwracający zamockowany klient — zero realnych wywołań HTTP.

    Zwraca dict {"ok": bool, "sync_status": "ok"|"error", "error": Optional[str], ...}.
    Błąd OKX NIGDY nie propaguje jako wyjątek do wywołującego batcha (take_snapshot_all) —
    jest złapany tu i zwrócony jako wynik z ok=False, żeby jeden zepsuty portfel nie
    wywalił reszty.
    """
    return _sync_okx_portfolio_totals(
        portfolio_id,
        expected_kind="real",
        expected_execution_mode="read_only",
        simulated_trading=False,
        caller_name="sync_real_okx_portfolio",
        conn=conn,
        snap_date=snap_date,
        okx_client_factory=okx_client_factory,
    )


def sync_trading_okx_portfolio(
    portfolio_id: int,
    conn: Optional[sqlite3.Connection] = None,
    snap_date: Optional[str] = None,
    okx_client_factory=None,
) -> dict:
    """#87: synchronizuje pojedynczy portfel game+okx+trading (np. Claude-krypto,
    futures demo) — sync_status + last_synced_at + snapshot z OKX get_balance().

    Motywacja: lokalny ledger transactions dla execute_okx_futures_order ma znany
    bug (qty błędnie skalowane 10× przy zapisie, brak synchronizacji zamknięć
    SL/TP z powrotem do transactions) — total_value/cash liczone z ledgera są
    niewiarygodne. Ta funkcja omija ledger całkowicie, tak jak sync_real_okx_portfolio
    robi to dla portfeli read-only — źródłem prawdy jest wyłącznie odpowiedź OKX.

    balance_field="usdc" (NIE totalEq całego konta) — konto demo OKX ma domyślny
    "starter pack" w wielu walutach (100k USD, 1 BTC, 50k XRP, ...) niezwiązany
    z grą; USDC to jedyna waluta w której X-Perps futures faktycznie się rozlicza.
    Konsekwencja świadomie zaakceptowana (user, 2026-07-21): total_value_pln =
    saldo USDC×kurs, BEZ odniesienia do baseline 20000 PLN startowego kapitału —
    portfel przestaje śledzić zwrot względem wpłaty, pokazuje wprost stan konta
    (get_portfolio_valuation w game.py zeruje resultVsDeposits/unrealizedPnlPln/
    portfolioReturnPct/returnPct dla tych portfeli, żeby nie pokazywać matematyki
    porównującej niepowiązane liczby).

    simulated_trading=True (demo trading) w przeciwieństwie do sync_real_okx_portfolio.
    """
    return _sync_okx_portfolio_totals(
        portfolio_id,
        expected_kind="game",
        expected_execution_mode="trading",
        simulated_trading=True,
        caller_name="sync_trading_okx_portfolio",
        balance_field="usdc",
        conn=conn,
        snap_date=snap_date,
        okx_client_factory=okx_client_factory,
    )


def is_stale(last_synced_at: Optional[str], *, now: Optional[datetime] = None, threshold_minutes: int = STALE_AFTER_MINUTES) -> bool:
    """True gdy last_synced_at brak lub starsze niż threshold_minutes."""
    if not last_synced_at:
        return True
    try:
        synced = datetime.fromisoformat(last_synced_at)
    except ValueError:
        return True
    now = now or datetime.now()
    age_minutes = (now - synced).total_seconds() / 60.0
    return age_minutes > threshold_minutes
