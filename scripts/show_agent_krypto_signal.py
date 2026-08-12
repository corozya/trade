#!/usr/bin/env python3
"""Read-only summary of the latest research-loop cycle for a human operator.

Reads the last JSON line from research_loop_observation.log and prints a
short, human-readable view of what the agent currently thinks about each
symbol: evaluate verdict, experiment failures, and the paper-trading signal
per symbol (decision/side/entry/SL/TP). Never touches OKX, never writes
anything -- pure log reader.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOG_PATH = (
    ROOT / "research" / "agent-krypto" / "logs" / "research_loop_observation.log"
)


def _last_json_lines(log_path: Path, count: int) -> list[dict]:
    lines = log_path.read_text().splitlines()
    parsed: list[dict] = []
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            parsed.append(json.loads(line))
        except json.JSONDecodeError:
            continue
        if len(parsed) >= count:
            break
    return list(reversed(parsed))


def _print_entry(entry: dict) -> None:
    run_id = entry.get("run_id")
    status = entry.get("status")
    provider = entry.get("provider")
    print(f"run_id: {run_id}  status: {status}  provider: {provider}")

    if status == "ERROR":
        print(f"  reason: {entry.get('reason')}")
        return

    result = entry.get("result") or {}
    phases = result.get("phases", {})

    experiment = phases.get("experiment", {})
    evaluate = phases.get("evaluate", {})
    paper = phases.get("paper", {})

    print(f"  experiment: {experiment.get('status')}")
    failures = experiment.get("failures") or []
    for f in failures:
        print(f"    - {f}")
    print(f"  evaluate: {evaluate.get('status')}")

    signal = paper.get("signal", {})
    if signal:
        print(
            f"  signal: feature={signal.get('feature')} "
            f"lookback={signal.get('lookback')} threshold={signal.get('threshold')}"
        )

    decisions = paper.get("decisions") or []
    by_symbol: dict[str, list[dict]] = {}
    for d in decisions:
        by_symbol.setdefault(d["symbol"], []).append(d)

    print("  paper decisions per symbol:")
    for symbol, rows in by_symbol.items():
        opens = [r for r in rows if r["decision"] == "OPEN"]
        waits = [r for r in rows if r["decision"] == "WAIT"]
        closes = [r for r in rows if r["decision"] == "CLOSE"]
        if opens:
            r = opens[-1]
            print(
                f"    {symbol}: OPEN {r.get('side')} @ {r.get('entry_price')} "
                f"SL={r.get('stop_loss_price')} TP={r.get('take_profit_price')}"
            )
        elif waits:
            print(f"    {symbol}: WAIT")
        if closes:
            r = closes[-1]
            pnl = r.get("pnl")
            print(f"    {symbol}: (closed prior position, pnl={pnl})")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log-path", type=Path, default=DEFAULT_LOG_PATH)
    parser.add_argument(
        "--count", type=int, default=1, help="how many recent cycles to show"
    )
    args = parser.parse_args(argv)

    if not args.log_path.is_file():
        print(f"no log file at {args.log_path}")
        return 1

    entries = _last_json_lines(args.log_path, args.count)
    if not entries:
        print("no parsable entries found")
        return 1

    for entry in entries:
        _print_entry(entry)
        print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
