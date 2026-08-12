"""Champion/challenger registry for agent-krypto strategy promotion (#141).

This module never decides trades and never auto-promotes. It compares a
challenger's already-computed walk-forward + one-shot-holdout evaluation
(``StrategyArtifact.promotion_decision`` from ``crypto_strategy_research``)
against the currently registered champion for a symbol, and durably records
whichever promotion a human operator explicitly approves. Rollback restores
a prior champion without deleting history — the registry is append-only.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


class ChampionRegistryError(ValueError):
    """Raised for invalid comparisons, missing promotion decisions, or replay."""


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _require_passed_decision(payload: Mapping[str, Any], *, label: str) -> Mapping[str, Any]:
    decision = payload.get("promotion_decision")
    if not isinstance(decision, Mapping) or not decision:
        raise ChampionRegistryError(f"{label} artifact lacks a promotion_decision")
    if not decision.get("passed"):
        raise ChampionRegistryError(f"{label} artifact promotion_decision did not pass")
    if decision.get("failures"):
        raise ChampionRegistryError(f"{label} artifact promotion_decision has recorded failures")
    if not payload.get("holdout_evaluated"):
        raise ChampionRegistryError(f"{label} artifact has not completed one-shot holdout evaluation")
    return decision


def _artifact_metric(payload: Mapping[str, Any], symbol: str) -> dict[str, Any]:
    holdout = payload.get("holdout_result")
    if not isinstance(holdout, Mapping) or not holdout:
        raise ChampionRegistryError("artifact holdout_result is missing")
    if payload.get("symbol") != symbol:
        raise ChampionRegistryError(
            f"artifact symbol {payload.get('symbol')!r} does not match requested symbol {symbol!r}"
        )
    return dict(holdout)


@dataclass(frozen=True)
class ComparisonResult:
    """Outcome of comparing a challenger against the current champion.

    ``challenger_wins`` reflects only the numeric comparison on cost-adjusted
    holdout expectancy (ties favor the incumbent champion — a challenger must
    strictly improve to be worth the operational risk of a switch). It is
    advisory only: nothing is promoted until ``ChampionRegistry.promote`` is
    called with an explicit human approval.
    """

    symbol: str
    challenger_strategy_version: str
    champion_strategy_version: str | None
    challenger_holdout_expectancy: float
    champion_holdout_expectancy: float | None
    challenger_wins: bool
    reason: str


def compare_to_champion(
    *,
    symbol: str,
    challenger_artifact: Mapping[str, Any],
    champion_artifact: Mapping[str, Any] | None,
) -> ComparisonResult:
    """Compare a challenger's evaluated artifact against the incumbent champion.

    Both artifacts (when the champion exists) must already carry a passing
    ``promotion_decision`` and a completed one-shot holdout — this function
    does not run walk-forward or holdout evaluation itself; that remains
    ``StrategyArtifact``'s (crypto_strategy_research.py) responsibility so the
    holdout stays one-shot and cannot be re-run to search for a better result.
    """
    challenger_decision = _require_passed_decision(challenger_artifact, label="challenger")
    challenger_metrics = _artifact_metric(challenger_artifact, symbol)
    challenger_expectancy = float(challenger_metrics.get("expectancy", 0.0))

    if champion_artifact is None:
        return ComparisonResult(
            symbol=symbol,
            challenger_strategy_version=str(challenger_artifact["strategy_version"]),
            champion_strategy_version=None,
            challenger_holdout_expectancy=challenger_expectancy,
            champion_holdout_expectancy=None,
            challenger_wins=True,
            reason="no incumbent champion for this symbol",
        )

    _require_passed_decision(champion_artifact, label="champion")
    champion_metrics = _artifact_metric(champion_artifact, symbol)
    champion_expectancy = float(champion_metrics.get("expectancy", 0.0))

    if challenger_artifact["strategy_version"] == champion_artifact["strategy_version"]:
        raise ChampionRegistryError("challenger and champion have the same strategy_version")

    wins = challenger_expectancy > champion_expectancy
    reason = (
        "challenger holdout expectancy exceeds champion"
        if wins
        else "challenger does not exceed champion holdout expectancy"
    )
    del challenger_decision  # validated above; not otherwise consumed here
    return ComparisonResult(
        symbol=symbol,
        challenger_strategy_version=str(challenger_artifact["strategy_version"]),
        champion_strategy_version=str(champion_artifact["strategy_version"]),
        challenger_holdout_expectancy=challenger_expectancy,
        champion_holdout_expectancy=champion_expectancy,
        challenger_wins=wins,
        reason=reason,
    )


class ChampionRegistry:
    """Sqlite-backed, append-only ledger of champions and promotion history.

    One current champion per ``symbol`` (``current_champions`` is a
    single-row-per-symbol table, updated only inside a transaction that also
    appends to the immutable ``promotion_history`` log) so ``rollback`` can
    always reconstruct "who was champion before this promotion" without
    losing the record of the promotion that is being rolled back.
    """

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, isolation_level=None)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS current_champions (
                    symbol TEXT NOT NULL PRIMARY KEY,
                    strategy_version TEXT NOT NULL,
                    artifact_json TEXT NOT NULL,
                    promoted_at TEXT NOT NULL,
                    approved_by TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS promotion_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    strategy_version TEXT NOT NULL,
                    previous_strategy_version TEXT,
                    artifact_json TEXT NOT NULL,
                    comparison_json TEXT,
                    approved_by TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    at TEXT NOT NULL
                )
                """
            )
        finally:
            conn.close()

    def current_champion(self, *, symbol: str) -> dict[str, Any] | None:
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT artifact_json FROM current_champions WHERE symbol = ?",
                (symbol,),
            ).fetchone()
        finally:
            conn.close()
        return json.loads(row[0]) if row else None

    def promote(
        self,
        *,
        comparison: ComparisonResult,
        challenger_artifact: Mapping[str, Any],
        approved_by: str,
    ) -> None:
        """Durably record a manually approved promotion. Never called automatically.

        Rejects outright if ``comparison.challenger_wins`` is false (fail-closed:
        a human cannot accidentally promote a losing challenger through this
        path) and if the artifact passed to ``promote`` does not match the one
        that was compared.
        """
        if not approved_by or not approved_by.strip():
            raise ChampionRegistryError("approved_by is required for a manual promotion")
        if not comparison.challenger_wins:
            raise ChampionRegistryError(
                "cannot promote a challenger that did not win the comparison "
                f"({comparison.reason})"
            )
        if str(challenger_artifact["strategy_version"]) != comparison.challenger_strategy_version:
            raise ChampionRegistryError(
                "challenger_artifact does not match the strategy_version that was compared"
            )
        _require_passed_decision(challenger_artifact, label="challenger")

        conn = sqlite3.connect(self.db_path, isolation_level=None)
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                existing = conn.execute(
                    "SELECT strategy_version FROM current_champions WHERE symbol = ?",
                    (comparison.symbol,),
                ).fetchone()
                existing_version = existing[0] if existing else None
                if existing_version != comparison.champion_strategy_version:
                    conn.execute("ROLLBACK")
                    raise ChampionRegistryError(
                        "current champion changed since comparison was computed; "
                        "re-run compare_to_champion before promoting"
                    )
                now = _iso_now()
                artifact_json = json.dumps(dict(challenger_artifact), sort_keys=True, default=str)
                conn.execute(
                    """
                    INSERT INTO current_champions
                        (symbol, strategy_version, artifact_json, promoted_at, approved_by)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(symbol) DO UPDATE SET
                        strategy_version = excluded.strategy_version,
                        artifact_json = excluded.artifact_json,
                        promoted_at = excluded.promoted_at,
                        approved_by = excluded.approved_by
                    """,
                    (
                        comparison.symbol,
                        comparison.challenger_strategy_version,
                        artifact_json,
                        now,
                        approved_by,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO promotion_history
                        (symbol, event_type, strategy_version, previous_strategy_version,
                         artifact_json, comparison_json, approved_by, reason, at)
                    VALUES (?, 'PROMOTE', ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        comparison.symbol,
                        comparison.challenger_strategy_version,
                        existing_version,
                        artifact_json,
                        json.dumps(
                            {
                                "challenger_holdout_expectancy": comparison.challenger_holdout_expectancy,
                                "champion_holdout_expectancy": comparison.champion_holdout_expectancy,
                                "reason": comparison.reason,
                            },
                            sort_keys=True,
                        ),
                        approved_by,
                        comparison.reason,
                        now,
                    ),
                )
                conn.execute("COMMIT")
            except sqlite3.Error:
                conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()

    def rollback(self, *, symbol: str, approved_by: str, reason: str) -> dict[str, Any]:
        """Restore the previous champion recorded in ``promotion_history``.

        Finds the most recent ``PROMOTE`` event for ``symbol`` and restores
        its ``previous_strategy_version``'s artifact as champion again. Raises
        if there is nothing to roll back to (no prior champion on record) —
        an operator must not be able to roll back into an undefined state.
        """
        if not approved_by or not approved_by.strip():
            raise ChampionRegistryError("approved_by is required for a rollback")
        if not reason or not reason.strip():
            raise ChampionRegistryError("reason is required for a rollback")

        conn = sqlite3.connect(self.db_path, isolation_level=None)
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                last_promote = conn.execute(
                    """
                    SELECT previous_strategy_version FROM promotion_history
                    WHERE symbol = ? AND event_type = 'PROMOTE'
                    ORDER BY id DESC LIMIT 1
                    """,
                    (symbol,),
                ).fetchone()
                if last_promote is None or last_promote[0] is None:
                    conn.execute("ROLLBACK")
                    raise ChampionRegistryError(
                        f"no prior champion recorded for symbol={symbol!r}; nothing to roll back to"
                    )
                previous_version = last_promote[0]
                previous_row = conn.execute(
                    """
                    SELECT artifact_json FROM promotion_history
                    WHERE symbol = ? AND strategy_version = ?
                    ORDER BY id DESC LIMIT 1
                    """,
                    (symbol, previous_version),
                ).fetchone()
                if previous_row is None:
                    conn.execute("ROLLBACK")
                    raise ChampionRegistryError(
                        f"previous champion artifact for {previous_version!r} is missing from history"
                    )
                current = conn.execute(
                    "SELECT strategy_version FROM current_champions WHERE symbol = ?",
                    (symbol,),
                ).fetchone()
                current_version = current[0] if current else None
                artifact_json = previous_row[0]
                now = _iso_now()
                conn.execute(
                    """
                    INSERT INTO current_champions
                        (symbol, strategy_version, artifact_json, promoted_at, approved_by)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(symbol) DO UPDATE SET
                        strategy_version = excluded.strategy_version,
                        artifact_json = excluded.artifact_json,
                        promoted_at = excluded.promoted_at,
                        approved_by = excluded.approved_by
                    """,
                    (symbol, previous_version, artifact_json, now, approved_by),
                )
                conn.execute(
                    """
                    INSERT INTO promotion_history
                        (symbol, event_type, strategy_version, previous_strategy_version,
                         artifact_json, comparison_json, approved_by, reason, at)
                    VALUES (?, 'ROLLBACK', ?, ?, ?, NULL, ?, ?, ?)
                    """,
                    (symbol, previous_version, current_version, artifact_json, approved_by, reason, now),
                )
                conn.execute("COMMIT")
                return json.loads(artifact_json)
            except sqlite3.Error:
                conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()

    def history(self, *, symbol: str) -> list[dict[str, Any]]:
        """Full, ordered audit trail of promotions and rollbacks for a symbol."""
        conn = sqlite3.connect(self.db_path)
        try:
            rows = conn.execute(
                """
                SELECT event_type, strategy_version, previous_strategy_version,
                       comparison_json, approved_by, reason, at
                FROM promotion_history WHERE symbol = ? ORDER BY id ASC
                """,
                (symbol,),
            ).fetchall()
        finally:
            conn.close()
        return [
            {
                "event_type": row[0],
                "strategy_version": row[1],
                "previous_strategy_version": row[2],
                "comparison": json.loads(row[3]) if row[3] else None,
                "approved_by": row[4],
                "reason": row[5],
                "at": row[6],
            }
            for row in rows
        ]
