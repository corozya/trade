#!/usr/bin/env python3
"""Validate and serialize the complete BTC/ETH/DOGE analysis set for a prompt."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

SYMBOLS = ("BTC", "ETH", "DOGE")
REQUIRED_SECTIONS = ("price", "indicators_15m", "higher_tf_context", "futures")


def _parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError("brak analyzed_at")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def prepare(
    data_dir: Path,
    max_age_seconds: int,
    cycle_started_at: datetime,
    now: datetime | None = None,
    cycle_tolerance_seconds: int = 2,
) -> dict[str, dict]:
    if max_age_seconds <= 0:
        raise ValueError("max_age_seconds musi być dodatnie")
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    cycle_started_at = cycle_started_at.astimezone(timezone.utc)
    snapshots: dict[str, dict] = {}

    for expected in SYMBOLS:
        path = data_dir / f"{expected}_analysis.json"
        try:
            payload = json.loads(path.read_text())
        except FileNotFoundError as exc:
            raise ValueError(f"{expected}: brak {path}") from exc
        except json.JSONDecodeError as exc:
            raise ValueError(f"{expected}: invalid JSON w {path}: {exc}") from exc

        if not isinstance(payload, dict):
            raise ValueError(f"{expected}: root snapshotu nie jest obiektem")
        actual_symbol = payload.get("symbol")
        if not isinstance(actual_symbol, str) or actual_symbol.split("-", 1)[0] != expected:
            raise ValueError(f"{expected}: mismatched symbol={actual_symbol!r}")
        missing = [key for key in REQUIRED_SECTIONS if not isinstance(payload.get(key), dict)]
        if missing:
            raise ValueError(f"{expected}: brak sekcji: {', '.join(missing)}")

        analyzed_at = _parse_timestamp(payload.get("analyzed_at"))
        age = (now - analyzed_at).total_seconds()
        if age < -60:
            raise ValueError(f"{expected}: analyzed_at jest w przyszłości o {-age:.0f}s")
        if age > max_age_seconds:
            raise ValueError(f"{expected}: stale snapshot age={age:.0f}s > {max_age_seconds}s")
        pre_cycle_seconds = (cycle_started_at - analyzed_at).total_seconds()
        if pre_cycle_seconds > cycle_tolerance_seconds:
            raise ValueError(
                f"{expected}: pre-cycle snapshot analyzed_at={payload.get('analyzed_at')} "
                f"jest wcześniejszy od cycle start o {pre_cycle_seconds:.0f}s"
            )
        snapshots[expected] = payload

    return snapshots


def main() -> int:
    if len(sys.argv) != 4:
        print(
            "użycie: prepare_agent_krypto_snapshots.py DATA_DIR MAX_AGE_SECONDS CYCLE_STARTED_AT",
            file=sys.stderr,
        )
        return 2
    try:
        result = prepare(Path(sys.argv[1]), int(sys.argv[2]), _parse_timestamp(sys.argv[3]))
    except (OSError, ValueError) as exc:
        print(exc, file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
