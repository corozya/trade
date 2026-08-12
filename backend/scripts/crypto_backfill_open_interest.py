#!/usr/bin/env python3
"""#164 — Open Interest backfill (1D history + 5m current tail) into CryptoDataLake.

Podzadanie #161, blocked-by #167 (shared retry/idempotency/full-incremental
infra in ``services.crypto_backfill`` — already delivered). This script is
intentionally standalone rather than wired into ``crypto_backfill_cli.py``'s
``--data-kind`` dispatch: that shared CLI is being extended concurrently for
#162 (OHLCV multi-symbol/multi-timeframe) and this script does not need to
block on or collide with that work landing. It reuses the exact same shared
building blocks (``services.crypto_backfill``) so folding it into the
dispatcher later is a thin wiring change, not a rewrite.

## Endpoint

``GET /api/v5/rubik/stat/contracts/open-interest-volume`` via
``OkxClient.get_open_interest_history()`` (services/okx_client.py). Verified
facts (#161 research, Obsidian
``Projekty/BOT/OKX-Dane-Historyczne-OI-Funding-Research.md``), NOT guessed:

- The documented ``contract-open-interest-history`` endpoint is DEAD (always
  returns ``{"data":[]}``); the correct endpoint is
  ``rubik/stat/contracts/open-interest-volume``.
- Parameter is ``ccy`` (base currency, e.g. "BTC"), NOT ``instId`` — data is
  aggregated across all contracts of that base currency, not per instId.
- Returns ``[ts, oi, vol]`` in quote-currency (USD-equivalent).
- ``period`` in {5m, 1H, 1D}. Verified ceilings: 1D -> ~180 points (~6
  months), 5m -> ~575 points (~2 days) — finer period means SHORTER lookback,
  a hard REST ceiling independent of pagination (rubik/stat/* has no
  after/before pagination beyond that ceiling, unlike funding-rate-history
  or candles/history-candles).

## Two-track model (#164 scope, not to be conflated)

1. ``--timeframe 1d`` backward backfill — the only granularity that
   physically reaches back toward the ~6-month REST ceiling. "12 months" is
   explicitly NOT reachable here and this script does not attempt to work
   around that (per #164 task description).
2. ``--timeframe 5m`` current-tail collection — the densest available
   granularity, meant to run incrementally (cron) from deployment onward. It
   is a growing tail, not a backfill: REST cannot retroactively densify
   history older than what OKX's server already retains at 5m resolution.

Both tracks share the same adapter (``OpenInterestHistoryAdapter``) and the
same ``services.crypto_backfill`` idempotency/resume/retry contract — the
only difference is which ``--timeframe`` an operator passes, exactly as for
OHLCV's per-timeframe independent pulls.

## Point samples, not candles

OI rows are point-in-time samples, not OHLC candles: ``available_at`` is set
equal to ``observed_at`` (no forward-looking "candle close" delay to model),
so ``CryptoDataLake.read_as_of_duckdb`` as-of lookups work correctly without
needing OHLCV's ``shift(1)`` guard.

## Multi-symbol join (#164 PM addendum, consultation with Zarządca-Ryzyka)

Because OI is aggregated per base currency, the 5 symbols' OI never overlap
in a single row and cannot be correlated at one instant without a deliberate
join. See ``CryptoDataLake.read_multi_symbol_as_of_duckdb`` in
``services/crypto_data_lake.py`` — this script's ``--example-join`` flag
demonstrates it end-to-end against one just-published dataset.

Usage:
  crypto_backfill_open_interest.py --full --timeframe 1d \
      --symbol BTC-USDT-SWAP --lake-root /app/data/lake --alias demo_main_full
  crypto_backfill_open_interest.py --incremental --timeframe 5m \
      --symbol BTC-USDT-SWAP --lake-root /app/data/lake --alias demo_main_full
  crypto_backfill_open_interest.py --full --timeframe 1d \
      --symbols BTC-USDT-SWAP,ETH-USDT-SWAP --lake-root /app/data/lake --alias demo_main_full
  crypto_backfill_open_interest.py --full --timeframe 1d \
      --lake-root /app/data/lake --alias demo_main_full  # all CryptoDataLake.SYMBOLS
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger(__name__)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

# Same convention as main.py: .env lives at repo root (BOT/.env), three
# levels above this script (scripts/ -> backend/ -> portfolio-tracker/ -> BOT/).
load_dotenv(Path(__file__).resolve().parents[2] / ".env")

from services.crypto_backfill import (
    BackfillMode,
    BackfillRunner,
    add_mode_arguments,
    resolve_since,
)
from services.crypto_data_lake import SYMBOLS, CryptoDataLake
from services.okx_client import OkxClient

DATA_KIND = "open_interest"

# rubik/stat/* has no deep pagination beyond its own ceiling (#161 research):
# period=1D -> ~180 pts (~6mo), period=5m -> ~575 pts (~2 days). A --full run
# always starts "as far back as the endpoint will give" — there is no benefit
# to requesting an earlier `since` than the API can ever satisfy, and no
# harm: the adapter simply stops once OKX returns an empty/older-duplicate page.
_HORIZON_BY_TIMEFRAME = {
    "1d": timedelta(days=190),  # slightly past the ~180pt/~6mo ceiling
    "5m": timedelta(days=3),  # slightly past the ~575pt/~2day ceiling
}


def _symbol_to_ccy(symbol: str) -> str:
    # "BTC-USDT-SWAP" -> "BTC". CryptoDataLake.SYMBOLS are always
    # "{ccy}-USDT-SWAP" (see services/crypto_data_lake.py SYMBOLS tuple).
    return symbol.split("-")[0]


def _to_okx_period(timeframe: str) -> str:
    # CryptoDataLake.TIMEFRAMES use lowercase ("1d"); OKX period param wants "1D".
    if timeframe == "1d":
        return "1D"
    if timeframe == "1h":
        return "1H"
    return timeframe  # "5m" is already correct as-is


class OpenInterestHistoryAdapter:
    """Adapter for ``rubik/stat/contracts/open-interest-volume`` (#164).

    One instance backfills one (symbol, timeframe) pair. ``timeframe`` must
    be "1d" (backward-looking backfill track) or "5m" (current-tail track,
    see module docstring) — "1h" is accepted too since the endpoint supports
    it, but #164's scope only wires 1d/5m into the CLI below.
    """

    name = "okx-open-interest-volume"

    def __init__(self, client: OkxClient, *, symbol: str, timeframe: str, since: datetime):
        self._client = client
        self.symbol = symbol
        self.timeframe = timeframe
        self.since = since

    def fetch(self) -> Iterable[Mapping[str, Any]]:
        ccy = _symbol_to_ccy(self.symbol)
        period = _to_okx_period(self.timeframe)
        rows: list[dict[str, Any]] = []
        after: str | None = None
        seen_ts: set[str] = set()
        while True:
            payload = self._client.get_open_interest_history(
                ccy, period=period, limit=100, after=after
            )
            points = payload.get("data", []) if isinstance(payload, dict) else []
            if not points:
                break
            stop = False
            progressed = False
            for point in points:
                ts_raw = str(point[0])
                if ts_raw in seen_ts:
                    continue  # endpoint has been observed to not strictly page past its ceiling
                seen_ts.add(ts_raw)
                ts_ms = int(ts_raw)
                observed_at = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
                if observed_at <= self.since:
                    stop = True
                    continue
                progressed = True
                rows.append(
                    {
                        "symbol": self.symbol,
                        "timeframe": self.timeframe,
                        "data_kind": DATA_KIND,
                        # Point sample, not a candle: available_at == observed_at
                        # (#164 scope note) so as-of joins need no shift(1) guard.
                        "observed_at": observed_at,
                        "available_at": observed_at,
                        "source": self.name,
                        "open_interest": float(point[1]),
                        "volume": float(point[2]),
                    }
                )
            after = points[-1][0]
            if stop or not progressed or len(points) < 100:
                break
        if not rows:
            raise SystemExit(
                f"no new open interest points for {self.symbol}/{self.timeframe} "
                f"since {self.since.isoformat()}"
            )
        return rows

    def lineage(self) -> Mapping[str, Any]:
        return {
            "name": self.name,
            "endpoint": "/api/v5/rubik/stat/contracts/open-interest-volume",
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "since": self.since.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        }


def _registry_path(lake_root: Path) -> Path:
    return lake_root / "raw" / "latest.json"


def _load_registry(lake_root: Path) -> dict[str, str]:
    path = _registry_path(lake_root)
    if not path.is_file():
        return {}
    return json.loads(path.read_text())


def _save_registry(lake_root: Path, registry: Mapping[str, str]) -> None:
    """Merge ``registry`` into whatever is currently on disk before writing.

    See crypto_backfill_cli.py::_save_registry for why: without this,
    running this script alongside crypto_backfill_cli.py (different
    process, same registry file) silently erases whichever process's keys
    were written first once the other one saves its own stale in-memory
    copy. Reproduced in production.
    """
    path = _registry_path(lake_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    on_disk = _load_registry(lake_root)
    merged = {**on_disk, **registry}
    tmp = path.with_suffix(f".tmp-{os.getpid()}")
    tmp.write_text(json.dumps(merged, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, path)


def _registry_key(*, symbol: str, timeframe: str) -> str:
    return f"{DATA_KIND}/{symbol}/{timeframe}"


def _print_example_join(lake: CryptoDataLake, dataset_id: str, *, timeframe: str) -> None:
    """Demonstrate the multi-symbol as-of join (#164 PM addendum)."""
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    joined = lake.read_multi_symbol_as_of_duckdb(
        dataset_id,
        now,
        data_kind=DATA_KIND,
        timeframe=timeframe,
        value_column="open_interest",
    )
    preview = joined.to_pylist()[-5:]
    print(json.dumps({"example_join_preview_last_5_rows": preview}, default=str))


def _parse_csv_list(value: str | None) -> list[str] | None:
    if value is None:
        return None
    items = [item.strip() for item in value.split(",") if item.strip()]
    if not items:
        raise argparse.ArgumentTypeError("expected a non-empty comma-separated list")
    return items


def _backfill_one_symbol(
    *,
    client: OkxClient,
    lake: CryptoDataLake,
    lake_root: Path,
    registry: dict[str, str],
    mode: BackfillMode,
    symbol: str,
    timeframe: str,
    base_dataset_id_override: str | None,
) -> dict[str, Any]:
    key = _registry_key(symbol=symbol, timeframe=timeframe)
    resume_base_dataset_id = base_dataset_id_override or registry.get(key)

    horizon_start = datetime.now(timezone.utc) - _HORIZON_BY_TIMEFRAME.get(
        timeframe, timedelta(days=190)
    )
    since = resolve_since(
        mode=mode,
        lake=lake,
        base_dataset_id=resume_base_dataset_id,
        symbol=symbol,
        timeframe=timeframe,
        data_kind=DATA_KIND,
        full_horizon_start=horizon_start,
    )

    # See crypto_backfill_cli.py::_backfill_one_pair for why --full must not
    # merge into a prior run's dataset (spurious "conflicting duplicate
    # record" on a bar whose value settled differently between two pulls).
    ingest_base_dataset_id = resume_base_dataset_id if mode is BackfillMode.INCREMENTAL else None

    adapter = OpenInterestHistoryAdapter(client, symbol=symbol, timeframe=timeframe, since=since)
    version = BackfillRunner(lake).run(adapter, base_dataset_id=ingest_base_dataset_id)

    registry[key] = version.dataset_id
    _save_registry(lake_root, registry)

    return {
        "ok": True,
        "mode": mode.value,
        "data_kind": DATA_KIND,
        "symbol": symbol,
        "timeframe": timeframe,
        "since": since.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "dataset_id": version.dataset_id,
        "row_count": version.manifest["row_count"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    add_mode_arguments(parser)
    parser.add_argument(
        "--timeframe",
        required=True,
        choices=["1d", "5m", "1h"],
        help="1d = wsteczny backfill (~6mo ceiling), 5m = rosnący ogon bieżący (cron).",
    )
    symbol_group = parser.add_mutually_exclusive_group()
    symbol_group.add_argument("--symbol", help="Single symbol (legacy one-pair invocation).")
    symbol_group.add_argument(
        "--symbols",
        help="Comma-separated symbols. Defaults to all CryptoDataLake.SYMBOLS when omitted "
        "together with --symbol.",
    )
    parser.add_argument("--alias", required=True, help="OKX credentials alias (env OKX_<ALIAS>_*)")
    parser.add_argument(
        "--example-join",
        action="store_true",
        help="Po backfillu wypisz przykład multi-symbol as-of join (patrz "
        "CryptoDataLake.read_multi_symbol_as_of_duckdb) na opublikowanym dataset_id.",
    )
    args = parser.parse_args(argv)

    symbols = _parse_csv_list(args.symbols) or ([args.symbol] if args.symbol else list(SYMBOLS))

    lake_root = Path(args.lake_root)
    lake = CryptoDataLake(lake_root)
    registry = _load_registry(lake_root)

    log.info(
        "backfill start: data_kind=%s timeframe=%s mode=%s symbols=%s",
        DATA_KIND, args.timeframe, args.mode.value, ",".join(symbols),
    )

    results: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    with OkxClient(args.alias) as client:
        for i, symbol in enumerate(symbols, start=1):
            log.info("[%d/%d] %s %s: pobieram...", i, len(symbols), symbol, args.timeframe)
            try:
                result = _backfill_one_symbol(
                    client=client,
                    lake=lake,
                    lake_root=lake_root,
                    registry=registry,
                    mode=args.mode,
                    symbol=symbol,
                    timeframe=args.timeframe,
                    base_dataset_id_override=args.base_dataset_id if len(symbols) == 1 else None,
                )
                results.append(result)
                log.info(
                    "[%d/%d] %s %s: OK, %d wierszy (dataset %s)",
                    i, len(symbols), symbol, args.timeframe,
                    result["row_count"], result["dataset_id"],
                )
                if args.example_join:
                    _print_example_join(lake, result["dataset_id"], timeframe=args.timeframe)
            except SystemExit as exc:
                log.warning("[%d/%d] %s %s: BLAD %s", i, len(symbols), symbol, args.timeframe, exc)
                failures.append({"symbol": symbol, "timeframe": args.timeframe, "error": str(exc)})

    log.info("backfill zakonczony: %d ok, %d bledow", len(results), len(failures))

    print(
        json.dumps(
            {
                "ok": not failures,
                "data_kind": DATA_KIND,
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
