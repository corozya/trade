"""Loaders: Stooq CSV + Bitget feather → normalized OHLCV DataFrame."""

from __future__ import annotations

from pathlib import Path

import pandas as pd


OHLCV_COLS = ["date", "open", "high", "low", "close", "volume"]


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out.columns = [c.strip().lower().strip("<>") for c in out.columns]
    rename = {
        "vol": "volume",
        "ticker": "ticker",
        "per": "per",
        "time": "time",
        "openint": "openint",
    }
    out = out.rename(columns=rename)
    missing = [c for c in OHLCV_COLS if c not in out.columns]
    if missing:
        raise ValueError(f"Missing OHLCV columns: {missing}")
    out = out[OHLCV_COLS].copy()
    out["date"] = pd.to_datetime(out["date"], utc=True, errors="coerce")
    for c in ("open", "high", "low", "close", "volume"):
        out[c] = pd.to_numeric(out[c], errors="coerce")
    out = out.dropna(subset=["date", "open", "high", "low", "close"]).sort_values("date")
    out = out.reset_index(drop=True)
    return out


def load_stooq_csv(path: str | Path, tail: int | None = 500) -> pd.DataFrame:
    """Stooq ASCII daily: <TICKER>,<PER>,<DATE>,<TIME>,<OPEN>,..."""
    path = Path(path)
    df = pd.read_csv(path)
    # DATE may be int YYYYMMDD
    if "DATE" in df.columns or "<DATE>" in df.columns:
        col = "DATE" if "DATE" in df.columns else "<DATE>"
        df[col] = pd.to_datetime(df[col].astype(str), format="%Y%m%d", errors="coerce")
    out = _normalize(df)
    if tail:
        out = out.tail(tail).reset_index(drop=True)
    return out


def load_bitget_feather(path: str | Path, tail: int | None = 500) -> pd.DataFrame:
    path = Path(path)
    df = pd.read_feather(path)
    out = _normalize(df)
    if tail:
        out = out.tail(tail).reset_index(drop=True)
    return out
