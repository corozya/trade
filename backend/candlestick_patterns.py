"""Candlestick pattern detection (#226) — own body/wick-ratio logic, NOT
talib (verified NOT installed in backend/.venv,
ModuleNotFoundError; installing it needs the native TA-Lib C library, a
stack change out of scope without user approval — see task #226 description).

Same shape convention as indicators.py/risk_indicator.py: pure functions on
lists of OHLCV rows (as returned by /api/ohlcv's `_read_series("ohlcv", ...)`
before its `{time, open, high, low, close, volume}` reshape — this module
takes the raw lake rows with `open/high/low/close` keys directly).

Confidence heuristic: for single-candle patterns, body/wick proportions
relative to the candle's own high-low range (talib has no free ATR-relative
confidence output either — that scaling choice is this module's own design,
not a port of anything). For multi-candle patterns, confidence blends each
component candle's own single-candle strength.

Detected on-the-fly per request against already-read /api/ohlcv rows (task
decision: cheap O(n) scan over already-loaded candles, analogous to
risk_indicator.py's find_divergences() — NOT precomputed/backfilled into the
data lake like rsi/macd/atr/stochastic/risk_indicator, no dedicated
freshness tracking).
"""
from __future__ import annotations

from typing import Any


def _body(row: dict[str, Any]) -> float:
    return abs(row["close"] - row["open"])


def _range(row: dict[str, Any]) -> float:
    return row["high"] - row["low"]


def _upper_wick(row: dict[str, Any]) -> float:
    return row["high"] - max(row["open"], row["close"])


def _lower_wick(row: dict[str, Any]) -> float:
    return min(row["open"], row["close"]) - row["low"]


def _is_bullish(row: dict[str, Any]) -> bool:
    return row["close"] > row["open"]


def _is_bearish(row: dict[str, Any]) -> bool:
    return row["close"] < row["open"]


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


# ---------------------------------------------------------------------------
# Single-candle patterns
# ---------------------------------------------------------------------------

def _detect_doji(row: dict[str, Any]) -> dict[str, Any] | None:
    """Body is a tiny fraction of the candle's range — indecision, direction
    neutral. Threshold: body <= 10% of range (standard convention)."""
    rng = _range(row)
    if rng <= 0:
        return None
    body_ratio = _body(row) / rng
    if body_ratio > 0.1:
        return None
    confidence = _clamp01(1.0 - body_ratio / 0.1)
    return {"pattern_name": "doji", "direction": "neutral", "strength": round(confidence, 3)}


def _detect_hammer_or_shooting_star(row: dict[str, Any]) -> dict[str, Any] | None:
    """Small body near one end of the range, opposite wick >= 2x the body,
    near-absent wick on the body's own side. Hammer = long lower wick (body
    near top, bullish reversal signal at a low); Shooting star = long upper
    wick (body near bottom, bearish reversal signal at a high). Direction
    here reflects the reversal signal implied by the shape itself — whether
    it actually sits at a swing low/high is a caller-side (chart-context)
    judgment, not something this single-candle check can know."""
    rng = _range(row)
    if rng <= 0:
        return None
    body = _body(row)
    upper = _upper_wick(row)
    lower = _lower_wick(row)
    body_ratio = body / rng
    if body_ratio > 0.35:  # body must be small relative to the whole range
        return None

    if lower >= 2.0 * max(body, rng * 0.001) and upper <= body + rng * 0.05:
        # long lower wick, small/absent upper wick -> hammer
        confidence = _clamp01((lower / rng) * (1.0 - body_ratio))
        return {"pattern_name": "hammer", "direction": "bullish", "strength": round(confidence, 3)}

    if upper >= 2.0 * max(body, rng * 0.001) and lower <= body + rng * 0.05:
        # long upper wick, small/absent lower wick -> shooting star
        confidence = _clamp01((upper / rng) * (1.0 - body_ratio))
        return {"pattern_name": "shooting_star", "direction": "bearish", "strength": round(confidence, 3)}

    return None


