"""Durable run ledger for the agent-krypto orchestrator (#101).

Tracks one row per orchestrator run: run_id, phase, status, config version,
timestamps and result. This is the persistence layer behind idempotent retry,
single-run locking and resume-after-crash; it never talks to OKX or any
research module directly.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

RUN_STATUSES = ("PENDING", "RUNNING", "WAIT", "ERROR", "DONE")

_ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "PENDING": {"RUNNING", "ERROR"},
    "RUNNING": {"WAIT", "ERROR", "DONE"},
    "WAIT": {"RUNNING", "ERROR"},
    "ERROR": {"RUNNING"},
    "DONE": set(),
}

LOCK_STALE_AFTER_SECONDS = 900


class RunStoreError(ValueError):
    """Base error for the run store; every failure here is fail-closed."""


class IllegalRunTransitionError(RunStoreError):
    """Raised when a status transition is not in ``_ALLOWED_TRANSITIONS``."""


class RunFingerprintMismatchError(RunStoreError):
    """Raised when the same run_id is retried with a different request payload."""


class RunLockHeldError(RunStoreError):
    """Raised when another run for the same (phase, symbol) is RUNNING and fresh."""


class LeaseLostError(RunStoreError):
    """Raised when a caller's lease_token no longer matches the row's current
    lease — another owner has since reclaimed it. Never treat this as a
    successful write."""


def validate_run_transition(current: str, new_status: str) -> None:
    if new_status not in _ALLOWED_TRANSITIONS.get(current, set()):
        raise IllegalRunTransitionError(f"illegal run transition {current} -> {new_status}")


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _utc(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def request_fingerprint(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


class RunStore:
    """SQLite-backed run ledger. PRIMARY KEY (run_id) is the idempotency key,
    the same pattern as ``HoldoutClaimStore`` and the OKX execution ledger.
    """

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        try:
            # CREATE TABLE IF NOT EXISTS only applies its column list to a
            # brand-new table: a DB file left over from a version of this
            # store predating `lease_token` already has `orchestrator_runs`
            # without that column, and SQLite will not add it retroactively.
            # The explicit ALTER TABLE migration below is required for
            # existing durable run stores to keep working after this change,
            # not just fresh ones.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS orchestrator_runs (
                    run_id TEXT NOT NULL PRIMARY KEY,
                    phase TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    status TEXT NOT NULL,
                    config_version TEXT NOT NULL,
                    config_hash TEXT NOT NULL,
                    request_fingerprint TEXT NOT NULL,
                    lease_token TEXT,
                    result_json TEXT,
                    reason TEXT,
                    provider TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS orchestrator_run_history (
                    run_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    at TEXT NOT NULL,
                    reason TEXT,
                    FOREIGN KEY (run_id) REFERENCES orchestrator_runs(run_id)
                )
                """
            )
            existing_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(orchestrator_runs)")
            }
            if "lease_token" not in existing_columns:
                conn.execute("ALTER TABLE orchestrator_runs ADD COLUMN lease_token TEXT")
            if "provider" not in existing_columns:
                # #119: pure audit annotation (which of Claude/Codex proposed
                # this run's result). Never read by acquire()/transition()'s
                # transition-legality checks or by any PromotionPolicy/
                # execution-boundary code — adding it retroactively to an
                # existing DB file cannot change fail-closed behavior for
                # rows written before this column existed.
                conn.execute("ALTER TABLE orchestrator_runs ADD COLUMN provider TEXT")
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def create(
        self,
        *,
        run_id: str,
        phase: str,
        symbol: str,
        config_version: str,
        config_hash: str,
        request_fingerprint: str,
        provider: str | None = None,
        now: datetime | None = None,
    ) -> Mapping[str, Any]:
        now = now or datetime.now(timezone.utc)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM orchestrator_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if existing:
                if existing["request_fingerprint"] != request_fingerprint:
                    conn.execute("ROLLBACK")
                    raise RunFingerprintMismatchError(
                        f"run_id {run_id!r} already exists with a different request payload"
                    )
                conn.execute("COMMIT")
                return dict(existing)
            timestamp = _iso(now)
            conn.execute(
                """
                INSERT INTO orchestrator_runs
                    (run_id, phase, symbol, status, config_version, config_hash,
                     request_fingerprint, lease_token, result_json, reason, provider,
                     created_at, updated_at)
                VALUES (?, ?, ?, 'PENDING', ?, ?, ?, NULL, NULL, NULL, ?, ?, ?)
                """,
                (run_id, phase, symbol, config_version, config_hash, request_fingerprint,
                 provider, timestamp, timestamp),
            )
            conn.execute(
                "INSERT INTO orchestrator_run_history (run_id, status, at, reason) VALUES (?,?,?,?)",
                (run_id, "PENDING", timestamp, "created"),
            )
            conn.execute("COMMIT")
            return dict(conn.execute(
                "SELECT * FROM orchestrator_runs WHERE run_id=?", (run_id,)
            ).fetchone())
        finally:
            conn.close()

    def acquire(
        self,
        *,
        run_id: str,
        phase: str,
        symbol: str,
        stale_after_seconds: int = LOCK_STALE_AFTER_SECONDS,
        now: datetime | None = None,
    ) -> Mapping[str, Any]:
        """Move ``run_id`` into RUNNING under a fresh ``lease_token``, refusing
        if any other run for the same (phase, symbol) is RUNNING and not stale.

        Any stale RUNNING row (heartbeat older than ``stale_after_seconds``)
        blocking this acquisition is atomically moved to ERROR ("stale lock
        reclaimed") in the same transaction that grants the new lock —
        including ``run_id`` itself, when it is the crashed run retrying its
        own logical work. That self-reclaim (RUNNING -stale-> ERROR ->
        RUNNING, all inside this one transaction) is what makes a bare retry
        of the same run_id after a crash succeed without a second run first
        having to mark it ERROR by hand. A *fresh* RUNNING row — whether it
        belongs to ``run_id`` or another run — always blocks acquisition, and
        every other RUNNING row for (phase, symbol) is checked, not just one.

        Every acquisition — including a self-reclaim of the caller's own
        run_id — mints a brand-new ``lease_token``. The old token (held by
        whatever process last acquired the lock, possibly a crashed one that
        is about to resume) stops matching, so ``heartbeat``/``transition``
        calls made under it are rejected rather than silently mutating a
        lease this call has already taken over.
        """
        now = now or datetime.now(timezone.utc)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM orchestrator_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                raise RunStoreError(f"unknown run_id {run_id!r}; call create() first")

            blockers = conn.execute(
                """
                SELECT * FROM orchestrator_runs
                WHERE phase=? AND symbol=? AND status='RUNNING'
                """,
                (phase, symbol),
            ).fetchall()
            timestamp = _iso(now)
            stale_ids: list[str] = []
            for blocker in blockers:
                age = (now - _utc(blocker["updated_at"])).total_seconds()
                if age < stale_after_seconds:
                    conn.execute("ROLLBACK")
                    if blocker["run_id"] == run_id:
                        raise RunLockHeldError(
                            f"run {run_id!r} is already RUNNING and its lease is fresh"
                        )
                    raise RunLockHeldError(
                        f"run {blocker['run_id']!r} is RUNNING for ({phase}, {symbol})"
                    )
                stale_ids.append(blocker["run_id"])

            for stale_id in stale_ids:
                conn.execute(
                    "UPDATE orchestrator_runs SET status='ERROR', reason=?, "
                    "lease_token=NULL, updated_at=? WHERE run_id=?",
                    ("stale lock reclaimed", timestamp, stale_id),
                )
                conn.execute(
                    "INSERT INTO orchestrator_run_history (run_id, status, at, reason) "
                    "VALUES (?,?,?,?)",
                    (stale_id, "ERROR", timestamp, "stale lock reclaimed"),
                )

            # If run_id was itself just reclaimed above (self stale-retry),
            # its in-transaction status is now ERROR -> RUNNING is legal. If
            # it was never RUNNING to begin with, use its pre-transaction
            # status from `row`.
            effective_current = "ERROR" if row["run_id"] in stale_ids else row["status"]
            validate_run_transition(effective_current, "RUNNING")
            lease_token = uuid.uuid4().hex
            conn.execute(
                "UPDATE orchestrator_runs SET status='RUNNING', lease_token=?, "
                "updated_at=? WHERE run_id=?",
                (lease_token, timestamp, run_id),
            )
            conn.execute(
                "INSERT INTO orchestrator_run_history (run_id, status, at, reason) VALUES (?,?,?,?)",
                (run_id, "RUNNING", timestamp, "lock acquired"),
            )
            conn.execute("COMMIT")
            return dict(conn.execute(
                "SELECT * FROM orchestrator_runs WHERE run_id=?", (run_id,)
            ).fetchone())
        finally:
            conn.close()

    def heartbeat(self, *, run_id: str, lease_token: str, now: datetime | None = None) -> bool:
        """Refresh the lease. Returns False if the row is no longer RUNNING
        under this exact ``lease_token`` — either lost to another owner via a
        stale-reclaim, or the run reached a terminal status — so the caller
        can react instead of silently continuing to hold a lock it no longer
        owns.
        """
        now = now or datetime.now(timezone.utc)
        conn = self._connect()
        try:
            cursor = conn.execute(
                "UPDATE orchestrator_runs SET updated_at=? "
                "WHERE run_id=? AND status='RUNNING' AND lease_token=?",
                (_iso(now), run_id, lease_token),
            )
            return cursor.rowcount > 0
        finally:
            conn.close()

    def transition(
        self,
        *,
        run_id: str,
        new_status: str,
        reason: str,
        result: Mapping[str, Any] | None = None,
        lease_token: str | None = None,
        now: datetime | None = None,
    ) -> Mapping[str, Any]:
        """Move ``run_id`` to ``new_status``.

        ``lease_token`` is required for any transition *out of* RUNNING
        (WAIT/ERROR/DONE) — the caller must prove it still holds the lease it
        was granted by ``acquire()``. If another owner has since reclaimed a
        stale lock, the token no longer matches and this raises
        ``LeaseLostError`` instead of persisting a result over a lease this
        caller no longer owns. Transitions that do not originate from
        RUNNING (e.g. PENDING -> ERROR before any lock was ever acquired)
        have no lease to check and may omit it.
        """
        now = now or datetime.now(timezone.utc)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM orchestrator_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                raise RunStoreError(f"unknown run_id {run_id!r}")
            if row["status"] == "RUNNING" and row["lease_token"] != lease_token:
                conn.execute("ROLLBACK")
                raise LeaseLostError(
                    f"run_id {run_id!r} lease_token does not match its current lease; "
                    "another owner has reclaimed this run"
                )
            try:
                validate_run_transition(row["status"], new_status)
            except IllegalRunTransitionError:
                conn.execute("ROLLBACK")
                raise
            timestamp = _iso(now)
            result_json = json.dumps(dict(result)) if result is not None else row["result_json"]
            new_lease_token = None if new_status != "RUNNING" else row["lease_token"]
            conn.execute(
                """
                UPDATE orchestrator_runs
                SET status=?, reason=?, result_json=?, lease_token=?, updated_at=?
                WHERE run_id=?
                """,
                (new_status, reason, result_json, new_lease_token, timestamp, run_id),
            )
            conn.execute(
                "INSERT INTO orchestrator_run_history (run_id, status, at, reason) VALUES (?,?,?,?)",
                (run_id, new_status, timestamp, reason),
            )
            conn.execute("COMMIT")
            return dict(conn.execute(
                "SELECT * FROM orchestrator_runs WHERE run_id=?", (run_id,)
            ).fetchone())
        finally:
            conn.close()

    def get(self, *, run_id: str) -> Mapping[str, Any] | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM orchestrator_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def find_resumable(self, *, phase: str, symbol: str) -> Mapping[str, Any] | None:
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT * FROM orchestrator_runs
                WHERE phase=? AND symbol=? AND status IN ('PENDING','RUNNING','WAIT')
                ORDER BY created_at LIMIT 1
                """,
                (phase, symbol),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def history(self, *, run_id: str) -> list[Mapping[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM orchestrator_run_history WHERE run_id=? ORDER BY at",
                (run_id,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()
