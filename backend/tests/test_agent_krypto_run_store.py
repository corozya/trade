from __future__ import annotations

import sqlite3

import pytest

from services.agent_krypto_run_store import (
    IllegalRunTransitionError,
    RunFingerprintMismatchError,
    RunStore,
)


@pytest.fixture
def store(tmp_path):
    return RunStore(db_path=tmp_path / "runs.db")


def test_create_is_idempotent_for_same_fingerprint(store):
    first = store.create(
        run_id="run-1", phase="INGEST", symbol="BTC-USDT-SWAP",
        config_version="v1", config_hash="abc", request_fingerprint="fp-1",
    )
    second = store.create(
        run_id="run-1", phase="INGEST", symbol="BTC-USDT-SWAP",
        config_version="v1", config_hash="abc", request_fingerprint="fp-1",
    )
    assert first["run_id"] == second["run_id"] == "run-1"
    assert first["created_at"] == second["created_at"]


def test_create_rejects_different_fingerprint_for_same_run_id(store):
    store.create(
        run_id="run-1", phase="INGEST", symbol="BTC-USDT-SWAP",
        config_version="v1", config_hash="abc", request_fingerprint="fp-1",
    )
    with pytest.raises(RunFingerprintMismatchError):
        store.create(
            run_id="run-1", phase="INGEST", symbol="BTC-USDT-SWAP",
            config_version="v1", config_hash="abc", request_fingerprint="fp-2",
        )


def test_transition_rejects_illegal_move_and_leaves_row_unchanged(store):
    store.create(
        run_id="run-1", phase="INGEST", symbol="BTC-USDT-SWAP",
        config_version="v1", config_hash="abc", request_fingerprint="fp-1",
    )
    before = store.get(run_id="run-1")
    with pytest.raises(IllegalRunTransitionError):
        store.transition(run_id="run-1", new_status="DONE", reason="skip RUNNING")
    after = store.get(run_id="run-1")
    assert after["status"] == before["status"] == "PENDING"
    assert store.history(run_id="run-1") == [
        {"run_id": "run-1", "status": "PENDING", "at": before["created_at"], "reason": "created"}
    ]


def test_transition_legal_path_updates_result_and_history(store):
    store.create(
        run_id="run-1", phase="INGEST", symbol="BTC-USDT-SWAP",
        config_version="v1", config_hash="abc", request_fingerprint="fp-1",
    )
    acquired = store.acquire(run_id="run-1", phase="INGEST", symbol="BTC-USDT-SWAP")
    row = store.transition(
        run_id="run-1", new_status="DONE", reason="ok", result={"dataset_id": "market-x"},
        lease_token=acquired["lease_token"],
    )
    assert row["status"] == "DONE"
    history = store.history(run_id="run-1")
    assert [h["status"] for h in history] == ["PENDING", "RUNNING", "DONE"]


def test_transition_rejects_stale_lease_token(store):
    store.create(
        run_id="run-1", phase="INGEST", symbol="BTC-USDT-SWAP",
        config_version="v1", config_hash="abc", request_fingerprint="fp-1",
    )
    store.acquire(run_id="run-1", phase="INGEST", symbol="BTC-USDT-SWAP")
    from services.agent_krypto_run_store import LeaseLostError
    with pytest.raises(LeaseLostError):
        store.transition(
            run_id="run-1", new_status="DONE", reason="ok",
            lease_token="not-the-real-token",
        )


def test_get_returns_full_contract(store):
    store.create(
        run_id="run-1", phase="INGEST", symbol="BTC-USDT-SWAP",
        config_version="v1", config_hash="abc", request_fingerprint="fp-1",
    )
    row = store.get(run_id="run-1")
    for key in (
        "run_id", "phase", "symbol", "status", "config_version", "config_hash",
        "request_fingerprint", "result_json", "reason", "provider", "created_at", "updated_at",
    ):
        assert key in row


def test_get_unknown_run_id_returns_none(store):
    assert store.get(run_id="does-not-exist") is None


def test_legacy_db_without_lease_token_is_migrated_on_init(tmp_path):
    """#101 review-round-4 P0: a DB file created by a pre-lease_token version
    of this store must still work — RunStore.__init__() has to ALTER TABLE
    the missing column in rather than assume CREATE TABLE IF NOT EXISTS
    handles it (it does not, for an already-existing table)."""
    db_path = tmp_path / "legacy_runs.db"
    legacy_conn = sqlite3.connect(db_path)
    try:
        legacy_conn.execute(
            """
            CREATE TABLE orchestrator_runs (
                run_id TEXT NOT NULL PRIMARY KEY,
                phase TEXT NOT NULL,
                symbol TEXT NOT NULL,
                status TEXT NOT NULL,
                config_version TEXT NOT NULL,
                config_hash TEXT NOT NULL,
                request_fingerprint TEXT NOT NULL,
                result_json TEXT,
                reason TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        legacy_conn.execute(
            """
            CREATE TABLE orchestrator_run_history (
                run_id TEXT NOT NULL,
                status TEXT NOT NULL,
                at TEXT NOT NULL,
                reason TEXT,
                FOREIGN KEY (run_id) REFERENCES orchestrator_runs(run_id)
            )
            """
        )
        legacy_conn.execute(
            """
            INSERT INTO orchestrator_runs
                (run_id, phase, symbol, status, config_version, config_hash,
                 request_fingerprint, result_json, reason, created_at, updated_at)
            VALUES ('legacy-run-1', 'EXPERIMENT', 'BTC-USDT-SWAP', 'WAIT', 'v1', 'abc',
                    'fp-legacy', NULL, 'was waiting', '2026-01-01T00:00:00+00:00',
                    '2026-01-01T00:00:00+00:00')
            """
        )
        legacy_conn.execute(
            """
            INSERT INTO orchestrator_run_history (run_id, status, at, reason)
            VALUES ('legacy-run-1', 'WAIT', '2026-01-01T00:00:00+00:00', 'was waiting')
            """
        )
        legacy_conn.commit()
    finally:
        legacy_conn.close()

    # Opening this legacy DB through RunStore must migrate the schema
    # in place, without touching the existing row's data.
    migrated_store = RunStore(db_path=db_path)
    row = migrated_store.get(run_id="legacy-run-1")
    assert row["status"] == "WAIT"
    assert row["reason"] == "was waiting"
    assert row["request_fingerprint"] == "fp-legacy"
    assert row["lease_token"] is None

    # And it must be fully usable going forward: acquire mints a lease token,
    # heartbeat/transition work correctly under it.
    acquired = migrated_store.acquire(
        run_id="legacy-run-1", phase="EXPERIMENT", symbol="BTC-USDT-SWAP"
    )
    assert acquired["status"] == "RUNNING"
    assert acquired["lease_token"]
    assert migrated_store.heartbeat(run_id="legacy-run-1", lease_token=acquired["lease_token"])
    done = migrated_store.transition(
        run_id="legacy-run-1", new_status="DONE", reason="ok",
        result={"dataset_id": "market-x"}, lease_token=acquired["lease_token"],
    )
    assert done["status"] == "DONE"
