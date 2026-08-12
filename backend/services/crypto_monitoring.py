"""Data-quality and drift monitoring for agent-krypto (#143).

Wraps versioned thresholds around signals that already exist elsewhere in the
codebase — ``crypto_market_ingestion.readiness_errors`` (freshness/
completeness), ``paper_execution_ledger`` (PnL/errors) and feature Parquet
snapshots (drift) — and turns them into a single structured, fail-closed
``MonitoringReport``. This module never ingests data or executes trades; it
only reads what other modules already published and classifies it.
"""
from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

from services.crypto_market_ingestion import readiness_errors

OK = "OK"
WARNING = "WARNING"
CRITICAL = "CRITICAL"
_STATUS_RANK = {OK: 0, WARNING: 1, CRITICAL: 2}
_STATUSES = frozenset(_STATUS_RANK)


class MonitoringError(ValueError):
    """Raised for invalid thresholds or malformed monitoring input."""


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _worst(statuses: Sequence[str]) -> str:
    return max(statuses, key=lambda status: _STATUS_RANK[status]) if statuses else OK


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str
    detail: str

    def __post_init__(self) -> None:
        if self.status not in _STATUSES:
            raise MonitoringError(f"unknown status: {self.status}")

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "status": self.status, "detail": self.detail}


@dataclass(frozen=True)
class MonitoringThresholds:
    """Versioned thresholds; never hardcode these at a call site.

    ``max_age`` and ``warning_age`` govern freshness (also covers a stale
    Bitget feather package — ``check_data_quality`` reports it the same way
    as any other stale OHLCV source). ``max_drift_z`` is a z-score cutoff for
    per-feature distribution drift between a reference and current window.
    ``min_win_rate``/``max_consecutive_losses`` gate signal effectiveness and
    paper PnL health.
    """

    warning_age: timedelta
    max_age: timedelta
    max_drift_z: float = 3.0
    min_win_rate: float = 0.35
    max_consecutive_losses: int = 6
    max_error_rate: float = 0.05

    def __post_init__(self) -> None:
        if self.warning_age <= timedelta(0):
            raise MonitoringError("warning_age must be positive")
        if self.max_age <= self.warning_age:
            raise MonitoringError("max_age must exceed warning_age")
        if self.max_drift_z <= 0:
            raise MonitoringError("max_drift_z must be positive")
        if not 0 <= self.min_win_rate <= 1:
            raise MonitoringError("min_win_rate must be in [0, 1]")
        if self.max_consecutive_losses < 1:
            raise MonitoringError("max_consecutive_losses must be >= 1")
        if not 0 <= self.max_error_rate <= 1:
            raise MonitoringError("max_error_rate must be in [0, 1]")


@dataclass(frozen=True)
class MonitoringReport:
    schema_version: str
    generated_at: str
    status: str
    checks: tuple[CheckResult, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "generated_at": self.generated_at,
            "status": self.status,
            "checks": [check.to_dict() for check in self.checks],
        }


MONITORING_SCHEMA_VERSION = "monitoring.v1"


def check_data_quality(
    records: Sequence[Mapping[str, Any]],
    *,
    as_of: str | datetime,
    thresholds: MonitoringThresholds,
    required_symbols: Sequence[str],
    required_timeframes: Sequence[str],
    required_data_kinds: Sequence[str],
) -> CheckResult:
    """Freshness/completeness/latency, fail-closed on any hard readiness error.

    Reuses ``readiness_errors`` (missing/stale OHLCV or data kinds) as the
    CRITICAL gate — an empty or stale Bitget feather package surfaces here as
    a "missing ohlcv"/"stale ohlcv" entry exactly like any other source would,
    so there is no separate code path to bypass. A softer, WARNING-level
    staleness (older than ``warning_age`` but within ``max_age``) is reported
    when the hard gate passes but freshness is degrading.
    """
    hard_errors = readiness_errors(
        records,
        as_of=as_of,
        max_age=thresholds.max_age,
        required_symbols=required_symbols,
        required_timeframes=required_timeframes,
        required_data_kinds=required_data_kinds,
    )
    if hard_errors:
        return CheckResult("data_quality", CRITICAL, "; ".join(hard_errors))

    soft_errors = readiness_errors(
        records,
        as_of=as_of,
        max_age=thresholds.warning_age,
        required_symbols=required_symbols,
        required_timeframes=required_timeframes,
        required_data_kinds=required_data_kinds,
    )
    if soft_errors:
        return CheckResult("data_quality", WARNING, "; ".join(soft_errors))
    return CheckResult("data_quality", OK, "all required symbols/timeframes/data kinds are fresh")


