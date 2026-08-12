"""Structured insights and paper-feedback layer for agent-krypto (#142).

After a trial (``ExperimentRunner`` result, ``crypto_experiment_runner.py``)
or a paper cycle (``paper_execution_ledger``, ``paper_execution.py``) is
evaluated, this module turns the raw numbers into a structured report: facts,
anomalies, rejection reasons, a recommendation and a proposed next
``LearningRequest``. It never mutates a model, never re-runs an experiment,
and never calls ``ChampionRegistry.promote``/``rollback`` — the report is
read-only advisory output that a human or a separate research loop consumes.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


class InsightsError(ValueError):
    """Raised for malformed trial/paper input; this module is fail-closed."""


INSIGHTS_SCHEMA_VERSION = "insights.v1"


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class Fact:
    label: str
    value: Any


@dataclass(frozen=True)
class Anomaly:
    label: str
    detail: str
    severity: str  # "info" | "warning" | "critical"

    def __post_init__(self) -> None:
        if self.severity not in {"info", "warning", "critical"}:
            raise InsightsError(f"unknown anomaly severity: {self.severity}")


@dataclass(frozen=True)
class NextExperimentProposal:
    """A proposed ``LearningRequest``-shaped follow-up. Never auto-submitted."""

    request_id: str
    base_dataset_version: str
    requested_by: str
    symbols: tuple[str, ...]
    hypothesis: str
    features: tuple[str, ...]

    def to_learning_request(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "base_dataset_version": self.base_dataset_version,
            "requested_by": self.requested_by,
            "symbols": list(self.symbols),
            "hypothesis": self.hypothesis,
            "features": list(self.features),
        }


@dataclass(frozen=True)
class InsightReport:
    schema_version: str
    run_id: str
    generated_at: str
    source: str  # "trial" | "paper"
    facts: tuple[Fact, ...]
    anomalies: tuple[Anomaly, ...]
    rejection_reasons: tuple[str, ...]
    recommendation: str
    next_experiment: NextExperimentProposal | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "generated_at": self.generated_at,
            "source": self.source,
            "facts": [{"label": f.label, "value": f.value} for f in self.facts],
            "anomalies": [
                {"label": a.label, "detail": a.detail, "severity": a.severity}
                for a in self.anomalies
            ],
            "rejection_reasons": list(self.rejection_reasons),
            "recommendation": self.recommendation,
            "next_experiment": (
                self.next_experiment.to_learning_request() if self.next_experiment else None
            ),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n"

    def to_markdown(self) -> str:
        lines = [
            f"# Insight report — {self.run_id}",
            "",
            f"- schema_version: `{self.schema_version}`",
            f"- source: `{self.source}`",
            f"- generated_at: `{self.generated_at}`",
            "",
            "## Facts",
            "",
        ]
        for fact in self.facts:
            lines.append(f"- **{fact.label}**: {fact.value}")
        lines += ["", "## Anomalies", ""]
        if self.anomalies:
            for anomaly in self.anomalies:
                lines.append(f"- [{anomaly.severity}] **{anomaly.label}**: {anomaly.detail}")
        else:
            lines.append("- none")
        lines += ["", "## Rejection reasons", ""]
        if self.rejection_reasons:
            for reason in self.rejection_reasons:
                lines.append(f"- {reason}")
        else:
            lines.append("- none (trial accepted / no rejection)")
        lines += ["", "## Recommendation", "", self.recommendation, ""]
        lines += ["## Proposed next experiment", ""]
        if self.next_experiment:
            request = self.next_experiment.to_learning_request()
            lines.append("```json")
            lines.append(json.dumps(request, indent=2, sort_keys=True))
            lines.append("```")
        else:
            lines.append("- none proposed")
        return "\n".join(lines) + "\n"


def _fold_expectancies(fold_metrics: Sequence[Mapping[str, Any]]) -> list[float]:
    return [float(fold.get("expectancy", 0.0)) for fold in fold_metrics]


ESCALATION_STREAK_THRESHOLD = 3

# Signal features considered "exhausted" together once one of them has
# repeatedly failed with a critical anomaly — proposing another member of
# the same family is not a fundamentally different hypothesis.
_SIGNAL_FEATURE_FAMILIES: tuple[tuple[str, ...], ...] = (
    ("bb_percent_b", "bb_upper", "bb_lower", "bb_mid", "bb_width"),
    ("close", "open", "high", "low"),
)


def _feature_family(feature: str) -> tuple[str, ...]:
    for family in _SIGNAL_FEATURE_FAMILIES:
        if feature in family:
            return family
    return (feature,)


def _repeated_critical_failure_streak(
    history: Sequence[Mapping[str, Any]], *, signal_feature: str, symbol: str | None
) -> int:
    """Count consecutive most-recent rejected trials (same symbol, same
    signal_feature family) that carry a critical anomaly.

    ``history`` is expected newest-first: prior ``InsightReport.to_dict()``
    payloads (or an equivalent mapping with ``facts``/``anomalies``) for the
    same symbol, oldest evidence first is not required — only a prefix of
    consecutive matches from the start of the sequence is counted, so caller
    order matters.
    """
    family = _feature_family(signal_feature)
    streak = 0
    for entry in history:
        facts = {f["label"]: f["value"] for f in entry.get("facts", [])}
        if symbol is not None and facts.get("symbol") not in (None, symbol):
            continue
        params = facts.get("params") or {}
        entry_feature = params.get("signal_feature")
        if entry_feature not in family:
            break
        anomalies = entry.get("anomalies", [])
        has_critical = any(a.get("severity") == "critical" for a in anomalies)
        if not has_critical:
            break
        streak += 1
    return streak


def build_trial_insight_report(
    trial_result: Mapping[str, Any],
    *,
    history: Sequence[Mapping[str, Any]] = (),
    available_signal_features: Sequence[str] = (),
) -> InsightReport:
    """Build a report from one ``ExperimentRunner.run`` result (trial JSON).

    Reads only already-computed fields (``accepted``, ``reason``,
    ``fold_metrics``, ``bootstrap_ci``, ``lineage``) — it never touches the
    holdout partition or re-derives metrics itself.

    ``history`` (optional, newest-first) is a sequence of prior
    ``InsightReport.to_dict()`` payloads for the same symbol/dataset lineage.
    When the current trial's ``signal_feature`` (or a close relative in the
    same family, e.g. other Bollinger-derived columns) has failed with a
    critical anomaly for ``ESCALATION_STREAK_THRESHOLD`` consecutive prior
    trials, ``next_experiment`` escalates to proposing a different
    ``signal_feature``/``model_variant`` instead of re-tuning
    ``lookback``/``threshold`` within the same exhausted family. Without
    ``history`` (the default), behavior is unchanged from before this
    parameter existed.
    """
    required = {"trial_id", "accepted", "lineage", "fold_metrics", "bootstrap_ci", "params"}
    missing = required - trial_result.keys()
    if missing:
        raise InsightsError(f"trial_result missing required fields: {sorted(missing)}")

    lineage = trial_result["lineage"]
    fold_metrics = trial_result["fold_metrics"]
    bootstrap_ci = trial_result["bootstrap_ci"]
    accepted = bool(trial_result["accepted"])
    reason = trial_result.get("reason")
    expectancies = _fold_expectancies(fold_metrics)
    positive_folds = sum(1 for value in expectancies if value > 0)
    symbol = str(next(iter(trial_result.get("symbol_metrics", {"?": None}))))

    facts = [
        Fact("trial_id", trial_result["trial_id"]),
        Fact("dataset_version", lineage.get("dataset_version")),
        Fact("strategy_version", lineage.get("strategy_version")),
        Fact("seed", lineage.get("seed")),
        Fact("accepted", accepted),
        Fact("symbol", symbol),
        Fact("fold_count", len(fold_metrics)),
        Fact("positive_fold_fraction", positive_folds / len(fold_metrics) if fold_metrics else 0.0),
        Fact("bootstrap_ci_lower", bootstrap_ci.get("lower")),
        Fact("bootstrap_ci_upper", bootstrap_ci.get("upper")),
        Fact("params", dict(trial_result["params"])),
    ]

    anomalies: list[Anomaly] = []
    if fold_metrics and positive_folds == 0:
        anomalies.append(
            Anomaly("all_folds_negative", "every fold has non-positive expectancy", "critical")
        )
    if expectancies and (max(expectancies) - min(expectancies)) > 3 * abs(
        sum(expectancies) / len(expectancies) or 1.0
    ):
        anomalies.append(
            Anomaly(
                "high_fold_dispersion",
                "fold expectancy spread is large relative to the mean; result may be regime-dependent",
                "warning",
            )
        )
    if bootstrap_ci.get("lower", 0) <= 0 <= bootstrap_ci.get("upper", 0):
        anomalies.append(
            Anomaly(
                "bootstrap_ci_straddles_zero",
                "the 95% bootstrap CI includes zero; edge is not statistically distinguished from noise",
                "warning",
            )
        )
    if trial_result.get("lineage", {}).get("holdout", {}).get("accessed"):
        anomalies.append(
            Anomaly(
                "holdout_accessed_in_trial",
                "trial result reports holdout access, which must never happen outside FINAL_EVALUATION",
                "critical",
            )
        )

    rejection_reasons = [] if accepted else [str(reason)] if reason else ["rejected without a recorded reason"]

    if accepted:
        recommendation = (
            "Trial passed cost-adjusted walk-forward gates; proceed to champion/challenger "
            "comparison (compare_to_champion) before any promotion."
        )
    elif any(a.severity == "critical" for a in anomalies):
        recommendation = (
            "Do not iterate on this configuration blindly; investigate the critical anomaly "
            "before spending further trial budget."
        )
    else:
        recommendation = (
            "Rejected on walk-forward gates; consider adjusting lookback/threshold within the "
            "approved search space or trying an alternative signal_feature."
        )

    next_experiment = None
    if not accepted:
        params = dict(trial_result["params"])
        current_feature = str(params.get("signal_feature", "bb_percent_b"))
        streak = _repeated_critical_failure_streak(
            history, signal_feature=current_feature, symbol=symbol
        )
        # this trial's own critical anomalies count toward the streak too
        if any(a.severity == "critical" for a in anomalies):
            streak += 1

        if streak >= ESCALATION_STREAK_THRESHOLD:
            exhausted_family = set(_feature_family(current_feature))
            alternatives = [
                f for f in available_signal_features if f not in exhausted_family
            ]
            if alternatives:
                proposed_features = tuple(alternatives)
                hypothesis = (
                    f"{current_feature} (and related features) has failed with a critical "
                    f"anomaly on {symbol} for {streak} consecutive trials; re-tuning "
                    "lookback/threshold within this family is unlikely to recover an edge — "
                    f"try a different signal_feature ({', '.join(proposed_features)}) or "
                    "an alternative model_variant instead"
                )
            else:
                proposed_features = (current_feature,)
                hypothesis = (
                    f"{current_feature} (and related features) has failed with a critical "
                    f"anomaly on {symbol} for {streak} consecutive trials, and no alternative "
                    "signal_feature is available in the current feature dataset; consider "
                    "requesting a new feature (e.g. from an external signal source) or a new "
                    "model_variant before spending further trial budget on this family"
                )
        else:
            proposed_features = (current_feature,)
            hypothesis = (
                f"Varying lookback/threshold around {params.get('lookback')}/"
                f"{params.get('threshold')} may recover a positive cost-adjusted edge"
            )

        next_experiment = NextExperimentProposal(
            request_id=f"learn-{trial_result['trial_id']}",
            base_dataset_version=str(lineage.get("dataset_version")),
            requested_by="agent-krypto-research",
            symbols=(symbol,),
            hypothesis=hypothesis,
            features=proposed_features,
        )

    return InsightReport(
        schema_version=INSIGHTS_SCHEMA_VERSION,
        run_id=str(trial_result["trial_id"]),
        generated_at=_iso_now(),
        source="trial",
        facts=tuple(facts),
        anomalies=tuple(anomalies),
        rejection_reasons=tuple(rejection_reasons),
        recommendation=recommendation,
        next_experiment=next_experiment,
    )


def build_paper_insight_report(
    conn: sqlite3.Connection, *, run_id: str
) -> InsightReport:
    """Build a report from one paper run's ledger rows (``paper_execution.py``).

    Reads only rows already committed to ``paper_execution_ledger`` for
    ``run_id``; never re-simulates trades and never touches an exchange.
    """
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM paper_execution_ledger WHERE run_id = ? ORDER BY id ASC", (run_id,)
    ).fetchall()
    if not rows:
        raise InsightsError(f"no paper_execution_ledger rows for run_id={run_id!r}")

    closed = [row for row in rows if row["decision"] in {"CLOSE", "REDUCE"} and row["pnl"] is not None]
    total_pnl = sum(float(row["pnl"]) for row in closed)
    total_fees = sum(float(row["fee"]) for row in rows)
    win_count = sum(1 for row in closed if float(row["pnl"]) > 0)
    symbols = sorted({row["symbol"] for row in rows})

    facts = [
        Fact("run_id", run_id),
        Fact("decision_count", len(rows)),
        Fact("closed_trade_count", len(closed)),
        Fact("total_pnl", total_pnl),
        Fact("total_fees", total_fees),
        Fact("win_rate", win_count / len(closed) if closed else None),
        Fact("symbols", symbols),
        Fact(
            "model_version",
            rows[0]["model_version"] if rows else None,
        ),
    ]

    anomalies: list[Anomaly] = []
    if closed and total_pnl <= 0:
        anomalies.append(
            Anomaly("non_positive_paper_pnl", "cumulative paper PnL is not positive for this run", "warning")
        )
    open_positions = [
        row for row in rows if row["decision"] == "OPEN" and row["pnl"] is None
    ]
    if open_positions:
        anomalies.append(
            Anomaly(
                "unclosed_positions_at_report_time",
                f"{len(open_positions)} OPEN decision(s) have no matching CLOSE yet",
                "info",
            )
        )
    dataset_versions = {row["dataset_version"] for row in rows}
    if len(dataset_versions) > 1:
        anomalies.append(
            Anomaly(
                "dataset_version_drift_within_run",
                f"run mixes dataset_versions {sorted(dataset_versions)}",
                "critical",
            )
        )

    rejection_reasons: tuple[str, ...] = ()
    if closed and total_pnl <= 0:
        recommendation = (
            "Paper PnL is not positive; do not promote this strategy_version to demo. "
            "Feed the losing symbols/regimes back into the candidate generator's next search."
        )
    elif not closed:
        recommendation = "No closed trades yet; continue observing before drawing a conclusion."
    else:
        recommendation = (
            "Paper PnL is positive across observed cycles; continue multi-cycle observation "
            "per the roadmap before considering a manual promotion to demo."
        )

    next_experiment = None
    if closed and total_pnl <= 0 and symbols:
        next_experiment = NextExperimentProposal(
            request_id=f"learn-paper-{run_id}",
            base_dataset_version=str(rows[0]["dataset_version"]),
            requested_by="agent-krypto-research",
            symbols=tuple(symbols),
            hypothesis=(
                "Paper execution underperformed on these symbols; re-examine lookback/threshold "
                "and consider an alternative signal_feature or model_variant"
            ),
            features=("bb_percent_b",),
        )

    return InsightReport(
        schema_version=INSIGHTS_SCHEMA_VERSION,
        run_id=run_id,
        generated_at=_iso_now(),
        source="paper",
        facts=tuple(facts),
        anomalies=tuple(anomalies),
        rejection_reasons=rejection_reasons,
        recommendation=recommendation,
        next_experiment=next_experiment,
    )


class InsightReportStore:
    """Append-only, content-addressed persistence keyed by ``run_id``.

    A second report for the same ``run_id`` is rejected outright: insights
    are a point-in-time read of already-committed trial/paper data, so
    re-generating one for the same run must never silently overwrite the
    first (that would let a later, different report replace the audit trail
    a promotion/rollback decision may already have been based on).
    """

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, isolation_level=None)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS insight_reports (
                    run_id TEXT NOT NULL PRIMARY KEY,
                    source TEXT NOT NULL,
                    report_json TEXT NOT NULL,
                    generated_at TEXT NOT NULL
                )
                """
            )
        finally:
            conn.close()

    def save(self, report: InsightReport) -> None:
        conn = sqlite3.connect(self.db_path, isolation_level=None)
        try:
            try:
                conn.execute(
                    "INSERT INTO insight_reports (run_id, source, report_json, generated_at) "
                    "VALUES (?, ?, ?, ?)",
                    (report.run_id, report.source, report.to_json(), report.generated_at),
                )
            except sqlite3.IntegrityError as exc:
                raise InsightsError(
                    f"insight report already recorded for run_id={report.run_id!r}"
                ) from exc
        finally:
            conn.close()

    def get(self, *, run_id: str) -> dict[str, Any] | None:
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT report_json FROM insight_reports WHERE run_id = ?", (run_id,)
            ).fetchone()
        finally:
            conn.close()
        return json.loads(row[0]) if row else None

    def list_recent(
        self, *, symbol: str, source: str = "trial", limit: int = 20
    ) -> list[dict[str, Any]]:
        """Newest-first trial reports for ``symbol``, for escalation history.

        Filters in Python (not SQL) on the ``symbol`` fact embedded in each
        report's JSON payload — the table has no dedicated ``symbol`` column,
        and this method is a read-only convenience over the same
        append-only data ``get()`` already exposes, not a new persistence
        path.
        """
        conn = sqlite3.connect(self.db_path)
        try:
            rows = conn.execute(
                "SELECT report_json FROM insight_reports WHERE source = ? "
                "ORDER BY generated_at DESC",
                (source,),
            ).fetchall()
        finally:
            conn.close()

        matches: list[dict[str, Any]] = []
        for (report_json,) in rows:
            payload = json.loads(report_json)
            facts = {f["label"]: f["value"] for f in payload.get("facts", [])}
            if facts.get("symbol") == symbol:
                matches.append(payload)
            if len(matches) >= limit:
                break
        return matches
