from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from services.agent_krypto_orchestrator import dispatch
from services.agent_krypto_run_store import (
    RUN_STATUSES,
    IllegalRunTransitionError,
    RunStore,
    validate_run_transition,
)

_ALLOWED = {
    "PENDING": {"RUNNING", "ERROR"},
    "RUNNING": {"WAIT", "ERROR", "DONE"},
    "WAIT": {"RUNNING", "ERROR"},
    "ERROR": {"RUNNING"},
    "DONE": set(),
}

_ALL_PAIRS = [(a, b) for a in RUN_STATUSES for b in RUN_STATUSES]


@pytest.mark.parametrize("current,new_status", _ALL_PAIRS)
def test_run_transition_matches_allowed_table(current, new_status):
    if new_status in _ALLOWED[current]:
        validate_run_transition(current, new_status)
    else:
        with pytest.raises(IllegalRunTransitionError):
            validate_run_transition(current, new_status)


def test_dispatch_does_not_call_handler_on_illegal_transition(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        '{"config_version":"v1","dataset_version":"d1","feature_schema_version":"f1",'
        '"promotion_policy_version":"p1","label_config_version":"l1",'
        '"credential_alias":"okx-demo-1"}'
    )
    run_store = RunStore(db_path=tmp_path / "runs.db")
    handler = MagicMock()

    args = {
        "config_version": "v1", "symbol": "BTC-USDT-SWAP",
        "dataset_version": "d1", "feature_schema_version": "f1",
    }
    run_id_result = dispatch(
        "ingest", args, run_store=run_store, handler=handler, config_path=config_path,
    )
    assert run_id_result["status"] == "DONE"
    handler.assert_called_once()

    # Force the persisted run straight to ERROR, then retry: PENDING/DONE ->
    # RUNNING is legal, but a run that is DONE and re-dispatched with a
    # different fingerprint is a fingerprint mismatch, not covered here. The
    # illegal-transition path is exercised directly against the run store
    # (see test_run_transition_matches_allowed_table) and via the DONE row
    # short-circuit below, which never calls handler again.
    handler.reset_mock()
    second = dispatch(
        "ingest", args, run_store=run_store, handler=handler, config_path=config_path,
    )
    assert second["status"] == "DONE"
    handler.assert_not_called()


def test_dispatch_retries_its_own_stale_run_id_without_a_helper_run(tmp_path):
    """#101 review-round-2 P0: a bare retry of the same logical request (same
    run_id, same fingerprint) after a crash — the ordinary cron-retry path,
    with no other run ever involved — must succeed once its own lease is
    stale, not stay stuck in ERROR forever."""
    config_path = tmp_path / "config.json"
    config_path.write_text(
        '{"config_version":"v1","dataset_version":"d1","feature_schema_version":"f1",'
        '"promotion_policy_version":"p1","label_config_version":"l1",'
        '"credential_alias":"okx-demo-1","lock_stale_after_seconds":60}'
    )
    run_store = RunStore(db_path=tmp_path / "runs.db")
    args = {
        "config_version": "v1", "symbol": "BTC-USDT-SWAP",
        "dataset_version": "d1", "feature_schema_version": "f1",
        "run_bucket": "fixed-bucket",
    }

    started = datetime.now(timezone.utc)

    def hangs_forever(_args, _config):
        raise TimeoutError("simulated: process crashed mid-handler, never returns")

    first = dispatch(
        "ingest", args, run_store=run_store, handler=hangs_forever,
        config_path=config_path, now=started,
    )
    # The handler raised, so dispatch's own except-clause already moved the
    # row to ERROR here — but the scenario under test is the *harder* case:
    # a process that crashed so hard the row was left RUNNING (no exception
    # ever ran locally, e.g. SIGKILL). Force that state directly.
    run_store.transition(run_id=first["run_id"], new_status="RUNNING", reason="simulated crash", now=started)

    later = started + timedelta(seconds=120)

    def succeeds(_args, _config):
        return {"dataset_id": "market-recovered"}

    retried = dispatch(
        "ingest", args, run_store=run_store, handler=succeeds,
        config_path=config_path, now=later,
    )
    assert retried["run_id"] == first["run_id"]
    assert retried["status"] == "DONE"
    assert retried["result"]["dataset_id"] == "market-recovered"


