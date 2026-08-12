"""Loader cen EOD z plików Stooq + kurs FX.

Przeniesione/zrefaktoryzowane z `backend/main.py` v1 — logika wyszukiwania
plików cenowych i proxy SMH.L pozostaje identyczna (zweryfikowana).
Rozszerzone (#17): archiwum Stooq, close_as_of, FX historyczne.
"""
import csv
import json
from bisect import bisect_right
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Optional

from services.db import BACKEND_DIR, BOT_ROOT, GRA_DIR, PRICE_FOLDERS

SMH_US_PROXY_BASELINE = 590.77

# #86: symbole futures krypto OKX (agent-krypto, portfolio_id=17) — long/short
# jako dwa niezależne symbole w ledgerze (patrz .claude/skills/agent-krypto/SKILL.md).
# Kwotowane w USD (nie mają sufiksu .US/.L jak reszta NAME_TO_TICKER, więc
# currency_for_symbol musi je rozpoznać jawnie).
CRYPTO_FUTURES_BASES = frozenset({"BTC", "ETH"})
CRYPTO_MARKET_DATA_DIR = BACKEND_DIR / "data" / "crypto_market"


def _crypto_futures_base(symbol: str) -> Optional[str]:
    """'BTC'/'BTC-SHORT' -> 'BTC' jeśli to symbol futures krypto obsługiwany
    przez agenta-krypto, inaczej None (żeby nie kolidować z NAME_TO_TICKER/Stooq)."""
    s = (symbol or "").upper().strip()
    base = s[: -len("-SHORT")] if s.endswith("-SHORT") else s
    return base if base in CRYPTO_FUTURES_BASES else None

USDPLN_PATH = (
    BOT_ROOT / "stocks" / "stooq" / "world" / "data" / "daily" / "world" / "currencies" / "major" / "usdpln.txt"
)
GBPPLN_PATH = (
    BOT_ROOT / "stocks" / "stooq" / "world" / "data" / "daily" / "world" / "currencies" / "major" / "gbppln.txt"
)

# Symbole w historii transakcji (nazwy spółek z eMakler/BOSSA) -> symbol pliku cenowego Stooq
NAME_TO_TICKER = {
    "COLUMBUS": "CLC.WA",
    "TEXT": "TXT.WA",
    "SYGNITY": "SGN.WA",
    "SYNEKTIK": "SNT",
    "BIGCHEESE": "BCS.WA",
    "SPACEX": "SPCX.US",
    "SMH LN ETF": "SMH.L",
    "RAFAKO": None,  # notowane szczątkowo - brak pliku cenowego, wycena pomijana
    "PFIZER INC": "PFE.US",
    "Realty Income Corporation": "O.US",
    "Socket Mobile Inc": "SCKT.US",
    "Cameco Corp.": "CCJ.US",
    "ASBIS": "ASB.WA",
    "EUROCASH": "EUR.WA",
    "BROADCOM LTD": "AVGO.US",
    "KGHM": "KGH",
    # #17 — seed reali / archiwum Stooq
    "PEPCO": "PCE",
    "XTB": "XTB",
    "ZABKA": "ZAB",
    "JSW": "JSW",
    "COGNOR": "COG",
    "PURE": "PUR",
    "DIAG": "DIG",
    "SYN2BIO": "S2B",
    "SENTINELONE": "S.US",
}

USD_TICKER_SUFFIXES = (".US",)
GBP_TICKER_SUFFIXES = (".L", ".UK")

_STOOQ_INDEX: Optional[dict[str, Path]] = None


def _stooq_filename_index() -> dict[str, Path]:
    """Lazy index basename → path w archiwum Stooq (bez indicators)."""
    global _STOOQ_INDEX
    if _STOOQ_INDEX is not None:
        return _STOOQ_INDEX
    roots = [
        BOT_ROOT / "stocks" / "stooq" / "pl" / "data" / "daily" / "pl" / "wse stocks",
        BOT_ROOT / "stocks" / "stooq" / "us" / "data" / "daily" / "us",
    ]
    idx: dict[str, Path] = {}
    for root in roots:
        if not root.exists():
            continue
        for p in root.rglob("*.txt"):
            if "indicators" in p.parts:
                continue
            key = p.name.lower()
            if key not in idx:
                idx[key] = p
    _STOOQ_INDEX = idx
    return idx


