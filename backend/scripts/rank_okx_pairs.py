#!/usr/bin/env python3
"""Read-only OKX market scanner ranked by 24h turnover and price change.

Examples::

    .venv/bin/python scripts/rank_okx_pairs.py --quote USDT --limit 30
    .venv/bin/python scripts/rank_okx_pairs.py --sort change --json

The endpoint is public market data (the client signs requests consistently
with the rest of this project).  This script never imports execution modules
and never calls an order endpoint.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

BACKEND_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_ROOT))

from services.okx_client import OkxClient, OkxError


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def normalize_tickers(payload: Any, quote: str | None = "USDT") -> list[dict[str, Any]]:
    """Convert an OKX response to sortable, JSON-friendly ranking rows."""
    rows = payload.get("data", []) if isinstance(payload, dict) else payload
    result: list[dict[str, Any]] = []
    for ticker in rows or []:
        if not isinstance(ticker, dict):
            continue
        inst_id = str(ticker.get("instId", ""))
        if quote and not inst_id.endswith(f"-{quote}-SWAP"):
            continue
        last = _number(ticker.get("last"))
        open24h = _number(ticker.get("open24h"))
        vol_ccy = _number(ticker.get("volCcy24h"))
        vol = _number(ticker.get("vol24h"))
        # Derivative volCcy24h is contract/base volume on some instruments;
        # multiplying by last gives a comparable quote-currency turnover.
        turnover = vol_ccy * last if vol_ccy and last else vol * last
        change = ((last - open24h) / open24h * 100.0) if open24h else None
        result.append({
            "symbol": inst_id,
            "last": last,
            "change_24h_pct": change,
            "turnover_24h": turnover,
            "vol24h": vol,
            "volCcy24h": vol_ccy,
            "high24h": _number(ticker.get("high24h")),
            "low24h": _number(ticker.get("low24h")),
            "ts": ticker.get("ts"),
        })
    return result


def rank_tickers(rows: list[dict[str, Any]], sort: str) -> list[dict[str, Any]]:
    if sort == "turnover":
        return sorted(rows, key=lambda row: row.get("turnover_24h") or 0.0, reverse=True)
    # "change" means largest movers in either direction (absolute percent).
    return sorted(rows, key=lambda row: abs(row.get("change_24h_pct") or 0.0), reverse=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("alias", nargs="?", default="demo_main_full")
    parser.add_argument("--inst-type", default="SWAP", choices=("SWAP", "FUTURES", "OPTION", "MARGIN"))
    parser.add_argument("--quote", default="USDT", help="quote suffix filter; use empty string for all")
    parser.add_argument("--sort", choices=("turnover", "change"), default="turnover")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)
    try:
        with OkxClient(args.alias) as client:
            rows = normalize_tickers(client.get_tickers(args.inst_type), args.quote or None)
    except OkxError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    rows = rank_tickers(rows, args.sort)[: max(0, args.limit)]
    if args.as_json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        print("rank\tsymbol\tlast\tchange_24h_pct\tturnover_24h")
        for index, row in enumerate(rows, 1):
            change = "" if row["change_24h_pct"] is None else f"{row['change_24h_pct']:.2f}%"
            print(f"{index}\t{row['symbol']}\t{row['last']:.10g}\t{change}\t{row['turnover_24h']:.6g}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
