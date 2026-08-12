"""Risk Indicator (#181) — simplified port of pv.pine's "Risk Indicator"
(Julien-PH, MPL-2.0, upstream `pv.pine`): a 0-100 weighted average
of oscillators, plus bull/bear divergence detection against price.

Not 1:1 with the original (user decision, 2026-08-07) — 6 of its 16
components, the ones with a standard closed-form definition (no per-bar
recursive state like Aroon/STC/Heikin-Ashi-Count/TD-Sequential need):
RSI, Stochastic RSI (K/D delta), Williams %R, Bollinger Bands %, Money Flow
Index, Momentum %. Weights are fixed at 1.0 each (equal-weighted mean) —
the original's per-component weight sliders are not exposed, matching the
task's "doesn't need to be as configurable" scope decision.
"""
from __future__ import annotations

from typing import Any


def _clamp(value: float, lo: float = 0.0, hi: float = 100.0, default: float = 50.0) -> float:
    if value != value:  # NaN check without importing math
        return default
    return max(lo, min(hi, value))


def _rsi(closes: list[float], length: int = 14) -> list[float]:
    """Wilder's RSI. First `length` values are 50 (neutral, matching the
    original's `nz(...,50)` fallback convention) since there's no prior
    average to smooth from."""
    out = [50.0] * len(closes)
    if len(closes) <= length:
        return out
    gains = [0.0] * len(closes)
    losses = [0.0] * len(closes)
    for i in range(1, len(closes)):
        delta = closes[i] - closes[i - 1]
        gains[i] = max(delta, 0.0)
        losses[i] = max(-delta, 0.0)
    avg_gain = sum(gains[1 : length + 1]) / length
    avg_loss = sum(losses[1 : length + 1]) / length
    for i in range(length + 1, len(closes)):
        avg_gain = (avg_gain * (length - 1) + gains[i]) / length
        avg_loss = (avg_loss * (length - 1) + losses[i]) / length
        rs = avg_gain / avg_loss if avg_loss != 0 else float("inf")
        out[i] = 100.0 - 100.0 / (1.0 + rs) if avg_loss != 0 else 100.0
    return out


def _sma(values: list[float], length: int) -> list[float]:
    out = [float("nan")] * len(values)
    for i in range(length - 1, len(values)):
        out[i] = sum(values[i - length + 1 : i + 1]) / length
    return out


def _stochastic_rsi(closes: list[float], rsi_length: int = 14, smooth_k: int = 4, smooth_d: int = 4) -> tuple[list[float], list[float], list[float]]:
    """Returns (k, d, kd_delta) — kd_delta is `_clamp((k - d) + 50)`, same as
    pv.pine's stochasticRsiMethod."""
    rsi_vals = _rsi(closes, rsi_length)
    stoch = [float("nan")] * len(closes)
    for i in range(rsi_length - 1, len(closes)):
        window = rsi_vals[i - rsi_length + 1 : i + 1]
        lo, hi = min(window), max(window)
        stoch[i] = (rsi_vals[i] - lo) / (hi - lo) * 100.0 if hi != lo else 50.0
    k_raw = _sma(stoch, smooth_k)
    k = [v if v == v else 50.0 for v in k_raw]
    d_raw = _sma(k, smooth_d)
    d = [v if v == v else 50.0 for v in d_raw]
    kd_delta = [_clamp((k[i] - d[i]) + 50.0) for i in range(len(closes))]
    return k, d, kd_delta


def _williams_r(highs: list[float], lows: list[float], closes: list[float], length: int = 21) -> list[float]:
    """Rescaled to 0-100 (0 = at the period low, 100 = at the period high) —
    pv.pine's williamsRangeMethod, not the traditional -100..0 convention."""
    out = [50.0] * len(closes)
    for i in range(length - 1, len(closes)):
        window_high = max(highs[i - length + 1 : i + 1])
        window_low = min(lows[i - length + 1 : i + 1])
        span = window_high - window_low
        out[i] = _clamp((closes[i] - window_low) / span * 100.0, default=50.0) if span != 0 else 50.0
    return out


def _bollinger_pct(closes: list[float], length: int = 20, mult: float = 3.0) -> list[float]:
    """% position of close within the Bollinger Bands (0 = lower band, 100 =
    upper band), pv.pine's bbMethod."""
    out = [50.0] * len(closes)
    for i in range(length - 1, len(closes)):
        window = closes[i - length + 1 : i + 1]
        mean = sum(window) / length
        variance = sum((v - mean) ** 2 for v in window) / length
        stdev = variance**0.5
        upper = mean + mult * stdev
        lower = mean - mult * stdev
        span = upper - lower
        out[i] = _clamp((closes[i] - lower) / span * 100.0, default=50.0) if span != 0 else 50.0
    return out


