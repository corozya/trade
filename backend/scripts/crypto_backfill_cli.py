#!/usr/bin/env python3
"""#167 — shared CLI entrypoint for OKX historical backfills into CryptoDataLake.

This is the fundament dispatcher for the four per-data-kind backfills
(#162 OHLCV, #163 funding, #164 open interest, #165 taker volume, #166
long/short ratio). It owns the shared, cross-cutting concerns so none of the
four are reimplemented per data kind:

- ``--full`` / ``--incremental`` mode (services.crypto_backfill.BackfillMode)
- lake root / base-dataset-id plumbing (services.crypto_backfill.add_mode_arguments)
- a small on-disk registry (``<lake-root>/raw/latest.json``) mapping
  data_kind -> last published dataset_id, so ``--incremental`` runs do not
  require the operator to pass ``--base-dataset-id`` by hand every cron tick.

Fetching itself (the OKX pagination + row-shaping per data_kind) is
implemented per-backfill (#162-166) as a ``MarketDataAdapter`` and wired in
via ``--data-kind``. Until those adapters exist, ``--data-kind`` accepts only
``ohlcv`` reusing the already-existing OHLCV history-candles support in
``OkxClient.get_candles(history=True)`` as a first concrete slice — this
keeps the CLI runnable end-to-end (including in Docker) rather than a pure
stub, without scope-creeping into #162's full OHLCV backfill design.

Usage:
  crypto_backfill_cli.py --full --data-kind ohlcv --symbol BTC-USDT-SWAP \
      --timeframe 1d --lake-root /app/data/lake --alias demo_main_full
  crypto_backfill_cli.py --incremental --data-kind ohlcv --symbol BTC-USDT-SWAP \
      --timeframe 1d --lake-root /app/data/lake --alias demo_main_full

#162 extends this dispatcher for the OHLCV backfill's actual scope (12
months back, every ``CryptoDataLake.SYMBOLS`` x every requested timeframe,
not just a single symbol/timeframe pair):

  crypto_backfill_cli.py --full --data-kind ohlcv \
      --symbols BTC-USDT-SWAP,ETH-USDT-SWAP --timeframes 1m,5m,15m,1h,4h,1d \
      --lake-root /app/data/lake --alias demo_main_full

``--symbols``/``--timeframes`` (comma-separated) default to *all*
``CryptoDataLake.SYMBOLS``/``TIMEFRAMES`` when omitted, so a bare
``--full --data-kind ohlcv`` backfills every symbol/timeframe combination.
The legacy singular ``--symbol``/``--timeframe`` flags remain supported for
one-pair invocations (e.g. Docker smoke tests, #167's original slice) and
are mutually exclusive with their plural counterparts. Per #161's PM
decision, timeframes are never resampled from 1m — each timeframe is an
independent OKX history-candles pull, so adding a new timeframe later is a
matter of passing it in ``--timeframes``, not touching this script.

Each (symbol, timeframe) pair gets its own adapter run/publish/registry
entry (idempotency and resume cursors are keyed per symbol+timeframe+data_kind
in ``services.crypto_backfill.resume_cursor``) — one pair failing does not
abort the others; failures are collected and reported at the end so an
operator can re-run only what failed (--incremental is a safe retry: already
published pairs report "no new candles" and exit cleanly).
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
    DEFAULT_FULL_BACKFILL_HORIZON,
    BackfillMode,
    BackfillRunner,
    add_mode_arguments,
    resolve_since,
)
from services.crypto_data_lake import SYMBOLS, TIMEFRAMES, CryptoDataLake
from services.okx_client import OkxClient


class OhlcvHistoryAdapter:
    """OKX history-candles adapter — the first concrete data_kind slice.

    Uses the client's existing retry-on-read (``OkxClient._get`` already
    wraps every call in ``_retry_read``), so this adapter does not duplicate
    retry/backoff logic itself, per #167 point 2.
    """

    name = "okx-history-candles"

    def __init__(self, client: OkxClient, *, symbol: str, timeframe: str, since: datetime):
        self._client = client
        self.symbol = symbol
        self.timeframe = timeframe
        self.since = since

    def fetch(self) -> Iterable[Mapping[str, Any]]:
        bar = _to_okx_bar(self.timeframe)
        interval = _timeframe_to_timedelta(self.timeframe)
        rows: list[dict[str, Any]] = []
        after: str | None = None
        while True:
            payload = self._client.get_candles(
                self.symbol, bar=bar, limit=100, after=after, history=True
            )
            candles = payload.get("data", []) if isinstance(payload, dict) else []
            if not candles:
                break
            stop = False
            for candle in candles:
                ts_ms = int(candle[0])
                observed_at = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
                # Strictly-after: `since` is the observed_at of the last row
                # already published. Re-including it would resend the exact
                # same key merge_records() just deduplicated by, and worse —
                # an in-progress (not yet closed) candle can have a different
                # OHLC than what was published earlier, which merge_records()
                # correctly rejects as a conflicting duplicate rather than
                # silently overwriting immutable history.
                if observed_at <= self.since:
                    stop = True
                    continue
                rows.append(
                    {
                        "symbol": self.symbol,
                        "timeframe": self.timeframe,
                        "data_kind": "ohlcv",
                        "observed_at": observed_at,
                        "available_at": observed_at + interval,
                        "source": self.name,
                        "open": float(candle[1]),
                        "high": float(candle[2]),
                        "low": float(candle[3]),
                        "close": float(candle[4]),
                        "volume": float(candle[5]),
                    }
                )
            after = candles[-1][0]
            if stop or len(candles) < 100:
                break
        if not rows:
            raise SystemExit(
                f"no new candles for {self.symbol}/{self.timeframe} since {self.since.isoformat()}"
            )
        return rows

    def lineage(self) -> Mapping[str, Any]:
        return {
            "name": self.name,
            "endpoint": "/api/v5/market/history-candles",
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "since": self.since.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        }


class TakerVolumeHistoryAdapter:
    """OKX rubik/stat/taker-volume history adapter (#165, podzadanie #161).

    Same dwutorowy model as OI (#164): period=1D for the backward backfill
    (~6 months, hard REST ceiling for rubik/stat/*, same as
    get_open_interest_history) and period=5m for the dense, growing tail
    collected from deployment onward. This adapter takes ``period`` as an
    explicit argument (not derived from ``timeframe``) because
    CryptoDataLake.TIMEFRAMES uses lowercase ("1d"/"5m") while the OKX period
    param for rubik/stat/* wants "1D" (uppercase D) / "5m" — same asymmetry
    already handled for OHLCV by ``_to_okx_bar``.

    Endpoint returns ``[ts, sellVol, buyVol]`` in quote-currency, aggregated
    per base currency (ccy), NOT per instId — these are point samples (not
    candles), so ``available_at`` == ``observed_at`` (no close-of-bar lag to
    account for, unlike OHLCV) per #161/#164 as-of lookup guidance.
    """

    name = "okx-rubik-taker-volume"

    def __init__(
        self, client: "OkxClient", *, symbol: str, timeframe: str, period: str, since: datetime
    ):
        self._client = client
        self.symbol = symbol
        self.timeframe = timeframe
        self.period = period
        self.since = since

    def fetch(self) -> Iterable[Mapping[str, Any]]:
        ccy = _to_ccy(self.symbol)
        rows: list[dict[str, Any]] = []
        after: str | None = None
        # rubik/stat/* does not actually page past its own ~180pt/~6mo
        # ceiling (#161/#164 research) — it has been observed to return the
        # same page again rather than an empty one when `after` is past the
        # ceiling, which without a seen_ts/progressed guard spins forever
        # (reproduced empirically: identical `after` on every request until
        # OKX's rate limiter kills the loop). Same guard as
        # OpenInterestHistoryAdapter.fetch, required here too.
        seen_ts: set[str] = set()
        while True:
            payload = self._client.get_taker_volume_history(
                ccy, period=self.period, limit=100, after=after
            )
            points = payload.get("data", []) if isinstance(payload, dict) else []
            if not points:
                break
            stop = False
            progressed = False
            for point in points:
                ts_raw = str(point[0])
                if ts_raw in seen_ts:
                    continue
                seen_ts.add(ts_raw)
                ts_ms = int(ts_raw)
                observed_at = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
                # Same strictly-after resume semantics as OhlcvHistoryAdapter:
                # `since` is the observed_at of the last point already
                # published; re-including it would resend a key
                # merge_records() already deduplicated.
                if observed_at <= self.since:
                    stop = True
                    continue
                progressed = True
                rows.append(
                    {
                        "symbol": self.symbol,
                        "timeframe": self.timeframe,
                        "data_kind": "taker_volume",
                        "observed_at": observed_at,
                        # Point sample, not a candle close — available_at ==
                        # observed_at (no MTF shift needed for as-of join).
                        "available_at": observed_at,
                        "source": self.name,
                        "taker_sell_volume": float(point[1]),
                        "taker_buy_volume": float(point[2]),
                    }
                )
            after = points[-1][0]
            if stop or not progressed or len(points) < 100:
                break
        if not rows:
            raise SystemExit(
                f"no new taker-volume points for {self.symbol}/{self.timeframe} since "
                f"{self.since.isoformat()}"
            )
        return rows

    def lineage(self) -> Mapping[str, Any]:
        return {
            "name": self.name,
            "endpoint": "/api/v5/rubik/stat/taker-volume",
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "period": self.period,
            "since": self.since.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        }


class LongShortRatioHistoryAdapter:
    """OKX rubik/stat/contracts/long-short-account-ratio history adapter
    (#166, podzadanie #161).

    Same dwutorowy model as OI/taker-volume (#164/#165): period=1D for the
    backward backfill (~6 months, hard REST ceiling for rubik/stat/*, same
    as get_open_interest_history/get_taker_volume_history) and period=5m for
    the dense, growing tail collected from deployment onward. ``period`` is
    an explicit argument (not derived from ``timeframe``) for the same
    lowercase/uppercase asymmetry reason as ``TakerVolumeHistoryAdapter``.

    Endpoint returns ``[ts, ratio]`` — stosunek liczby kont long do short
    (not volume/position value) — aggregated per base currency (ccy), NOT
    per instId. Point sample, not a candle: ``available_at`` == ``observed_at``
    per #161/#164/#165 as-of lookup guidance.
    """

    name = "okx-rubik-long-short-account-ratio"

    def __init__(
        self, client: "OkxClient", *, symbol: str, timeframe: str, period: str, since: datetime
    ):
        self._client = client
        self.symbol = symbol
        self.timeframe = timeframe
        self.period = period
        self.since = since

    def fetch(self) -> Iterable[Mapping[str, Any]]:
        ccy = _to_ccy(self.symbol)
        rows: list[dict[str, Any]] = []
        after: str | None = None
        # Same rubik/stat/* non-paging ceiling as TakerVolumeHistoryAdapter
        # (#161/#164 research) — without this guard the loop spins forever
        # on an identical repeated page once past the ~6mo ceiling.
        seen_ts: set[str] = set()
        while True:
            payload = self._client.get_long_short_account_ratio_history(
                ccy, period=self.period, limit=100, after=after
            )
            points = payload.get("data", []) if isinstance(payload, dict) else []
            if not points:
                break
            stop = False
            progressed = False
            for point in points:
                ts_raw = str(point[0])
                if ts_raw in seen_ts:
                    continue
                seen_ts.add(ts_raw)
                ts_ms = int(ts_raw)
                observed_at = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
                # Same strictly-after resume semantics as the sibling
                # rubik/stat/* adapters: `since` is the observed_at of the
                # last point already published; re-including it would resend
                # a key merge_records() already deduplicated.
                if observed_at <= self.since:
                    stop = True
                    continue
                progressed = True
                rows.append(
                    {
                        "symbol": self.symbol,
                        "timeframe": self.timeframe,
                        "data_kind": "long_short_ratio",
                        "observed_at": observed_at,
                        # Point sample, not a candle close — available_at ==
                        # observed_at (no MTF shift needed for as-of join).
                        "available_at": observed_at,
                        "source": self.name,
                        "long_short_ratio": float(point[1]),
                    }
                )
            after = points[-1][0]
            if stop or not progressed or len(points) < 100:
                break
        if not rows:
            raise SystemExit(
                f"no new long-short-ratio points for {self.symbol}/{self.timeframe} since "
                f"{self.since.isoformat()}"
            )
        return rows

    def lineage(self) -> Mapping[str, Any]:
        return {
            "name": self.name,
            "endpoint": "/api/v5/rubik/stat/contracts/long-short-account-ratio",
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "period": self.period,
            "since": self.since.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        }


class FundingRateHistoryAdapter:
    """OKX public/funding-rate-history adapter (#163, podzadanie #161).

    Unlike the rubik/stat/* siblings (OI/taker-volume/long-short-ratio),
    funding-rate-history supports real after/before pagination (per #161/#163
    research) but with semantics reversed from what the parameter names
    suggest: ``after=<ts>`` returns rows OLDER than ts (the direction this
    adapter paginates in), ``before=<ts>`` returns NEWER. Verified empirically
    2026-08-06 — see OkxClient.get_funding_rate_history docstring. Horizon is
    a hard ~3-month REST ceiling (not the 12-month default used for OHLCV);
    the caller (main()) passes ``since`` accordingly.

    Funding occurs on a fixed ~8h cycle, which has no matching entry in
    ``CryptoDataLake.TIMEFRAMES`` (1m/5m/15m/1h/4h/1d) — per PM decision
    (#163), records are published with ``timeframe="1h"`` as the closest
    existing bucket rather than extending the shared schema for this one
    data_kind. This is a storage-bucket label only, not a claim that funding
    updates hourly.

    Endpoint takes ``instId`` (not ``ccy``, unlike rubik/stat/*) and returns
    per-instrument rows keyed by ``fundingTime``/``fundingRate``. Point
    sample, not a candle: ``available_at`` == ``observed_at``.
    """

    name = "okx-funding-rate-history"
    TIMEFRAME = "1h"

    def __init__(self, client: "OkxClient", *, symbol: str, since: datetime):
        self._client = client
        self.symbol = symbol
        self.since = since

    def fetch(self) -> Iterable[Mapping[str, Any]]:
        rows: list[dict[str, Any]] = []
        after: str | None = None
        while True:
            payload = self._client.get_funding_rate_history(
                self.symbol, limit=100, after=after
            )
            points = payload.get("data", []) if isinstance(payload, dict) else []
            if not points:
                break
            stop = False
            for point in points:
                ts_ms = int(point["fundingTime"])
                observed_at = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
                # Same strictly-after resume semantics as the other adapters:
                # `since` is the observed_at of the last point already
                # published; re-including it would resend a key
                # merge_records() already deduplicated.
                if observed_at <= self.since:
                    stop = True
                    continue
                rows.append(
                    {
                        "symbol": self.symbol,
                        "timeframe": self.TIMEFRAME,
                        "data_kind": "funding",
                        "observed_at": observed_at,
                        # Point sample, not a candle close — available_at ==
                        # observed_at (no MTF shift needed for as-of join).
                        "available_at": observed_at,
                        "source": self.name,
                        "funding_rate": float(point["fundingRate"]),
                        "realized_rate": (
                            float(point["realizedRate"])
                            if point.get("realizedRate") not in (None, "")
                            else None
                        ),
                    }
                )
            # after=<oldest ts so far> continues paginating further back per
            # the reversed after/before semantics documented above.
            after = points[-1]["fundingTime"]
            if stop or len(points) < 100:
                break
        if not rows:
            raise SystemExit(
                f"no new funding-rate points for {self.symbol} since {self.since.isoformat()}"
            )
        return rows

    def lineage(self) -> Mapping[str, Any]:
        return {
            "name": self.name,
            "endpoint": "/api/v5/public/funding-rate-history",
            "symbol": self.symbol,
            "timeframe": self.TIMEFRAME,
            "since": self.since.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        }


def _to_ccy(symbol: str) -> str:
    # CryptoDataLake.SYMBOLS are instIds like "BTC-USDT-SWAP"; rubik/stat/*
    # endpoints (taker-volume, open-interest-volume, long-short-account-ratio)
    # take the base currency only ("BTC"), not the instId — same extraction
    # needed by #164's get_open_interest_history caller.
    return symbol.split("-")[0]


def _to_okx_period(timeframe: str) -> str:
    # rubik/stat/* period param mirrors OKX's bar casing ("1D"/"5m") but only
    # supports 5m/1H/1D (NOT 15m, unlike candles) — verified manually
    # 2026-08-04, see OkxClient.get_taker_volume docstring.
    if timeframe not in ("5m", "1h", "1d"):
        raise ValueError(f"unsupported taker_volume period: {timeframe} (rubik/stat/* wspiera 5m/1H/1D)")
    return _to_okx_bar(timeframe)


def _to_okx_bar(timeframe: str) -> str:
    # CryptoDataLake.TIMEFRAMES use lowercase ("1h"); OKX bar param wants "1H".
    if timeframe.endswith("h") or timeframe.endswith("d"):
        return timeframe[:-1] + timeframe[-1].upper()
    return timeframe


def _timeframe_to_timedelta(timeframe: str) -> timedelta:
    unit = timeframe[-1]
    value = int(timeframe[:-1])
    if unit == "m":
        return timedelta(minutes=value)
    if unit == "h":
        return timedelta(hours=value)
    if unit == "d":
        return timedelta(days=value)
    raise ValueError(f"unsupported timeframe: {timeframe}")


def _registry_path(lake_root: Path) -> Path:
    return lake_root / "raw" / "latest.json"


def _load_registry(lake_root: Path) -> dict[str, str]:
    path = _registry_path(lake_root)
    if not path.is_file():
        return {}
    return json.loads(path.read_text())


def _save_registry(lake_root: Path, registry: Mapping[str, str]) -> None:
    """Merge ``registry`` into whatever is currently on disk before writing.

    Reproduced in production: two backfill processes (e.g. this CLI running
    ohlcv full-batch alongside crypto_backfill_open_interest.py) each hold
    their own in-memory copy of the registry loaded once at startup. Without
    re-reading here, the process that finishes later overwrites the whole
    file with its stale in-memory copy plus its own new keys — silently
    erasing every key the other process wrote in the meantime (open_interest/*
    entries vanished this way after a long-running ohlcv --full run
    overwrote them). Merging on every save doesn't make concurrent writes to
    the *same* key safe, but different processes always write disjoint keys
    (different data_kind/symbol/timeframe), so a merge is sufficient here.
    """
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


def _build_adapter(
    client: OkxClient, *, data_kind: str, symbol: str, timeframe: str, since: datetime
):
    """Dispatch to the concrete per-data_kind adapter. Adding a new data_kind
    (#163 funding, #164 open_interest, #166 long_short_ratio) means adding one
    branch here plus the adapter class, not touching the batch loop above."""
    if data_kind == "ohlcv":
        return OhlcvHistoryAdapter(client, symbol=symbol, timeframe=timeframe, since=since)
    if data_kind == "funding":
        return FundingRateHistoryAdapter(client, symbol=symbol, since=since)
    if data_kind == "taker_volume":
        period = _to_okx_period(timeframe)
        return TakerVolumeHistoryAdapter(
            client, symbol=symbol, timeframe=timeframe, period=period, since=since
        )
    if data_kind == "long_short_ratio":
        period = _to_okx_period(timeframe)
        return LongShortRatioHistoryAdapter(
            client, symbol=symbol, timeframe=timeframe, period=period, since=since
        )
    raise ValueError(f"unsupported data_kind: {data_kind}")


def _backfill_one_pair(
    *,
    client: OkxClient,
    lake: CryptoDataLake,
    lake_root: Path,
    registry: dict[str, str],
    mode: BackfillMode,
    data_kind: str,
    symbol: str,
    timeframe: str,
    base_dataset_id_override: str | None,
) -> dict[str, Any]:
    """Run one (symbol, timeframe) backfill and update the on-disk registry in place.

    Isolated per pair so a caller iterating many pairs (#162's actual OHLCV
    scope: every symbol x every timeframe) can catch a single pair's
    ``SystemExit``/error without aborting the rest of the batch.
    """
    key = _registry_key(data_kind=data_kind, symbol=symbol, timeframe=timeframe)
    resume_base_dataset_id = base_dataset_id_override or registry.get(key)

    # funding-rate-history has a hard ~3-month REST ceiling (#161/#163
    # research, verified empirically) — requesting further back than that
    # cannot work around the API and only wastes a doomed pagination pass.
    # A --full run always starts "as far back as the endpoint will give",
    # same convention as OpenInterestHistoryAdapter's horizon table.
    horizon = (
        timedelta(days=95) if data_kind == "funding" else DEFAULT_FULL_BACKFILL_HORIZON
    )
    horizon_start = datetime.now(timezone.utc) - horizon
    since = resolve_since(
        mode=mode,
        lake=lake,
        base_dataset_id=resume_base_dataset_id,
        symbol=symbol,
        timeframe=timeframe,
        data_kind=data_kind,
        full_horizon_start=horizon_start,
    )

    # --full re-fetches the whole horizon from scratch (see `since` above,
    # always `horizon_start` in FULL mode) — it must NOT merge into whatever
    # dataset the registry still points at from a prior run, or a bar whose
    # OHLC settled differently between the two pulls (e.g. a not-yet-closed
    # candle re-fetched after it closed) raises a spurious "conflicting
    # duplicate record" IngestionError even though this is a deliberate
    # clean re-backfill, not an accidental double-write. Reproduced in
    # production: re-running --full against a lake that already had a
    # partial 5m OHLCV dataset from an earlier interrupted session crashed
    # here. Only --incremental (which genuinely continues an existing
    # dataset) passes a base_dataset_id to the ingestor.
    ingest_base_dataset_id = resume_base_dataset_id if mode is BackfillMode.INCREMENTAL else None

    adapter = _build_adapter(client, data_kind=data_kind, symbol=symbol, timeframe=timeframe, since=since)
    version = BackfillRunner(lake).run(adapter, base_dataset_id=ingest_base_dataset_id)

    registry[key] = version.dataset_id
    _save_registry(lake_root, registry)

    return {
        "ok": True,
        "mode": mode.value,
        "data_kind": data_kind,
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
        "--data-kind",
        required=True,
        choices=["ohlcv", "funding", "taker_volume", "long_short_ratio"],
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
        help="Comma-separated timeframes (e.g. 1m,5m,15m,1h,4h,1d). Defaults to all "
        "CryptoDataLake.TIMEFRAMES when omitted together with --timeframe. Adding a new "
        "timeframe later only requires listing it here, per #161's requirement — no code "
        "change to this script.",
    )
    parser.add_argument("--alias", required=True, help="OKX credentials alias (env OKX_<ALIAS>_*)")
    args = parser.parse_args(argv)

    # Docker (Dockerfile.backfill / docker-compose.yml `backfill` service)
    # always mounts the lake at /app/data/lake — --lake-root stays
    # a required, explicit argument for host/test use, but the compose
    # wrapper (scripts/backfill_docker.sh) does not force operators to repeat
    # it every invocation.

    symbols = _parse_csv_list(args.symbols) or ([args.symbol] if args.symbol else list(SYMBOLS))
    # rubik/stat/* data kinds (taker_volume, long_short_ratio, and later
    # open_interest) only support 5m/1H/1D — defaulting to the full
    # CryptoDataLake.TIMEFRAMES (which includes 1m/15m/4h, OHLCV-only) would
    # crash in _to_okx_period. An explicit --timeframes still overrides this
    # and is validated per-pair (bad values fail loudly, not silently).
    # funding has no --timeframes concept at all: FundingRateHistoryAdapter
    # always publishes under the fixed "1h" storage bucket (#163 PM decision,
    # see adapter docstring) — iterating other timeframes would just refetch
    # and republish the exact same rows under a different registry key.
    if args.data_kind == "ohlcv":
        default_timeframes = list(TIMEFRAMES)
    elif args.data_kind == "funding":
        default_timeframes = [FundingRateHistoryAdapter.TIMEFRAME]
    else:
        default_timeframes = ["5m", "1h", "1d"]
    timeframes = _parse_csv_list(args.timeframes) or (
        [args.timeframe] if args.timeframe else default_timeframes
    )

    lake_root = Path(args.lake_root)
    lake = CryptoDataLake(lake_root)
    registry = _load_registry(lake_root)

    total_pairs = len(symbols) * len(timeframes)
    log.info(
        "backfill start: data_kind=%s mode=%s pairs=%d symbols=%s timeframes=%s",
        args.data_kind, args.mode.value, total_pairs, ",".join(symbols), ",".join(timeframes),
    )

    results: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    with OkxClient(args.alias) as client:
        pair_num = 0
        for symbol in symbols:
            for timeframe in timeframes:
                pair_num += 1
                log.info("[%d/%d] %s %s: pobieram...", pair_num, total_pairs, symbol, timeframe)
                try:
                    result = _backfill_one_pair(
                        client=client,
                        lake=lake,
                        lake_root=lake_root,
                        registry=registry,
                        mode=args.mode,
                        data_kind=args.data_kind,
                        symbol=symbol,
                        timeframe=timeframe,
                        # --base-dataset-id only makes sense for a single
                        # explicit pair; batch runs resolve each pair's
                        # base from the registry instead.
                        base_dataset_id_override=(
                            args.base_dataset_id
                            if len(symbols) == 1 and len(timeframes) == 1
                            else None
                        ),
                    )
                    results.append(result)
                    log.info(
                        "[%d/%d] %s %s: OK, %d wierszy (dataset %s)",
                        pair_num, total_pairs, symbol, timeframe,
                        result["row_count"], result["dataset_id"],
                    )
                except SystemExit as exc:
                    log.warning("[%d/%d] %s %s: BLAD %s", pair_num, total_pairs, symbol, timeframe, exc)
                    failures.append({"symbol": symbol, "timeframe": timeframe, "error": str(exc)})

    log.info("backfill zakonczony: %d ok, %d bledow", len(results), len(failures))

    print(
        json.dumps(
            {
                "ok": not failures,
                "data_kind": args.data_kind,
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
