"""Shared infrastructure for OKX historical backfills into ``CryptoDataLake``.

Part of #167 (podzadanie #161) — this module is the fundament for the four
per-data-kind backfills (#162 OHLCV, #163 funding, #164 open interest, #165
taker volume, #166 long/short ratio). It intentionally does NOT know about
any specific OKX endpoint; each backfill script owns its own pagination
request shape and calls into these building blocks:

- :func:`retry_read` — generic retry/backoff wrapper for one paginated
  request. Mirrors the contract of ``services.okx_client._retry_read``
  (retry only on ``OkxRateLimitError``/``httpx.TransportError``, linear
  backoff) but is not coupled to ``OkxClient`` so any read callable can use
  it, including calls made through ``OkxClient`` helper methods that already
  retry internally (in that case this wrapper is a no-op pass-through).
- :func:`resume_cursor` — idempotent "where do I continue from" for one
  (symbol, timeframe, data_kind) key, derived from the latest already
  published ``observed_at`` in an existing dataset version. Every backfill
  script calls this once per key instead of implementing its own bookkeeping.
- :class:`BackfillMode` / :func:`resolve_mode` — shared ``--full`` vs
  ``--incremental`` CLI contract (see :func:`add_mode_arguments`).
- :class:`BackfillRunner` — thin orchestration wrapping
  ``services.crypto_market_ingestion.CryptoMarketIngestor`` so a backfill
  script only has to supply an adapter that yields already-canonical rows.

Idempotency at the storage layer is already provided by
``CryptoMarketIngestor.ingest`` (content-addressed publish + conflict
detection on merge) — this module adds the piece that was still missing:
knowing which point in time to resume paginating an upstream API from.
"""

from __future__ import annotations

import argparse
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Callable, Optional, TypeVar

from services.crypto_data_lake import CryptoDataLake, DatasetVersion, _utc_iso
from services.crypto_market_ingestion import (
    CryptoMarketIngestor,
    IngestionError,
    MarketDataAdapter,
)

T = TypeVar("T")

# Default full-backfill horizon for OHLCV per #161 comment history (12 mies.
# wstecz). Non-OHLCV data_kinds are capped by the OKX rubik/stat/* REST
# ceiling (~6 months) regardless of this constant — callers pass their own
# ``since`` when the API horizon is shorter than this default.
DEFAULT_FULL_BACKFILL_HORIZON = timedelta(days=365)


# -- retry / backoff ---------------------------------------------------------


def retry_read(
    fn: Callable[[], T],
    *,
    retry_exceptions: tuple[type[BaseException], ...],
    max_attempts: int = 3,
    backoff_seconds: float = 0.5,
) -> T:
    """Retry a single read call with linear backoff.

    Same contract as ``services.okx_client._retry_read``: only exceptions in
    ``retry_exceptions`` are retried (callers pass e.g.
    ``(OkxRateLimitError, httpx.TransportError)``); everything else
    propagates immediately. This function is deliberately generic (no OKX
    import) so it can wrap any paginated read used by a backfill loop,
    including non-OKX sources later.
    """
    attempt = 0
    last_exc: Optional[BaseException] = None
    while attempt < max_attempts:
        try:
            return fn()
        except retry_exceptions as exc:
            last_exc = exc
            attempt += 1
            if attempt >= max_attempts:
                break
            time.sleep(backoff_seconds * attempt)
    assert last_exc is not None
    raise last_exc


# -- resume / idempotency -----------------------------------------------------


def resume_cursor(
    lake: CryptoDataLake,
    *,
    base_dataset_id: str | None,
    symbol: str,
    timeframe: str,
    data_kind: str,
) -> str | None:
    """Return the latest published ``observed_at`` for one backfill key.

    ``None`` means "nothing published yet for this key" — the caller should
    start a full backfill from its configured horizon. A non-``None`` value
    is the UTC ISO-8601 timestamp of the most recent row already in the
    dataset for (symbol, timeframe, data_kind); an incremental run resumes
    strictly after this point.

    ``base_dataset_id`` is the dataset version to resume from (the previous
    backfill run's output). Reading a missing/unset dataset is treated as
    "no cursor yet" rather than an error, so the very first invocation of a
    backfill script (no prior version) transparently falls back to a full
    backfill.
    """
    if not base_dataset_id:
        return None
    try:
        table = lake.read_version(base_dataset_id)
    except FileNotFoundError:
        return None
    if table.num_rows == 0:
        return None
    mask_symbol = table["symbol"].to_pylist()
    mask_timeframe = table["timeframe"].to_pylist()
    mask_kind = table["data_kind"].to_pylist()
    observed = table["observed_at"].to_pylist()
    matches = [
        _utc_iso(ts)
        for ts, sym, tf, kind in zip(observed, mask_symbol, mask_timeframe, mask_kind)
        if sym == symbol and tf == timeframe and kind == data_kind
    ]
    if not matches:
        return None
    return max(matches)