def _money_flow_index(highs: list[float], lows: list[float], closes: list[float], volumes: list[float], length: int = 14) -> list[float]:
    out = [50.0] * len(closes)
    typical = [(highs[i] + lows[i] + closes[i]) / 3.0 for i in range(len(closes))]
    raw_flow = [typical[i] * volumes[i] for i in range(len(closes))]
    for i in range(length, len(closes)):
        pos_flow = sum(raw_flow[j] for j in range(i - length + 1, i + 1) if typical[j] > typical[j - 1])
        neg_flow = sum(raw_flow[j] for j in range(i - length + 1, i + 1) if typical[j] < typical[j - 1])
        if neg_flow == 0:
            out[i] = 100.0 if pos_flow > 0 else 50.0
        else:
            money_ratio = pos_flow / neg_flow
            out[i] = 100.0 - 100.0 / (1.0 + money_ratio)
    return out


def _momentum_pct(closes: list[float], length: int = 10, ampl: float = 100.0, offset: float = 50.0) -> list[float]:
    out = [50.0] * len(closes)
    for i in range(length, len(closes)):
        prev = closes[i - length]
        ratio = (closes[i] / prev - 1.0) if prev != 0 else 0.0
        out[i] = _clamp(ratio * ampl + offset)
    return out


def compute_risk_ratio(rows: list[dict[str, Any]]) -> list[float]:
    """Equal-weighted mean of the 6 components above, one value per row in
    `rows` (each row needs open/high/low/close/volume — same shape
    /api/ohlcv already returns). Matches pv.pine's `MaxMinNz(safeDivide(sum,
    activeWeights, 50.0))` — clamped 0-100, defaulting to 50 (neutral)."""
    closes = [r["close"] for r in rows]
    highs = [r["high"] for r in rows]
    lows = [r["low"] for r in rows]
    volumes = [r["volume"] for r in rows]

    rsi = _rsi(closes)
    _, _, kd_delta = _stochastic_rsi(closes)
    wr = _williams_r(highs, lows, closes)
    bb = _bollinger_pct(closes)
    mfi = _money_flow_index(highs, lows, closes, volumes)
    mom = _momentum_pct(closes)

    return [_clamp((rsi[i] + kd_delta[i] + wr[i] + bb[i] + mfi[i] + mom[i]) / 6.0) for i in range(len(rows))]


def find_divergences(
    risk_ratio: list[float],
    closes: list[float],
    pivot_len: int = 3,
    min_bars_between: int = 5,
    max_bars_between: int = 60,
) -> list[dict[str, Any]]:
    """Bull divergence: price makes a lower low while risk_ratio makes a
    higher low. Bear divergence: price makes a higher high while risk_ratio
    makes a lower high. Direct port of pv.pine's isBullDivergence/
    isBearDivergence, minus its `confLength` (extra delay bars added purely
    to avoid repainting a LIVE, still-updating chart) — not needed here
    since this endpoint always runs against already-closed candles from the
    lake, nothing left to repaint.

    Returns [{index, type: "bull"|"bear"}] — `index` is into `risk_ratio`/
    `closes`, for the caller to map back to a candle time.
    """
    n = len(risk_ratio)
    divergences: list[dict[str, Any]] = []

    def is_pivot_low(i: int) -> bool:
        if i - pivot_len < 0 or i + pivot_len >= n:
            return False
        window = risk_ratio[i - pivot_len : i + pivot_len + 1]
        return risk_ratio[i] == min(window) and window.count(risk_ratio[i]) == 1

    def is_pivot_high(i: int) -> bool:
        if i - pivot_len < 0 or i + pivot_len >= n:
            return False
        window = risk_ratio[i - pivot_len : i + pivot_len + 1]
        return risk_ratio[i] == max(window) and window.count(risk_ratio[i]) == 1

    prev_pl_idx: int | None = None
    prev_ph_idx: int | None = None
    for i in range(n):
        if is_pivot_low(i):
            if prev_pl_idx is not None:
                bars_between = i - prev_pl_idx
                if min_bars_between <= bars_between <= max_bars_between:
                    if closes[i] < closes[prev_pl_idx] and risk_ratio[i] > risk_ratio[prev_pl_idx]:
                        divergences.append({"index": i, "type": "bull"})
            prev_pl_idx = i
        if is_pivot_high(i):
            if prev_ph_idx is not None:
                bars_between = i - prev_ph_idx
                if min_bars_between <= bars_between <= max_bars_between:
                    if closes[i] > closes[prev_ph_idx] and risk_ratio[i] < risk_ratio[prev_ph_idx]:
                        divergences.append({"index": i, "type": "bear"})
            prev_ph_idx = i
    return divergences
