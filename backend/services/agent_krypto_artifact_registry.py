"""Durable, atomic StrategyArtifact registry for the agent-krypto runtime."""

from __future__ import annotations

import json
import hashlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from services.crypto_strategy_research import (
    ArtifactRejected,
    HoldoutClaimStore,
    require_promoted_artifact,
)


class ArtifactRegistryError(ValueError):
    """A registry invariant or atomic promotion was rejected."""


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _digest(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), default=str
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


class StrategyArtifactRegistry:
    """SQLite registry with one active artifact per symbol.

    Artifact persistence, retirement of the previous active version, activation
    of the new version and audit rows share one ``BEGIN IMMEDIATE`` transaction.
    Runtime reads join the active pointer to the immutable artifact payload and
    then apply the live compatibility gate.
    """

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS strategy_artifacts (
                    artifact_hash TEXT PRIMARY KEY,
                    strategy_version TEXT NOT NULL UNIQUE,
                    symbol TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS active_strategy_artifacts (
                    symbol TEXT PRIMARY KEY,
                    artifact_hash TEXT NOT NULL UNIQUE,
                    activated_at TEXT NOT NULL,
                    FOREIGN KEY (artifact_hash) REFERENCES strategy_artifacts(artifact_hash)
                );
                CREATE TABLE IF NOT EXISTS strategy_artifact_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    artifact_hash TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    action TEXT NOT NULL,
                    from_status TEXT,
                    to_status TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    compatibility_json TEXT NOT NULL,
                    at TEXT NOT NULL
                );
                """
            )
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @staticmethod
    def _compatibility(payload: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "dataset_version": payload.get("dataset_version"),
            "feature_schema_version": payload.get("feature_schema_version"),
            "promotion_policy_version": payload.get("promotion_policy_version"),
        }

    def persist(self, artifact: Mapping[str, Any], *, reason: str) -> None:
        """Persist a non-active lifecycle state and its audit event."""
        payload = dict(artifact)
        required = {"artifact_hash", "strategy_version", "symbol", "status"}
        missing = required - payload.keys()
        if missing:
            raise ArtifactRegistryError(f"artifact missing fields: {sorted(missing)}")
        if payload["status"] == "PROMOTED":
            raise ArtifactRegistryError("PROMOTED artifacts must use activate()")
        now = _iso_now()
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT artifact_hash, status FROM strategy_artifacts WHERE strategy_version=?",
                (payload["strategy_version"],),
            ).fetchone()
            if existing and existing["artifact_hash"] != payload["artifact_hash"]:
                raise ArtifactRegistryError("strategy_version is already bound to another hash")
            previous = existing["status"] if existing else None
            conn.execute(
                """
                INSERT INTO strategy_artifacts
                    (artifact_hash,strategy_version,symbol,status,payload_json,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(artifact_hash) DO UPDATE SET
                    status=excluded.status,payload_json=excluded.payload_json,updated_at=excluded.updated_at
                """,
                (
                    payload["artifact_hash"], payload["strategy_version"], payload["symbol"],
                    payload["status"], encoded, payload.get("created_at", now), now,
                ),
            )
            conn.execute(
                """
                INSERT INTO strategy_artifact_audit
                    (artifact_hash,symbol,action,from_status,to_status,reason,compatibility_json,at)
                VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    payload["artifact_hash"], payload["symbol"], "PERSIST", previous,
                    payload["status"], reason,
                    json.dumps(self._compatibility(payload), sort_keys=True), now,
                ),
            )
            conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def activate(
        self,
        artifact: Mapping[str, Any],
        *,
        claim_store: HoldoutClaimStore,
        expected_dataset_version: str,
        expected_feature_schema_version: str,
        expected_promotion_policy_version: str,
        reason: str,
    ) -> None:
        """Atomically validate, persist and make one artifact active."""
        payload = require_promoted_artifact(
            artifact,
            symbol=str(artifact.get("symbol")),
            expected_dataset_version=expected_dataset_version,
            expected_feature_schema_version=expected_feature_schema_version,
            expected_promotion_policy_version=expected_promotion_policy_version,
        )
        fold_metrics = artifact.get("fold_metrics")
        decision = artifact.get("promotion_decision") or {}
        if not isinstance(fold_metrics, list) or not fold_metrics:
            raise ArtifactRejected("artifact has no fold_metrics evidence")
        required_fold_fields = {
            "expectancy", "max_drawdown", "profit_factor", "trade_count"
        }
        if any(
            not isinstance(fold, Mapping)
            or not required_fold_fields.issubset(fold)
            for fold in fold_metrics
        ):
            raise ArtifactRejected("artifact fold_metrics evidence is incomplete")
        if (
            decision.get("fold_count") != len(fold_metrics)
            or decision.get("fold_count") != artifact.get("n_folds")
            or decision.get("fold_metrics_hash") != _digest(fold_metrics)
        ):
            raise ArtifactRejected("promotion decision does not match fold_metrics evidence")
        measured_oos_trades = sum(int(fold["trade_count"]) for fold in fold_metrics)
        if (
            measured_oos_trades <= 0
            or decision.get("oos_trade_count") != measured_oos_trades
            or decision.get("costs_included") is not True
        ):
            raise ArtifactRejected(
                "promotion decision has inconsistent OOS trade/cost evidence"
            )
        if decision.get("artifact_policy_version") != artifact.get(
            "promotion_policy_version"
        ):
            raise ArtifactRejected("promotion decision policy compatibility is inconsistent")
        holdout_result = artifact.get("holdout_result")
        if (
            not isinstance(holdout_result, Mapping)
            or decision.get("holdout_result_hash") != _digest(holdout_result)
        ):
            raise ArtifactRejected("promotion decision does not match holdout evidence")
        claim = claim_store.get_claim(
            strategy_version=str(artifact.get("strategy_version"))
        )
        if not claim:
            raise ArtifactRejected("artifact has no durable holdout claim")
        if claim["config_hash"] != artifact.get("artifact_hash"):
            raise ArtifactRejected("holdout claim config_hash does not match artifact")
        if _digest(claim["holdout_result"]) != _digest(holdout_result):
            raise ArtifactRejected("holdout claim result does not match artifact")

        now = _iso_now()
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT artifact_hash,status FROM strategy_artifacts WHERE strategy_version=?",
                (payload["strategy_version"],),
            ).fetchone()
            if existing and existing["artifact_hash"] != payload["artifact_hash"]:
                raise ArtifactRegistryError("strategy_version is already bound to another hash")
            history = payload.get("history") or []
            from_status = (
                existing["status"]
                if existing
                else history[-2].get("status")
                if len(history) >= 2
                else None
            )
            previous = conn.execute(
                """
                SELECT a.artifact_hash,a.payload_json
                FROM active_strategy_artifacts x
                JOIN strategy_artifacts a ON a.artifact_hash=x.artifact_hash
                WHERE x.symbol=?
                """,
                (payload["symbol"],),
            ).fetchone()
            if previous and previous["artifact_hash"] != payload["artifact_hash"]:
                retired = json.loads(previous["payload_json"])
                retired["status"] = "RETIRED"
                retired.setdefault("history", []).append(
                    {"status": "RETIRED", "at": now, "reason": "superseded atomically"}
                )
                conn.execute(
                    "UPDATE strategy_artifacts SET status='RETIRED',payload_json=?,updated_at=? "
                    "WHERE artifact_hash=?",
                    (json.dumps(retired, sort_keys=True, separators=(",", ":")), now,
                     previous["artifact_hash"]),
                )
                conn.execute(
                    """
                    INSERT INTO strategy_artifact_audit
                        (artifact_hash,symbol,action,from_status,to_status,reason,compatibility_json,at)
                    VALUES (?,?,?,?,?,?,?,?)
                    """,
                    (
                        previous["artifact_hash"], payload["symbol"], "RETIRE", "PROMOTED",
                        "RETIRED", "superseded atomically",
                        json.dumps(self._compatibility(retired), sort_keys=True), now,
                    ),
                )
            conn.execute(
                """
                INSERT INTO strategy_artifacts
                    (artifact_hash,strategy_version,symbol,status,payload_json,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(artifact_hash) DO UPDATE SET
                    status=excluded.status,payload_json=excluded.payload_json,updated_at=excluded.updated_at
                """,
                (
                    payload["artifact_hash"], payload["strategy_version"], payload["symbol"],
                    "PROMOTED", encoded, payload.get("created_at", now), now,
                ),
            )
            conn.execute(
                """
                INSERT INTO active_strategy_artifacts(symbol,artifact_hash,activated_at)
                VALUES (?,?,?)
                ON CONFLICT(symbol) DO UPDATE SET
                    artifact_hash=excluded.artifact_hash,activated_at=excluded.activated_at
                """,
                (payload["symbol"], payload["artifact_hash"], now),
            )
            conn.execute(
                """
                INSERT INTO strategy_artifact_audit
                    (artifact_hash,symbol,action,from_status,to_status,reason,compatibility_json,at)
                VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    payload["artifact_hash"], payload["symbol"], "ACTIVATE", from_status,
                    "PROMOTED", reason, json.dumps(self._compatibility(payload), sort_keys=True), now,
                ),
            )
            conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def get_active(
        self,
        *,
        symbol: str,
        expected_dataset_version: str,
        expected_feature_schema_version: str,
        expected_promotion_policy_version: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT a.payload_json
                FROM active_strategy_artifacts x
                JOIN strategy_artifacts a ON a.artifact_hash=x.artifact_hash
                WHERE x.symbol=?
                """,
                (symbol,),
            ).fetchall()
        finally:
            conn.close()
        if len(rows) != 1:
            raise ArtifactRejected(
                f"expected exactly one active StrategyArtifact for {symbol}; found {len(rows)}"
            )
        return require_promoted_artifact(
            json.loads(rows[0]["payload_json"]),
            symbol=symbol,
            expected_dataset_version=expected_dataset_version,
            expected_feature_schema_version=expected_feature_schema_version,
            expected_promotion_policy_version=expected_promotion_policy_version,
            now=now,
        )

    def audit(self, *, symbol: str) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM strategy_artifact_audit WHERE symbol=? ORDER BY id",
                    (symbol,),
                )
            ]
        finally:
            conn.close()
