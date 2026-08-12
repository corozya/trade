"""Build unified TA snapshot JSON (stocks = crypto)."""

from __future__ import annotations

from typing import Any

import pandas as pd

from .indicators import compute_core
from .renko import renko_bias
from .zones import compute_zones

REQUIRED_TOP_LEVEL = (
    "schema_version",
    "symbol",
    "asset_class",
    "source",
    "timeframe",
    "as_of",
    "bars",
    "price",
    "trend",
    "momentum",
    "volatility",
    "volume",
    "regime",
    "multi_tf",
    "zones",
    "bias",
    "rag_hint",
)


def _wt_zone(rsi: float) -> str:
    if rsi >= 70:
        return "overbought"
    if rsi <= 30:
        return "oversold"
    return "neutral"


def _bias_score(core: dict, renko: dict) -> float:
    score = float(core["regime"]["score"])
    if renko.get("trend") == "up":
        score += 0.15
    elif renko.get("trend") == "down":
        score -= 0.15
    if core["ha"]["color"] == "green":
        score += 0.1
    else:
        score -= 0.1
    return round(max(-1.0, min(1.0, score)), 4)


def build_snapshot(
    df: pd.DataFrame,
    *,
    symbol: str,
    asset_class: str,
    source: str,
    timeframe: str = "1d",
    higher_tf_df: pd.DataFrame | None = None,
    higher_tf: str | None = None,
) -> dict[str, Any]:
    if len(df) < 60:
        raise ValueError(f"Need ≥60 bars, got {len(df)}")

    core = compute_core(df)
    renko = renko_bias(df)
    zones = compute_zones(df, atr=core["atr14"], price=core["close"])

    last = df.iloc[-1]
    as_of = pd.Timestamp(last["date"]).isoformat()

    multi_tf = None
    if higher_tf_df is not None and len(higher_tf_df) >= 60:
        h = compute_core(higher_tf_df)
        multi_tf = {
            "higher_tf": higher_tf or "higher",
            "direction": h["trend"]["direction"],
            "rsi14": h["momentum"]["rsi14"],
            "aligned": h["trend"]["direction"] == core["trend"]["direction"],
        }

    bias_score = _bias_score(core, renko)
    rsi = core["momentum"]["rsi14"]

    snapshot = {
        "schema_version": "1.0",
        "symbol": symbol,
        "asset_class": asset_class,
        "source": source,
        "timeframe": timeframe,
        "as_of": as_of,
        "bars": int(len(df)),
        "price": {
            "open": float(last["open"]),
            "high": float(last["high"]),
            "low": float(last["low"]),
            "close": float(last["close"]),
            "volume": float(last["volume"]),
        },
        "trend": core["trend"],
        "momentum": core["momentum"],
        "volatility": core["volatility"],
        "volume": core["volume"],
        "regime": core["regime"],
        "multi_tf": multi_tf,
        "zones": zones,
        "bias": {"ha": core["ha"], "renko": renko},
        "rag_hint": {
            "symbol": symbol,
            "timeframe": timeframe,
            "bias_score": bias_score,
            "ema_alignment": core["trend"]["ema_alignment"],
            "wt1": rsi,
            "wt_zone": _wt_zone(rsi),
            "renko_trend": renko["trend"],
            "risk_ratio": min(100.0, core["volatility"]["atr_pct"] * 10),
            "risk_zone": "high"
            if core["volatility"]["atr_pct"] > 4
            else ("low" if core["volatility"]["atr_pct"] < 1.5 else "mid"),
        },
    }
    return snapshot


def validate_snapshot(snapshot: dict) -> list[str]:
    errors = []
    for key in REQUIRED_TOP_LEVEL:
        if key not in snapshot:
            errors.append(f"missing:{key}")
    zones = snapshot.get("zones") or {}
    sources = zones.get("sources") or []
    if len(sources) < 2:
        errors.append("zones.sources<2")
    if "confluence" not in zones:
        errors.append("missing:zones.confluence")
    bias = snapshot.get("bias") or {}
    if not bias.get("ha") and not bias.get("renko"):
        errors.append("missing:bias.ha|renko")
    return errors
