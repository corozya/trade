#!/usr/bin/env python3
"""#79: krok 2/3 pipeline'u agenta krypto — przelicza surowe dane #78 na gotowy
snapshot analityczny (wskaźniki prekalkulowane) dla agenta decyzyjnego (#80).

Wejście: backend/data/crypto_market/{symbol}_latest.json (#78).
Wyjście: backend/data/crypto_market/{symbol}_analysis.json —
płaska struktura per symbol (price/indicators_15m/higher_tf_context/orderbook/
futures), format ustalony z konsultacji agenta-tradera (#80, 2026-07-21, patrz
komentarz na #80 dla pełnego przykładu JSON).

Wskaźniki na 15m (spec #79/#91): RSI14, EMA20, EMA50, ATR14,
Bollinger(20,2), VWAP, MACD(12,26,9), Stochastic RSI(14,14,3,3),
ADX(14) z DI+/DI-, relative volume(20) oraz OBV ze slope(20)
(rolling na dostępnym oknie świec). Reużywa `ta_stack.indicators.compute_core`
(RSI/EMA20/EMA50/ATR/BB już tam gotowe) + `ta_stack.zones._swing_sr` (swing
high/low). VWAP i EMA200 (kontekst 1h) dopisane lokalnie — brak w ta_stack.

Fibonacci pozostaje pominięte. OBV zostało dodane w #91 na jawne
zapotrzebowanie agenta jako pomocnicze potwierdzenie wolumenu.

Agent NIE commituje zmian w tym skrypcie (ustalenie z sesji 2026-07-21) —
edycje zostają jako uncommitted diff, user przegląda/commituje ręcznie.

Użycie:
    cd backend
    .venv/bin/python scripts/analyze_crypto_market_data.py

Odporność: brak/niepełne dane wejściowe dla danego symbolu (np. #78 failował)
nie wywalają całego skryptu — symbol jest pomijany z komunikatem [FAIL],
pozostałe symbole liczą się normalnie.
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Any, Optional

import pandas as pd

BACKEND_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = BACKEND_ROOT.parent
sys.path.insert(0, str(BACKEND_ROOT))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ta_stack.indicators import _ema, compute_core
from ta_stack.zones import _swing_sr

from services.okx_market import ALLOWED_OKX_FUTURES_BASES

DATA_DIR = BACKEND_ROOT / "data" / "crypto_market"

SYMBOLS: list[str] = sorted(ALLOWED_OKX_FUTURES_BASES)

# OKX candle row: [ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm] — pierwsza
# jest najnowsza (malejąco po czasie), odwracamy do rosnącego przed analizą.
_CANDLE_COLS = ["ts", "open", "high", "low", "close", "volume"]

# Poniżej tylu barów EMA50 jest zbyt zniekształcone efektem rozgrzewki
# (ewm adjust=False konwerguje powoli) — trend zwraca "unknown" zamiast
# fałszywie pewnego up/down.
_MIN_BARS_FOR_EMA50 = 50

_MACD_MIN_BARS = 26 + 9 - 1
_STOCH_RSI_MIN_BARS = 14 + 14 + 3 + 3 - 2
_ADX_MIN_BARS = 14 * 2
_VOLUME_PERIOD = 20


class AnalysisError(Exception):
    """Błąd domenowy analizy (brak/niepełne dane wejściowe)."""


def okx_candles_to_df(raw_candles: list[list[str]]) -> pd.DataFrame:
    """Konwertuje surowe świece OKX (najnowsza pierwsza) na DataFrame OHLCV
    posortowany rosnąco po czasie — format zgodny z ta_stack (open/high/low/close/volume)."""
    if not raw_candles:
        raise AnalysisError("brak świec do konwersji")
    rows = [row[: len(_CANDLE_COLS)] for row in raw_candles]
    df = pd.DataFrame(rows, columns=_CANDLE_COLS)
    df["ts"] = pd.to_datetime(df["ts"].astype("int64"), unit="ms", utc=True)
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.replace([float("inf"), float("-inf")], float("nan"))
    df = df.rename(columns={"ts": "date"}).dropna(subset=["open", "high", "low", "close"])
    df = df.sort_values("date").reset_index(drop=True)
    if df.empty:
        raise AnalysisError("brak kompletnych, skończonych świec OHLC")
    return df


def _rolling_vwap(df: pd.DataFrame) -> float:
    """VWAP rolling na całym dostępnym oknie świec (brak w ta_stack — spec #79
    wymaga VWAP jako punkt odniesienia ceny, sesyjny/rolling akceptowalne)."""
    valid = df["volume"].notna() & df["volume"].ge(0)
    typical = (df.loc[valid, "high"] + df.loc[valid, "low"] + df.loc[valid, "close"]) / 3.0
    volume = df.loc[valid, "volume"]
    cum_vol = volume.sum()
    if not cum_vol:
        return float(df["close"].iloc[-1])
    return float((typical * volume).sum() / cum_vol)


def _indicator_result(required_bars: int, **values: Optional[float]) -> dict[str, Any]:
    """Buduje bezpieczny kontrakt wskaźnika bez NaN/inf w JSON."""
    clean: dict[str, Optional[float]] = {}
    valid = True
    for key, value in values.items():
        if value is None or not math.isfinite(float(value)):
            clean[key] = None
            valid = False
        else:
            clean[key] = round(float(value), 6)
    return {
        "status": "ok" if valid else "insufficient_data",
        "required_bars": required_bars,
        **clean,
    }


def _macd(df: pd.DataFrame) -> dict[str, Any]:
    close = df["close"].astype(float)
    if len(close) < _MACD_MIN_BARS:
        return _indicator_result(_MACD_MIN_BARS, line=None, signal=None, histogram=None)
    line = close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()
    signal = line.ewm(span=9, adjust=False).mean()
    return _indicator_result(
        _MACD_MIN_BARS,
        line=line.iloc[-1],
        signal=signal.iloc[-1],
        histogram=(line - signal).iloc[-1],
    )


def _rsi_series(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.astype(float).diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = gain / loss.replace(0, float("nan"))
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return rsi.where(loss.ne(0), 100.0).where(gain.ne(0) | loss.ne(0), 50.0)


def _stoch_rsi(df: pd.DataFrame) -> dict[str, Any]:
    if len(df) < _STOCH_RSI_MIN_BARS:
        return _indicator_result(_STOCH_RSI_MIN_BARS, k=None, d=None)
    rsi = _rsi_series(df["close"], 14)
    low = rsi.rolling(14, min_periods=14).min()
    high = rsi.rolling(14, min_periods=14).max()
    spread = high - low
    raw = ((rsi - low) / spread.replace(0, float("nan")) * 100.0).where(spread.ne(0), 50.0)
    k = raw.rolling(3, min_periods=3).mean()
    d = k.rolling(3, min_periods=3).mean()
    return _indicator_result(_STOCH_RSI_MIN_BARS, k=k.iloc[-1], d=d.iloc[-1])


def _adx(df: pd.DataFrame, period: int = 14) -> dict[str, Any]:
    required = period * 2
    if len(df) < required:
        return _indicator_result(required, adx=None, plus_di=None, minus_di=None)

    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
    minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)
    true_range = pd.concat(
        [(high - low).abs(), (high - close.shift()).abs(), (low - close.shift()).abs()],
        axis=1,
    ).max(axis=1)
    atr = true_range.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    safe_atr = atr.replace(0, float("nan"))
    plus_di = (100.0 * plus_dm.ewm(alpha=1 / period, adjust=False).mean() / safe_atr).fillna(0.0)
    minus_di = (100.0 * minus_dm.ewm(alpha=1 / period, adjust=False).mean() / safe_atr).fillna(0.0)
    di_sum = plus_di + minus_di
    dx = (100.0 * (plus_di - minus_di).abs() / di_sum.replace(0, float("nan"))).fillna(0.0)
    adx = dx.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    return _indicator_result(required, adx=adx.iloc[-1], plus_di=plus_di.iloc[-1], minus_di=minus_di.iloc[-1])


def _volume_indicators(df: pd.DataFrame, period: int = _VOLUME_PERIOD) -> tuple[dict[str, Any], dict[str, Any]]:
    missing = _indicator_result(period, value=None)
    missing_obv = _indicator_result(period, value=None, slope=None)
    if len(df) < period:
        return missing, missing_obv

    volume = pd.to_numeric(df["volume"], errors="coerce")
    if volume.tail(period).isna().any() or (volume.tail(period) < 0).any() or not volume.tail(period).map(math.isfinite).all():
        return missing, missing_obv

    mean_volume = float(volume.tail(period).mean())
    relative = float(volume.iloc[-1]) / mean_volume if mean_volume > 0 else None

    direction = df["close"].astype(float).diff().apply(lambda value: 1.0 if value > 0 else (-1.0 if value < 0 else 0.0))
    obv = (direction * volume).fillna(0.0).cumsum()
    window = obv.tail(period).reset_index(drop=True)
    x_mean = (period - 1) / 2.0
    denominator = sum((i - x_mean) ** 2 for i in range(period))
    slope = sum((i - x_mean) * (float(window.iloc[i]) - float(window.mean())) for i in range(period)) / denominator
    return (
        _indicator_result(period, value=relative),
        _indicator_result(period, value=obv.iloc[-1], slope=slope),
    )


def _pct_change(df: pd.DataFrame, bars_back: int) -> Optional[float]:
    if len(df) <= bars_back:
        return None
    last = float(df["close"].iloc[-1])
    prev = float(df["close"].iloc[-1 - bars_back])
    if not prev:
        return None
    return round((last - prev) / prev * 100.0, 4)


def _extract_section(raw: dict[str, Any]) -> Optional[dict[str, Any]]:
    if not raw or not raw.get("ok"):
        return None
    data = raw.get("data", {})
    rows = data.get("data") if isinstance(data, dict) else None
    return rows[0] if isinstance(rows, list) and rows else None


def _higher_tf_trend(df_1h: pd.DataFrame) -> dict[str, Any]:
    """Trend 1h wg spec #79: relacja EMA50/EMA200 (nie EMA20/EMA50 jak
    ta_stack.compute_core, który jest kalibrowany pod 1d swing) + swing high/low.

    Poniżej _MIN_BARS_FOR_EMA50 świec EMA50 jest zbyt zniekształcone efektem
    rozgrzewki (ewm adjust=False) — "unknown" zamiast fałszywie pewnego trendu.
    EMA200 z natury rzeczy jest przybliżone dopóki historia < 200 barów (#78
    obecnie pobiera 80 świec 1H) — to świadomy kompromis (spec #79 akceptuje
    "orientacyjny" kontekst wyższego TF), nie blokuje wyniku."""
    if len(df_1h) < _MIN_BARS_FOR_EMA50:
        swing = _swing_sr(df_1h)
        return {
            "trend_1h": "unknown",
            "ema50_1h": None,
            "ema200_1h": None,
            "last_swing_high_1h": swing.get("resistance"),
            "last_swing_low_1h": swing.get("support"),
            "adx_1h": _adx(df_1h),
        }

    close = df_1h["close"].astype(float)
    ema50 = _ema(close, 50)
    ema200 = _ema(close, min(len(df_1h), 200))
    e50 = float(ema50.iloc[-1])
    e200 = float(ema200.iloc[-1])

    if e50 > e200 * 1.002:
        trend = "up"
    elif e50 < e200 * 0.998:
        trend = "down"
    else:
        trend = "range"

    swing = _swing_sr(df_1h)
    return {
        "trend_1h": trend,
        "ema50_1h": round(e50, 6),
        "ema200_1h": round(e200, 6),
        "last_swing_high_1h": swing.get("resistance"),
        "last_swing_low_1h": swing.get("support"),
        "adx_1h": _adx(df_1h),
    }


def _higher_tf_4h_direction(df_4h: pd.DataFrame) -> str:
    """Kierunek orientacyjny 4h (spec #79: bez pełnego zestawu wskaźników) —
    EMA20 vs EMA50 na 4h, prosta heurystyka analogiczna do ta_stack.compute_core.

    Poniżej _MIN_BARS_FOR_EMA50 świec EMA50 jest zniekształcone efektem
    rozgrzewki (ewm adjust=False konwerguje powoli) — zwraca "unknown" zamiast
    fałszywie pewnego up/down."""
    if len(df_4h) < _MIN_BARS_FOR_EMA50:
        return "unknown"
    close = df_4h["close"].astype(float)
    ema_fast = _ema(close, 20)
    ema_slow = _ema(close, 50)
    ef = float(ema_fast.iloc[-1])
    es = float(ema_slow.iloc[-1])
    if ef > es * 1.002:
        return "up"
    if ef < es * 0.998:
        return "down"
    return "range"


def _orderbook_summary(ob_data: dict[str, Any]) -> Optional[dict[str, Any]]:
    bids = ob_data.get("bids") or []
    asks = ob_data.get("asks") or []
    if not bids or not asks:
        return None
    best_bid = float(bids[0][0])
    best_ask = float(asks[0][0])
    mid = (best_bid + best_ask) / 2.0
    spread_bps = ((best_ask - best_bid) / mid * 10_000.0) if mid else 0.0

    depth_low = mid * 0.999
    depth_high = mid * 1.001
    depth_bid_usd = sum(float(p) * float(sz) for p, sz, *_ in bids if float(p) >= depth_low)
    depth_ask_usd = sum(float(p) * float(sz) for p, sz, *_ in asks if float(p) <= depth_high)

    return {
        "best_bid": best_bid,
        "best_ask": best_ask,
        "spread_bps": round(spread_bps, 4),
        "depth_0.1pct_bid_usd": round(depth_bid_usd, 2),
        "depth_0.1pct_ask_usd": round(depth_ask_usd, 2),
    }


def _candle_rows(candles: dict[str, Any], label: str) -> Optional[list[list[str]]]:
    section = candles.get(label, {})
    if not section.get("ok"):
        return None
    data = section.get("data", {})
    rows = data.get("data") if isinstance(data, dict) else None
    return rows if isinstance(rows, list) and rows else None


def analyze_symbol(symbol: str, raw: dict[str, Any]) -> dict[str, Any]:
    candles = raw.get("candles", {})

    c15_rows = _candle_rows(candles, "15m")
    c1h_rows = _candle_rows(candles, "1H")
    c4h_rows = _candle_rows(candles, "4H")

    if not c15_rows:
        raise AnalysisError(f"{symbol}: brak świec 15m w danych wejściowych (#78 fetch failował?)")

    df15 = okx_candles_to_df(c15_rows)
    core15 = compute_core(df15)
    relative_volume, obv = _volume_indicators(df15)

    last_price = float(df15["close"].iloc[-1])
    price = {
        "last": last_price,
        "change_pct_15m": _pct_change(df15, 1),
        "change_pct_1h": _pct_change(df15, 4),
        "change_pct_24h": _pct_change(df15, 96),
    }

    indicators_15m = {
        "rsi14": core15["momentum"]["rsi14"],
        "ema20": core15["trend"]["ema_fast"],
        "ema50": core15["trend"]["ema_slow"],
        "atr14": core15["volatility"]["atr14"],
        "bb": _bollinger_bounds(df15),
        "vwap": round(_rolling_vwap(df15), 6),
        "macd": _macd(df15),
        "stoch_rsi": _stoch_rsi(df15),
        "adx": _adx(df15),
        "relative_volume": relative_volume,
        "obv": obv,
    }

    higher_tf_context: dict[str, Any] = {}
    if c1h_rows:
        higher_tf_context.update(_higher_tf_trend(okx_candles_to_df(c1h_rows)))
    else:
        higher_tf_context.update(
            {"trend_1h": "unknown", "ema50_1h": None, "ema200_1h": None,
             "last_swing_high_1h": None, "last_swing_low_1h": None,
             "adx_1h": _adx(pd.DataFrame(columns=["high", "low", "close"]))}
        )
    higher_tf_context["trend_4h"] = _higher_tf_4h_direction(okx_candles_to_df(c4h_rows)) if c4h_rows else "unknown"

    orderbook = None
    ob_raw = _extract_section(raw.get("orderbook", {}))
    if ob_raw:
        orderbook = _orderbook_summary(ob_raw)

    funding = _extract_section(raw.get("funding_rate", {})) or {}
    oi = _extract_section(raw.get("open_interest", {})) or {}
    futures = {
        "funding_rate": _to_float(funding.get("fundingRate")),
        "funding_rate_predicted": _to_float(funding.get("nextFundingRate")),
        "open_interest": _to_float(oi.get("oi")),
        "open_interest_ccy": _to_float(oi.get("oiCcy")),
    }

    return {
        "symbol": raw.get("inst_id", symbol),
        "analyzed_at": raw.get("fetched_at"),
        "price": price,
        "indicators_15m": indicators_15m,
        "higher_tf_context": higher_tf_context,
        "orderbook": orderbook,
        "futures": futures,
    }


def _bollinger_bounds(df: pd.DataFrame) -> dict[str, float]:
    close = df["close"].astype(float)
    mid = close.rolling(20).mean()
    std = close.rolling(20).std()
    upper = mid + 2 * std
    lower = mid - 2 * std
    i = -1
    m = float(mid.iloc[i]) if pd.notna(mid.iloc[i]) else float(close.iloc[i])
    u = float(upper.iloc[i]) if pd.notna(upper.iloc[i]) else m
    l = float(lower.iloc[i]) if pd.notna(lower.iloc[i]) else m
    return {"upper": round(u, 6), "mid": round(m, 6), "lower": round(l, 6)}


def _to_float(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _pass(step: str, detail: str = "") -> None:
    suffix = f" — {detail}" if detail else ""
    print(f"[PASS] {step}{suffix}")


def _fail(step: str, detail: str = "") -> None:
    suffix = f" — {detail}" if detail else ""
    print(f"[FAIL] {step}{suffix}")


def main() -> int:
    print("Analyze crypto market data")
    print("-" * 60)

    any_success = False

    for symbol in SYMBOLS:
        in_path = DATA_DIR / f"{symbol}_latest.json"
        if not in_path.exists():
            _fail(symbol, f"brak pliku wejściowego {in_path} — uruchom najpierw fetch_crypto_market_data.py")
            continue

        try:
            raw = json.loads(in_path.read_text())
            result = analyze_symbol(symbol, raw)
        except (AnalysisError, ValueError, KeyError) as exc:
            _fail(symbol, str(exc))
            continue

        out_path = DATA_DIR / f"{symbol}_analysis.json"
        out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False))
        _pass(symbol, f"zapisano -> {out_path}")
        any_success = True

    print("-" * 60)
    if any_success:
        print("WYNIK: analiza zapisana dla przynajmniej jednego symbolu.")
        return 0
    print("WYNIK: FAIL — żaden symbol nie miał wystarczających danych wejściowych.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