def clear_pricing_caches() -> None:
    """Reset cache (testy / po podmianie PRICE_FOLDERS)."""
    global _STOOQ_INDEX
    _STOOQ_INDEX = None
    load_price_series.cache_clear()
    _load_fx_series.cache_clear()
    _load_gbp_fx_series.cache_clear()


def load_fx() -> tuple[float, str]:
    fx_path = GRA_DIR / "fx.json"
    if not fx_path.exists():
        return 3.77505, "brak-danych"
    fx = json.loads(fx_path.read_text())
    return fx["USDPLN"]["rate"], fx["USDPLN"]["date"]


def load_fx_gbp() -> tuple[Optional[float], Optional[str]]:
    """GBPPLN bieżący (ostatni dostępny wiersz archiwum Stooq). None gdy brak danych —
    NIE zgadujemy kursu (#4: instrumenty .L/.UK bez kursu muszą być jawnie odrzucane,
    nie traktowane jako PLN 1:1)."""
    dates, rates = _load_gbp_fx_series()
    if not dates:
        return None, None
    return rates[-1], dates[-1]


def load_fx_gbp_as_of(as_of: str) -> tuple[Optional[float], Optional[str]]:
    """GBPPLN z dnia ≤ as_of. None gdy brak danych do tej daty."""
    dates, rates = _load_gbp_fx_series()
    if not dates:
        return None, None
    i = bisect_right(dates, as_of) - 1
    if i < 0:
        return None, None
    return rates[i], dates[i]


def _normalize_stooq_date(raw: str) -> str:
    if len(raw) == 8 and raw.isdigit():
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    return raw


def find_price_file(symbol: str) -> Optional[Path]:
    base = symbol.lower()
    candidates = [base]
    if "." in base:
        prefix, suffix = base.split(".", 1)
        if suffix == "wa":
            candidates.append(prefix)
    for folder in PRICE_FOLDERS:
        for cand in candidates:
            f = folder / f"{cand}.txt"
            if f.exists():
                return f
    idx = _stooq_filename_index()
    for cand in candidates:
        hit = idx.get(f"{cand}.txt")
        if hit is not None:
            return hit
    return None


def last_close(path: Path) -> tuple[Optional[float], Optional[str]]:
    with open(path) as f:
        r = csv.reader(f)
        next(r, None)
        last = None
        for row in r:
            last = row
        if last is None:
            return None, None
        return float(last[7]), _normalize_stooq_date(last[2])


_NAME_TO_TICKER_UPPER = {k.upper(): v for k, v in NAME_TO_TICKER.items()}


def resolve_ticker(name_or_symbol: str) -> Optional[str]:
    """Mapuje nazwę spółki z historii transakcji na symbol pliku cenowego.

    Lookup case-insensitive (klucze NAME_TO_TICKER porownywane .upper()) —
    dane z historia.db/transactions bywaja zapisane inna wielkoscia liter
    niz klucz w dict (np. "CAMECO CORP." vs "Cameco Corp."), a to wciaz ta
    sama spolka."""
    if name_or_symbol in NAME_TO_TICKER:
        return NAME_TO_TICKER[name_or_symbol]
    upper = (name_or_symbol or "").upper()
    if upper in _NAME_TO_TICKER_UPPER:
        return _NAME_TO_TICKER_UPPER[upper]
    return name_or_symbol  # zalozenie: to juz jest poprawny symbol


def currency_for_symbol(name_or_symbol: str) -> str:
    """Waluta notowania (native) na podstawie mapowania Stooq / sufiksu tickera."""
    if _crypto_futures_base(name_or_symbol) is not None:
        return "USD"
    ticker = resolve_ticker(name_or_symbol)
    if ticker is None:
        return "PLN"
    t = ticker.upper()
    if t.endswith(USD_TICKER_SUFFIXES):
        return "USD"
    if t.endswith(GBP_TICKER_SUFFIXES):
        return "GBP"
    return "PLN"


