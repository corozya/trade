"""#228 (podzadanie #227) — shared infrastructure for precomputed-indicator
backfills into ``CryptoDataLake``.

This is the fundament for the four indicator backfills that follow it: #229
(rsi, macd), #230 (stochastic, atr), #231 (risk_indicator). It plays the same
role for indicators that ``services.crypto_backfill`` plays for OKX-sourced
data_kinds (open_interest/funding/taker_volume/long_short_ratio), but the
source is different in one crucial way:

- OI/funding/etc. pull from a *remote* OKX REST endpoint, so
  ``services.crypto_backfill`` centers on network retry/backoff
  (:func:`services.crypto_backfill.retry_read`) and pagination cursors.
- Indicators are a pure *transformation* of an already-backfilled local
  ``ohlcv`` series (read via ``CryptoDataLake.read_as_of_duckdb``, no network
  call). There is nothing to retry — the only thing that can go wrong is
  "the source series doesn't have enough history yet", which is a `ValueError`,
  not a flaky-network condition worth backing off on.

This module still reuses the *shared* pieces from ``services.crypto_backfill``
that are not network-specific: :class:`~services.crypto_backfill.BackfillMode`,
:func:`~services.crypto_backfill.add_mode_arguments`,
:func:`~services.crypto_backfill.resolve_since` (repurposed here to resolve
"how far back into the ohlcv series do we need to look", not an OKX
timestamp), :func:`~services.crypto_backfill.resume_cursor`, and
``CryptoMarketIngestor``/``FixtureMarketDataAdapter`` publish/idempotency
plumbing via a small local adapter (:class:`IndicatorAdapter`) that satisfies
the same ``MarketDataAdapter`` protocol used everywhere else in the lake.

## One module, N indicators — the ``IndicatorSpec`` registry

Every concrete indicator (rsi, macd, stochastic, atr, risk_indicator) is
declared as one :class:`IndicatorSpec` entry in :data:`INDICATOR_SPECS`
instead of a hand-written copy of the backfill script. A spec says:

- ``data_kind`` — must be one of the 5 new ``CryptoDataLake.DATA_KINDS``
  entries added alongside this module.
- ``warmup`` — how many *leading* source bars the underlying
  ``indicators.py`` function needs before it produces its first non-NaN
  value (e.g. 14 for RSI/ATR's default length, ``slow + signal_length`` for
  MACD's default 26+9). Used only to decide how much extra ohlcv lookback a
  resumed/incremental run needs to feed the transform so its *first newly
  published point* is numerically correct (see "Incremental update
  strategy" below) — it does not gate whether a --full run succeeds; a
  --full run always uses the entire available ohlcv history.
- ``transform`` — a callable ``(rows: list[dict]) -> list[dict[str, Any]]``
  that takes ohlcv rows (already sorted by ``observed_at``) and returns one
  dict of indicator columns per input row (NaN columns for warmup rows are
  dropped before publish — see :func:`_run_transform`). This is intentionally
  the *raw* ``indicators.py``/``risk_indicator.py`` functions, not reimplemented
  here — #229/#230/#231 register their function, they do not write a new one.

### Adding a new indicator (#229/#230/#231 read this)

1. Import the transform function from ``crypto-dashboard/backend/indicators.py``
   (or ``risk_indicator.py`` for risk_indicator) — do NOT reimplement the math
   here.
2. Wrap it in a small adapter function with signature
   ``(rows: list[dict[str, Any]]) -> list[dict[str, Any]]`` if the raw
   function returns positional lists/tuples (e.g. ``macd()`` returns
   ``(macd_line, signal_line, histogram)`` — the wrapper zips those into
   ``[{"macd": ..., "signal": ..., "histogram": ...}, ...]`` dicts, one per
   input row, matching the input row count exactly).
3. Add one ``IndicatorSpec(...)`` entry to :data:`INDICATOR_SPECS` with the
   new ``data_kind`` (already present in ``CryptoDataLake.DATA_KINDS`` as of
   #228) and the correct ``warmup``.
4. Nothing else changes — :func:`run_indicator_backfill` and the CLI
   (``scripts/crypto_backfill_indicators.py --indicator <name> ...``) pick it
   up automatically via the registry.

## Incremental update strategy (design decision, #228 scope)

Wilder's smoothing (RSI/ATR) and EMA (MACD) are both recurrence relations:
the value at bar N only needs the value at bar N-1 plus the new input, not
the full history. Two ways to exploit that for an ``--incremental`` run were
considered:

1. **Persist recurrence state** (avg_gain/avg_loss for RSI, avg_tr for ATR,
   last EMA for MACD) in a small side record, read it back, and advance it
   by exactly the new bar(s).
2. **Recompute from a sufficiently long tail** of ohlcv history (warmup + a
   safety margin) on every incremental run, then publish only the newly
   produced points (the underlying ``CryptoMarketIngestor.ingest`` /
   ``merge_records`` already reject conflicting duplicates, so republishing
   the same already-published point with the same value is a safe no-op).

This module implements **(2)**. Rationale: Wilder/EMA state converges from
any reasonable seed given enough trailing bars (standard property of
exponential smoothing — the influence of the seed decays geometrically,
though empirically slower than a naive "few multiples of length" guess — see
``_RECOMPUTE_SAFETY_MULTIPLIER``'s comment for the measured convergence
curve), so recomputing from ``warmup * _RECOMPUTE_SAFETY_MULTIPLIER`` bars of
trailing history reproduces the *converged* recurrence value bit-for-bit at
the publish boundary without ever persisting a second artifact. This avoids (a) a
whole new small-record storage format in the lake with its own
versioning/idempotency story, and (b) a second failure mode where the
side-state record and the published series can silently drift out of sync
(e.g. a --full re-backfill that resets the series but forgets to reset the
side record). The cost is recomputing a bounded, small window on every
incremental tick — cheap relative to a 15m/5m cron cadence and the size of a
single ohlcv read. If a future indicator's recurrence does NOT converge
within a bounded tail (none of rsi/macd/stochastic/atr/risk_indicator do),
option (1) remains available without changing this module's public
interface — ``IndicatorAdapter.fetch()`` is where that would plug in.

## Multi-column indicators (macd, stochastic)

A single ``data_kind`` row can carry more than one numeric field (e.g.
``macd`` rows have ``macd``/``signal``/``histogram`` columns, mirroring how
``open_interest`` rows already carry both ``open_interest`` and ``volume``
— ``CryptoDataLake`` has never required a data_kind to be single-column).
:class:`IndicatorSpec.transform` returns one dict of columns per row; those
columns are merged directly into the canonical record alongside the required
fields.

Usage:
  crypto_backfill_indicators.py --full --indicator rsi \
      --symbol BTC-USDT-SWAP --timeframe 15m --lake-root /app/research/agent-krypto
  crypto_backfill_indicators.py --incremental --indicator rsi \
      --symbol BTC-USDT-SWAP --timeframe 15m --lake-root /app/research/agent-krypto
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping, Sequence

from services.crypto_data_lake import DATA_KINDS, CryptoDataLake, _utc_iso
from services.crypto_market_ingestion import IngestionError

# See "Incremental update strategy" in the module docstring: an --incremental
# run recomputes the transform over this many trailing warmup lengths of
# ohlcv history so Wilder/EMA recurrence state has converged by the time it
# reaches the first point actually newer than the resume cursor.
#
# Measured empirically for #228 (RSI length=14, BTC-USDT-SWAP/15m, 35k-bar
# series): a 5x multiplier (70 bars) was NOT enough — it reproduced the last
# point 0.32 RSI points off the full-history ground truth. Wilder's
# recurrence converges geometrically but slower than initially assumed;
# 20x (280 bars) got the error down to ~3.7e-8, 30x to ~3e-12 (float64 noise
# floor), and 50x reproduced the ground truth bit-for-bit in the same test.
# 50 is used as the floor with headroom above the point where the measured
# error already hit the float64 noise floor.
_RECOMPUTE_SAFETY_MULTIPLIER = 50


TransformFn = Callable[[list[dict[str, Any]]], list[dict[str, Any]]]


@dataclass(frozen=True)
class IndicatorSpec:
    """One entry in :data:`INDICATOR_SPECS` — see module docstring "Adding a
    new indicator" for how #229/#230/#231 register theirs."""

    data_kind: str
    warmup: int
    transform: TransformFn
    source_timeframe: str | None = None  # None = same timeframe as requested

    def __post_init__(self) -> None:
        if self.data_kind not in DATA_KINDS:
            raise ValueError(f"unknown data_kind for indicator spec: {self.data_kind}")
        if self.warmup < 1:
            raise ValueError("warmup must be >= 1")


