"""Durable candidate cursor for the candidate-driven experiment cycle (#148).

Tracks, per ``(search_space_version, seed)``, the next unused ``trial_index``
a ``CandidateGenerator`` (#140) should hand out — so a new process invoked
repeatedly (the future ``candidate-cycle`` CLI command, #149) advances through
the search space one candidate per invocation instead of always regenerating
the first one. This module never runs an experiment itself; it only decides
*which* candidate index is next and persists that decision durably (SQLite,
WAL), the same pattern as ``agent_krypto_run_store.RunStore``/
``crypto_strategy_research.HoldoutClaimStore``.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from services.crypto_candidate_generator import CandidateGeneratorError, SearchSpace


class CandidateCursorError(ValueError):
    """Raised for exhausted budgets, bucket replay conflicts, or bad input."""


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class CursorAdvanceResult:
    """One durable decision: which ``trial_index`` a caller should run.

    ``replayed`` is ``True`` when this result was NOT newly advanced — the
    same ``run_bucket`` was already reserved earlier (a retry within the same
    logical cycle), so the caller gets back the exact same ``trial_index`` it
    would have gotten the first time, never a new one.
    """

    search_space_version: str
    seed: int
    trial_index: int
    run_bucket: str
    replayed: bool


class CandidateCursor:
    """SQLite-backed, durable cursor: next ``trial_index`` per (search_space_version, seed).

    Two tables:
    - ``candidate_cursor``: one row per ``(search_space_version, seed)`` with
      the highest ``trial_index`` already reserved — the actual "position".
    - ``candidate_cursor_reservations``: one row per ``(search_space_version,
      seed, run_bucket)``, PRIMARY KEY on all three — this is what makes a
      retry with the same ``run_bucket`` idempotent (return the previously
      reserved index) instead of silently advancing the cursor twice for what
      is logically one cycle.
    """

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, isolation_level=None)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS candidate_cursor (
                    search_space_version TEXT NOT NULL,
                    seed INTEGER NOT NULL,
                    next_trial_index INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (search_space_version, seed)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS candidate_cursor_reservations (
                    search_space_version TEXT NOT NULL,
                    seed INTEGER NOT NULL,
                    run_bucket TEXT NOT NULL,
                    trial_index INTEGER NOT NULL,
                    reserved_at TEXT NOT NULL,
                    PRIMARY KEY (search_space_version, seed, run_bucket)
                )
                """
            )
        finally:
            conn.close()

    def advance(
        self, *, search_space: SearchSpace, seed: int, run_bucket: str
    ) -> CursorAdvanceResult:
        """Reserve and return the next ``trial_index`` for one logical cycle.

        Fail-closed when the search space is exhausted (``next_trial_index``
        would reach ``search_space.grid_size``): this never silently wraps
        back to index 0, which would make ``CandidateGenerator``/
        ``dedupe_against_registry`` (#140) re-emit an already-tried
        ``candidate_id``. The caller must mint a new ``seed`` or a new
        ``SearchSpace`` (a new ``search_space_version``) to keep going —
        that is an explicit operator decision, not something this cursor
        makes on its own.
        """
        if not run_bucket or not run_bucket.strip():
            raise CandidateCursorError("run_bucket is required")
        search_space_version = search_space.version
        grid_size = search_space.grid_size

        conn = sqlite3.connect(self.db_path, isolation_level=None)
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                existing_reservation = conn.execute(
                    "SELECT trial_index FROM candidate_cursor_reservations "
                    "WHERE search_space_version=? AND seed=? AND run_bucket=?",
                    (search_space_version, seed, run_bucket),
                ).fetchone()
                if existing_reservation is not None:
                    conn.execute("ROLLBACK")
                    return CursorAdvanceResult(
                        search_space_version=search_space_version,
                        seed=seed,
                        trial_index=existing_reservation[0],
                        run_bucket=run_bucket,
                        replayed=True,
                    )

                row = conn.execute(
                    "SELECT next_trial_index FROM candidate_cursor "
                    "WHERE search_space_version=? AND seed=?",
                    (search_space_version, seed),
                ).fetchone()
                next_index = row[0] if row is not None else 0

                if next_index >= grid_size:
                    conn.execute("ROLLBACK")
                    raise CandidateCursorError(
                        f"search space exhausted for search_space_version={search_space_version!r} "
                        f"seed={seed}: next_trial_index={next_index} >= grid_size={grid_size}; "
                        "mint a new seed or a new SearchSpace to continue"
                    )

                now = _iso_now()
                conn.execute(
                    "INSERT INTO candidate_cursor_reservations "
                    "(search_space_version, seed, run_bucket, trial_index, reserved_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (search_space_version, seed, run_bucket, next_index, now),
                )
                conn.execute(
                    """
                    INSERT INTO candidate_cursor (search_space_version, seed, next_trial_index, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(search_space_version, seed) DO UPDATE SET
                        next_trial_index = excluded.next_trial_index,
                        updated_at = excluded.updated_at
                    """,
                    (search_space_version, seed, next_index + 1, now),
                )
                conn.execute("COMMIT")
                return CursorAdvanceResult(
                    search_space_version=search_space_version,
                    seed=seed,
                    trial_index=next_index,
                    run_bucket=run_bucket,
                    replayed=False,
                )
            except sqlite3.Error:
                conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()

    def peek(self, *, search_space_version: str, seed: int) -> int:
        """Return the next ``trial_index`` that WOULD be reserved, without reserving it."""
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT next_trial_index FROM candidate_cursor "
                "WHERE search_space_version=? AND seed=?",
                (search_space_version, seed),
            ).fetchone()
        finally:
            conn.close()
        return row[0] if row is not None else 0