def _crypto_futures_last_close(symbol: str) -> tuple[Optional[float], Optional[str]]:
    """Ostatni close z data/crypto_market/{base}_latest.json (#78, świeże co
    cykl crona agenta-krypto — patrz scripts/fetch_crypto_market_data.py).

    Używa świecy 15m (najkrótszy dostępny interwał w danych #78) — bierze
    ostatni wiersz (najświeższy po odwróceniu kolejności OKX: candles są
    zwracane najnowsza-pierwsza, patrz analyze_crypto_market_data.py).
    Zwraca (close, iso_date) lub (None, None) gdy plik brak/niepełny."""
    base = _crypto_futures_base(symbol)
    if base is None:
        return None, None
    path = CRYPTO_MARKET_DATA_DIR / f"{base}_latest.json"
    if not path.exists():
        return None, None
    try:
        raw = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None, None

    section = raw.get("candles", {}).get("15m", {})
    if not section.get("ok"):
        return None, None
    rows = section.get("data", {}).get("data")
    if not isinstance(rows, list) or not rows:
        return None, None

    # OKX candle row: [ts, o, h, l, c, ...], najnowsza pierwsza (malejąco po czasie).
    newest = rows[0]
    try:
        close = float(newest[4])
        ts_ms = int(newest[0])
    except (IndexError, TypeError, ValueError):
        return None, None

    date = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    return close, date


def price_source_path(symbol: str) -> Optional[str]:
    """Sciezka pliku Stooq (wzgledem BOT_ROOT) uzyta przez price_for/close_as_of
    dla danego symbolu, albo None (brak pliku / krypto futures / proxy SMH bez
    zrodlowego SMH.US). Uzywana wylacznie do audytu (kolumna prices.source_path),
    nie wplywa na sama wycene."""
    if _crypto_futures_base(symbol) is not None:
        return None
    ticker = resolve_ticker(symbol)
    if ticker is None:
        return None
    path = find_price_file("SMH.US") if ticker in ("SMH.L", "SMH LN ETF") else find_price_file(ticker)
    if path is None:
        return None
    try:
        return str(path.relative_to(BOT_ROOT))
    except ValueError:
        return str(path)


def change_pct_1d(symbol: str, current_date: str, current_close_native: float) -> Optional[float]:
    """Zmiana % dzien-do-dnia: (current_close_native / poprzednia_sesja_close - 1) * 100.

    Ten sam wzor co day_change_pct w services/game.py::_compute_position_performance_metrics
    (celowo zduplikowany zamiast importu — pricing.py nie zalezy od game.py).
    None gdy brak poprzedniej sesji w serii (pierwszy dzien danych) lub proxy SMH."""
    if symbol in ("SMH.L", "SMH LN ETF"):
        return None
    dates, closes = load_price_series(symbol)
    if not dates:
        return None
    i = bisect_right(dates, current_date) - 1
    if i <= 0:
        return None
    prev_close = closes[i - 1]
    if not prev_close:
        return None
    return round((current_close_native / prev_close - 1) * 100, 2)


def price_for(symbol: str):
    """Zwraca (cena_lub_proxy_tuple, data) lub (None, None) gdy brak danych.

    Proxy: dla SMH.L zwraca ("PROXY_SMH", pct_change) zamiast ceny bezwzględnej.
    """
    crypto_close, crypto_date = _crypto_futures_last_close(symbol)
    if crypto_close is not None:
        return crypto_close, crypto_date

    ticker = resolve_ticker(symbol)
    if ticker is None:
        return None, None
    if ticker in ("SMH.L", "SMH LN ETF"):
        smh_us = find_price_file("SMH.US")
        if smh_us is None:
            return None, None
        close, date = last_close(smh_us)
        return ("PROXY_SMH", close / SMH_US_PROXY_BASELINE - 1), date
    path = find_price_file(ticker)
    if path is None:
        return None, None
    return last_close(path)


def is_usd_ticker(name_or_symbol: str) -> bool:
    """USD-quoted equity/ETF do liczenia usExposurePct (limit max_us_pct w mandacie).

    Krypto futures (BTC/ETH) są kwotowane w USD (currency_for_symbol="USD" dla
    poprawnego przeliczenia FX), ale NIE są US equity exposure — portfele
    krypto (agent-krypto) mają allowed_asset_types=["krypto"], nie max_us_pct,
    więc to rozróżnienie nie zmienia dziś zachowania, ale zapobiega przyszłemu
    zafałszowaniu usExposurePct gdyby ktoś dodał max_us_pct do takiego portfela.

    UWAGA (#86, ograniczenie znane): services/game.py::value_positions liczy
    us_value jako `is_usd_ticker(symbol) OR display_currency == "USD"` — drugi
    człon warunku i tak wliczy BTC/ETH do us_value, bo ich display_currency
    jest "USD". Naprawa tego wymagałaby zmiany w game.py (poza zakresem #86,
    bo dziś żaden portfel krypto nie ma max_us_pct w mandacie — brak realnego
    wpływu). Zostawione jako świadomie znane ograniczenie."""
    if _crypto_futures_base(name_or_symbol) is not None:
        return False
    return currency_for_symbol(name_or_symbol) == "USD"


