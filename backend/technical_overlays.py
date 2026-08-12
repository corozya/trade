"""Technical overlays for crypto-dashboard (#242): Bollinger Bands, EMA
(21/50/200), multi-timeframe EMA projection, VWAP (session/anchored), CVD
(session/anchored), and offset-shifted M30/H1 aggregation from base 5m/15m
OHLCV/taker_volume rows.

Computed ON-DEMAND from already-backfilled ``ohlcv``/``taker_volume``
data_kinds (same pattern as candlestick_patterns.py / liquidation_heatmap.py)
— NOT a new precomputed CryptoDataLake data_kind. Every function here is a
pure transform: `list[dict] -> list[dict]`, no I/O, no lake access — main.py
does the `_read_series()` call and passes rows in.

## Anti-look-ahead / anti-repainting rules (task #242, non-negotiable)

1. Only CLOSED candles ever enter a series. The caller (main.py) is
   responsible for not passing an in-progress candle; this module additionally
   never needs "now" to decide anything — it is a pure function of rows
   already in hand, which is itself a look-ahead safeguard: there is no path
   for a function here to peek at data that doesn't exist yet in its own
   input list.
2. Every series function processes rows strictly in order, using only
   `rows[0..i]` to produce the value at index `i`. No function scans forward.
3. EMA-projection forward-fills a source-timeframe (HTF) value onto a
   target-timeframe (LTF) timeline starting *at* the HTF candle's close time,
   never before — see `project_ema_multi_timeframe`.
4. Warm-up rows get `None` (JSON null), never a partially-warmed number.

## Time semantics

Every OHLCV row's `observed_at` is the candle's OPEN time (confirmed against
CryptoDataLake data: `available_at` = `observed_at` + timeframe duration =
candle CLOSE time). This module works entirely with the OPEN-time convention
used by the rest of crypto-dashboard's endpoints (`time` in every existing
/api/* series is the candle's `observed_at`), but computes candle CLOSE time
internally wherever a rule requires "as of the close" (offset bucketing,
session boundaries, EMA projection's `source_candle_close_time`).

All timestamps are UTC canonical (ISO-8601 `...Z`), never Europe/Warsaw or any
local zone — DST transitions do not shift bucket boundaries because nothing
here ever converts to local time. VWAP's session reset uses UTC midnight;
the API surfaces `session_timezone: "UTC"` explicitly so the UI can label it,
per task #242 ("jawna strefa bazowa (UTC canonical, UI pokazuje ją userowi)").
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

_NAN = float("nan")


def _parse_ts(iso_ts: str) -> datetime:
    return datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))


def _fmt_ts(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


_TIMEFRAME_SECONDS = {
    "1m": 60,
    "5m": 5 * 60,
    "15m": 15 * 60,
    "30m": 30 * 60,
    "1h": 60 * 60,
    "4h": 4 * 60 * 60,
    "1d": 24 * 60 * 60,
}


def timeframe_seconds(timeframe: str) -> int:
    if timeframe not in _TIMEFRAME_SECONDS:
        raise ValueError(f"unsupported timeframe: {timeframe!r}")
    return _TIMEFRAME_SECONDS[timeframe]


def candle_close_time(observed_at: str, timeframe: str) -> str:
    """observed_at (candle OPEN time) + timeframe duration = CLOSE time."""
    dt = _parse_ts(observed_at)
    return _fmt_ts(dt + timedelta(seconds=timeframe_seconds(timeframe)))


# ---------------------------------------------------------------------------
# Bollinger Bands
# ---------------------------------------------------------------------------


def bollinger_bands(
    closes: list[float], period: int = 20, stddev_mult: float = 2.0
) -> list[dict[str, float | None]]:
    """Returns one dict per input close: middle/upper/lower/bandwidth/percent_b.
    All fields None for the first `period - 1` rows (warm-up) — SMA needs a
    full window, so there is no earlier point where these are numerically
    valid. `bandwidth = (upper - lower) / middle`; `percent_b = (close -
    lower) / (upper - lower)` (None instead of divide-by-zero if upper==lower,
    i.e. a fully flat window)."""
    n = len(closes)
    out: list[dict[str, float | None]] = [
        {"middle": None, "upper": None, "lower": None, "bandwidth": None, "percent_b": None}
        for _ in range(n)
    ]
    for i in range(period - 1, n):
        window = closes[i - period + 1 : i + 1]
        mean = sum(window) / period
        variance = sum((v - mean) ** 2 for v in window) / period
        stdev = variance**0.5
        upper = mean + stddev_mult * stdev
        lower = mean - stddev_mult * stdev
        span = upper - lower
        bandwidth = (span / mean) if mean != 0 else None
        percent_b = ((closes[i] - lower) / span) if span != 0 else None
        out[i] = {
            "middle": mean,
            "upper": upper,
            "lower": lower,
            "bandwidth": bandwidth,
            "percent_b": percent_b,
        }
    return out


def bandwidth_percentile(bandwidths: list[float | None], window: int = 100) -> list[float | None]:
    """Historical percentile (0-100) of each bandwidth value within the
    trailing `window` closed points ending at (and including) that point —
    strictly backward-looking: index i only ever looks at
    bandwidths[max(0, i-window+1) : i+1], never forward. None until `window`
    non-None bandwidth points have accumulated (or the point itself is None)."""
    n = len(bandwidths)
    out: list[float | None] = [None] * n
    for i in range(n):
        if bandwidths[i] is None:
            continue
        lo = max(0, i - window + 1)
        trailing = [v for v in bandwidths[lo : i + 1] if v is not None]
        if len(trailing) < window:
            continue
        current = bandwidths[i]
        rank = sum(1 for v in trailing if v <= current)
        out[i] = 100.0 * rank / len(trailing)
    return out


# ---------------------------------------------------------------------------
# EMA
# ---------------------------------------------------------------------------


def ema(values: list[float], period: int) -> list[float | None]:
    """EMA seeded with a plain SMA of the first `period` values, None before
    the seed point (index `period - 1`) — same convention as
    indicators.py's `_ema` but returns None instead of NaN (JSON-friendly
    without a separate filter step, task #242 explicitly requires null, not
    NaN, for warm-up/undefined points)."""
    n = len(values)
    out: list[float | None] = [None] * n
    if n < period:
        return out
    k = 2.0 / (period + 1)
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    prev = seed
    for i in range(period, n):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


# ---------------------------------------------------------------------------
# EMA multi-timeframe projection
# ---------------------------------------------------------------------------


def project_ema_multi_timeframe(
    target_rows: list[dict[str, Any]],
    source_rows: list[dict[str, Any]],
    source_timeframe: str,
    target_timeframe: str,
    period: int,
) -> list[dict[str, Any]]:
    """Forward-fills a source-timeframe (HTF) EMA onto the target-timeframe
    (LTF) timeline. Zero look-ahead: the HTF EMA value computed off a source
    candle only becomes visible on the target timeline starting at that
    source candle's CLOSE time (`candle_close_time`), never earlier — a
    target row whose own open time is before the first source candle has
    even closed gets `value: None` (nothing valid to show yet), never the
    partially-formed HTF candle's number.

    `target_rows`/`source_rows`: ohlcv rows sorted by observed_at ascending,
    each `{"observed_at": ..., "close": ...}` (extra keys ignored).

    Returns one dict per target row: `{target_time, value, period,
    source_timeframe, source_candle_close_time, target_timeframe,
    last_updated_at}` — `source_candle_close_time`/`last_updated_at` are None
    alongside `value` when nothing has closed yet on the source series."""
    source_closes = [r["close"] for r in source_rows]
    source_ema = ema(source_closes, period)
    source_close_times = [candle_close_time(r["observed_at"], source_timeframe) for r in source_rows]

    # Build a list of (close_time, ema_value) for every source point that has
    # a real EMA value (skips warm-up None points — those contribute nothing
    # to project onto the target timeline).
    checkpoints: list[tuple[str, float]] = [
        (source_close_times[i], source_ema[i]) for i in range(len(source_rows)) if source_ema[i] is not None
    ]

    out: list[dict[str, Any]] = []
    cp_idx = 0
    active_value: float | None = None
    active_close_time: str | None = None
    for row in target_rows:
        target_time = row["observed_at"]
        # Advance to the latest checkpoint whose source candle closed AT OR
        # BEFORE this target candle's own open time — i.e. the HTF value must
        # already be finalized by the moment this target bar begins forming,
        # otherwise it wasn't actually knowable at that point in history.
        while cp_idx < len(checkpoints) and checkpoints[cp_idx][0] <= target_time:
            active_close_time, active_value = checkpoints[cp_idx]
            cp_idx += 1
        out.append(
            {
                "target_time": target_time,
                "value": active_value,
                "period": period,
                "source_timeframe": source_timeframe,
                "source_candle_close_time": active_close_time,
                "target_timeframe": target_timeframe,
                "last_updated_at": active_close_time,
            }
        )
    return out


# ---------------------------------------------------------------------------
# VWAP
# ---------------------------------------------------------------------------


def _session_key(observed_at: str) -> str:
    """UTC calendar-day key — session VWAP/CVD reset at UTC midnight
    (canonical, per task #242's "jawna strefa bazowa (UTC canonical)")."""
    return observed_at[:10]  # "YYYY-MM-DD" prefix of an ISO-8601 UTC string


def vwap_session(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Session VWAP, resetting at every UTC calendar-day boundary. Each row
    needs open/high/low/close/volume (typical price = (H+L+C)/3, standard
    convention). `session_timezone` is always "UTC" — surfaced per-point so
    the API contract stays self-describing without a separate lookup."""
    out: list[dict[str, Any]] = []
    cum_pv = 0.0
    cum_vol = 0.0
    current_session: str | None = None
    for row in rows:
        session = _session_key(row["observed_at"])
        if session != current_session:
            current_session = session
            cum_pv = 0.0
            cum_vol = 0.0
        typical = (row["high"] + row["low"] + row["close"]) / 3.0
        cum_pv += typical * row["volume"]
        cum_vol += row["volume"]
        value = (cum_pv / cum_vol) if cum_vol != 0 else None
        out.append(
            {
                "time": row["observed_at"],
                "value": value,
                "mode": "session",
                "anchor_time": None,
                "session_timezone": "UTC",
            }
        )
    return out


def vwap_anchored(rows: list[dict[str, Any]], anchor_time: str) -> list[dict[str, Any]]:
    """Anchored VWAP starting accumulation at the row whose observed_at ==
    anchor_time (must be an existing closed candle per task #242 — "bez
    automatycznego zgadywania kotwicy w MVP"; rows strictly before the
    anchor get value=None, nothing to compute yet)."""
    out: list[dict[str, Any]] = []
    cum_pv = 0.0
    cum_vol = 0.0
    anchored_started = False
    for row in rows:
        if not anchored_started:
            if row["observed_at"] < anchor_time:
                out.append(
                    {"time": row["observed_at"], "value": None, "mode": "anchored", "anchor_time": anchor_time, "session_timezone": "UTC"}
                )
                continue
            anchored_started = True
        typical = (row["high"] + row["low"] + row["close"]) / 3.0
        cum_pv += typical * row["volume"]
        cum_vol += row["volume"]
        value = (cum_pv / cum_vol) if cum_vol != 0 else None
        out.append(
            {"time": row["observed_at"], "value": value, "mode": "anchored", "anchor_time": anchor_time, "session_timezone": "UTC"}
        )
    return out


# ---------------------------------------------------------------------------
# CVD (Cumulative Volume Delta)
# ---------------------------------------------------------------------------

# #265: rows entering cvd_session/cvd_anchored only need taker_buy_volume/
# taker_sell_volume/observed_at — carrying these OPTIONAL keys through when
# present (bucket_public_trades sets them; the currency_aggregate Rubik rows
# never have them) lets the API layer surface per-bucket coverage detail
# (trade_count/first_trade_time/last_trade_time) without cvd_session/
# cvd_anchored needing a source_scope-specific branch.
_PASSTHROUGH_COVERAGE_KEYS = ("trade_count", "first_trade_time", "last_trade_time")


def _coverage_fields(row: dict[str, Any]) -> dict[str, Any]:
    return {k: row[k] for k in _PASSTHROUGH_COVERAGE_KEYS if k in row}


def cvd_session(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Session-reset CVD from taker_buy_volume/taker_sell_volume rows —
    signed delta = buy - sell, cumulative sum reset at every UTC calendar-day
    boundary. `source` is always explicitly "okx" — task #242: "jawnie
    oznaczyć źródło jako OKX (nie cały rynek)". #265: rows may come from
    either the currency-wide Rubik taker-volume series OR
    bucket_public_trades' instrument-specific buckets — this function does
    not know or care which; the API layer (main.py) is what stamps
    source_scope onto the response envelope, keeping this a pure numeric
    transform either way."""
    out: list[dict[str, Any]] = []
    cum = 0.0
    current_session: str | None = None
    for row in rows:
        session = _session_key(row["observed_at"])
        if session != current_session:
            current_session = session
            cum = 0.0
        buy = row["taker_buy_volume"]
        sell = row["taker_sell_volume"]
        delta = buy - sell
        cum += delta
        out.append(
            {
                "time": row["observed_at"],
                "value": cum,
                "delta": delta,
                "taker_buy": buy,
                "taker_sell": sell,
                "mode": "session",
                "anchor_time": None,
                "source": "okx",
                **_coverage_fields(row),
            }
        )
    return out


def cvd_anchored(rows: list[dict[str, Any]], anchor_time: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    cum = 0.0
    anchored_started = False
    for row in rows:
        buy = row["taker_buy_volume"]
        sell = row["taker_sell_volume"]
        delta = buy - sell
        if not anchored_started:
            if row["observed_at"] < anchor_time:
                out.append(
                    {
                        "time": row["observed_at"],
                        "value": None,
                        "delta": delta,
                        "taker_buy": buy,
                        "taker_sell": sell,
                        "mode": "anchored",
                        "anchor_time": anchor_time,
                        "source": "okx",
                        **_coverage_fields(row),
                    }
                )
                continue
            anchored_started = True
        cum += delta
        out.append(
            {
                "time": row["observed_at"],
                "value": cum,
                "delta": delta,
                "taker_buy": buy,
                "taker_sell": sell,
                "mode": "anchored",
                "anchor_time": anchor_time,
                "source": "okx",
                **_coverage_fields(row),
            }
        )
    return out


# ---------------------------------------------------------------------------
# Instrument-specific CVD from archived/live public trades (#265)
# ---------------------------------------------------------------------------

# OKX /api/v5/market/trades side values.
_TRADE_SIDE_BUY = "buy"
_TRADE_SIDE_SELL = "sell"


def bucket_public_trades(
    trades: list[dict[str, Any]], timeframe: str, *, now: datetime | None = None
) -> list[dict[str, Any]]:
    """Buckets raw OKX public trades (`/api/v5/market/trades` shape —
    ``{"side": "buy"|"sell", "sz": "<str float>", "ts": "<str epoch ms>"}``,
    one specific ``instId``'s tape) into closed, non-overlapping
    `timeframe`-sized windows, aligned to the plain (unshifted) UTC grid —
    same boundary convention as every other data_kind here (candle_close_time
    etc.), NOT the offset-shift feature.

    #265 no-look-ahead rule: a bucket is only emitted if its CLOSE time is
    <= `now` (defaults to current UTC wall clock) — the bucket the tape is
    CURRENTLY inside (still forming) is always dropped, exactly like a
    still-forming OHLCV candle never enters /api/ohlcv. `limit` on the raw
    trades pull is irrelevant to which buckets close — a bucket's own
    completeness is decided purely by whether `now` has passed its close
    time, never by how many trades happened to be included in this batch.

    Each output row carries `taker_buy_volume`/`taker_sell_volume` (summed
    `sz`, base-currency units — same unit family Rubik taker-volume reports
    in quote-currency, callers must not conflate the two, see /api/cvd's
    `units` field) plus `trade_count`/`first_trade_time`/`last_trade_time`
    for coverage/gap detection — a bucket with 0 trades is never synthesized
    (no interpolation, task #265 AC): only buckets that actually contain at
    least one trade appear in the output. Buckets are sorted ascending by
    `observed_at`; input `trades` may arrive in any order (OKX returns
    newest-first)."""
    bucket_seconds = timeframe_seconds(timeframe)
    now_dt = now if now is not None else datetime.now(timezone.utc)

    buckets: dict[datetime, list[dict[str, Any]]] = {}
    for trade in trades:
        ts_ms = int(trade["ts"])
        trade_dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
        epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
        elapsed = (trade_dt - epoch).total_seconds()
        bucket_index = int(elapsed // bucket_seconds)
        bucket_start = epoch + timedelta(seconds=bucket_index * bucket_seconds)
        buckets.setdefault(bucket_start, []).append(trade)

    out: list[dict[str, Any]] = []
    for bucket_start in sorted(buckets):
        bucket_close = bucket_start + timedelta(seconds=bucket_seconds)
        if bucket_close > now_dt:
            continue  # still-forming bucket — never emitted (no look-ahead)
        members = buckets[bucket_start]
        buy_vol = sum(float(t["sz"]) for t in members if t["side"] == _TRADE_SIDE_BUY)
        sell_vol = sum(float(t["sz"]) for t in members if t["side"] == _TRADE_SIDE_SELL)
        member_times = sorted(
            datetime.fromtimestamp(int(t["ts"]) / 1000, tz=timezone.utc) for t in members
        )
        out.append(
            {
                "observed_at": _fmt_ts(bucket_start),
                "taker_buy_volume": buy_vol,
                "taker_sell_volume": sell_vol,
                "trade_count": len(members),
                "first_trade_time": _fmt_ts(member_times[0]),
                "last_trade_time": _fmt_ts(member_times[-1]),
            }
        )
    return out


# ---------------------------------------------------------------------------
# Offset-shifted M30/H1 aggregation
# ---------------------------------------------------------------------------

# Allowed offsets per synthetic timeframe (task #242 scope — M30: 0/15,
# H1: 0/15/30/45). Rejecting anything else at the API layer keeps "every
# combination is a separate, regular, non-overlapping series" true — an
# arbitrary offset could produce a partial/irregular first bucket.
ALLOWED_OFFSETS = {"30m": (0, 15), "1h": (0, 15, 30, 45)}


def _bucket_start(open_time: datetime, bucket_seconds: int, offset_seconds: int) -> datetime:
    """Floor `open_time` to the bucket boundary of size `bucket_seconds`,
    shifted by `offset_seconds` — e.g. bucket_seconds=1800 (30m),
    offset_seconds=900 (15m offset) produces boundaries at :15/:45 instead of
    :00/:30. Anchored to UTC epoch, so it is stable across days/DST (there is
    no DST in UTC)."""
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    shifted = open_time - timedelta(seconds=offset_seconds)
    elapsed = (shifted - epoch).total_seconds()
    bucket_index = int(elapsed // bucket_seconds)
    bucket_start_shifted = epoch + timedelta(seconds=bucket_index * bucket_seconds)
    return bucket_start_shifted + timedelta(seconds=offset_seconds)


def aggregate_offset_ohlcv(
    base_rows: list[dict[str, Any]], base_timeframe: str, target_timeframe: str, offset_minutes: int
) -> list[dict[str, Any]]:
    """Builds a synthetic OHLCV series at `target_timeframe` (must be "30m"
    or "1h") from `base_rows` (must be finer-grained, e.g. 5m base for a 30m
    target), bucket boundaries shifted by `offset_minutes` from the
    unshifted UTC grid. A bucket is emitted ONLY if it is fully covered by
    complete base candles AND the bucket itself has already closed relative
    to the base data's own extent (no partial/forming buckets) — task #242:
    "budowane wyłącznie z kompletnych świec bazowych". Each combination
    (target_timeframe, offset) is independent and non-overlapping; this
    function is never called with overlapping/rolling windows."""
    if target_timeframe not in ALLOWED_OFFSETS:
        raise ValueError(f"unsupported target_timeframe for offset aggregation: {target_timeframe!r}")
    if offset_minutes not in ALLOWED_OFFSETS[target_timeframe]:
        raise ValueError(
            f"offset_minutes={offset_minutes} not allowed for {target_timeframe} "
            f"(allowed: {ALLOWED_OFFSETS[target_timeframe]})"
        )
    base_seconds = timeframe_seconds(base_timeframe)
    target_seconds = timeframe_seconds(target_timeframe)
    if target_seconds % base_seconds != 0:
        raise ValueError(f"target_timeframe {target_timeframe} not a multiple of base {base_timeframe}")
    offset_seconds = offset_minutes * 60

    buckets: dict[datetime, list[dict[str, Any]]] = {}
    for row in base_rows:
        open_dt = _parse_ts(row["observed_at"])
        bucket_start = _bucket_start(open_dt, target_seconds, offset_seconds)
        buckets.setdefault(bucket_start, []).append(row)

    expected_base_bars = target_seconds // base_seconds
    out: list[dict[str, Any]] = []
    for bucket_start in sorted(buckets):
        members = sorted(buckets[bucket_start], key=lambda r: r["observed_at"])
        if len(members) != expected_base_bars:
            continue  # incomplete bucket (partial coverage at series edges) — never emitted
        # Verify contiguity: consecutive base candles with no gap. A hole in
        # the middle (backfill gap) must not silently produce a bucket that
        # LOOKS complete by count alone but skips real bars.
        contiguous = all(
            _parse_ts(members[i]["observed_at"]) + timedelta(seconds=base_seconds) == _parse_ts(members[i + 1]["observed_at"])
            for i in range(len(members) - 1)
        )
        if not contiguous:
            continue
        bucket_close = bucket_start + timedelta(seconds=target_seconds)
        last_base_close = _parse_ts(members[-1]["observed_at"]) + timedelta(seconds=base_seconds)
        if last_base_close != bucket_close:
            continue  # last base candle doesn't actually reach the bucket's close — not complete
        out.append(
            {
                "observed_at": _fmt_ts(bucket_start),
                "open": members[0]["open"],
                "high": max(r["high"] for r in members),
                "low": min(r["low"] for r in members),
                "close": members[-1]["close"],
                "volume": sum(r["volume"] for r in members),
                "offset_minutes": offset_minutes,
                "timeframe": target_timeframe,
            }
        )
    return out


def aggregate_offset_taker_volume(
    base_rows: list[dict[str, Any]], base_timeframe: str, target_timeframe: str, offset_minutes: int
) -> list[dict[str, Any]]:
    """Same bucketing rule as aggregate_offset_ohlcv, for taker_volume rows
    (taker_buy_volume/taker_sell_volume summed per bucket) — kept as a
    separate function (not a generic reducer) so each call site stays
    explicit about which columns it sums, matching this module's style."""
    if target_timeframe not in ALLOWED_OFFSETS:
        raise ValueError(f"unsupported target_timeframe for offset aggregation: {target_timeframe!r}")
    if offset_minutes not in ALLOWED_OFFSETS[target_timeframe]:
        raise ValueError(
            f"offset_minutes={offset_minutes} not allowed for {target_timeframe} "
            f"(allowed: {ALLOWED_OFFSETS[target_timeframe]})"
        )
    return _aggregate_taker_volume_buckets(base_rows, base_timeframe, target_timeframe, offset_minutes)


# #253: plain (non-offset-shifted) aggregation target timeframes — distinct
# from ALLOWED_OFFSETS, which gates the offset-SHIFT feature (task #242) and
# must not grow just because a timeframe also needs a synthetic fallback
# here. 15m/4h have no offset variants, only offset_minutes=0.
_PLAIN_AGGREGATION_TIMEFRAMES = {"15m": "5m", "4h": "1h"}


def aggregate_taker_volume(
    base_rows: list[dict[str, Any]], base_timeframe: str, target_timeframe: str
) -> list[dict[str, Any]]:
    """Plain (unshifted, offset_minutes=0) taker_volume aggregation for a
    target timeframe OKX's rubik/stat/* source never backfills natively
    (15m, 4h — see #253: verified missing for every symbol, not just one).
    Same non-look-ahead bucketing as aggregate_offset_taker_volume, kept
    separate because ALLOWED_OFFSETS (the offset-SHIFT feature's allowlist)
    must not be widened just to satisfy this unrelated fallback."""
    if target_timeframe not in _PLAIN_AGGREGATION_TIMEFRAMES:
        raise ValueError(f"unsupported target_timeframe for plain aggregation: {target_timeframe!r}")
    if base_timeframe != _PLAIN_AGGREGATION_TIMEFRAMES[target_timeframe]:
        raise ValueError(
            f"base_timeframe {base_timeframe!r} does not match expected "
            f"{_PLAIN_AGGREGATION_TIMEFRAMES[target_timeframe]!r} for target {target_timeframe!r}"
        )
    return _aggregate_taker_volume_buckets(base_rows, base_timeframe, target_timeframe, 0)


def _aggregate_taker_volume_buckets(
    base_rows: list[dict[str, Any]], base_timeframe: str, target_timeframe: str, offset_minutes: int
) -> list[dict[str, Any]]:
    base_seconds = timeframe_seconds(base_timeframe)
    target_seconds = timeframe_seconds(target_timeframe)
    if target_seconds % base_seconds != 0:
        raise ValueError(f"target_timeframe {target_timeframe} not a multiple of base {base_timeframe}")
    offset_seconds = offset_minutes * 60

    buckets: dict[datetime, list[dict[str, Any]]] = {}
    for row in base_rows:
        open_dt = _parse_ts(row["observed_at"])
        bucket_start = _bucket_start(open_dt, target_seconds, offset_seconds)
        buckets.setdefault(bucket_start, []).append(row)

    expected_base_bars = target_seconds // base_seconds
    out: list[dict[str, Any]] = []
    for bucket_start in sorted(buckets):
        members = sorted(buckets[bucket_start], key=lambda r: r["observed_at"])
        if len(members) != expected_base_bars:
            continue
        contiguous = all(
            _parse_ts(members[i]["observed_at"]) + timedelta(seconds=base_seconds) == _parse_ts(members[i + 1]["observed_at"])
            for i in range(len(members) - 1)
        )
        if not contiguous:
            continue
        bucket_close = bucket_start + timedelta(seconds=target_seconds)
        last_base_close = _parse_ts(members[-1]["observed_at"]) + timedelta(seconds=base_seconds)
        if last_base_close != bucket_close:
            continue
        out.append(
            {
                "observed_at": _fmt_ts(bucket_start),
                "taker_buy_volume": sum(r["taker_buy_volume"] for r in members),
                "taker_sell_volume": sum(r["taker_sell_volume"] for r in members),
                "offset_minutes": offset_minutes,
                "timeframe": target_timeframe,
            }
        )
    return out
