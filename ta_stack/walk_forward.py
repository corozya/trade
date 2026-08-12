"""Walk-forward / simple rule backtest → outcomes into RAG."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import numpy as np
import pandas as pd

from .snapshot import build_snapshot


@dataclass
class WalkForwardResult:
    n_folds: int
    n_trades: int
    wins: int
    avg_pnl_pct: float
    outcomes_written: int


def _rsi_series(close: pd.Series, period: int = 14) -> pd.Series:
    try:
        import talib

        return talib.RSI(close.astype(float), timeperiod=period)
    except Exception:
        delta = close.diff()
        gain = delta.clip(lower=0).rolling(period).mean()
        loss = (-delta.clip(upper=0)).rolling(period).mean()
        rs = gain / loss.replace(0, np.nan)
        return 100 - (100 / (1 + rs))


def walk_forward_rsi_mean_reversion(
    df: pd.DataFrame,
    *,
    symbol: str,
    asset_class: str,
    source: str,
    timeframe: str,
    rag,
    train_bars: int = 120,
    test_bars: int = 40,
    hold_bars: int = 5,
    rsi_buy: float = 30.0,
    rsi_sell: float = 70.0,
) -> WalkForwardResult:
    """
    Walk-forward: na oknie train budujemy kontekst, na test szukamy RSI setupów,
    mierzymy forward return (hold_bars) i upsertujemy pattern+outcome do RAG.
    """
    close = df["close"].astype(float)
    rsi = _rsi_series(close)
    n = len(df)
    start = train_bars
    folds = 0
    trades = 0
    wins = 0
    pnls: list[float] = []
    written = 0

    while start + test_bars + hold_bars < n:
        folds += 1
        test_end = start + test_bars
        for i in range(start, test_end):
            if i + hold_bars >= n:
                break
            r = float(rsi.iloc[i]) if pd.notna(rsi.iloc[i]) else 50.0
            if r > rsi_buy and r < rsi_sell:
                continue
            direction = "LONG" if r <= rsi_buy else "SHORT"
            entry = float(close.iloc[i])
            exit_px = float(close.iloc[i + hold_bars])
            if direction == "LONG":
                pnl = (exit_px - entry) / entry * 100.0
            else:
                pnl = (entry - exit_px) / entry * 100.0
            result = "win" if pnl > 0 else "loss"
            trades += 1
            if result == "win":
                wins += 1
            pnls.append(pnl)

            # Snapshot z historii do i (włącznie) — bez lookahead w feature'ach
            hist = df.iloc[: i + 1].tail(max(train_bars, 80)).reset_index(drop=True)
            if len(hist) < 60:
                continue
            snap = build_snapshot(
                hist,
                symbol=symbol,
                asset_class=asset_class,
                source=source,
                timeframe=timeframe,
            )
            pattern = snap["rag_hint"]
            outcome = {"direction": direction, "pnl_pct": round(pnl, 4), "result": result}
            ts = pd.Timestamp(df.iloc[i]["date"]).isoformat()
            if hasattr(rag, "upsert_pattern"):
                rag.upsert_pattern(pattern, outcome, timestamp=ts)
                written += 1
        start = test_end

    avg = float(np.mean(pnls)) if pnls else 0.0
    return WalkForwardResult(
        n_folds=folds,
        n_trades=trades,
        wins=wins,
        avg_pnl_pct=round(avg, 4),
        outcomes_written=written,
    )


def seed_rag_from_snapshots(
    snapshots_with_outcomes: list[tuple[dict, dict]],
    rag,
) -> int:
    """Upsert listy (snapshot|rag_hint, outcome). Zwraca liczbę zapisów."""
    n = 0
    for item, outcome in snapshots_with_outcomes:
        pattern = item.get("rag_hint", item)
        ts = item.get("as_of") or datetime.now(timezone.utc).isoformat()
        rag.upsert_pattern(pattern, outcome, timestamp=ts)
        n += 1
    return n