def _rsi_transform(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from indicators import rsi as _rsi_fn  # crypto-dashboard/backend/indicators.py

    closes = [float(row["close"]) for row in rows]
    values = _rsi_fn(closes)
    return [{"rsi": value} for value in values]


def _macd_transform(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from indicators import macd as _macd_fn  # crypto-dashboard/backend/indicators.py

    closes = [float(row["close"]) for row in rows]
    macd_line, signal_line, histogram = _macd_fn(closes)
    return [
        {"macd": macd_line[i], "signal": signal_line[i], "histogram": histogram[i]}
        for i in range(len(rows))
    ]


def _atr_transform(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from indicators import atr as _atr_fn  # crypto-dashboard/backend/indicators.py

    highs = [float(row["high"]) for row in rows]
    lows = [float(row["low"]) for row in rows]
    closes = [float(row["close"]) for row in rows]
    values = _atr_fn(highs, lows, closes)
    return [{"atr": value} for value in values]


def _stochastic_transform(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from indicators import stochastic as _stochastic_fn  # crypto-dashboard/backend/indicators.py

    highs = [float(row["high"]) for row in rows]
    lows = [float(row["low"]) for row in rows]
    closes = [float(row["close"]) for row in rows]
    k, d = _stochastic_fn(highs, lows, closes)
    return [{"k": k[i], "d": d[i]} for i in range(len(rows))]


def _support_resistance_records(
    ohlcv_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """#233: NOT an ``IndicatorSpec.transform`` (see module docstring — those
    are 1-row-in/1-row-out; S/R levels are stateful across the whole input
    window, see crypto-dashboard/backend/sr_levels.py's module docstring for
    why). This builds canonical lake records directly from
    ``sr_levels.compute_level_events``'s output instead of going through
    :func:`_run_transform`. Always recomputes over the FULL ``ohlcv_rows``
    window it's given — there is no bounded "trailing warmup" shortcut for
    this data_kind (a level created near the start of history can still be
    open now), so incremental correctness is provided by
    :func:`run_support_resistance_backfill` recomputing from the entire
    available ohlcv history every time and letting
    ``CryptoMarketIngestor``/``merge_records`` drop already-published
    (symbol, timeframe, data_kind, observed_at, available_at, source)
    duplicates rather than by trimming the input here."""
    from sr_levels import compute_level_events  # crypto-dashboard/backend/sr_levels.py

    events = compute_level_events(ohlcv_rows)
    records: list[dict[str, Any]] = []
    for ev in events:
        anchor = ohlcv_rows[0]
        records.append(
            {
                "symbol": anchor["symbol"],
                "timeframe": anchor["timeframe"],
                "data_kind": "support_resistance",
                "observed_at": ev["observed_at"],
                "available_at": ev["available_at"],
                # level_id embedded in `source` (not just a static string
                # like every other indicator's source) so multiple levels'
                # events landing on the SAME observed_at bar don't collide
                # on services.crypto_market_ingestion._record_key, which
                # includes `source` but not any level-specific field.
                "source": f"crypto-indicator-backfill/support_resistance/{ev['level_id']}",
                "level_id": ev["level_id"],
                "level_type": ev["type"],
                "price_top": ev["price_top"],
                "price_bottom": ev["price_bottom"],
                "status": ev["status"],
                "volume": ev["volume"],
                "touch_count": ev["touch_count"],
                "created_at": ev["created_at"],
                "last_touched_at": ev["last_touched_at"],
                "event": ev["event"],
            }
        )
    return records


def run_support_resistance_backfill(
    *,
    lake: CryptoDataLake,
    ohlcv_dataset_id: str,
) -> list[dict[str, Any]]:
    """Full recompute of S/R level events for one (symbol, timeframe) —
    always reads the ENTIRE available ohlcv history (no --incremental
    trailing-window shortcut, see :func:`_support_resistance_records`
    docstring). Safe to call repeatedly: already-published events are
    dropped as duplicates by ``CryptoMarketIngestor``/``merge_records``
    (same (symbol, timeframe, data_kind, observed_at, available_at, source)
    key convention as every other data_kind), so re-running this after new
    ohlcv bars land only publishes genuinely new events."""
    all_rows = _read_ohlcv_rows(lake, ohlcv_dataset_id=ohlcv_dataset_id)
    if not all_rows:
        raise ValueError(f"ohlcv dataset {ohlcv_dataset_id} has no rows")
    return _support_resistance_records(all_rows)


def _risk_indicator_transform(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """#231: unlike the other transforms above, ``compute_risk_ratio`` wants
    full ohlcv rows (it reads open/high/low/close/volume off each dict
    itself — see risk_indicator.py), not pre-extracted close lists, so this
    wrapper just passes ``rows`` straight through instead of building a
    closes/highs/lows list first. It also never returns NaN (every component
    in risk_indicator.py clamps to a neutral 50.0 default during its own
    warmup rather than leaving a gap — see e.g. `_rsi`'s `[50.0] * len(closes)`
    seed) so ``warmup`` here only sizes the incremental recompute window
    (see module docstring, "Incremental update strategy"), it never causes
    ``_run_transform`` to drop leading rows as NaN the way rsi/macd/atr/
    stochastic's warmup does."""
    from risk_indicator import compute_risk_ratio  # crypto-dashboard/backend/risk_indicator.py

    values = compute_risk_ratio(rows)
    return [{"risk_ratio": value} for value in values]


# #229 (podzadanie #227) wires macd in alongside #228's rsi
# proof-of-mechanism — #230 wires stochastic/atr in below following the
# "Adding a new indicator" steps in the module docstring; #231 adds
# risk_indicator (its transform signature differs slightly — it consumes
# full ohlcv rows, not just closes — see risk_indicator.compute_risk_ratio).
INDICATOR_SPECS: dict[str, IndicatorSpec] = {
    "rsi": IndicatorSpec(data_kind="rsi", warmup=14, transform=_rsi_transform),
    "macd": IndicatorSpec(data_kind="macd", warmup=26 + 9, transform=_macd_transform),
    "atr": IndicatorSpec(data_kind="atr", warmup=14, transform=_atr_transform),
    # k_length=14, k_smooth=3, d_smooth=3 (indicators.py defaults) — %D
    # needs 14 bars for raw %K to start, +3 for %K's SMA smoothing, +3 more
    # for %D's SMA smoothing of the smoothed %K.
    "stochastic": IndicatorSpec(data_kind="stochastic", warmup=14 + 3 + 3, transform=_stochastic_transform),
    # Longest lookback among the 6 equal-weighted components in
    # risk_indicator.compute_risk_ratio: Williams %R (length=21) > Bollinger
    # %B (20) > RSI/Stochastic-RSI/MFI (14) > Momentum % (10).
    "risk_indicator": IndicatorSpec(data_kind="risk_indicator", warmup=21, transform=_risk_indicator_transform),
}


def _read_ohlcv_rows(
    lake: CryptoDataLake, *, ohlcv_dataset_id: str
) -> list[dict[str, Any]]:
    table = lake.read_as_of_duckdb(ohlcv_dataset_id, "9999-12-31T23:59:59Z")
    rows = table.to_pylist()
    rows.sort(key=lambda row: row["observed_at"])
    return rows


def _run_transform(
    spec: IndicatorSpec,
    ohlcv_rows: list[dict[str, Any]],
    *,
    since_exclusive: datetime | None,
) -> list[dict[str, Any]]:
    """Run ``spec.transform`` over ``ohlcv_rows`` and return canonical
    indicator records for every row strictly newer than ``since_exclusive``
    (``None`` = publish everything, the --full path) whose transform output
    is not NaN (the warmup period never gets a published row — matches how
    ``indicators.py`` itself represents "not enough history yet")."""
    if len(ohlcv_rows) <= spec.warmup:
        raise ValueError(
            f"not enough ohlcv history for {spec.data_kind}: have {len(ohlcv_rows)} "
            f"bars, need > {spec.warmup} (warmup)"
        )
    columns_per_row = spec.transform(ohlcv_rows)
    if len(columns_per_row) != len(ohlcv_rows):
        raise IngestionError(
            f"{spec.data_kind} transform returned {len(columns_per_row)} rows "
            f"for {len(ohlcv_rows)} input bars (must be 1:1)"
        )
    records: list[dict[str, Any]] = []
    for ohlcv_row, columns in zip(ohlcv_rows, columns_per_row):
        observed_at = datetime.fromisoformat(_utc_iso(ohlcv_row["observed_at"]).replace("Z", "+00:00"))
        if since_exclusive is not None and observed_at <= since_exclusive:
            continue
        numeric_values = [v for v in columns.values() if isinstance(v, float)]
        if any(v != v for v in numeric_values):  # NaN check, warmup row — skip
            continue
        records.append(
            {
                "symbol": ohlcv_row["symbol"],
                "timeframe": ohlcv_row["timeframe"],
                "data_kind": spec.data_kind,
                "observed_at": ohlcv_row["observed_at"],
                # Indicators are derived synchronously from an already-closed
                # candle, so they become knowable at the same instant the
                # source candle does — same available_at == source's
                # available_at convention as the candle itself (no extra lag
                # to model; unlike a point-sample data_kind there IS a
                # meaningful "close" already baked into the ohlcv row this
                # was derived from).
                "available_at": ohlcv_row["available_at"],
                "source": f"crypto-indicator-backfill/{spec.data_kind}",
                **columns,
            }
        )
    return records


def resolve_lookback_start(
    spec: IndicatorSpec,
    *,
    mode_is_full: bool,
    resume_cursor_at: str | None,
) -> datetime | None:
    """How far back into the ohlcv series a run needs to read.

    --full: ``None`` (read the entire available ohlcv history — a full
    recompute is a deliberate, explicit operator action, same convention as
    ``crypto_backfill_open_interest.py``'s --full).

    --incremental with a resume cursor: ``cursor - warmup * safety_multiplier``
    worth of *bars* is what's actually needed, but ohlcv rows are read by
    dataset (no direct "N bars back from a timestamp" query) — callers pass
    this timestamp only as a documentation/lower-bound hint; the actual
    trimming happens by slicing the already-sorted ohlcv rows to the last
    ``warmup * safety_multiplier`` entries at-or-before publishing new points
    (see :func:`run_indicator_backfill`), which is exact regardless of
    timeframe/gaps.
    """
    if mode_is_full or resume_cursor_at is None:
        return None
    return datetime.fromisoformat(resume_cursor_at.replace("Z", "+00:00"))


def compute_indicator_records(
    spec: IndicatorSpec,
    *,
    lake: CryptoDataLake,
    ohlcv_dataset_id: str,
    resume_cursor_at: str | None,
    mode_is_full: bool,
) -> list[dict[str, Any]]:
    """Read source ohlcv, run ``spec.transform``, return new canonical
    indicator records ready for ``CryptoMarketIngestor.ingest``/``publish``.

    For an incremental run this trims the ohlcv input to the trailing
    ``warmup * _RECOMPUTE_SAFETY_MULTIPLIER`` bars *before* the resume cursor
    plus everything after it (see module docstring, "Incremental update
    strategy") rather than replaying the full history — bounded cost
    independent of how long the series has grown.
    """
    all_rows = _read_ohlcv_rows(lake, ohlcv_dataset_id=ohlcv_dataset_id)
    if not all_rows:
        raise ValueError(f"ohlcv dataset {ohlcv_dataset_id} has no rows")

    since_exclusive = resolve_lookback_start(
        spec, mode_is_full=mode_is_full, resume_cursor_at=resume_cursor_at
    )
    if since_exclusive is None:
        window_rows = all_rows
    else:
        # Trim to a bounded trailing window: the safety-multiplied warmup
        # immediately before the cursor (for recurrence convergence) plus
        # everything at/after it (the candidate new points). Index-based —
        # exact regardless of timeframe granularity or gaps in observed_at.
        cursor_index = next(
            (
                i
                for i, row in enumerate(all_rows)
                if datetime.fromisoformat(_utc_iso(row["observed_at"]).replace("Z", "+00:00"))
                > since_exclusive
            ),
            len(all_rows),
        )
        lookback_bars = spec.warmup * _RECOMPUTE_SAFETY_MULTIPLIER
        window_start = max(0, cursor_index - lookback_bars)
        window_rows = all_rows[window_start:]

    return _run_transform(spec, window_rows, since_exclusive=since_exclusive)


__all__ = [
    "INDICATOR_SPECS",
    "IndicatorSpec",
    "compute_indicator_records",
    "resolve_lookback_start",
    "run_support_resistance_backfill",
]