def _detect_marubozu(row: dict[str, Any]) -> dict[str, Any] | None:
    """Body fills almost the entire range, negligible wicks on both sides —
    strong one-directional conviction candle. Threshold: body >= 90% of
    range."""
    rng = _range(row)
    if rng <= 0:
        return None
    body_ratio = _body(row) / rng
    if body_ratio < 0.9:
        return None
    confidence = _clamp01((body_ratio - 0.9) / 0.1)
    direction = "bullish" if _is_bullish(row) else "bearish" if _is_bearish(row) else "neutral"
    return {"pattern_name": "marubozu", "direction": direction, "strength": round(confidence, 3)}


def _single_candle_patterns(row: dict[str, Any]) -> list[dict[str, Any]]:
    detectors = (_detect_doji, _detect_marubozu, _detect_hammer_or_shooting_star)
    found = []
    for detector in detectors:
        result = detector(row)
        if result is not None:
            found.append(result)
    return found


# ---------------------------------------------------------------------------
# Multi-candle patterns (2-3 candles)
# ---------------------------------------------------------------------------

def _detect_engulfing(prev: dict[str, Any], curr: dict[str, Any]) -> dict[str, Any] | None:
    """Bullish engulfing: prev bearish, curr bullish, curr's body fully
    engulfs prev's body (curr open <= prev close, curr close >= prev open).
    Bearish engulfing: mirror image."""
    prev_body = _body(prev)
    curr_body = _body(curr)
    if prev_body <= 0 or curr_body <= 0:
        return None

    if _is_bearish(prev) and _is_bullish(curr) and curr["open"] <= prev["close"] and curr["close"] >= prev["open"]:
        confidence = _clamp01(curr_body / prev_body / 2.0)
        return {"pattern_name": "bullish_engulfing", "direction": "bullish", "strength": round(confidence, 3)}

    if _is_bullish(prev) and _is_bearish(curr) and curr["open"] >= prev["close"] and curr["close"] <= prev["open"]:
        confidence = _clamp01(curr_body / prev_body / 2.0)
        return {"pattern_name": "bearish_engulfing", "direction": "bearish", "strength": round(confidence, 3)}

    return None


def _detect_piercing_or_dark_cloud(prev: dict[str, Any], curr: dict[str, Any]) -> dict[str, Any] | None:
    """Piercing line: prev bearish, curr bullish, curr opens below prev's
    low-ish (gap down) and closes above the midpoint of prev's body (but not
    above prev's open — that would be an engulfing instead). Dark cloud
    cover: mirror image."""
    prev_body = _body(prev)
    if prev_body <= 0:
        return None
    prev_mid = (prev["open"] + prev["close"]) / 2.0

    if (
        _is_bearish(prev)
        and _is_bullish(curr)
        and curr["open"] < prev["close"]
        and prev_mid < curr["close"] < prev["open"]
    ):
        penetration = (curr["close"] - prev_mid) / (prev["open"] - prev_mid)
        confidence = _clamp01(penetration)
        return {"pattern_name": "piercing_line", "direction": "bullish", "strength": round(confidence, 3)}

    if (
        _is_bullish(prev)
        and _is_bearish(curr)
        and curr["open"] > prev["close"]
        and prev["open"] < curr["close"] < prev_mid
    ):
        penetration = (prev_mid - curr["close"]) / (prev_mid - prev["open"])
        confidence = _clamp01(penetration)
        return {"pattern_name": "dark_cloud_cover", "direction": "bearish", "strength": round(confidence, 3)}

    return None