@lru_cache(maxsize=256)
def load_price_series(symbol: str) -> tuple[tuple[str, ...], tuple[float, ...]]:
    """Pełna seria EOD (daty ISO rosnąco, close). Puste gdy brak pliku."""
    ticker = resolve_ticker(symbol)
    if ticker is None:
        return (), ()
    if ticker in ("SMH.L", "SMH LN ETF"):
        path = find_price_file("SMH.US")
    else:
        path = find_price_file(ticker)
    if path is None:
        return (), ()
    dates: list[str] = []
    closes: list[float] = []
    with open(path) as f:
        r = csv.reader(f)
        next(r, None)
        for row in r:
            if len(row) < 8:
                continue
            dates.append(_normalize_stooq_date(row[2]))
            closes.append(float(row[7]))
    return tuple(dates), tuple(closes)


def close_as_of(symbol: str, as_of: str):
    """Ostatni close ≤ as_of (YYYY-MM-DD). Proxy SMH jak price_for.

    Zwraca (cena_lub_proxy, data_ceny) lub (None, None).
    """
    ticker = resolve_ticker(symbol)
    if ticker is None:
        return None, None
    dates, closes = load_price_series(symbol)
    if not dates:
        return None, None
    i = bisect_right(dates, as_of) - 1
    if i < 0:
        return None, None
    close = closes[i]
    date = dates[i]
    if ticker in ("SMH.L", "SMH LN ETF"):
        return ("PROXY_SMH", close / SMH_US_PROXY_BASELINE - 1), date
    return close, date


@lru_cache(maxsize=1)
def _load_fx_series() -> tuple[tuple[str, ...], tuple[float, ...]]:
    if not USDPLN_PATH.exists():
        return (), ()
    dates: list[str] = []
    rates: list[float] = []
    with open(USDPLN_PATH) as f:
        r = csv.reader(f)
        next(r, None)
        for row in r:
            if len(row) < 8:
                continue
            dates.append(_normalize_stooq_date(row[2]))
            rates.append(float(row[7]))
    return tuple(dates), tuple(rates)


def load_fx_as_of(as_of: str) -> tuple[float, str]:
    """USDPLN z dnia ≤ as_of; fallback na load_fx()."""
    dates, rates = _load_fx_series()
    if dates:
        i = bisect_right(dates, as_of) - 1
        if i >= 0:
            return rates[i], dates[i]
    return load_fx()


@lru_cache(maxsize=1)
def _load_gbp_fx_series() -> tuple[tuple[str, ...], tuple[float, ...]]:
    if not GBPPLN_PATH.exists():
        return (), ()
    dates: list[str] = []
    rates: list[float] = []
    with open(GBPPLN_PATH) as f:
        r = csv.reader(f)
        next(r, None)
        for row in r:
            if len(row) < 8:
                continue
            dates.append(_normalize_stooq_date(row[2]))
            rates.append(float(row[7]))
    return tuple(dates), tuple(rates)


def fx_rate_for_currency(currency: str, as_of: Optional[str] = None) -> tuple[Optional[float], Optional[str]]:
    """Mnożnik native→PLN dla dowolnej obsługiwanej waluty (#4 — jawny provider,
    wspólny dla execute_trade i wyceny, zamiast rozrzuconych if/elif). None gdy brak
    kursu — wywołujący musi jawnie odrzucić operację, nie zgadywać 1:1."""
    c = (currency or "PLN").upper()
    if c == "PLN":
        return 1.0, None
    if c == "USD":
        return load_fx_as_of(as_of) if as_of else load_fx()
    if c == "GBP":
        return load_fx_gbp_as_of(as_of) if as_of else load_fx_gbp()
    return None, None


def get_price_history(symbol: str, days: int = 30) -> list[dict]:
    """Zwraca ostatnie `days` sesji EOD: [{date, close}, ...] (najstarsze pierwsze).

    Data w formacie YYYY-MM-DD. Pusty wynik gdy brak pliku cenowego.
    """
    if days <= 0:
        return []
    dates, closes = load_price_series(symbol)
    if not dates:
        return []
    n = min(days, len(dates))
    return [{"date": dates[i], "close": closes[i]} for i in range(len(dates) - n, len(dates))]
