"""Operational orchestrator over the agent-krypto research modules (#101).

Owns the run-level state machine (PENDING/RUNNING/WAIT/ERROR/DONE), versioned
configuration loading and the fail-closed dispatch boundary the CLI uses. It
does not implement research logic itself — every phase handler delegates to
the already-reviewed domain modules (#94-#99) and only *gates* whether they
may run.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from services.agent_krypto_run_store import (
    LOCK_STALE_AFTER_SECONDS,
    IllegalRunTransitionError,
    LeaseLostError,
    RunLockHeldError,
    RunStore,
    request_fingerprint,
)

CYCLE_BUCKET_MINUTES = 15

# #119: audit-only alternation of which provider "proposed" a run's result
# (recorded in run_store.provider). This is never read by acquire()/
# transition()'s transition-legality checks, by PromotionPolicy, or by the
# execution boundary in dispatch()/run_agent_krypto_cycle — it grants no
# additional execution capability to either provider.
PROVIDERS = ("claude", "codex")

# Values that must never be treated as a real, resolved version. A config
# shipped with these left in place is a placeholder that was never wired up
# for the target environment — treating it as present would let a run
# execute against an undefined dataset/feature/policy version.
PLACEHOLDER_VALUES = {"", "unset", "todo", "tbd", "changeme", "none", "null"}

RUN_PHASES = (
    "INGEST",
    "REQUEST",
    "EXPERIMENT",
    "EVALUATE",
    "PAPER",
    "DEMO",
    "PROMOTE",
    "CYCLE",
    "E2E",
    "RESEARCH_LOOP",
)

# Config fields every subcommand must have available before any domain call.
REQUIRED_CONFIG_FIELDS = {
    "config_version",
    "dataset_version",
    "feature_schema_version",
    "promotion_policy_version",
    "label_config_version",
    "credential_alias",
}

# Additional per-command required version fields, checked against the merged
# config+args payload. Missing any of these is WAIT, not ERROR: the run is a
# legal no-op waiting on an upstream artifact/version, not a defect.
REQUIRED_VERSION_FIELDS: dict[str, tuple[str, ...]] = {
    # Ingest is the phase that mints dataset_version; requiring it beforehand
    # makes the first real ingest impossible.
    "ingest": (),
    # LearningRequest mints feature_schema_version, so only its base dataset
    # must exist before dispatch.
    "request": ("dataset_version",),
    "experiment": ("dataset_version", "feature_schema_version"),
    "evaluate": ("promotion_policy_version",),
    "promote": ("promotion_policy_version",),
    "cycle": (
        "dataset_version",
        "feature_schema_version",
        "promotion_policy_version",
    ),
    # e2e deliberately mints its own dataset/feature/promotion-policy versions
    # from a synthetic in-process fixture (see _handle_e2e), so it does not
    # require the environment's real versioned config to already be resolved
    # — that is the whole point of an offline smoke run that works before any
    # real dataset has ever been ingested.
    "e2e": (),
    # research-loop mints its own dataset_version/feature_schema_version from
    # a real ingest/request call within the handler (like e2e), and never
    # promotes, so it needs no pre-resolved version either — the first real
    # research-loop run is what resolves those versions in the first place.
    "research-loop": (),
    "status": (),
}

# Matches long hex or base64-alphabet runs (API keys/secrets/tokens), but not
# ordinary identifiers or paths: those are mixed-case with underscores/dots/
# slashes, while secrets are dense, high-entropy runs of one alphabet with no
# separators. Requiring a run made *entirely* of hex digits, or entirely of
# base64 characters including at least one digit, keeps config/dataset names
# like ``agent_krypto_orchestrator_config`` (letters + underscores only, no
# digits) out of the match.
_SECRET_LIKE = re.compile(r"(?:[0-9a-fA-F]{32,}|(?=[A-Za-z0-9+/]{32,})[A-Za-z0-9+/]*[0-9][A-Za-z0-9+/]*)")


class OrchestratorError(ValueError):
    """Illegal run transition or missing required config/version — fail-closed."""


class OrchestratorConfigError(OrchestratorError):
    """Versioned configuration is missing, malformed or references an unknown version."""


def default_cycle_bucket(now: datetime) -> str:
    """Stable UTC 15-minute bucket (floor to :00/:15/:30/:45).

    Used as the ``cycle`` command's default ``date_bucket`` when the caller
    does not pass an explicit ``--run-bucket``: the operational 15-minute
    cron cycle must map to a run_id that changes every quarter-hour, not once
    per calendar day — a daily bucket would make every cycle after the first
    in a given day a no-op cache hit against the first cycle's stale result.
    """
    floored_minute = (now.minute // CYCLE_BUCKET_MINUTES) * CYCLE_BUCKET_MINUTES
    bucket_start = now.replace(minute=floored_minute, second=0, microsecond=0)
    return bucket_start.isoformat()


_PROVIDER_EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)


def provider_for_bucket(date_bucket: str) -> str:
    """Deterministic Claude/Codex alternation, keyed off the same 15-minute
    ``date_bucket`` string ``default_cycle_bucket``/``resolve_run_id`` already
    use (#119).

    Pure audit annotation: the returned value is recorded as
    ``run_store.provider`` to show which provider "proposed" a run's result
    as a review, never as its executor. It is never consulted by
    ``validate_run_transition``, ``PromotionPolicy.evaluate`` or
    ``require_promoted_artifact`` — changing it cannot change what any run is
    allowed to do. Alternation is the parity of the number of 15-minute
    periods since a fixed epoch (not a hash of the bucket string), so
    consecutive real 15-minute buckets always land on opposite providers —
    a hash-of-string index (the original #119 implementation) is
    deterministic per bucket but does not guarantee that adjacent buckets
    differ, which defeats "alternation" (flagged in #119 review: observed
    sequence ``codex, claude, claude, codex``). Falls back to a hash of the
    string for inputs that are not a parseable ISO timestamp (e.g. ad-hoc
    ``--run-bucket`` values used only by tests), which stays deterministic
    per input but is not guaranteed to alternate for such non-standard
    buckets.
    """
    try:
        bucket_dt = datetime.fromisoformat(date_bucket)
    except ValueError:
        bucket_index = int(hashlib.sha256(date_bucket.encode()).hexdigest(), 16)
        return PROVIDERS[bucket_index % len(PROVIDERS)]
    if bucket_dt.tzinfo is None:
        bucket_dt = bucket_dt.replace(tzinfo=timezone.utc)
    periods_since_epoch = int(
        (bucket_dt - _PROVIDER_EPOCH).total_seconds() // (CYCLE_BUCKET_MINUTES * 60)
    )
    return PROVIDERS[periods_since_epoch % len(PROVIDERS)]


def redact(message: str) -> str:
    """Mask any substring that looks like a secret before it reaches logs/reason."""
    return _SECRET_LIKE.sub("[REDACTED]", message)


def redact_value(value: Any) -> Any:
    """Recursively redact secret-like strings anywhere in a JSON-shaped value.

    ``reason`` is not the only place a secret can leak: a handler's ``result``
    can nest arbitrarily deep (nested dicts/lists from domain modules), so the
    whole envelope must be scrubbed before it reaches stdout/the run store,
    not just the top-level reason string.
    """
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, Mapping):
        return {key: redact_value(val) for key, val in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_value(val) for val in value]
    return value


def config_hash(config: Mapping[str, Any]) -> str:
    encoded = json.dumps(config, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def load_orchestrator_config(
    config_version: str,
    *,
    config_path: str | Path = "config/agent_krypto_orchestrator_config.json",
) -> dict[str, Any]:
    """Load and validate the versioned orchestrator config.

    Fail-closed: a missing required field, an unknown field, or a
    ``config_version`` that does not match the file raises rather than
    silently falling back to a default.
    """
    path = Path(config_path)
    if not path.exists():
        raise OrchestratorConfigError(f"config file not found: {path}")
    try:
        payload = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise OrchestratorConfigError(f"config file is not valid JSON: {exc}") from exc

    if payload.get("config_version") != config_version:
        raise OrchestratorConfigError(
            f"requested config_version {config_version!r} does not match "
            f"{payload.get('config_version')!r} in {path}"
        )
    missing = REQUIRED_CONFIG_FIELDS - payload.keys()
    if missing:
        raise OrchestratorConfigError(f"config is missing required fields: {sorted(missing)}")
    unknown = set(payload.keys()) - REQUIRED_CONFIG_FIELDS - {"lock_stale_after_seconds"}
    if unknown:
        raise OrchestratorConfigError(f"config has unknown fields: {sorted(unknown)}")
    return payload


def _is_placeholder(value: Any) -> bool:
    return not value or str(value).strip().lower() in PLACEHOLDER_VALUES


def missing_required_versions(command: str, merged: Mapping[str, Any]) -> list[str]:
    required = REQUIRED_VERSION_FIELDS.get(command, ())
    return [field for field in required if _is_placeholder(merged.get(field))]


def resolve_run_id(
    *,
    phase: str,
    symbol: str,
    config_hash_value: str,
    date_bucket: str,
    logical_work_id: str,
) -> str:
    """Deterministic run_id: same logical work maps to the same row.

    ``date_bucket`` is caller-supplied (e.g. the current UTC date, or a
    15-minute cycle bucket for ``cycle``) so a cron re-trigger of the same
    logical unit of work lands on the same run instead of creating a
    duplicate. ``logical_work_id`` disambiguates *which* unit of work this is
    within that bucket — for ``cycle`` it can be left as a constant (the
    15-minute bucket alone already identifies one logical cycle per symbol),
    but for ingest/request/experiment/evaluate/promote it must be derived
    from the request payload itself (e.g. its fingerprint or an explicit
    request/trial/strategy id). Without it, two distinct requests for the
    same symbol on the same day collapse onto one run_id and the second one
    fails as a fingerprint mismatch instead of getting its own run.
    """
    digest = hashlib.sha256(
        f"{phase}:{symbol}:{config_hash_value}:{date_bucket}:{logical_work_id}".encode()
    ).hexdigest()
    return f"run-{digest[:24]}"


def envelope(
    command: str,
    *,
    status: str,
    reason: str | None,
    run_id: str | None,
    result: Mapping[str, Any] | None = None,
    config_version: str | None = None,
    provider: str | None = None,
) -> dict[str, Any]:
    return {
        "command": command,
        "run_id": run_id,
        "phase": PHASE_FOR.get(command),
        "status": status,
        "reason": redact(reason) if reason else reason,
        "result": redact_value(dict(result)) if result is not None else None,
        "config_version": config_version,
        "provider": provider,
    }


PHASE_FOR: dict[str, str] = {
    "ingest": "INGEST",
    "request": "REQUEST",
    "experiment": "EXPERIMENT",
    "evaluate": "EVALUATE",
    "promote": "PROMOTE",
    "cycle": "CYCLE",
    "e2e": "E2E",
    "research-loop": "RESEARCH_LOOP",
    "status": None,
}


def dispatch(
    command: str,
    args: Mapping[str, Any],
    *,
    run_store: RunStore,
    handler: Callable[[Mapping[str, Any], Mapping[str, Any]], Mapping[str, Any]],
    config_path: str | Path = "config/agent_krypto_orchestrator_config.json",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Fail-closed dispatch shared by every mutating CLI subcommand.

    Ordering is deliberate and mirrors ``run_agent_krypto_cycle``/
    ``require_promoted_artifact``: config load, then required-version check,
    then run-transition validation, then lock acquisition — all *before* the
    domain ``handler`` is ever invoked. Nothing upstream of the handler call
    touches ``services.crypto_*``/``services.okx_*``.
    """
    now = now or datetime.now(timezone.utc)
    config_version = args.get("config_version")
    if not config_version:
        return envelope(command, status="ERROR", reason="config_version is required", run_id=None)

    try:
        config = load_orchestrator_config(config_version, config_path=config_path)
    except OrchestratorConfigError as exc:
        return envelope(
            command, status="ERROR", reason=f"config: {exc}", run_id=None,
            config_version=config_version,
        )

    merged = {**config, **{k: v for k, v in args.items() if v is not None}}
    missing = missing_required_versions(command, merged)
    if missing:
        return envelope(
            command, status="WAIT", reason=f"missing versions: {missing}", run_id=None,
            config_version=config_version,
        )

    symbol = str(args.get("symbol") or "-")
    phase = PHASE_FOR[command]
    fingerprint = request_fingerprint(merged)
    # For `cycle` (and the observation-mode `research-loop`, #116/#118) the
    # 15-minute bucket alone already identifies one logical unit of work per
    # symbol (it either replays or it doesn't — there is no "second distinct
    # cycle"/"second distinct research-loop run" within the same bucket).
    # Every other command can receive genuinely distinct requests for the
    # same symbol within the same bucket (two LearningRequests, two
    # experiments, two promotions), so its own fingerprint disambiguates them
    # into separate runs instead of colliding into one run_id and failing as
    # a mismatch.
    _bucketed_commands = ("cycle", "research-loop")
    logical_work_id = command if command in _bucketed_commands else fingerprint
    default_bucket = (
        default_cycle_bucket(now) if command in _bucketed_commands else now.date().isoformat()
    )
    run_id = resolve_run_id(
        phase=phase,
        symbol=symbol,
        config_hash_value=config_hash(config),
        date_bucket=str(args.get("run_bucket") or default_bucket),
        logical_work_id=logical_work_id,
    )

    provider = provider_for_bucket(str(args.get("run_bucket") or default_bucket))
    run_store.create(
        run_id=run_id,
        phase=phase,
        symbol=symbol,
        config_version=config_version,
        config_hash=config_hash(config),
        request_fingerprint=fingerprint,
        provider=provider,
        now=now,
    )

    current = run_store.get(run_id=run_id)
    if current["status"] == "DONE":
        result = json.loads(current["result_json"]) if current["result_json"] else None
        return envelope(
            command, status="DONE", reason=current["reason"], run_id=run_id,
            result=result, config_version=config_version, provider=current.get("provider"),
        )

    # Transition legality (including the RUNNING -stale-> ERROR -> RUNNING
    # self-reclaim for a crashed run retrying its own run_id) is fully owned
    # by acquire() — it has the lease/staleness information this call site
    # does not, so re-validating "current -> RUNNING" here first would reject
    # a live process's own legitimate self-reclaim before acquire() ever gets
    # to attempt it. Do not duplicate that check.
    stale_after = int(config.get("lock_stale_after_seconds", LOCK_STALE_AFTER_SECONDS))
    try:
        acquired = run_store.acquire(
            run_id=run_id, phase=phase, symbol=symbol,
            stale_after_seconds=stale_after, now=now,
        )
    except RunLockHeldError as exc:
        return envelope(
            command, status="WAIT", reason=str(exc), run_id=run_id,
            config_version=config_version, provider=provider,
        )
    except IllegalRunTransitionError as exc:
        return envelope(
            command, status="ERROR", reason=str(exc), run_id=run_id,
            config_version=config_version, provider=provider,
        )
    lease_token = acquired["lease_token"]

    # A handler that runs longer than stale_after_seconds must keep proving
    # it is alive under its own lease_token, or a concurrent acquire() would
    # legitimately reclaim the lock mid-flight and mint a new token — at
    # which point this handler's heartbeat calls stop matching and must be
    # treated as lease loss, not silently ignored. The heartbeat runs on a
    # background thread for the duration of the (synchronous) handler call
    # only; any exception raised while heartbeating (e.g. the run_store's
    # backing DB became unreachable) is itself treated as lease loss —
    # fail-closed, since a heartbeat failure means this process can no
    # longer prove to anyone else that it still owns the lock.
    stop_heartbeat = threading.Event()
    lease_lost = threading.Event()
    heartbeat_error: list[BaseException] = []

    def _heartbeat_loop() -> None:
        interval = max(1.0, stale_after / 3)
        while not stop_heartbeat.wait(interval):
            try:
                if not run_store.heartbeat(run_id=run_id, lease_token=lease_token):
                    lease_lost.set()
                    return
            except Exception as exc:  # heartbeat itself must be fail-closed
                heartbeat_error.append(exc)
                lease_lost.set()
                return

    heartbeat_thread = threading.Thread(target=_heartbeat_loop, daemon=True)
    heartbeat_thread.start()
    try:
        result = dict(handler(merged, config))
    except Exception as exc:  # execution boundary must be fail-closed
        reason = redact(str(exc))
        stop_heartbeat.set()
        heartbeat_thread.join()
        if not lease_lost.is_set():
            try:
                run_store.transition(
                    run_id=run_id, new_status="ERROR", reason=reason,
                    lease_token=lease_token, now=now,
                )
            except LeaseLostError:
                pass
        return envelope(
            command, status="ERROR", reason=reason, run_id=run_id,
            config_version=config_version, provider=provider,
        )
    stop_heartbeat.set()
    heartbeat_thread.join()

    if lease_lost.is_set():
        # Do not persist DONE over a lease we no longer hold: another owner
        # may have already reclaimed and re-run this work. Report ERROR
        # without touching the row further.
        reason = "lock lease was lost during execution; result was not persisted"
        if heartbeat_error:
            reason = f"{reason} (heartbeat error: {redact(str(heartbeat_error[0]))})"
        return envelope(
            command, status="ERROR", reason=reason, run_id=run_id,
            config_version=config_version, provider=provider,
        )

    scrubbed_result = redact_value(result)
    try:
        run_store.transition(
            run_id=run_id, new_status="DONE", reason="ok", result=scrubbed_result,
            lease_token=lease_token, now=now,
        )
    except LeaseLostError as exc:
        return envelope(
            command, status="ERROR", reason=str(exc), run_id=run_id,
            config_version=config_version, provider=provider,
        )
    return envelope(
        command, status="DONE", reason=None, run_id=run_id, result=scrubbed_result,
        config_version=config_version, provider=provider,
    )