def check_feature_drift(
    *,
    reference: Mapping[str, Sequence[float]],
    current: Mapping[str, Sequence[float]],
    thresholds: MonitoringThresholds,
) -> CheckResult:
    """Per-feature mean-shift z-score between a reference and current window.

    ``reference``/``current`` map feature name -> values. A feature present
    in only one window is itself a CRITICAL anomaly (schema drift), not a
    numeric comparison. Empty inputs are fail-closed (CRITICAL), never
    silently treated as "no drift".
    """
    if not reference or not current:
        raise MonitoringError("reference and current feature windows must be non-empty")
    missing = (set(reference) ^ set(current))
    if missing:
        return CheckResult(
            "feature_drift", CRITICAL, f"feature set mismatch between windows: {sorted(missing)}"
        )
    worst_z = 0.0
    worst_feature = None
    for name, ref_values in reference.items():
        cur_values = current[name]
        if not ref_values or not cur_values:
            return CheckResult("feature_drift", CRITICAL, f"feature {name} has an empty window")
        ref_mean = sum(ref_values) / len(ref_values)
        ref_var = sum((v - ref_mean) ** 2 for v in ref_values) / len(ref_values)
        ref_std = math.sqrt(ref_var)
        cur_mean = sum(cur_values) / len(cur_values)
        z = abs(cur_mean - ref_mean) / ref_std if ref_std > 0 else (math.inf if cur_mean != ref_mean else 0.0)
        if z > worst_z:
            worst_z, worst_feature = z, name
    if worst_feature is None:
        return CheckResult("feature_drift", OK, "no feature drift detected")
    if worst_z > thresholds.max_drift_z:
        return CheckResult(
            "feature_drift", CRITICAL,
            f"feature {worst_feature} drifted {worst_z:.2f} std beyond reference mean",
        )
    if worst_z > thresholds.max_drift_z / 2:
        return CheckResult(
            "feature_drift", WARNING,
            f"feature {worst_feature} drifted {worst_z:.2f} std beyond reference mean",
        )
    return CheckResult("feature_drift", OK, "no feature drift detected")


def check_signal_effectiveness(
    *, closed_trade_pnls: Sequence[float], thresholds: MonitoringThresholds
) -> CheckResult:
    """Win rate and consecutive-loss streak from already-closed paper trades."""
    if not closed_trade_pnls:
        return CheckResult("signal_effectiveness", WARNING, "no closed trades to evaluate yet")
    wins = sum(1 for pnl in closed_trade_pnls if pnl > 0)
    win_rate = wins / len(closed_trade_pnls)
    streak = longest = 0
    for pnl in closed_trade_pnls:
        streak = streak + 1 if pnl <= 0 else 0
        longest = max(longest, streak)
    if longest >= thresholds.max_consecutive_losses:
        return CheckResult(
            "signal_effectiveness", CRITICAL,
            f"{longest} consecutive losing trades reached the {thresholds.max_consecutive_losses} limit",
        )
    if win_rate < thresholds.min_win_rate:
        return CheckResult(
            "signal_effectiveness", WARNING,
            f"win rate {win_rate:.2f} below minimum {thresholds.min_win_rate:.2f}",
        )
    return CheckResult("signal_effectiveness", OK, f"win rate {win_rate:.2f}, max streak {longest}")


def check_pnl_and_errors(
    *, total_pnl: float, error_count: int, decision_count: int, thresholds: MonitoringThresholds
) -> CheckResult:
    """Aggregate paper/live PnL health plus an operational error-rate gate."""
    if decision_count <= 0:
        raise MonitoringError("decision_count must be positive")
    error_rate = error_count / decision_count
    if error_rate > thresholds.max_error_rate:
        return CheckResult(
            "pnl_and_errors", CRITICAL,
            f"error rate {error_rate:.2%} exceeds limit {thresholds.max_error_rate:.2%}",
        )
    if total_pnl < 0:
        return CheckResult("pnl_and_errors", WARNING, f"cumulative PnL is negative: {total_pnl:.6f}")
    return CheckResult("pnl_and_errors", OK, f"cumulative PnL {total_pnl:.6f}, error rate {error_rate:.2%}")


def build_monitoring_report(checks: Sequence[CheckResult]) -> MonitoringReport:
    if not checks:
        raise MonitoringError("at least one check is required for a monitoring report")
    return MonitoringReport(
        schema_version=MONITORING_SCHEMA_VERSION,
        generated_at=_iso_now(),
        status=_worst([check.status for check in checks]),
        checks=tuple(checks),
    )


def paper_pnl_and_error_inputs(
    conn: sqlite3.Connection, *, run_id: str, error_count: int = 0
) -> dict[str, Any]:
    """Adapter: pull the numbers ``check_pnl_and_errors``/``check_signal_effectiveness``
    need directly from ``paper_execution_ledger`` for one run_id.
    """
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM paper_execution_ledger WHERE run_id = ? ORDER BY id ASC", (run_id,)
    ).fetchall()
    if not rows:
        raise MonitoringError(f"no paper_execution_ledger rows for run_id={run_id!r}")
    closed = [row for row in rows if row["decision"] in {"CLOSE", "REDUCE"} and row["pnl"] is not None]
    return {
        "total_pnl": sum(float(row["pnl"]) for row in closed),
        "closed_trade_pnls": [float(row["pnl"]) for row in closed],
        "decision_count": len(rows),
        "error_count": error_count,
    }