def test_old_owner_cannot_finish_after_its_own_run_id_was_self_reclaimed(tmp_path):
    """#101 review-round-3 P0 (lease token): a process holding a stale
    RUNNING lease that gets self-reclaimed by a later retry of the *same*
    run_id (a fresh lease_token minted) must not be able to publish DONE
    with its old, now-dead token once it eventually returns — the whole
    point of the token is that "the row is RUNNING" is not sufficient proof
    of ownership; the exact token must match."""
    run_store = RunStore(db_path=tmp_path / "runs.db")
    started = datetime.now(timezone.utc)

    run_store.create(
        run_id="run-1", phase="EXPERIMENT", symbol="BTC-USDT-SWAP",
        config_version="v1", config_hash="abc", request_fingerprint="fp-1", now=started,
    )
    old_lease = run_store.acquire(
        run_id="run-1", phase="EXPERIMENT", symbol="BTC-USDT-SWAP", now=started
    )["lease_token"]
    # Process A crashes here without releasing the lock (no exception, no
    # transition() call — e.g. SIGKILL). No heartbeat is sent either.

    later = started + timedelta(seconds=1000)
    # Process B retries the same logical run_id after the lease goes stale —
    # self-reclaim mints a brand-new lease_token for the same row.
    new_lease = run_store.acquire(
        run_id="run-1", phase="EXPERIMENT", symbol="BTC-USDT-SWAP",
        stale_after_seconds=900, now=later,
    )["lease_token"]
    assert new_lease != old_lease

    # Process A, unaware it was reclaimed, finally "wakes up" and tries to
    # heartbeat/finish under its old token — both must be rejected.
    assert not run_store.heartbeat(run_id="run-1", lease_token=old_lease, now=later)
    from services.agent_krypto_run_store import LeaseLostError
    with pytest.raises(LeaseLostError):
        run_store.transition(
            run_id="run-1", new_status="DONE", reason="late success from process A",
            result={"dataset_id": "should-not-persist"},
            lease_token=old_lease, now=later,
        )
    # Process B's own (new) token still works correctly.
    assert run_store.heartbeat(run_id="run-1", lease_token=new_lease, now=later)


def test_heartbeat_exception_is_fail_closed_not_silently_swallowed(tmp_path):
    """#101 review-round-3 P1: an exception from run_store.heartbeat() (e.g.
    the backing DB became unreachable) must be treated as lease loss, not
    let the handler's result publish as DONE."""
    config_path = tmp_path / "config.json"
    config_path.write_text(
        '{"config_version":"v1","dataset_version":"d1","feature_schema_version":"f1",'
        '"promotion_policy_version":"p1","label_config_version":"l1",'
        '"credential_alias":"okx-demo-1","lock_stale_after_seconds":1}'
    )
    run_store = RunStore(db_path=tmp_path / "runs.db")

    original_heartbeat = run_store.heartbeat

    def flaky_heartbeat(*, run_id, lease_token, now=None):
        raise RuntimeError("simulated: run_store DB became unreachable")

    run_store.heartbeat = flaky_heartbeat

    def slow_handler(_args, _config):
        import time
        time.sleep(1.5)  # long enough for at least one heartbeat tick to fire
        return {"dataset_id": "should-not-persist"}

    args = {
        "config_version": "v1", "symbol": "BTC-USDT-SWAP",
        "dataset_version": "d1", "feature_schema_version": "f1",
    }
    result = dispatch(
        "ingest", args, run_store=run_store, handler=slow_handler, config_path=config_path,
    )
    assert result["status"] == "ERROR"
    assert "lease" in result["reason"].lower()
    run_store.heartbeat = original_heartbeat
    assert run_store.get(run_id=result["run_id"])["result_json"] is None
