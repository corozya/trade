"""Synthetic multi-timeframe candle aggregation (#262, podzadanie #261):
deterministic H1->H3/H4 (and any count*base_timeframe==target_timeframe
combination) aggregation with explicit anchor/offset, closed-only /
provisional semantics, and full body/wick ratio metrics.

Distinct from technical_overlays.py's aggregate_offset_ohlcv/
aggregate_offset_taker_volume: those build a whole *regular, non-overlapping*
offset-shifted series (M30/H1 from 5m/15m base, ALLOWED_OFFSETS-gated) for
charting. This module answers a single caller-specified WINDOW (anchor_time
or offset_minutes pins one specific bucket boundary, `count` composing bars
of `base_timeframe`) and returns one aggregation record with full metrics —
the read-only analyst helper task #261 describes, not a chart series.

Pure functions on already-read OHLCV rows: no I/O, no lake access. main.py's
endpoint does the `_read_series()` call and passes rows in, exactly like
technical_overlays.py's docstring convention.

## Anti-look-ahead rule
`aggregate_window` only ever consumes rows from `base_rows` (already
constrained by the caller to what's on disk "as of now") plus, only when
`include_provisional=True`, one caller-supplied still-forming candle appended
by main.py (from the same live OKX endpoint /api/live_candle already uses).
This module never reaches out for data itself.

## Time semantics
Same OPEN-time convention as technical_overlays.py: each base row's
`observed_at` is the candle's OPEN time; CLOSE time = observed_at +
base_timeframe duration. All timestamps UTC canonical ISO-8601 `...Z`.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from technical_overlays import timeframe_seconds

import re

_TIMEFRAME_RE = re.compile(r"^(\d+)([mhd])$")
_UNIT_SECONDS = {"m": 60, "h": 60 * 60, "d": 24 * 60 * 60}


def _any_timeframe_seconds(timeframe: str) -> int:
    """Parses the seconds duration of ANY well-formed `<int><m|h|d>` timeframe
    string, not just the exchange-standard ones `technical_overlays.
    timeframe_seconds` recognizes (1m/5m/15m/30m/1h/4h/1d). This module's
    `target_timeframe` is a synthetic, caller-composed value (e.g. "3h" from
    count=3 * base_timeframe="1h") that legitimately doesn't appear on any
    exchange's candle list — rejecting it here would make `count*base_
    timeframe==target_timeframe` validation impossible for exactly the
    non-standard combinations this module exists to support (#262 AC)."""
    match = _TIMEFRAME_RE.match(timeframe)
    if not match:
        raise ValueError(f"unsupported timeframe: {timeframe!r}")
    value, unit = match.groups()
    return int(value) * _UNIT_SECONDS[unit]


def _parse_ts(iso_ts: str) -> datetime:
    return datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))


def _fmt_ts(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class SyntheticCandleError(Exception):
    """Base class for all validation/aggregation errors this module raises.
    main.py maps subclasses to HTTP status codes (400/404/409/422)."""


class InvalidParametersError(SyntheticCandleError):
    """Bad timeframe/count/target/anchor/offset, or a contradiction between
    them (-> HTTP 400)."""


class WindowNotFoundError(SyntheticCandleError):
    """No base data at all covers the requested window (-> HTTP 404)."""


class IncompleteWindowError(SyntheticCandleError):
    """A gap/discontinuity in constituent candles, or (closed_only) an
    unclosed constituent — window exists but cannot be honestly assembled
    (-> HTTP 409/422)."""


def _body(o: float, c: float) -> float:
    return abs(c - o)


def _range(h: float, l: float) -> float:
    return h - l


def resolve_target(
    base_timeframe: str, count: int | None, target_timeframe: str | None
) -> tuple[int, str]:
    """Cross-validates count/target_timeframe against base_timeframe and
    returns (count, target_timeframe) fully resolved. Exactly one of
    count/target_timeframe may be omitted (the other is derived); if both are
    given they must agree (`count * base_timeframe == target_timeframe`,
    task #262 AC)."""
    try:
        base_seconds = timeframe_seconds(base_timeframe)
    except ValueError as exc:
        raise InvalidParametersError(str(exc)) from exc

    if count is None and target_timeframe is None:
        raise InvalidParametersError("either count or target_timeframe is required")

    if target_timeframe is not None:
        try:
            target_seconds = _any_timeframe_seconds(target_timeframe)
        except ValueError as exc:
            raise InvalidParametersError(str(exc)) from exc
        if target_seconds % base_seconds != 0:
            raise InvalidParametersError(
                f"target_timeframe {target_timeframe!r} is not a multiple of base_timeframe {base_timeframe!r}"
            )
        derived_count = target_seconds // base_seconds
        if count is not None and count != derived_count:
            raise InvalidParametersError(
                f"count={count} does not match target_timeframe={target_timeframe!r} "
                f"for base_timeframe={base_timeframe!r} (expected count={derived_count})"
            )
        count = derived_count
    else:
        if count is None or count < 1:
            raise InvalidParametersError(f"count must be a positive integer, got {count!r}")
        target_timeframe = _timeframe_for_seconds(count * base_seconds)

    if count < 1:
        raise InvalidParametersError(f"count must be >= 1, got {count}")
    # MVP scope (#261/#262): only 3x/4x base aggregation is in-scope.
    if count not in (3, 4):
        raise InvalidParametersError(f"count={count} not supported in MVP (only 3 or 4)")
    return count, target_timeframe


_SECONDS_TO_TIMEFRAME = {
    60: "1m", 5 * 60: "5m", 15 * 60: "15m", 30 * 60: "30m",
    60 * 60: "1h", 3 * 60 * 60: "3h", 4 * 60 * 60: "4h", 24 * 60 * 60: "1d",
}


def _timeframe_for_seconds(seconds: int) -> str:
    return _SECONDS_TO_TIMEFRAME.get(seconds, f"{seconds}s")


def resolve_window_start(
    *,
    base_timeframe: str,
    target_seconds: int,
    anchor_time: str | None,
    offset_minutes: int | None,
) -> datetime:
    """Resolves the exact open time of the target window from either
    `anchor_time` (an explicit ISO-8601 timestamp that must itself sit on the
    base_timeframe grid; it defines the target window boundary) or
    `offset_minutes` (a grid shift, applied against the most recent complete
    window boundary — same UTC-epoch-anchored bucketing rule as
    technical_overlays._bucket_start, so DST never enters into it). Exactly
    one of anchor_time/offset_minutes may be given; both or neither is a 400
    (task #262: "sprzeczne parametry")."""
    base_seconds = timeframe_seconds(base_timeframe)
    if anchor_time is not None and offset_minutes is not None:
        raise InvalidParametersError("anchor_time and offset_minutes are mutually exclusive")
    if anchor_time is None and offset_minutes is None:
        raise InvalidParametersError("one of anchor_time or offset_minutes is required")

    if anchor_time is not None:
        try:
            anchor_dt = _parse_ts(anchor_time)
        except ValueError as exc:
            raise InvalidParametersError(f"invalid anchor_time: {anchor_time!r}") from exc
        if anchor_dt.tzinfo is None:
            raise InvalidParametersError("anchor_time must include a timezone")
        anchor_dt = anchor_dt.astimezone(timezone.utc)
        epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
        elapsed = (anchor_dt - epoch).total_seconds()
        if elapsed % base_seconds != 0:
            raise InvalidParametersError(
                f"anchor_time {anchor_time!r} is not aligned to base_timeframe {base_timeframe!r} grid"
            )
        return anchor_dt

    # offset_minutes: shift applied to the target-window UTC grid, same
    # bucket-boundary math as technical_overlays._bucket_start.
    offset_seconds = offset_minutes * 60
    if offset_seconds < 0 or offset_seconds >= target_seconds:
        raise InvalidParametersError(
            f"offset_minutes={offset_minutes} out of range for target window of {target_seconds}s"
        )
    if offset_seconds % base_seconds != 0:
        raise InvalidParametersError(
            f"offset_minutes={offset_minutes} is not aligned to base_timeframe {base_timeframe!r} grid"
        )
    now = datetime.now(timezone.utc)
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    shifted = now - timedelta(seconds=offset_seconds)
    elapsed = (shifted - epoch).total_seconds()
    bucket_index = int(elapsed // target_seconds)
    bucket_start_shifted = epoch + timedelta(seconds=bucket_index * target_seconds)
    return bucket_start_shifted + timedelta(seconds=offset_seconds)


def aggregate_window(
    *,
    base_rows: list[dict[str, Any]],
    base_timeframe: str,
    count: int,
    target_timeframe: str,
    window_start: datetime,
    closed_only: bool,
    include_provisional: bool,
    now: datetime,
    provisional_row: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assembles exactly one target window starting at `window_start` from
    `base_rows` (already-read, ascending by observed_at). Raises
    WindowNotFoundError if nothing in base_rows overlaps the window at all,
    IncompleteWindowError if constituents have a gap/discontinuity or
    (closed_only) contain an unclosed bar. `provisional_row` — the
    still-forming base candle straight from OKX (same shape as base_rows,
    e.g. /api/live_candle) — is only consulted when include_provisional=True
    AND it is needed to fill exactly the LAST constituent slot; it is never
    substituted for an earlier (should-be-closed) constituent."""
    base_seconds = timeframe_seconds(base_timeframe)
    target_seconds = count * base_seconds
    window_end = window_start + timedelta(seconds=target_seconds)

    by_open: dict[datetime, dict[str, Any]] = {}
    for row in base_rows:
        open_dt = _parse_ts(row["observed_at"])
        if window_start <= open_dt < window_end:
            by_open[open_dt] = row

    expected_opens = [window_start + timedelta(seconds=i * base_seconds) for i in range(count)]

    last_open = expected_opens[-1]
    last_close = last_open + timedelta(seconds=base_seconds)
    last_is_closed_by_clock = last_close <= now

    members: list[dict[str, Any]] = []
    constituents_meta: list[dict[str, Any]] = []
    used_provisional = False
    for i, open_dt in enumerate(expected_opens):
        row = by_open.get(open_dt)
        is_last = i == len(expected_opens) - 1
        close_dt = open_dt + timedelta(seconds=base_seconds)
        row_is_closed = close_dt <= now

        if row is None:
            if (
                is_last
                and include_provisional
                and not row_is_closed
                and provisional_row is not None
                and _parse_ts(provisional_row["observed_at"]) == open_dt
            ):
                row = provisional_row
                used_provisional = True
            else:
                if not by_open and not members:
                    continue  # keep scanning; WindowNotFoundError raised below if truly nothing overlaps
                raise IncompleteWindowError(
                    f"missing constituent candle at {_fmt_ts(open_dt)} ({base_timeframe}) — "
                    "gap in base data, refusing to silently assemble a partial window"
                )

        is_closed = row_is_closed and not (is_last and used_provisional)
        if closed_only and not is_closed:
            raise IncompleteWindowError(
                f"constituent at {_fmt_ts(open_dt)} is not closed yet — closed_only=true forbids this window"
            )
        if not closed_only and not include_provisional and not is_closed:
            # Neither closed_only nor include_provisional requested this
            # relaxation explicitly — an unclosed constituent is still a gap.
            raise IncompleteWindowError(
                f"constituent at {_fmt_ts(open_dt)} is not closed yet — pass include_provisional=true "
                "to accept a still-forming final bar"
            )
        if not is_last and not is_closed:
            # An unclosed bar can only ever be the LAST constituent —
            # anything earlier must already be closed (there's no such thing
            # as a provisional bar in the middle of a completed sequence).
            raise IncompleteWindowError(
                f"constituent at {_fmt_ts(open_dt)} is unclosed but is not the final bar of the window"
            )

        members.append(row)
        constituents_meta.append({
            "open_time": _fmt_ts(open_dt),
            "close_time": _fmt_ts(close_dt),
            "is_closed": is_closed,
        })

    if not members:
        raise WindowNotFoundError(
            f"no base data overlaps window {_fmt_ts(window_start)}..{_fmt_ts(window_end)}"
        )
    if len(members) != count:
        raise IncompleteWindowError(
            f"window {_fmt_ts(window_start)}..{_fmt_ts(window_end)} has {len(members)}/{count} constituents"
        )

    window_is_closed = all(m["is_closed"] for m in constituents_meta) and last_is_closed_by_clock

    o = float(members[0]["open"])
    h = max(float(r["high"]) for r in members)
    l = min(float(r["low"]) for r in members)
    c = float(members[-1]["close"])
    volumes = [r.get("volume") for r in members]
    v = sum(float(x) for x in volumes if x is not None) if any(x is not None for x in volumes) else None

    rng = _range(h, l)
    body = _body(o, c)
    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l

    if c > o:
        direction = "bullish"
    elif c < o:
        direction = "bearish"
    else:
        direction = "doji"

    is_doji = body == 0
    if rng == 0:
        # Degenerate O=H=L=C window — every ratio is undefined, not 0/0.
        body_pct_range = None
        upper_wick_pct_range = None
        lower_wick_pct_range = None
        close_location_pct = None
    else:
        body_pct_range = body / rng
        upper_wick_pct_range = upper_wick / rng
        lower_wick_pct_range = lower_wick / rng
        close_location_pct = (c - l) / rng
    if is_doji or body == 0:
        upper_wick_to_body = None
        lower_wick_to_body = None
    else:
        upper_wick_to_body = upper_wick / body
        lower_wick_to_body = lower_wick / body

    return {
        "base_timeframe": base_timeframe,
        "target_timeframe": target_timeframe,
        "count": count,
        "open_time": _fmt_ts(window_start),
        "close_time": _fmt_ts(window_end),
        "is_closed": window_is_closed,
        "constituents": constituents_meta,
        "open": o,
        "high": h,
        "low": l,
        "close": c,
        "volume": v,
        "direction": direction,
        "range": rng,
        "body": body,
        "upper_wick": upper_wick,
        "lower_wick": lower_wick,
        "body_pct_range": body_pct_range,
        "upper_wick_pct_range": upper_wick_pct_range,
        "lower_wick_pct_range": lower_wick_pct_range,
        "upper_wick_to_body": upper_wick_to_body,
        "lower_wick_to_body": lower_wick_to_body,
        "close_location_pct": close_location_pct,
        "is_doji": is_doji,
    }