# -- full vs incremental CLI contract -----------------------------------------


class BackfillMode(str, Enum):
    FULL = "full"
    INCREMENTAL = "incremental"


def add_mode_arguments(parser: argparse.ArgumentParser) -> None:
    """Attach the shared ``--full``/``--incremental`` flag pair to a parser.

    Every backfill CLI (#162-166) calls this so the operator-facing surface
    is identical across data kinds. Mutually exclusive, one is required —
    there is no silent default, an operator must state intent explicitly.
    """
    mode_group = parser.add_mutually_exclusive_group(required=True)
    mode_group.add_argument(
        "--full",
        dest="mode",
        action="store_const",
        const=BackfillMode.FULL,
        help="Pełny backfill do granicy horyzontu (12 mies. dla OHLCV, sufit API dla pozostałych).",
    )
    mode_group.add_argument(
        "--incremental",
        dest="mode",
        action="store_const",
        const=BackfillMode.INCREMENTAL,
        help="Dociągnij tylko punkty nowsze niż ostatnio opublikowane (tryb cronowy).",
    )
    parser.add_argument(
        "--lake-root",
        required=True,
        help="Katalog root CryptoDataLake (zawiera raw/versions/).",
    )
    parser.add_argument(
        "--base-dataset-id",
        default=None,
        help="dataset_id poprzedniej wersji do wznowienia (wymagany efektywnie dla --incremental; "
        "przy jego braku backfill traktuje run jak pierwszy pełny).",
    )


def resolve_since(
    *,
    mode: BackfillMode,
    lake: CryptoDataLake,
    base_dataset_id: str | None,
    symbol: str,
    timeframe: str,
    data_kind: str,
    full_horizon_start: datetime,
) -> datetime:
    """Resolve the effective start timestamp for one backfill key.

    ``--full`` always starts at ``full_horizon_start`` regardless of any
    existing dataset (re-running --full is a deliberate, explicit operator
    action, not the cron path). ``--incremental`` resumes from
    :func:`resume_cursor`, falling back to ``full_horizon_start`` when there
    is no prior published point for this key (first-ever incremental run
    behaves like a full backfill, per #167 point 4).
    """
    if mode is BackfillMode.FULL:
        return full_horizon_start
    cursor = resume_cursor(
        lake,
        base_dataset_id=base_dataset_id,
        symbol=symbol,
        timeframe=timeframe,
        data_kind=data_kind,
    )
    if cursor is None:
        return full_horizon_start
    return datetime.fromisoformat(cursor.replace("Z", "+00:00"))


# -- orchestration -------------------------------------------------------------


@dataclass(frozen=True)
class BackfillRunner:
    """Thin wrapper around ``CryptoMarketIngestor`` for backfill scripts.

    Backfill scripts (#162-166) build one ``MarketDataAdapter`` per run (its
    ``fetch()`` does the paginated OKX calls using :func:`retry_read` and
    :func:`resolve_since` internally) and hand it here. This keeps the
    publish/merge/idempotency path identical across all data kinds — no
    per-script reimplementation of dataset versioning.
    """

    lake: CryptoDataLake

    def run(
        self,
        adapter: MarketDataAdapter,
        *,
        base_dataset_id: str | None,
    ) -> DatasetVersion:
        ingestor = CryptoMarketIngestor(self.lake)
        return ingestor.ingest([adapter], base_dataset_id=base_dataset_id)


__all__ = [
    "DEFAULT_FULL_BACKFILL_HORIZON",
    "BackfillMode",
    "BackfillRunner",
    "IngestionError",
    "add_mode_arguments",
    "resolve_since",
    "resume_cursor",
    "retry_read",
]
