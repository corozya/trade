"""Strefy: TDST + SMC FVG + swing S/R → confluence (inspiracja smart-money-concepts)."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd


def _tdst_levels(df: pd.DataFrame) -> dict[str, float | None]:
    """Tom DeMark Sequential True Support/Resistance (9-bar setups)."""
    close = df["close"]
    high = df["high"]
    low = df["low"]

    sell_setup = close > close.shift(4)
    buy_setup = close < close.shift(4)

    sell_groups = sell_setup.ne(sell_setup.shift()).cumsum()
    buy_groups = buy_setup.ne(buy_setup.shift()).cumsum()
    sell_counts = sell_setup.groupby(sell_groups).cumsum()
    buy_counts = buy_setup.groupby(buy_groups).cumsum()

    sell_done = sell_counts == 9
    buy_done = buy_counts == 9

    support = pd.Series(
        np.where(sell_done, low.rolling(9).min(), np.nan), index=df.index
    ).ffill()
    resistance = pd.Series(
        np.where(buy_done, high.rolling(9).max(), np.nan), index=df.index
    ).ffill()

    s = float(support.iloc[-1]) if pd.notna(support.iloc[-1]) else None
    r = float(resistance.iloc[-1]) if pd.notna(resistance.iloc[-1]) else None
    price = float(close.iloc[-1])
    near = "none"
    if s is not None and r is not None:
        near = "support" if abs(price - s) <= abs(price - r) else "resistance"
    elif s is not None:
        near = "support"
    elif r is not None:
        near = "resistance"
    return {"support": s, "resistance": r, "near": near}


def _swing_sr(df: pd.DataFrame, left: int = 3, right: int = 3) -> dict[str, float | None]:
    high = df["high"].to_numpy()
    low = df["low"].to_numpy()
    n = len(df)
    swing_highs: list[float] = []
    swing_lows: list[float] = []
    for i in range(left, n - right):
        window_h = high[i - left : i + right + 1]
        window_l = low[i - left : i + right + 1]
        if high[i] == window_h.max():
            swing_highs.append(float(high[i]))
        if low[i] == window_l.min():
            swing_lows.append(float(low[i]))

    price = float(df["close"].iloc[-1])
    supports = [x for x in swing_lows if x <= price]
    resistances = [x for x in swing_highs if x >= price]
    return {
        "support": max(supports) if supports else (swing_lows[-1] if swing_lows else None),
        "resistance": min(resistances) if resistances else (swing_highs[-1] if swing_highs else None),
    }


def _fvg_zones(df: pd.DataFrame, lookback: int = 80) -> list[dict[str, Any]]:
    """3-candle Fair Value Gaps (bullish/bearish)."""
    sub = df.tail(lookback).reset_index(drop=True)
    fvgs: list[dict[str, Any]] = []
    for i in range(2, len(sub)):
        c0_high = float(sub.loc[i - 2, "high"])
        c0_low = float(sub.loc[i - 2, "low"])
        c2_high = float(sub.loc[i, "high"])
        c2_low = float(sub.loc[i, "low"])
        # Bullish FVG: gap up between candle0 high and candle2 low
        if c2_low > c0_high:
            fvgs.append(
                {
                    "type": "bullish",
                    "low": c0_high,
                    "high": c2_low,
                    "mid": (c0_high + c2_low) / 2,
                }
            )
        # Bearish FVG: gap down between candle0 low and candle2 high
        if c2_high < c0_low:
            fvgs.append(
                {
                    "type": "bearish",
                    "low": c2_high,
                    "high": c0_low,
                    "mid": (c2_high + c0_low) / 2,
                }
            )
    return fvgs[-10:]  # keep recent


def compute_zones(df: pd.DataFrame, atr: float, price: float) -> dict:
    tdst = _tdst_levels(df)
    swing = _swing_sr(df)
    fvgs = _fvg_zones(df)

    bull_fvgs = [f for f in fvgs if f["type"] == "bullish"]
    bear_fvgs = [f for f in fvgs if f["type"] == "bearish"]
    nearest_sup = max((f["high"] for f in bull_fvgs if f["high"] <= price), default=None)
    nearest_res = min((f["low"] for f in bear_fvgs if f["low"] >= price), default=None)
    if nearest_sup is None and bull_fvgs:
        nearest_sup = bull_fvgs[-1]["mid"]
    if nearest_res is None and bear_fvgs:
        nearest_res = bear_fvgs[-1]["mid"]

    smc = {
        "fvg": fvgs,
        "nearest_support": nearest_sup,
        "nearest_resistance": nearest_res,
    }

    window = max(atr * 1.5, price * 0.005) if atr or price else 1.0
    support_votes = 0
    resistance_votes = 0
    levels: list[float] = []

    for src_name, level, side in (
        ("tdst", tdst.get("support"), "support"),
        ("tdst", tdst.get("resistance"), "resistance"),
        ("swing", swing.get("support"), "support"),
        ("swing", swing.get("resistance"), "resistance"),
        ("smc", smc.get("nearest_support"), "support"),
        ("smc", smc.get("nearest_resistance"), "resistance"),
    ):
        if level is None:
            continue
        if abs(float(level) - price) <= window:
            levels.append(float(level))
            if side == "support":
                support_votes += 1
            else:
                resistance_votes += 1

    total = support_votes + resistance_votes
    if total == 0:
        conf = {"score": 0.0, "side": "none", "levels": []}
    elif support_votes > resistance_votes:
        conf = {
            "score": round(support_votes / max(total, 1), 4),
            "side": "support",
            "levels": sorted(set(round(x, 6) for x in levels)),
        }
    elif resistance_votes > support_votes:
        conf = {
            "score": round(resistance_votes / max(total, 1), 4),
            "side": "resistance",
            "levels": sorted(set(round(x, 6) for x in levels)),
        }
    else:
        conf = {
            "score": round(0.5, 4),
            "side": "none",
            "levels": sorted(set(round(x, 6) for x in levels)),
        }

    return {
        "sources": ["tdst", "smc_fvg", "swing_sr"],
        "tdst": tdst,
        "smc": smc,
        "swing": swing,
        "confluence": conf,
    }
