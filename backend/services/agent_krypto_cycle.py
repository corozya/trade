"""Fail-closed integration boundary for the agent-krypto execution cycle."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from services.crypto_strategy_research import ArtifactRejected, require_promoted_artifact


def _wait(reason: str) -> dict[str, Any]:
    return {"status": "WAIT", "reason": reason, "execution_result": None}


def run_agent_krypto_cycle(
    *,
    artifact: Mapping[str, Any] | Any,
    symbol: str,
    expected_dataset_version: str,
    expected_feature_schema_version: str,
    expected_promotion_policy_version: str,
    trade_intent: Mapping[str, Any] | None,
    execute: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    sync_ok: bool = True,
    position_ok: bool = True,
    dataset_ok: bool = True,
) -> dict[str, Any]:
    """Gate one intent and execute it only when every upstream check is clean.

    ``execute`` is injected deliberately: production can bind the OKX Demo
    adapter while tests use an offline fake.  No failure is converted into a
    trade; every uncertain state returns WAIT with no successful completion.
    """
    for ready, reason in (
        (sync_ok, "portfolio sync unavailable"),
        (position_ok, "position state unavailable"),
        (dataset_ok, "dataset unavailable or incompatible"),
    ):
        if not ready:
            return _wait(reason)

    try:
        require_promoted_artifact(
            artifact,
            symbol=symbol,
            expected_dataset_version=expected_dataset_version,
            expected_feature_schema_version=expected_feature_schema_version,
            expected_promotion_policy_version=expected_promotion_policy_version,
        )
    except (ArtifactRejected, TypeError, ValueError) as exc:
        return _wait(f"strategy rejected: {exc}")

    if not trade_intent:
        return _wait("missing TradeIntent")
    if not isinstance(trade_intent, Mapping):
        return _wait("invalid TradeIntent")

    # WAIT is a complete agent decision, not an execution request.  Keeping
    # this branch at the backend boundary prevents a declarative WAIT payload
    # from ever reaching #99 (and therefore from being interpreted as a
    # malformed order by an adapter).
    decision = trade_intent.get("decision")
    if decision == "WAIT":
        reason = str(trade_intent.get("reason") or "agent selected WAIT")
        return _wait(reason)
    if decision not in (None, "TRADE"):
        return _wait(f"invalid TradeIntent decision: {decision}")

    try:
        result = dict(execute(trade_intent))
    except Exception as exc:  # execution boundary must be fail-closed
        return _wait(f"execution failed: {exc}")

    if not result.get("ok") or result.get("state") != "filled":
        response = _wait(f"execution not final: {result.get('state', 'unknown')}")
        response["execution_result"] = result
        return response
    return {"status": "COMPLETED", "reason": None, "execution_result": result}
