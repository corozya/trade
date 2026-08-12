#!/usr/bin/env python3
"""#228 (podzadanie #227) — shared backfill CLI for precomputed indicators
(rsi, macd, stochastic, atr, risk_indicator) into ``CryptoDataLake``.

This is the indicator-side counterpart to ``crypto_backfill_cli.py`` /
``crypto_backfill_open_interest.py``, but there is no OKX API call anywhere
in this script: every indicator is a pure transformation of an
already-backfilled local ``ohlcv`` series (see
``services.crypto_indicator_backfill`` module docstring for the full design
rationale — the ``IndicatorSpec`` registry, why --incremental recomputes
from a bounded trailing window instead of persisting Wilder/EMA state
separately, and the step-by-step "Adding a new indicator" guide that
#229/#230/#231 use to wire in macd/stochastic/atr/risk_indicator without
touching this script).

## Two-track model (mirrors OI/funding's --full / --incremental, #164/#167)

1. ``--full`` — recompute the indicator over the *entire* available ohlcv
   history for one (symbol, timeframe) and publish as a fresh dataset
   version (no ``base_dataset_id`` merge — same "don't merge into a run that
   might have settled differently" rule as every other --full backfill in
   this project, see ``crypto_backfill_cli.py::_backfill_one_pair``).
2. ``--incremental`` — resume from the last published indicator point
   (via ``services.crypto_backfill.resume_cursor``, keyed the same way as
   every other data_kind) and append only the newly computable points, using
   a bounded trailing window of ohlcv history for correct Wilder/EMA
   convergence (see module docstring above).

## Requires an already-backfilled ohlcv series

Unlike OI/funding, this script does NOT fetch its own source data — it reads
whatever ``ohlcv`` dataset is already in ``<lake-root>/raw/latest.json`` for
the requested (symbol, timeframe). Run ``crypto_backfill_cli.py --data-kind
ohlcv`` first (already done for all ``CryptoDataLake.SYMBOLS`` x TIMEFRAMES
as of #228). If the ohlcv key is missing this exits with a clear error
rather than silently producing nothing.

Usage:
  crypto_backfill_indicators.py --full --indicator rsi \
      --symbol BTC-USDT-SWAP --timeframe 15m --lake-root /app/research/agent-krypto
  crypto_backfill_indicators.py --incremental --indicator rsi \
      --symbol BTC-USDT-SWAP --timeframe 15m --lake-root /app/research/agent-krypto
  crypto_backfill_indicators.py --full --indicator rsi \
      --symbols BTC-USDT-SWAP,ETH-USDT-SWAP --timeframes 15m,1h --lake-root /app/research/agent-krypto
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger(__name__)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

# Same convention as the other backfill scripts: .env lives at repo root
# (BOT/.env). Not strictly needed here (no OKX creds used), kept for
# consistency in case a future indicator needs config from it.
load_dotenv(Path(__file__).resolve().parents[2] / ".env")

# crypto-dashboard/backend hosts indicators.py/risk_indicator.py — the raw
# transform functions this script's IndicatorSpec registry wraps (see
# services/crypto_indicator_backfill.py "Adding a new indicator").
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.crypto_backfill import (
    BackfillMode,
    add_mode_arguments,
    resume_cursor,
)
from services.crypto_data_lake import SYMBOLS, TIMEFRAMES, CryptoDataLake
from services.crypto_indicator_backfill import (
    INDICATOR_SPECS,
    compute_indicator_records,
    run_support_resistance_backfill,
)
from services.crypto_market_ingestion import CryptoMarketIngestor, FixtureMarketDataAdapter, IngestionError

# #233: support_resistance is not a 1-row-per-bar IndicatorSpec (see
# services.crypto_indicator_backfill module docstring / sr_levels.py) — it
# always does a full recompute (no bounded incremental window is safe for
# stateful levels), so it's dispatched separately from INDICATOR_SPECS below
# rather than forced into that registry's per-bar transform contract.
_SUPPORT_RESISTANCE_DATA_KIND = "support_resistance"


def _registry_path(lake_root: Path) -> Path:
    return lake_root / "raw" / "latest.json"


def _load_registry(lake_root: Path) -> dict[str, str]:
    path = _registry_path(lake_root)
    if not path.is_file():
        return {}
    return json.loads(path.read_text())


def _save_registry(lake_root: Path, registry: dict[str, str]) -> None:
    """Merge onto whatever is on disk before writing — same rationale as
    crypto_backfill_cli.py::_save_registry (concurrent processes write
    disjoint keys; a naive overwrite loses the other process's keys)."""
    import os

    path = _registry_path(lake_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    on_disk = _load_registry(lake_root)
    merged = {**on_disk, **registry}
    tmp = path.with_suffix(f".tmp-{os.getpid()}")
    tmp.write_text(json.dumps(merged, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, path)


def _registry_key(*, data_kind: str, symbol: str, timeframe: str) -> str:
    return f"{data_kind}/{symbol}/{timeframe}"


def _parse_csv_list(value: str | None) -> list[str] | None:
    if value is None:
        return None
    items = [item.strip() for item in value.split(",") if item.strip()]
    if not items:
        raise argparse.ArgumentTypeError("expected a non-empty comma-separated list")
    return items


def _backfill_one_pair(
    *,
    lake: CryptoDataLake,
    lake_root: Path,
    registry: dict[str, str],
    mode: BackfillMode,
    indicator: str,
    symbol: str,
    timeframe: str,
    base_dataset_id_override: str | None,
) -> dict[str, Any]:
    ohlcv_dataset_id = registry.get(_registry_key(data_kind="ohlcv", symbol=symbol, timeframe=timeframe))
    if ohlcv_dataset_id is None:
        raise IngestionError(
            f"no backfilled ohlcv for {symbol}/{timeframe} — run crypto_backfill_cli.py "
            f"--data-kind ohlcv first (see module docstring)"
        )

    if indicator == _SUPPORT_RESISTANCE_DATA_KIND:
        # #233: always a full recompute over the entire ohlcv history — see
        # run_support_resistance_backfill docstring for why no bounded
        # incremental window is safe here. --incremental and --full both
        # take this path; the distinction that matters is whether we MERGE
        # into the prior dataset version (incremental, so already-published
        # events stay content-addressed/unchanged and only new ones are
        # appended) or start a clean version (--full, same "deliberate
        # clean recompute" rule as every other --full backfill).
        data_kind = _SUPPORT_RESISTANCE_DATA_KIND
        key = _registry_key(data_kind=data_kind, symbol=symbol, timeframe=timeframe)
        resume_base_dataset_id = base_dataset_id_override or registry.get(key)
        records = run_support_resistance_backfill(lake=lake, ohlcv_dataset_id=ohlcv_dataset_id)
        if not records:
            raise SystemExit(
                f"no support_resistance level events for {symbol}/{timeframe} "
                f"(not enough ohlcv history yet, or no pivots passed the volume filter)"
            )
        ingest_base_dataset_id = resume_base_dataset_id if mode is BackfillMode.INCREMENTAL else None
        adapter = FixtureMarketDataAdapter(
            records=records,
            source_metadata={
                "indicator": indicator,
                "data_kind": data_kind,
                "ohlcv_dataset_id": ohlcv_dataset_id,
                "symbol": symbol,
                "timeframe": timeframe,
                "mode": mode.value,
            },
            name=f"crypto-indicator-backfill/{indicator}",
        )
        version = CryptoMarketIngestor(lake).ingest([adapter], base_dataset_id=ingest_base_dataset_id)
        registry[key] = version.dataset_id
        _save_registry(lake_root, registry)
        return {
            "ok": True,
            "mode": mode.value,
            "indicator": indicator,
            "data_kind": data_kind,
            "symbol": symbol,
            "timeframe": timeframe,
            "ohlcv_dataset_id": ohlcv_dataset_id,
            "dataset_id": version.dataset_id,
            "row_count": version.manifest["row_count"],
            "new_points_this_run": len(records),
        }

    spec = INDICATOR_SPECS[indicator]

    key = _registry_key(data_kind=spec.data_kind, symbol=symbol, timeframe=timeframe)
    resume_base_dataset_id = base_dataset_id_override or registry.get(key)

    cursor_at = resume_cursor(
        lake,
        base_dataset_id=resume_base_dataset_id,
        symbol=symbol,
        timeframe=timeframe,
        data_kind=spec.data_kind,
    )

    records = compute_indicator_records(
        spec,
        lake=lake,
        ohlcv_dataset_id=ohlcv_dataset_id,
        resume_cursor_at=cursor_at,
        mode_is_full=(mode is BackfillMode.FULL),
    )
    if not records:
        raise SystemExit(
            f"no new {spec.data_kind} points for {symbol}/{timeframe} "
            f"(cursor={cursor_at}, mode={mode.value})"
        )

    # --full must not merge into a prior run's dataset — same rule as every
    # other --full backfill in this project (crypto_backfill_cli.py comment,
    # crypto_backfill_open_interest.py comment): a deliberate clean recompute,
    # not an accidental double-write.
    ingest_base_dataset_id = resume_base_dataset_id if mode is BackfillMode.INCREMENTAL else None

    adapter = FixtureMarketDataAdapter(
        records=records,
        source_metadata={
            "indicator": indicator,
            "data_kind": spec.data_kind,
            "ohlcv_dataset_id": ohlcv_dataset_id,
            "symbol": symbol,
            "timeframe": timeframe,
            "mode": mode.value,
        },
        name=f"crypto-indicator-backfill/{indicator}",
    )
    version = CryptoMarketIngestor(lake).ingest([adapter], base_dataset_id=ingest_base_dataset_id)

    registry[key] = version.dataset_id
    _save_registry(lake_root, registry)

    return {
        "ok": True,
        "mode": mode.value,
        "indicator": indicator,
        "data_kind": spec.data_kind,
        "symbol": symbol,
        "timeframe": timeframe,
        "ohlcv_dataset_id": ohlcv_dataset_id,
        "dataset_id": version.dataset_id,
        "row_count": version.manifest["row_count"],
        "new_points_this_run": len(records),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    add_mode_arguments(parser)
    parser.add_argument(
        "--indicator",
        required=True,
        choices=sorted({*INDICATOR_SPECS, _SUPPORT_RESISTANCE_DATA_KIND}),
        help="'rsi'/'macd' registered as of #228/#229 — #230/#231 add "
        "stochastic/atr/risk_indicator to services.crypto_indicator_backfill.INDICATOR_SPECS. "
        "'support_resistance' (#233) is dispatched separately (always full recompute, see module docstring).",
    )
    symbol_group = parser.add_mutually_exclusive_group()
    symbol_group.add_argument("--symbol", help="Single symbol (legacy one-pair invocation).")
    symbol_group.add_argument(
        "--symbols",
        help="Comma-separated symbols. Defaults to all CryptoDataLake.SYMBOLS when omitted "
        "together with --symbol.",
    )
    timeframe_group = parser.add_mutually_exclusive_group()
    timeframe_group.add_argument("--timeframe", help="Single timeframe (legacy one-pair invocation).")
    timeframe_group.add_argument(
        "--timeframes",
        help="Comma-separated timeframes. Defaults to all CryptoDataLake.TIMEFRAMES when "
        "omitted together with --timeframe.",
    )
    args = parser.parse_args(argv)

    symbols = _parse_csv_list(args.symbols) or ([args.symbol] if args.symbol else list(SYMBOLS))
    timeframes = _parse_csv_list(args.timeframes) or ([args.timeframe] if args.timeframe else list(TIMEFRAMES))

    lake_root = Path(args.lake_root)
    lake = CryptoDataLake(lake_root)
    registry = _load_registry(lake_root)

    total_pairs = len(symbols) * len(timeframes)
    log.info(
        "backfill start: indicator=%s mode=%s pairs=%d symbols=%s timeframes=%s",
        args.indicator, args.mode.value, total_pairs, ",".join(symbols), ",".join(timeframes),
    )

    results: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    pair_num = 0
    for symbol in symbols:
        for timeframe in timeframes:
            pair_num += 1
            log.info("[%d/%d] %s %s: liczę...", pair_num, total_pairs, symbol, timeframe)
            try:
                result = _backfill_one_pair(
                    lake=lake,
                    lake_root=lake_root,
                    registry=registry,
                    mode=args.mode,
                    indicator=args.indicator,
                    symbol=symbol,
                    timeframe=timeframe,
                    base_dataset_id_override=(
                        args.base_dataset_id if len(symbols) == 1 and len(timeframes) == 1 else None
                    ),
                )
                results.append(result)
                log.info(
                    "[%d/%d] %s %s: OK, %d wierszy (+%d nowych, dataset %s)",
                    pair_num, total_pairs, symbol, timeframe,
                    result["row_count"], result["new_points_this_run"], result["dataset_id"],
                )
            except (SystemExit, IngestionError, ValueError) as exc:
                log.warning("[%d/%d] %s %s: BLAD %s", pair_num, total_pairs, symbol, timeframe, exc)
                failures.append({"symbol": symbol, "timeframe": timeframe, "error": str(exc)})

    log.info("backfill zakonczony: %d ok, %d bledow", len(results), len(failures))

    print(
        json.dumps(
            {
                "ok": not failures,
                "indicator": args.indicator,
                "mode": args.mode.value,
                "results": results,
                "failures": failures,
            },
            indent=2,
        )
    )
    return 1 if failures and not results else 0


if __name__ == "__main__":
    raise SystemExit(main())
