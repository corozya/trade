"""Core TA indicators via TA-Lib (+ HA without extra deps)."""

from __future__ import annotations

import numpy as np
import pandas as pd

try:
    import talib

    HAS_TALIB = True
except ImportError:
    HAS_TALIB = False


def _ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def compute_core(df: pd.DataFrame) -> dict:
    """Return last-bar indicator dict from OHLCV df."""
    close = df["close"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    volume = df["volume"].astype(float).fillna(0.0)
    o = df["open"].astype(float)

    if HAS_TALIB:
        rsi = talib.RSI(close, timeperiod=14)
        macd, macdsig, macdhist = talib.MACD(close, 12, 26, 9)
        atr = talib.ATR(high, low, close, timeperiod=14)
        adx = talib.ADX(high, low, close, timeperiod=14)
        stoch_k, stoch_d = talib.STOCH(high, low, close)
        upper, middle, lower = talib.BBANDS(close, timeperiod=20)
        ema_fast = talib.EMA(close, timeperiod=20)
        ema_slow = talib.EMA(close, timeperiod=50)
        obv = talib.OBV(close, volume)
    else:
        delta = close.diff()
        gain = delta.clip(lower=0).rolling(14).mean()
        loss = (-delta.clip(upper=0)).rolling(14).mean()
        rs = gain / loss.replace(0, np.nan)
        rsi = 100 - (100 / (1 + rs))
        ema12, ema26 = _ema(close, 12), _ema(close, 26)
        macd = ema12 - ema26
        macdsig = _ema(macd, 9)
        macdhist = macd - macdsig
        prev_c = close.shift(1)
        tr = pd.concat([(high - low), (high - prev_c).abs(), (low - prev_c).abs()], axis=1).max(axis=1)
        atr = tr.rolling(14).mean()
        ema_fast, ema_slow = _ema(close, 20), _ema(close, 50)
        adx = pd.Series(np.nan, index=close.index)
        stoch_k = ((close - low.rolling(14).min()) / (high.rolling(14).max() - low.rolling(14).min())).fillna(0.5) * 100
        stoch_d = stoch_k.rolling(3).mean()
        middle = close.rolling(20).mean()
        std = close.rolling(20).std()
        upper, lower = middle + 2 * std, middle - 2 * std
        obv = (np.sign(close.diff().fillna(0)) * volume).cumsum()

    i = -1
    c = float(close.iloc[i])
    atr_v = float(atr.iloc[i]) if pd.notna(atr.iloc[i]) else 0.0
    mid_v = float(middle.iloc[i]) if pd.notna(middle.iloc[i]) else c
    up_v = float(upper.iloc[i]) if pd.notna(upper.iloc[i]) else c
    lo_v = float(lower.iloc[i]) if pd.notna(lower.iloc[i]) else c
    ef = float(ema_fast.iloc[i]) if pd.notna(ema_fast.iloc[i]) else c
    es = float(ema_slow.iloc[i]) if pd.notna(ema_slow.iloc[i]) else c
    adx_v = float(adx.iloc[i]) if pd.notna(adx.iloc[i]) else 0.0

    if ef > es * 1.002:
        alignment = "bull"
    elif ef < es * 0.998:
        alignment = "bear"
    else:
        alignment = "flat"

    if adx_v >= 25 and alignment == "bull":
        direction = "up"
    elif adx_v >= 25 and alignment == "bear":
        direction = "down"
    elif alignment == "bull" and c > ef:
        direction = "up"
    elif alignment == "bear" and c < ef:
        direction = "down"
    else:
        direction = "sideways"

    vol_sma = volume.rolling(20).mean().iloc[i]
    rel_vol = float(volume.iloc[i] / vol_sma) if vol_sma and vol_sma > 0 else 1.0
    obv_slope = float(obv.iloc[i] - obv.iloc[max(-21, -len(obv))]) / 20.0

    bb_width = (up_v - lo_v) / mid_v if mid_v else 0.0
    atr_pct = (atr_v / c * 100.0) if c else 0.0

    # Regime
    if atr_pct > 5.0 and adx_v < 20:
        regime_label = "high_vol"
        regime_score = 0.0
    elif direction == "up":
        regime_label = "trend_up"
        regime_score = min(1.0, adx_v / 50.0)
    elif direction == "down":
        regime_label = "trend_down"
        regime_score = -min(1.0, adx_v / 50.0)
    else:
        regime_label = "range"
        regime_score = 0.0

    ha = heikin_ashi_bias(o, high, low, close)

    return {
        "trend": {
            "ema_fast": round(ef, 6),
            "ema_slow": round(es, 6),
            "ema_alignment": alignment,
            "adx": round(adx_v, 4),
            "direction": direction,
        },
        "momentum": {
            "rsi14": round(float(rsi.iloc[i]), 4) if pd.notna(rsi.iloc[i]) else 50.0,
            "macd": round(float(macd.iloc[i]), 6) if pd.notna(macd.iloc[i]) else 0.0,
            "macd_signal": round(float(macdsig.iloc[i]), 6) if pd.notna(macdsig.iloc[i]) else 0.0,
            "macd_hist": round(float(macdhist.iloc[i]), 6) if pd.notna(macdhist.iloc[i]) else 0.0,
            "stoch_k": round(float(stoch_k.iloc[i]), 4) if pd.notna(stoch_k.iloc[i]) else 50.0,
            "stoch_d": round(float(stoch_d.iloc[i]), 4) if pd.notna(stoch_d.iloc[i]) else 50.0,
        },
        "volatility": {
            "atr14": round(atr_v, 6),
            "atr_pct": round(atr_pct, 4),
            "bb_width": round(bb_width, 6),
        },
        "volume": {
            "obv_slope": round(obv_slope, 4),
            "rel_vol": round(rel_vol, 4),
        },
        "regime": {"label": regime_label, "score": round(regime_score, 4)},
        "ha": ha,
        "atr14": atr_v,
        "close": c,
    }


def heikin_ashi_bias(
    open_: pd.Series, high: pd.Series, low: pd.Series, close: pd.Series
) -> dict:
    ha_close = (open_ + high + low + close) / 4.0
    ha_open = pd.Series(index=close.index, dtype=float)
    ha_open.iloc[0] = (open_.iloc[0] + close.iloc[0]) / 2.0
    for i in range(1, len(close)):
        ha_open.iloc[i] = (ha_open.iloc[i - 1] + ha_close.iloc[i - 1]) / 2.0

    colors = (ha_close >= ha_open).tolist()
    last = colors[-1]
    consecutive = 1
    for v in reversed(colors[:-1]):
        if v == last:
            consecutive += 1
        else:
            break
    return {"color": "green" if last else "red", "consecutive": consecutive}
