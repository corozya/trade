from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from services.agent_krypto_run_store import RunLockHeldError, RunStore


@pytest.fixture
def store(tmp_path):
    return RunStore(db_path=tmp_path / "runs.db")


def test_acquire_succeeds_when_no_other_run_is_running(store):
    store.create(
        run_id="run-1", phase="EXPERIMENT", symbol="BTC-USDT-SWAP",
        config_version="v1", config_hash="abc", request_fingerprint="fp-1",
    )
    row = store.acquire(run_id="run-1", phase="EXPERIMENT", symbol="BTC-USDT-SWAP")
    assert row["status"] == "RUNNING"


def test_acquire_refuses_when_another_run_is_running_and_fresh(store):
    now = datetime.now(timezone.utc)
    store.create(
        run_id="run-1", phase="EXPERIMENT", symbol="BTC-USDT-SWAP",
        config_version="v1", config_hash="abc", request_fingerprint="fp-1", now=now,
    )
    store.acquire(run_id="run-1", phase="EXPERIMENT", symbol="BTC-USDT-SWAP", now=now)

    store.create(
        run_id="run-2", phase="EXPERIMENT", symbol="BTC-USDT-SWAP",
        config_version="v1", config_hash="abc", request_fingerprint="fp-2", now=now,
    )
    with pytest.raises(RunLockHeldError):
        store.acquire(
            run_id="run-2", phase="EXPERIMENT", symbol="BTC-USDT-SWAP",
            now=now + timedelta(seconds=5),
        )


def test_acquire_takes_over_stale_running_lock_without_double_execution(store):
    started = datetime.now(timezone.utc)
    store.create(
        run_id="run-1", phase="EXPERIMENT", symbol="BTC-USDT-SWAP",
        config_version="v1", config_hash="abc", request_fingerprint="fp-1", now=started,
    )
    store.acquire(run_id="run-1", phase="EXPERIMENT", symbol="BTC-USDT-SWAP", now=started)
    # run-1 crashed: no heartbeat, no release. run-2 targets the same logical
    # work far enough in the future that run-1's lock is considered dead.
    stale_check = started + timedelta(seconds=1000)

    store.create(
        run_id="run-2", phase="EXPERIMENT", symbol="BTC-USDT-SWAP",
        config_version="v1", config_hash="abc", request_fingerprint="fp-2", now=stale_check,
    )
    row = store.acquire(
        run_id="run-2", phase="EXPERIMENT", symbol="BTC-USDT-SWAP",
        stale_after_seconds=900, now=stale_check,
    )
    assert row["status"] == "RUNNING"
    # run-1's row is atomically reclaimed to ERROR (not left RUNNING forever,
    # which would make it impossible to legally resume via ERROR -> RUNNING).
    stale_row = store.get(run_id="run-1")
    assert stale_row["status"] == "ERROR"
    assert stale_row["reason"] == "stale lock reclaimed"


def test_reclaimed_stale_run_can_itself_be_resumed(store):
    started = datetime.now(timezone.utc)
    store.create(
        run_id="run-1", phase="EXPERIMENT", symbol="BTC-USDT-SWAP",
        config_version="v1", config_hash="abc", request_fingerprint="fp-1", now=started,
    )
    store.acquire(run_id="run-1", phase="EXPERIMENT", symbol="BTC-USDT-SWAP", now=started)

    stale_check = started + timedelta(seconds=1000)
    store.create(
        run_id="run-2", phase="EXPERIMENT", symbol="BTC-USDT-SWAP",
        config_version="v1", config_hash="abc", request_fingerprint="fp-2", now=stale_check,
    )
    acquired_2 = store.acquire(
        run_id="run-2", phase="EXPERIMENT", symbol="BTC-USDT-SWAP",
        stale_after_seconds=900, now=stale_check,
    )
    store.transition(
        run_id="run-2", new_status="WAIT", reason="paused",
        lease_token=acquired_2["lease_token"], now=stale_check,
    )

    # run-1 is now ERROR; it must be legally resumable (ERROR -> RUNNING) even
    # though another run currently holds the lock, once that lock is released.
    store.transition(run_id="run-2", new_status="ERROR", reason="failed", now=stale_check)
    later = stale_check + timedelta(seconds=1)
    resumed = store.acquire(
        run_id="run-1", phase="EXPERIMENT", symbol="BTC-USDT-SWAP", now=later,
    )
    assert resumed["status"] == "RUNNING"


