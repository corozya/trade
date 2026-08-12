"""Frozen research policy helpers for the multi-symbol offline loop (#111)."""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any, Mapping


def canonical_version(prefix: str, payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return f"{prefix}-{hashlib.sha256(encoded).hexdigest()[:16]}"


def corrected_expectancy_threshold(
    *, base_threshold: float, trial_count: int, oos_trade_count: int
) -> float:
    """Conservative Bonferroni-style search penalty.

    The square-root penalty is monotone in *all* attempted trials (accepted
    and rejected).  It cannot make the gate easier as the search grows.
    """
    if trial_count < 1:
        raise ValueError("trial_count must include every attempted trial")
    if oos_trade_count < 1:
        raise ValueError("oos_trade_count must be positive")
    # The first pre-registered trial is the unpenalized baseline. Every
    # additional attempt (including rejected ones) can only raise the gate.
    return base_threshold + math.sqrt(2.0 * math.log(trial_count) / oos_trade_count)


def atomic_update_versions(
    path: str | Path, *, label_version: str, policy_version: str
) -> dict[str, Any]:
    target = Path(path)
    payload = json.loads(target.read_text())
    payload["label_config_version"] = label_version
    payload["promotion_policy_version"] = policy_version
    temporary = target.with_suffix(f".{os.getpid()}.tmp")
    with temporary.open("w") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, target)
    return payload