def _detect_star(first: dict[str, Any], mid: dict[str, Any], last: dict[str, Any]) -> dict[str, Any] | None:
    """Morning star: first candle bearish with a real body, mid candle a
    small-bodied "star" that gaps down from first's body and doesn't overlap
    much, last candle bullish closing well into first's body. Evening star:
    mirror image (bullish, star gaps up, bearish close).

    Gap direction is checked loosely (mid's body vs first's body) rather than
    requiring a strict price gap — crypto futures trade continuously, real
    gaps between candle bodies are rare even at reversals."""
    first_body = _body(first)
    last_body = _body(last)
    mid_range = _range(mid)
    if first_body <= 0 or last_body <= 0:
        return None
    mid_body_ratio = _body(mid) / mid_range if mid_range > 0 else 0.0
    if mid_body_ratio > 0.4:  # mid candle must be small-bodied (the "star")
        return None
    first_mid = (first["open"] + first["close"]) / 2.0

    if (
        _is_bearish(first)
        and max(mid["open"], mid["close"]) <= first["close"] + first_body * 0.1
        and _is_bullish(last)
        and last["close"] > first_mid
    ):
        penetration = _clamp01((last["close"] - first_mid) / (first["open"] - first_mid)) if first["open"] != first_mid else 0.0
        confidence = _clamp01((1.0 - mid_body_ratio) * 0.5 + penetration * 0.5)
        return {"pattern_name": "morning_star", "direction": "bullish", "strength": round(confidence, 3)}

    if (
        _is_bullish(first)
        and min(mid["open"], mid["close"]) >= first["close"] - first_body * 0.1
        and _is_bearish(last)
        and last["close"] < first_mid
    ):
        penetration = _clamp01((first_mid - last["close"]) / (first_mid - first["open"])) if first["open"] != first_mid else 0.0
        confidence = _clamp01((1.0 - mid_body_ratio) * 0.5 + penetration * 0.5)
        return {"pattern_name": "evening_star", "direction": "bearish", "strength": round(confidence, 3)}

    return None


def _detect_three_soldiers_or_crows(a: dict[str, Any], b: dict[str, Any], c: dict[str, Any]) -> dict[str, Any] | None:
    """Three white soldiers: three consecutive bullish candles, each closing
    higher than the last, each opening within the previous candle's body
    (not gapping away), each with a small upper wick (real, not indecisive,
    advance). Three black crows: mirror image."""
    candles = (a, b, c)

    if all(_is_bullish(x) for x in candles):
        if not (b["close"] > a["close"] and c["close"] > b["close"]):
            return None
        if not (a["open"] < b["open"] < a["close"] and b["open"] < c["open"] < b["close"]):
            return None
        wick_ratios = [
            _upper_wick(x) / _range(x) if _range(x) > 0 else 1.0
            for x in candles
        ]
        if max(wick_ratios) > 0.3:
            return None
        confidence = _clamp01(1.0 - max(wick_ratios) / 0.3)
        return {"pattern_name": "three_white_soldiers", "direction": "bullish", "strength": round(confidence, 3)}

    if all(_is_bearish(x) for x in candles):
        if not (b["close"] < a["close"] and c["close"] < b["close"]):
            return None
        if not (a["open"] > b["open"] > a["close"] and b["open"] > c["open"] > b["close"]):
            return None
        wick_ratios = [
            _lower_wick(x) / _range(x) if _range(x) > 0 else 1.0
            for x in candles
        ]
        if max(wick_ratios) > 0.3:
            return None
        confidence = _clamp01(1.0 - max(wick_ratios) / 0.3)
        return {"pattern_name": "three_black_crows", "direction": "bearish", "strength": round(confidence, 3)}

    return None


def detect_patterns(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Scans `rows` (OHLCV rows with open/high/low/close, sorted ascending by
    time — same shape as _read_series("ohlcv", ...) rows) and returns every
    detected pattern as `{time, pattern_name, direction, strength}`, `time`
    taken from the LAST candle of the pattern (the one a trader would react
    to), sorted by time ascending. Multiple patterns can fire on/around the
    same candle (e.g. a hammer that's also part of a morning star) —
    deliberately not deduplicated, the caller decides what to do with
    overlapping signals."""
    out: list[dict[str, Any]] = []
    n = len(rows)

    for i in range(n):
        row = rows[i]
        time_key = row.get("time", row.get("observed_at"))
        for pattern in _single_candle_patterns(row):
            out.append({"time": time_key, **pattern})

        if i >= 1:
            prev = rows[i - 1]
            for detector in (_detect_engulfing, _detect_piercing_or_dark_cloud):
                result = detector(prev, row)
                if result is not None:
                    out.append({"time": time_key, **result})

        if i >= 2:
            first, mid, last = rows[i - 2], rows[i - 1], row
            star = _detect_star(first, mid, last)
            if star is not None:
                out.append({"time": time_key, **star})
            triple = _detect_three_soldiers_or_crows(first, mid, last)
            if triple is not None:
                out.append({"time": time_key, **triple})

    out.sort(key=lambda p: p["time"])
    return out