def test_three_concurrent_owners_only_one_fresh_lock_holder_blocks_others(store):
    now = datetime.now(timezone.utc)
    for i in (1, 2, 3):
        store.create(
            run_id=f"run-{i}", phase="EXPERIMENT", symbol="BTC-USDT-SWAP",
            config_version="v1", config_hash="abc", request_fingerprint=f"fp-{i}", now=now,
        )
    store.acquire(run_id="run-1", phase="EXPERIMENT", symbol="BTC-USDT-SWAP", now=now)

    with pytest.raises(RunLockHeldError):
        store.acquire(
            run_id="run-2", phase="EXPERIMENT", symbol="BTC-USDT-SWAP",
            now=now + timedelta(seconds=5),
        )
    with pytest.raises(RunLockHeldError):
        store.acquire(
            run_id="run-3", phase="EXPERIMENT", symbol="BTC-USDT-SWAP",
            now=now + timedelta(seconds=10),
        )
    # run-1 remains the sole fresh RUNNING owner throughout.
    assert store.get(run_id="run-1")["status"] == "RUNNING"
    assert store.get(run_id="run-2")["status"] == "PENDING"
    assert store.get(run_id="run-3")["status"] == "PENDING"


def test_same_run_id_retries_its_own_stale_lock_without_a_helper_run(store):
    """A crashed process retrying its own run_id (the ordinary cron-retry
    path — no other run ever gets involved) must succeed once its own lease
    goes stale, self-reclaiming RUNNING -stale-> ERROR -> RUNNING inside one
    acquire() call. This is the #101 review-round-2 P0 case: previously only
    a *different* run_id could unstick a stale RUNNING row."""
    started = datetime.now(timezone.utc)
    store.create(
        run_id="run-1", phase="EXPERIMENT", symbol="BTC-USDT-SWAP",
        config_version="v1", config_hash="abc", request_fingerprint="fp-1", now=started,
    )
    store.acquire(run_id="run-1", phase="EXPERIMENT", symbol="BTC-USDT-SWAP", now=started)
    # Process crashes here: no heartbeat, no release, no other run created.

    later = started + timedelta(seconds=1000)
    resumed = store.acquire(
        run_id="run-1", phase="EXPERIMENT", symbol="BTC-USDT-SWAP",
        stale_after_seconds=900, now=later,
    )
    assert resumed["status"] == "RUNNING"


def test_same_run_id_retry_is_blocked_while_its_own_lease_is_fresh(store):
    started = datetime.now(timezone.utc)
    store.create(
        run_id="run-1", phase="EXPERIMENT", symbol="BTC-USDT-SWAP",
        config_version="v1", config_hash="abc", request_fingerprint="fp-1", now=started,
    )
    store.acquire(run_id="run-1", phase="EXPERIMENT", symbol="BTC-USDT-SWAP", now=started)
    with pytest.raises(RunLockHeldError):
        store.acquire(
            run_id="run-1", phase="EXPERIMENT", symbol="BTC-USDT-SWAP",
            stale_after_seconds=900, now=started + timedelta(seconds=5),
        )


def test_heartbeat_prevents_false_stale_recovery_of_a_live_process(store):
    started = datetime.now(timezone.utc)
    store.create(
        run_id="run-1", phase="EXPERIMENT", symbol="BTC-USDT-SWAP",
        config_version="v1", config_hash="abc", request_fingerprint="fp-1", now=started,
    )
    acquired = store.acquire(run_id="run-1", phase="EXPERIMENT", symbol="BTC-USDT-SWAP", now=started)

    heartbeat_time = started + timedelta(seconds=800)
    assert store.heartbeat(
        run_id="run-1", lease_token=acquired["lease_token"], now=heartbeat_time
    )

    later = started + timedelta(seconds=1000)
    store.create(
        run_id="run-2", phase="EXPERIMENT", symbol="BTC-USDT-SWAP",
        config_version="v1", config_hash="abc", request_fingerprint="fp-2", now=later,
    )
    with pytest.raises(RunLockHeldError):
        store.acquire(
            run_id="run-2", phase="EXPERIMENT", symbol="BTC-USDT-SWAP",
            stale_after_seconds=900, now=later,
        )


def test_heartbeat_with_stale_lease_token_reports_lease_lost(store):
    """A process whose lock was reclaimed by someone else (stale takeover)
    must have its heartbeat calls rejected — the old lease_token no longer
    matches — so it cannot falsely believe it still owns the lock."""
    started = datetime.now(timezone.utc)
    store.create(
        run_id="run-1", phase="EXPERIMENT", symbol="BTC-USDT-SWAP",
        config_version="v1", config_hash="abc", request_fingerprint="fp-1", now=started,
    )
    old_lease = store.acquire(
        run_id="run-1", phase="EXPERIMENT", symbol="BTC-USDT-SWAP", now=started
    )["lease_token"]

    stale_check = started + timedelta(seconds=1000)
    store.create(
        run_id="run-2", phase="EXPERIMENT", symbol="BTC-USDT-SWAP",
        config_version="v1", config_hash="abc", request_fingerprint="fp-2", now=stale_check,
    )
    store.acquire(
        run_id="run-2", phase="EXPERIMENT", symbol="BTC-USDT-SWAP",
        stale_after_seconds=900, now=stale_check,
    )
    # run-1's stale lock was just reclaimed (moved to ERROR); its own old
    # heartbeat call must now report failure under the stale token.
    assert not store.heartbeat(
        run_id="run-1", lease_token=old_lease, now=stale_check + timedelta(seconds=1)
    )
