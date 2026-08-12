"""ATR Renko bias — inspiracja renkodf / _s Renko (bez zewnętrznej paczki)."""

from __future__ import annotations

import numpy as np
import pandas as pd

try:
    import talib

    HAS_TALIB = True
except ImportError:
    HAS_TALIB = False


def renko_bias(df: pd.DataFrame, atr_period: int = 14) -> dict:
    """
    Buduje cegły Renko z close + ATR brick size; zwraca bias ostatniego trendu.
    """
    close = df["close"].astype(float).to_numpy()
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    c_s = df["close"].astype(float)

    if HAS_TALIB:
        atr = talib.ATR(high, low, c_s, timeperiod=atr_period)
        brick = float(np.nanmean(atr.dropna().tail(50))) if atr.notna().any() else float(np.std(close[-50:]) or 1.0)
    else:
        prev = c_s.shift(1)
        tr = pd.concat([(high - low), (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
        brick = float(tr.rolling(atr_period).mean().dropna().tail(50).mean())

    if not brick or brick <= 0 or np.isnan(brick):
        brick = float(np.std(close[-50:]) or abs(close[-1]) * 0.01 or 1.0)

    # Simplified classic Renko on closes
    bricks_trend: list[bool] = []
    level = close[0]
    trend = True  # True = up
    for px in close[1:]:
        if trend:
            while px >= level + brick:
                level += brick
                bricks_trend.append(True)
            if px <= level - 2 * brick:
                trend = False
                level -= brick
                bricks_trend.append(False)
                while px <= level - brick:
                    level -= brick
                    bricks_trend.append(False)
        else:
            while px <= level - brick:
                level -= brick
                bricks_trend.append(False)
            if px >= level + 2 * brick:
                trend = True
                level += brick
                bricks_trend.append(True)
                while px >= level + brick:
                    level += brick
                    bricks_trend.append(True)

    if not bricks_trend:
        # fallback: compare last vs first
        up = close[-1] >= close[0]
        return {"trend": "up" if up else "down", "bricks": 0, "brick_size": round(brick, 6)}

    last_trend = bricks_trend[-1]
    return {
        "trend": "up" if last_trend else "down",
        "bricks": len(bricks_trend),
        "brick_size": round(brick, 6),
    }
