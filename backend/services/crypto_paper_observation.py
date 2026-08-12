"""Multi-cycle paper observation and GO/NO-GO promotion criteria (#144).

Reads already-recorded ``paper_execution_ledger`` rows (``paper_execution.py``)
for one ``run_id``, buckets them chronologically over an observation window
(time-based, e.g. 24-72h, or a fixed bucket count), and produces a structured
report with per-bucket and aggregate metrics plus an explicit GO/NO-GO
verdict. This module never places a trade and never promotes a strategy
itself — a GO verdict is advisory input to ``ChampionRegistry.promote``
(#141), which still requires a human ``approved_by``. Rollback on a NO-GO
verdict is likewise the operator calling ``ChampionRegistry.rollback``.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence


class PaperObservationError(ValueError):
    """Raised for malformed ledger input or invalid observation windows."""


OBSERVATION_SCHEMA_VERSION = "paper-observation.v1"
GO = "GO"
NO_GO = "NO_GO"


def _utc(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise PaperObservationError("timestamps must include a timezone")
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class ObservationCriteria:
    """Versioned GO/NO-GO thresholds; never hardcode these at a call site."""

    min_buckets: int
    min_closed_trades: int
    min_win_rate: float
    max_drawdown: float
    require_positive_pnl: bool = True
    max_consecutive_losing_buckets: int = 3

    def __post_init__(self) -> None:
        if self.min_buckets < 1:
            raise PaperObservationError("min_buckets must be >= 1")
        if self.min_closed_trades < 0:
            raise PaperObservationError("min_closed_trades must be >= 0")
        if not 0 <= self.min_win_rate <= 1:
            raise PaperObservationError("min_win_rate must be in [0, 1]")
        if self.max_drawdown <= 0:
            raise PaperObservationError("max_drawdown must be > 0")
        if self.max_consecutive_losing_buckets < 1:
            raise PaperObservationError("max_consecutive_losing_buckets must be >= 1")


@dataclass(frozen=True)
class BucketMetrics:
    index: int
    start: str
    end: str
    open_count: int
    close_count: int
    wait_count: int
    pnl: float
    fees: float
    stop_loss_hits: int
    take_profit_hits: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "start": self.start,
            "end": self.end,
            "open_count": self.open_count,
            "close_count": self.close_count,
            "wait_count": self.wait_count,
            "pnl": self.pnl,
            "fees": self.fees,
            "stop_loss_hits": self.stop_loss_hits,
            "take_profit_hits": self.take_profit_hits,
        }


@dataclass(frozen=True)
class PaperObservationReport:
    schema_version: str
    run_id: str
    generated_at: str
    window_start: str
    window_end: str
    bucket_count: int
    buckets: tuple[BucketMetrics, ...]
    total_pnl: float
    total_fees: float
    closed_trade_count: int
    win_rate: float | None
    max_drawdown: float
    verdict: str
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "generated_at": self.generated_at,
            "window_start": self.window_start,
            "window_end": self.window_end,
            "bucket_count": self.bucket_count,
            "buckets": [bucket.to_dict() for bucket in self.buckets],
            "total_pnl": self.total_pnl,
            "total_fees": self.total_fees,
            "closed_trade_count": self.closed_trade_count,
            "win_rate": self.win_rate,
            "max_drawdown": self.max_drawdown,
            "verdict": self.verdict,
            "reasons": list(self.reasons),
        }


def _load_rows(conn: sqlite3.Connection, *, run_id: str) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM paper_execution_ledger WHERE run_id = ? ORDER BY created_at ASC, id ASC",
        (run_id,),
    ).fetchall()
    if not rows:
        raise PaperObservationError(f"no paper_execution_ledger rows for run_id={run_id!r}")
    return rows


def _bucket_bounds(
    *, window_start: datetime, window_end: datetime, bucket_count: int
) -> list[tuple[datetime, datetime]]:
    if bucket_count < 1:
        raise PaperObservationError("bucket_count must be >= 1")
    span = window_end - window_start
    if span <= timedelta(0):
        raise PaperObservationError("window_end must be after window_start")
    step = span / bucket_count
    bounds = []
    for index in range(bucket_count):
        start = window_start + step * index
        end = window_end if index == bucket_count - 1 else window_start + step * (index + 1)
        bounds.append((start, end))
    return bounds


def build_paper_observation_report(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    bucket_count: int,
    criteria: ObservationCriteria,
    window_start: str | datetime | None = None,
    window_end: str | datetime | None = None,
) -> PaperObservationReport:
    """Bucket one run's ledger rows and evaluate GO/NO-GO.

    ``window_start``/``window_end`` default to the first/last ``created_at``
    already recorded for ``run_id`` — this function never invents an
    observation window wider than what was actually recorded, so a caller
    cannot claim a 24-72h observation happened when the ledger only spans a
    few minutes; that mismatch surfaces as a ``min_buckets``/``min_closed_trades``
    shortfall in the verdict instead of being silently accepted.
    """
    rows = _load_rows(conn, run_id=run_id)
    start = _utc(window_start) if window_start is not None else _utc(rows[0]["created_at"])
    end = _utc(window_end) if window_end is not None else _utc(rows[-1]["created_at"])
    if end <= start:
        raise PaperObservationError("observation window has zero or negative duration")

    bounds = _bucket_bounds(window_start=start, window_end=end, bucket_count=bucket_count)
    buckets: list[BucketMetrics] = []
    equity = 0.0
    peak = 0.0
    max_drawdown = 0.0
    closed_pnls: list[float] = []
    total_fees = 0.0

    for index, (bucket_start, bucket_end) in enumerate(bounds):
        in_bucket = [
            row for row in rows
            if bucket_start <= _utc(row["created_at"]) < bucket_end
            or (index == len(bounds) - 1 and _utc(row["created_at"]) == bucket_end)
        ]
        open_count = sum(1 for row in in_bucket if row["decision"] == "OPEN")
        close_count = sum(1 for row in in_bucket if row["decision"] in {"CLOSE", "REDUCE"})
        wait_count = sum(1 for row in in_bucket if row["decision"] == "WAIT")
        bucket_pnl = sum(float(row["pnl"]) for row in in_bucket if row["pnl"] is not None and row["decision"] in {"CLOSE", "REDUCE"})
        bucket_fees = sum(float(row["fee"]) for row in in_bucket)
        stop_loss_hits = sum(
            1 for row in in_bucket
            if row["decision"] in {"CLOSE", "REDUCE"} and row["exit_price"] is not None
            and row["stop_loss_price"] is not None
            and abs(float(row["exit_price"]) - float(row["stop_loss_price"])) < 1e-9
        )
        take_profit_hits = sum(
            1 for row in in_bucket
            if row["decision"] in {"CLOSE", "REDUCE"} and row["exit_price"] is not None
            and row["take_profit_price"] is not None
            and abs(float(row["exit_price"]) - float(row["take_profit_price"])) < 1e-9
        )
        for row in in_bucket:
            if row["decision"] in {"CLOSE", "REDUCE"} and row["pnl"] is not None:
                closed_pnls.append(float(row["pnl"]))
                equity += float(row["pnl"])
                peak = max(peak, equity)
                max_drawdown = max(max_drawdown, peak - equity)
            total_fees += float(row["fee"])
        buckets.append(
            BucketMetrics(
                index=index,
                start=_iso(bucket_start),
                end=_iso(bucket_end),
                open_count=open_count,
                close_count=close_count,
                wait_count=wait_count,
                pnl=bucket_pnl,
                fees=bucket_fees,
                stop_loss_hits=stop_loss_hits,
                take_profit_hits=take_profit_hits,
            )
        )

    total_pnl = sum(closed_pnls)
    win_rate = (sum(1 for pnl in closed_pnls if pnl > 0) / len(closed_pnls)) if closed_pnls else None

    reasons: list[str] = []
    if len(buckets) < criteria.min_buckets:
        reasons.append(f"only {len(buckets)} buckets observed, need >= {criteria.min_buckets}")
    if len(closed_pnls) < criteria.min_closed_trades:
        reasons.append(
            f"only {len(closed_pnls)} closed trades, need >= {criteria.min_closed_trades}"
        )
    if criteria.require_positive_pnl and total_pnl <= 0:
        reasons.append(f"cumulative PnL is not positive: {total_pnl:.6f}")
    if win_rate is not None and win_rate < criteria.min_win_rate:
        reasons.append(f"win rate {win_rate:.2f} below minimum {criteria.min_win_rate:.2f}")
    if max_drawdown > criteria.max_drawdown:
        reasons.append(f"drawdown {max_drawdown:.6f} exceeds limit {criteria.max_drawdown:.6f}")
    losing_streak = longest_losing_streak = 0
    for bucket in buckets:
        losing_streak = losing_streak + 1 if bucket.pnl <= 0 else 0
        longest_losing_streak = max(longest_losing_streak, losing_streak)
    if longest_losing_streak >= criteria.max_consecutive_losing_buckets:
        reasons.append(
            f"{longest_losing_streak} consecutive losing buckets reached the "
            f"{criteria.max_consecutive_losing_buckets} limit"
        )

    verdict = NO_GO if reasons else GO

    return PaperObservationReport(
        schema_version=OBSERVATION_SCHEMA_VERSION,
        run_id=run_id,
        generated_at=_iso(datetime.now(timezone.utc)),
        window_start=_iso(start),
        window_end=_iso(end),
        bucket_count=len(buckets),
        buckets=tuple(buckets),
        total_pnl=total_pnl,
        total_fees=total_fees,
        closed_trade_count=len(closed_pnls),
        win_rate=win_rate,
        max_drawdown=max_drawdown,
        verdict=verdict,
        reasons=tuple(reasons),
    )


def bucket_count_for_window(*, hours: float, bucket_hours: float = 4.0) -> int:
    """Convenience: fixed-cadence bucket count for a 24-72h observation window.

    ``hours`` should be in ``[24, 72]`` per the roadmap AC; this helper is not
    a hard gate (a caller may pass a fixed bucket count directly to
    ``build_paper_observation_report`` instead) but keeps the common case —
    "N-hour window sliced into fixed buckets" — from being reimplemented ad
    hoc at each call site.
    """
    if not 24 <= hours <= 72:
        raise PaperObservationError("observation window must be 24-72h per the roadmap AC")
    if bucket_hours <= 0:
        raise PaperObservationError("bucket_hours must be positive")
    return max(1, round(hours / bucket_hours))
