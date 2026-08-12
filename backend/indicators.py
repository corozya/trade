"""Standalone technical indicators for crypto-dashboard panels — RSI, MACD,
classic Stochastic Oscillator (user request, 2026-08-07). Independent from
risk_indicator.py's RSI/Stochastic-RSI helpers: those clamp to a 50-neutral
fallback (a Risk Indicator component must always contribute *something* to
the weighted mean), while these leave the warmup period as NaN — a
standalone panel should show nothing until the indicator has real values,
not a flat neutral line that could be mistaken for a signal.

NaN is JSON-serialized as `null` by FastAPI (not the literal `NaN` token,
which strict JSON parsers reject) — endpoints below filter it out before
returning rather than relying on that.
"""
from __future__ import annotations


def rsi(closes: list[float], length: int = 14) -> list[float]:
    """Wilder's RSI, NaN for the warmup period (first `length` bars)."""
    out = [float("nan")] * len(closes)
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
    out[length] = 100.0 - 100.0 / (1.0 + avg_gain / avg_loss) if avg_loss != 0 else 100.0
    for i in range(length + 1, len(closes)):
        avg_gain = (avg_gain * (length - 1) + gains[i]) / length
        avg_loss = (avg_loss * (length - 1) + losses[i]) / length
        out[i] = 100.0 - 100.0 / (1.0 + avg_gain / avg_loss) if avg_loss != 0 else 100.0
    return out


def atr(highs: list[float], lows: list[float], closes: list[float], length: int = 14) -> list[float]:
    """Wilder's ATR (Average True Range), NaN for the warmup period (first
    `length` bars) — same smoothing convention as rsi() above: seed with a
    plain SMA of the first `length` true ranges, then Wilder-smooth
    (avg * (length - 1) + new) / length for the rest."""
    n = len(closes)
    out = [float("nan")] * n
    if n <= length:
        return out
    true_ranges = [0.0] * n
    true_ranges[0] = highs[0] - lows[0]
    for i in range(1, n):
        true_ranges[i] = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
    avg_tr = sum(true_ranges[1 : length + 1]) / length
    out[length] = avg_tr
    for i in range(length + 1, n):
        avg_tr = (avg_tr * (length - 1) + true_ranges[i]) / length
        out[i] = avg_tr
    return out


def _ema(values: list[float], length: int) -> list[float]:
    """EMA seeded with a plain SMA of the first `length` values (standard
    convention) — NaN before that seed point."""
    out = [float("nan")] * len(values)
    if len(values) < length:
        return out
    k = 2.0 / (length + 1)
    seed = sum(values[:length]) / length
    out[length - 1] = seed
    for i in range(length, len(values)):
        out[i] = values[i] * k + out[i - 1] * (1 - k)
    return out


def macd(closes: list[float], fast: int = 12, slow: int = 26, signal_length: int = 9) -> tuple[list[float], list[float], list[float]]:
    """Returns (macd_line, signal_line, histogram) — standard 12/26/9."""
    ema_fast = _ema(closes, fast)
    ema_slow = _ema(closes, slow)
    macd_line = [
        ema_fast[i] - ema_slow[i] if ema_fast[i] == ema_fast[i] and ema_slow[i] == ema_slow[i] else float("nan")
        for i in range(len(closes))
    ]
    # _ema() needs a contiguous run of non-NaN inputs to seed its own SMA —
    # feeding it macd_line's leading NaNs (from the slower `slow` EMA warmup)
    # would push the signal line's own warmup out further than necessary,
    # so it's computed from the first index macd_line actually has a value.
    first_valid = next((i for i, v in enumerate(macd_line) if v == v), len(macd_line))
    signal_tail = _ema(macd_line[first_valid:], signal_length)
    signal_line = [float("nan")] * first_valid + signal_tail
    histogram = [
        macd_line[i] - signal_line[i] if macd_line[i] == macd_line[i] and signal_line[i] == signal_line[i] else float("nan")
        for i in range(len(closes))
    ]
    return macd_line, signal_line, histogram


def stochastic(highs: list[float], lows: list[float], closes: list[float], k_length: int = 14, k_smooth: int = 3, d_smooth: int = 3) -> tuple[list[float], list[float]]:
    """Classic Stochastic Oscillator (%K/%D, 0-100) — NOT Stochastic RSI
    (that one lives in risk_indicator.py, computed from RSI values rather
    than from price highs/lows directly). Returns (k, d), both smoothed
    (k_smooth on raw %K, d_smooth on smoothed %K — standard "slow
    stochastic" convention, matching most charting platforms' defaults)."""
    n = len(closes)
    raw_k = [float("nan")] * n
    for i in range(k_length - 1, n):
        window_high = max(highs[i - k_length + 1 : i + 1])
        window_low = min(lows[i - k_length + 1 : i + 1])
        span = window_high - window_low
        raw_k[i] = (closes[i] - window_low) / span * 100.0 if span != 0 else 50.0

    def _sma_skip_nan(values: list[float], length: int) -> list[float]:
        out = [float("nan")] * len(values)
        for i in range(len(values)):
            window = values[max(0, i - length + 1) : i + 1]
            if len(window) < length or any(v != v for v in window):
                continue
            out[i] = sum(window) / length
        return out

    k = _sma_skip_nan(raw_k, k_smooth)
    d = _sma_skip_nan(k, d_smooth)
    return k, d
