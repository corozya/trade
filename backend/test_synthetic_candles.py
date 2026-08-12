"""Tests for synthetic_candles.py (#264, podzadanie #261/#262): pure-function
coverage of H1->H3/H4 (and any count*base_timeframe==target_timeframe)
aggregation — resolve_target, resolve_window_start, aggregate_window.

Deterministic fixtures only (no lake/OKX I/O) — same convention as
test_technical_overlays.py. Covers categories 1-2, 4-8 of the #261 test list
at the pure-function layer; category 3 (closed-only HTTP-level "no result
before confirm") and the full error-code mapping are covered at the endpoint
layer in test_synthetic_candles_api.py, since that mapping happens in main.py.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from synthetic_candles import (
    IncompleteWindowError,
    InvalidParametersError,
    WindowNotFoundError,
    aggregate_window,
    resolve_target,
    resolve_window_start,
)

START = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _h1_row(open_dt: datetime, o: float, h: float, l: float, c: float, v: float | None = 10.0) -> dict:
    return {"observed_at": _iso(open_dt), "open": o, "high": h, "low": l, "close": c, "volume": v}


# ---------------------------------------------------------------------------
# 1. Unit: OHLCV correctness + all ratios, 3xH1 and 4xH1, bullish/bearish/doji
# ---------------------------------------------------------------------------


def _bullish_3h1_rows():
    # O=100 (first open), H=max(high)=112, L=min(low)=97, C=110 (last close).
    return [
        _h1_row(START, 100.0, 105.0, 97.0, 103.0, v=10.0),
        _h1_row(START + timedelta(hours=1), 103.0, 108.0, 101.0, 106.0, v=20.0),
        _h1_row(START + timedelta(hours=2), 106.0, 112.0, 104.0, 110.0, v=30.0),
    ]


def test_aggregate_3xh1_bullish_ohlcv_and_ratios():
    rows = _bullish_3h1_rows()
    now = START + timedelta(hours=3)  # exactly at close of last constituent
    result = aggregate_window(
        base_rows=rows, base_timeframe="1h", count=3, target_timeframe="3h",
        window_start=START, closed_only=True, include_provisional=False, now=now,
    )
    assert result["open"] == 100.0
    assert result["high"] == 112.0
    assert result["low"] == 97.0
    assert result["close"] == 110.0
    assert result["volume"] == pytest.approx(60.0)  # sum of 10+20+30
    assert result["direction"] == "bullish"
    assert result["is_closed"] is True
    assert result["count"] == 3
    assert result["target_timeframe"] == "3h"

    rng = 112.0 - 97.0
    body = abs(110.0 - 100.0)
    upper_wick = 112.0 - max(100.0, 110.0)
    lower_wick = min(100.0, 110.0) - 97.0
    assert result["range"] == pytest.approx(rng)
    assert result["body"] == pytest.approx(body)
    assert result["upper_wick"] == pytest.approx(upper_wick)
    assert result["lower_wick"] == pytest.approx(lower_wick)
    assert result["body_pct_range"] == pytest.approx(body / rng)
    assert result["upper_wick_pct_range"] == pytest.approx(upper_wick / rng)
    assert result["lower_wick_pct_range"] == pytest.approx(lower_wick / rng)
    assert result["upper_wick_to_body"] == pytest.approx(upper_wick / body)
    assert result["lower_wick_to_body"] == pytest.approx(lower_wick / body)
    assert result["close_location_pct"] == pytest.approx((110.0 - 97.0) / rng)
    assert result["is_doji"] is False
    assert len(result["constituents"]) == 3
    assert result["constituents"][0]["open_time"] == _iso(START)
    assert result["constituents"][-1]["close_time"] == _iso(START + timedelta(hours=3))
    assert all(c["is_closed"] for c in result["constituents"])


def test_aggregate_4xh1_bearish_ohlcv_and_ratios():
    rows = [
        _h1_row(START, 100.0, 101.0, 90.0, 95.0, v=5.0),
        _h1_row(START + timedelta(hours=1), 95.0, 96.0, 88.0, 90.0, v=6.0),
        _h1_row(START + timedelta(hours=2), 90.0, 91.0, 80.0, 85.0, v=7.0),
        _h1_row(START + timedelta(hours=3), 85.0, 87.0, 75.0, 78.0, v=8.0),
    ]
    now = START + timedelta(hours=4)
    result = aggregate_window(
        base_rows=rows, base_timeframe="1h", count=4, target_timeframe="4h",
        window_start=START, closed_only=True, include_provisional=False, now=now,
    )
    assert result["open"] == 100.0
    assert result["high"] == 101.0
    assert result["low"] == 75.0
    assert result["close"] == 78.0
    assert result["volume"] == pytest.approx(26.0)
    assert result["direction"] == "bearish"
    assert result["is_closed"] is True

    rng = 101.0 - 75.0
    body = abs(78.0 - 100.0)
    upper_wick = 101.0 - max(100.0, 78.0)
    lower_wick = min(100.0, 78.0) - 75.0
    assert result["body_pct_range"] == pytest.approx(body / rng)
    assert result["upper_wick_pct_range"] == pytest.approx(upper_wick / rng)
    assert result["lower_wick_pct_range"] == pytest.approx(lower_wick / rng)
    assert result["close_location_pct"] == pytest.approx((78.0 - 75.0) / rng)


def test_aggregate_doji_body_zero_yields_null_ratios_not_nan_or_inf():
    # Open of first == close of last (=100), but range is nonzero (wicks
    # exist) -> body=0, body_pct_range/close_location_pct still well-defined
    # (range != 0), but wick_to_body ratios must be null (division by
    # zero body), never Infinity/NaN.
    rows = [
        _h1_row(START, 100.0, 105.0, 95.0, 102.0),
        _h1_row(START + timedelta(hours=1), 102.0, 106.0, 98.0, 99.0),
        _h1_row(START + timedelta(hours=2), 99.0, 103.0, 96.0, 100.0),
    ]
    now = START + timedelta(hours=3)
    result = aggregate_window(
        base_rows=rows, base_timeframe="1h", count=3, target_timeframe="3h",
        window_start=START, closed_only=True, include_provisional=False, now=now,
    )
    assert result["open"] == 100.0
    assert result["close"] == 100.0
    assert result["body"] == 0.0
    assert result["is_doji"] is True
    assert result["direction"] == "doji"
    assert result["upper_wick_to_body"] is None
    assert result["lower_wick_to_body"] is None
    # range = high(106) - low(95) = 11 != 0, so range-relative ratios ARE defined.
    assert result["body_pct_range"] == pytest.approx(0.0)
    assert result["range"] == pytest.approx(11.0)


def test_aggregate_degenerate_o_eq_h_eq_l_eq_c_yields_null_range_ratios():
    # Every constituent is a flat zero-range bar -> aggregated O=H=L=C, so
    # even range-relative ratios are undefined (0/0), not 0.
    rows = [
        _h1_row(START, 100.0, 100.0, 100.0, 100.0),
        _h1_row(START + timedelta(hours=1), 100.0, 100.0, 100.0, 100.0),
        _h1_row(START + timedelta(hours=2), 100.0, 100.0, 100.0, 100.0),
    ]
    now = START + timedelta(hours=3)
    result = aggregate_window(
        base_rows=rows, base_timeframe="1h", count=3, target_timeframe="3h",
        window_start=START, closed_only=True, include_provisional=False, now=now,
    )
    assert result["range"] == 0.0
    assert result["body"] == 0.0
    assert result["is_doji"] is True
    assert result["body_pct_range"] is None
    assert result["upper_wick_pct_range"] is None
    assert result["lower_wick_pct_range"] is None
    assert result["close_location_pct"] is None
    assert result["upper_wick_to_body"] is None
    assert result["lower_wick_to_body"] is None


# ---------------------------------------------------------------------------
# 2. Boundary: exact anchor/offset, DST Europe/Warsaw vs UTC, full-hour grid
# ---------------------------------------------------------------------------


def test_resolve_target_count_and_target_timeframe_must_agree():
    assert resolve_target("1h", 3, None) == (3, "3h")
    assert resolve_target("1h", None, "4h") == (4, "4h")
    assert resolve_target("1h", 3, "3h") == (3, "3h")
    with pytest.raises(InvalidParametersError):
        resolve_target("1h", 3, "4h")


def test_resolve_target_rejects_out_of_mvp_scope_counts():
    with pytest.raises(InvalidParametersError):
        resolve_target("1h", 5, None)
    with pytest.raises(InvalidParametersError):
        resolve_target("1h", 2, None)
    with pytest.raises(InvalidParametersError):
        resolve_target("1h", 0, None)


def test_resolve_target_requires_target_multiple_of_base():
    with pytest.raises(InvalidParametersError):
        resolve_target("1h", None, "90m")


def test_resolve_window_start_exact_anchor_on_grid():
    dt = resolve_window_start(
        base_timeframe="1h", target_seconds=3 * 3600,
        anchor_time="2026-01-01T09:00:00Z", offset_minutes=None,
    )
    assert dt == datetime(2026, 1, 1, 9, 0, 0, tzinfo=timezone.utc)


def test_resolve_window_start_rejects_anchor_off_base_grid():
    # Not on the 1h base grid at all (minutes != 0).
    with pytest.raises(InvalidParametersError):
        resolve_window_start(
            base_timeframe="1h", target_seconds=3 * 3600,
            anchor_time="2026-01-01T09:30:00Z", offset_minutes=None,
        )


def test_resolve_window_start_anchor_defines_custom_target_boundary():
    # An explicit anchor defines the requested window. It only needs to be
    # aligned to the BASE grid; H3 08:00-11:00 is a required #261 fixture and
    # is intentionally shifted from the UTC-epoch H3 grid.
    dt = resolve_window_start(
        base_timeframe="1h", target_seconds=3 * 3600,
        anchor_time="2026-08-10T08:00:00Z", offset_minutes=None,
    )
    assert dt == datetime(2026, 8, 10, 8, 0, 0, tzinfo=timezone.utc)


def test_resolve_window_start_anchor_and_offset_mutually_exclusive():
    with pytest.raises(InvalidParametersError):
        resolve_window_start(
            base_timeframe="1h", target_seconds=3 * 3600,
            anchor_time="2026-01-01T09:00:00Z", offset_minutes=30,
        )


def test_resolve_window_start_requires_one_of_anchor_or_offset():
    with pytest.raises(InvalidParametersError):
        resolve_window_start(
            base_timeframe="1h", target_seconds=3 * 3600,
            anchor_time=None, offset_minutes=None,
        )


def test_resolve_window_start_rejects_offset_not_aligned_to_base_grid():
    with pytest.raises(InvalidParametersError):
        resolve_window_start(
            base_timeframe="1h", target_seconds=3 * 3600,
            anchor_time=None, offset_minutes=90,  # 90min not a multiple of 60min base
        )


def test_resolve_window_start_rejects_offset_out_of_range():
    with pytest.raises(InvalidParametersError):
        resolve_window_start(
            base_timeframe="1h", target_seconds=3 * 3600,
            anchor_time=None, offset_minutes=180,  # == target_seconds, must be < target
        )


def test_resolve_window_start_anchor_on_full_hour_boundary_h4():
    dt = resolve_window_start(
        base_timeframe="1h", target_seconds=4 * 3600,
        anchor_time="2026-01-01T08:00:00Z", offset_minutes=None,
    )
    assert dt == datetime(2026, 1, 1, 8, 0, 0, tzinfo=timezone.utc)
    assert dt.minute == 0 and dt.second == 0


def test_utc_bucketing_unaffected_by_europe_warsaw_dst_spring_forward():
    # 2026-03-29 01:00 UTC -> 03:00 CEST is the EU spring-forward instant
    # (Europe/Warsaw local time skips 02:00-03:00). This module works
    # entirely in UTC epoch-anchored grid math (see module docstring) so a
    # 3h window anchored either side of that instant must resolve to a
    # perfectly regular UTC boundary with no skipped/doubled bucket.
    before = resolve_window_start(
        base_timeframe="1h", target_seconds=3 * 3600,
        anchor_time="2026-03-29T00:00:00Z", offset_minutes=None,
    )
    after = resolve_window_start(
        base_timeframe="1h", target_seconds=3 * 3600,
        anchor_time="2026-03-29T03:00:00Z", offset_minutes=None,
    )
    assert after - before == timedelta(hours=3)
    assert before == datetime(2026, 3, 29, 0, 0, 0, tzinfo=timezone.utc)
    assert after == datetime(2026, 3, 29, 3, 0, 0, tzinfo=timezone.utc)


def test_utc_bucketing_unaffected_by_europe_warsaw_dst_fall_back():
    # 2026-10-25 is the EU fall-back transition (Europe/Warsaw 03:00 CEST ->
    # 02:00 CET, local 02:00-03:00 occurs twice) — again irrelevant here
    # since anchor_time is required to already be UTC or convertible to it,
    # and grid math is pure UTC epoch arithmetic.
    dt = resolve_window_start(
        base_timeframe="1h", target_seconds=4 * 3600,
        anchor_time="2026-10-25T00:00:00Z", offset_minutes=None,
    )
    assert dt == datetime(2026, 10, 25, 0, 0, 0, tzinfo=timezone.utc)


def test_anchor_time_accepts_non_utc_offset_and_normalizes_to_utc():
    # Europe/Warsaw is UTC+1 in winter (CET) — an anchor expressed in that
    # offset must resolve to the identical UTC instant as its UTC equivalent.
    dt_warsaw_offset = resolve_window_start(
        base_timeframe="1h", target_seconds=3 * 3600,
        anchor_time="2026-01-01T10:00:00+01:00", offset_minutes=None,
    )
    dt_utc = resolve_window_start(
        base_timeframe="1h", target_seconds=3 * 3600,
        anchor_time="2026-01-01T09:00:00Z", offset_minutes=None,
    )
    assert dt_warsaw_offset == dt_utc


def test_anchor_time_requires_timezone():
    with pytest.raises(InvalidParametersError):
        resolve_window_start(
            base_timeframe="1h", target_seconds=3 * 3600,
            anchor_time="2026-01-01T09:00:00", offset_minutes=None,  # naive, no tz
        )


# ---------------------------------------------------------------------------
# 3. Closed-only: no result before confirm of last H1, appears after without
#    changing historical data (pure-function layer: aggregate_window's own
#    `now`-driven is_closed / closed_only gate; HTTP 409 mapping tested in
#    test_synthetic_candles_api.py)
# ---------------------------------------------------------------------------


def test_closed_only_raises_before_last_constituent_confirmed():
    rows = _bullish_3h1_rows()
    now = START + timedelta(hours=2, minutes=59)  # 1 minute before last bar closes
    with pytest.raises(IncompleteWindowError):
        aggregate_window(
            base_rows=rows, base_timeframe="1h", count=3, target_timeframe="3h",
            window_start=START, closed_only=True, include_provisional=False, now=now,
        )


def test_closed_only_succeeds_exactly_at_confirm_without_changing_ohlc():
    rows = _bullish_3h1_rows()
    now_before = START + timedelta(hours=2, minutes=59)
    now_at_confirm = START + timedelta(hours=3)

    with pytest.raises(IncompleteWindowError):
        aggregate_window(
            base_rows=rows, base_timeframe="1h", count=3, target_timeframe="3h",
            window_start=START, closed_only=True, include_provisional=False, now=now_before,
        )
    result = aggregate_window(
        base_rows=rows, base_timeframe="1h", count=3, target_timeframe="3h",
        window_start=START, closed_only=True, include_provisional=False, now=now_at_confirm,
    )
    assert result["is_closed"] is True
    # Same underlying rows -> identical OHLCV as the plain unit test above:
    # confirm alone (no data change) must not alter the aggregate.
    assert result["open"] == 100.0
    assert result["high"] == 112.0
    assert result["low"] == 97.0
    assert result["close"] == 110.0


# ---------------------------------------------------------------------------
# 4. Provisional: is_closed=false, correct close_time, C/H/L update without
#    repainting closed records
# ---------------------------------------------------------------------------


def test_provisional_last_bar_marks_window_unclosed_with_correct_close_time():
    closed_rows = [
        _h1_row(START, 100.0, 105.0, 97.0, 103.0),
        _h1_row(START + timedelta(hours=1), 103.0, 108.0, 101.0, 106.0),
    ]
    still_forming_open = START + timedelta(hours=2)
    provisional_row = _h1_row(still_forming_open, 106.0, 109.0, 105.0, 107.5)
    now = still_forming_open + timedelta(minutes=30)  # mid-formation, not yet closed

    result = aggregate_window(
        base_rows=closed_rows, base_timeframe="1h", count=3, target_timeframe="3h",
        window_start=START, closed_only=False, include_provisional=True, now=now,
        provisional_row=provisional_row,
    )
    assert result["is_closed"] is False
    assert result["close_time"] == _iso(START + timedelta(hours=3))
    assert result["constituents"][-1]["is_closed"] is False
    assert result["constituents"][-1]["close_time"] == _iso(START + timedelta(hours=3))
    # High/low/close reflect the still-forming candle's current values.
    assert result["high"] == 109.0
    assert result["low"] == 97.0
    assert result["close"] == 107.5


def test_provisional_update_does_not_repaint_earlier_closed_constituents():
    closed_rows = [
        _h1_row(START, 100.0, 105.0, 97.0, 103.0),
        _h1_row(START + timedelta(hours=1), 103.0, 108.0, 101.0, 106.0),
    ]
    still_forming_open = START + timedelta(hours=2)
    now = still_forming_open + timedelta(minutes=10)

    tick_1 = _h1_row(still_forming_open, 106.0, 107.0, 105.5, 106.5)
    tick_2 = _h1_row(still_forming_open, 106.0, 111.0, 104.0, 109.0)  # later tick: wider range

    r1 = aggregate_window(
        base_rows=closed_rows, base_timeframe="1h", count=3, target_timeframe="3h",
        window_start=START, closed_only=False, include_provisional=True, now=now,
        provisional_row=tick_1,
    )
    r2 = aggregate_window(
        base_rows=closed_rows, base_timeframe="1h", count=3, target_timeframe="3h",
        window_start=START, closed_only=False, include_provisional=True, now=now,
        provisional_row=tick_2,
    )
    # The two CLOSED constituents' own metadata is identical across ticks —
    # only the provisional (last) constituent and the aggregate H/L/C move.
    assert r1["constituents"][:2] == r2["constituents"][:2]
    assert r1["open"] == r2["open"] == 100.0
    assert r1["high"] != r2["high"]
    assert r2["high"] == 111.0
    assert r2["low"] == 97.0  # still governed by the first closed bar's low
    assert r2["close"] == 109.0


def test_provisional_cannot_replace_an_earlier_unclosed_constituent():
    # provisional_row only ever fills the LAST slot — a gap in an EARLIER
    # slot must still raise, even with include_provisional=True.
    rows_missing_middle = [
        _h1_row(START, 100.0, 105.0, 97.0, 103.0),
        # hour 1 (START+1h) missing entirely
        _h1_row(START + timedelta(hours=2), 106.0, 110.0, 104.0, 108.0),
    ]
    provisional_row = _h1_row(START + timedelta(hours=2), 106.0, 110.0, 104.0, 108.0)
    now = START + timedelta(hours=2, minutes=30)
    with pytest.raises(IncompleteWindowError):
        aggregate_window(
            base_rows=rows_missing_middle, base_timeframe="1h", count=3, target_timeframe="3h",
            window_start=START, closed_only=False, include_provisional=True, now=now,
            provisional_row=provisional_row,
        )


def test_include_provisional_false_still_rejects_unclosed_final_bar():
    rows = [
        _h1_row(START, 100.0, 105.0, 97.0, 103.0),
        _h1_row(START + timedelta(hours=1), 103.0, 108.0, 101.0, 106.0),
        _h1_row(START + timedelta(hours=2), 106.0, 109.0, 104.0, 107.0),
    ]
    # Clock says the last bar hasn't closed yet, but caller passed neither
    # closed_only nor include_provisional relaxation explicitly.
    now = START + timedelta(hours=2, minutes=30)
    with pytest.raises(IncompleteWindowError):
        aggregate_window(
            base_rows=rows, base_timeframe="1h", count=3, target_timeframe="3h",
            window_start=START, closed_only=False, include_provisional=False, now=now,
        )


# ---------------------------------------------------------------------------
# 5. Gaps/errors: missing one constituent -> IncompleteWindowError, never a
#    synthetic candle from partial data (2/3 or 3/4)
# ---------------------------------------------------------------------------


def test_missing_middle_constituent_raises_not_partial_aggregate():
    rows = [
        _h1_row(START, 100.0, 105.0, 97.0, 103.0),
        # hour 1 missing
        _h1_row(START + timedelta(hours=2), 106.0, 112.0, 104.0, 110.0),
    ]
    now = START + timedelta(hours=3)
    with pytest.raises(IncompleteWindowError):
        aggregate_window(
            base_rows=rows, base_timeframe="1h", count=3, target_timeframe="3h",
            window_start=START, closed_only=True, include_provisional=False, now=now,
        )


def test_missing_last_of_four_constituents_raises_not_3_of_4_aggregate():
    rows = [
        _h1_row(START, 100.0, 101.0, 90.0, 95.0),
        _h1_row(START + timedelta(hours=1), 95.0, 96.0, 88.0, 90.0),
        _h1_row(START + timedelta(hours=2), 90.0, 91.0, 80.0, 85.0),
        # hour 3 missing entirely
    ]
    now = START + timedelta(hours=4)
    with pytest.raises((IncompleteWindowError, WindowNotFoundError)):
        aggregate_window(
            base_rows=rows, base_timeframe="1h", count=4, target_timeframe="4h",
            window_start=START, closed_only=True, include_provisional=False, now=now,
        )


def test_completely_empty_window_raises_window_not_found():
    with pytest.raises(WindowNotFoundError):
        aggregate_window(
            base_rows=[], base_timeframe="1h", count=3, target_timeframe="3h",
            window_start=START, closed_only=True, include_provisional=False,
            now=START + timedelta(hours=3),
        )


def test_gap_never_silently_assembles_partial_window():
    # Regression against the specific failure mode #261 forbids: a window
    # with N-1 of N constituents must NEVER produce a result dict at all.
    rows = [
        _h1_row(START, 100.0, 105.0, 97.0, 103.0),
        _h1_row(START + timedelta(hours=1), 103.0, 108.0, 101.0, 106.0),
        # hour 2 missing (would be 2/3)
    ]
    now = START + timedelta(hours=3)
    try:
        aggregate_window(
            base_rows=rows, base_timeframe="1h", count=3, target_timeframe="3h",
            window_start=START, closed_only=True, include_provisional=False, now=now,
        )
        assert False, "expected IncompleteWindowError/WindowNotFoundError, got a result instead"
    except (IncompleteWindowError, WindowNotFoundError):
        pass


def test_unclosed_bar_in_the_middle_of_window_raises():
    # An "unclosed" bar can only ever legitimately be the LAST constituent.
    # Simulate a data anomaly where an earlier bar's clock-implied close is
    # in the future relative to `now` (shouldn't happen in practice, but the
    # function must still refuse rather than silently accept it).
    rows = [
        _h1_row(START, 100.0, 105.0, 97.0, 103.0),
        _h1_row(START + timedelta(hours=1), 103.0, 108.0, 101.0, 106.0),
        _h1_row(START + timedelta(hours=2), 106.0, 112.0, 104.0, 110.0),
    ]
    now = START + timedelta(hours=1, minutes=30)  # only bar 0 is closed by clock
    with pytest.raises(IncompleteWindowError):
        aggregate_window(
            base_rows=rows, base_timeframe="1h", count=3, target_timeframe="3h",
            window_start=START, closed_only=True, include_provisional=False, now=now,
        )


# ---------------------------------------------------------------------------
# 8. Regression: standard and non-standard offsets without look-ahead
# ---------------------------------------------------------------------------


def test_no_look_ahead_aggregate_window_ignores_rows_after_now_naturally():
    # aggregate_window trusts `now` for is_closed computation; rows dated
    # after the window (not part of expected_opens) are irrelevant, and a
    # constituent whose clock-close is after `now` is correctly refused
    # under closed_only regardless of whether the row itself is present.
    rows = [
        _h1_row(START, 100.0, 105.0, 97.0, 103.0),
        _h1_row(START + timedelta(hours=1), 103.0, 108.0, 101.0, 106.0),
        _h1_row(START + timedelta(hours=2), 106.0, 999.0, 104.0, 998.0),  # data exists...
    ]
    now = START + timedelta(hours=2, minutes=59)  # ...but clock says not closed yet
    with pytest.raises(IncompleteWindowError):
        aggregate_window(
            base_rows=rows, base_timeframe="1h", count=3, target_timeframe="3h",
            window_start=START, closed_only=True, include_provisional=False, now=now,
        )


def test_standard_and_nonstandard_offsets_produce_disjoint_windows():
    # offset_minutes=0 (standard, epoch-anchored) vs offset_minutes=60
    # (non-standard, 1h-shifted) 3h grids must never land on the same
    # window_start.
    #
    # Both windows are resolved via offset_minutes (not anchor_time): an
    # explicit anchor defines one caller-selected window, while offsets define
    # recurring shifted grids. This test exercises the latter contract.
    #
    # offset_minutes resolves against wall-clock `now()` (module docstring:
    # "applied against the most recent complete window boundary") — by
    # design, since main.py's caller never threads a `now` through for this
    # path (it's the "give me the current window" HTTP use case). That means
    # the two grids' window_start values are each independently floor(now)
    # on their own shifted 3h grid, and their `shifted - base` relationship
    # can be +1h, -2h, etc. depending on where `now()` currently falls in the
    # cycle — asserting a fixed `timedelta(hours=1)` here is flaky by
    # construction (this exact assertion failed against real now()). The
    # actual AC/invariant is disjointness (a 1h-shifted grid must never
    # collide with the unshifted grid's boundary), which holds unconditionally
    # since 3600 % (3*3600) != 0.
    base = resolve_window_start(
        base_timeframe="1h", target_seconds=3 * 3600,
        anchor_time=None, offset_minutes=0,
    )
    shifted = resolve_window_start(
        base_timeframe="1h", target_seconds=3 * 3600,
        anchor_time=None, offset_minutes=60,
    )
    assert base != shifted
    assert (shifted - base) % timedelta(hours=3) == timedelta(hours=1)


def test_regression_h3_and_h4_offsets_all_resolve_to_base_grid_multiples():
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    for target_seconds, offsets in ((3 * 3600, (0, 60, 120)), (4 * 3600, (0, 60, 120, 180))):
        for offset_minutes in offsets:
            dt = resolve_window_start(
                base_timeframe="1h", target_seconds=target_seconds,
                anchor_time=None, offset_minutes=offset_minutes,
            )
            elapsed = (dt - epoch).total_seconds()
            assert elapsed % 3600 == 0, f"offset={offset_minutes} not on 1h base grid"
            # dt must not be in the future relative to itself trivially, and
            # must reproduce the exact offset shift from a target-boundary.
            assert (elapsed - offset_minutes * 60) % target_seconds == 0


# ---------------------------------------------------------------------------
# #264 analytical verification: named WLD XPERP fixture + 100+ windows for
# every supported H3/H4 offset. References below are calculated independently
# from the aggregate function (plain OHLCV rules and literal expected ratios).
# ---------------------------------------------------------------------------


WLD_XPERP = "WLD-USD_UM_XPERP-310613"
WLD_FIXTURE_START = datetime(2026, 8, 10, 8, 0, 0, tzinfo=timezone.utc)
WLD_H1_FIXTURE = [
    _h1_row(WLD_FIXTURE_START, 0.3250, 0.3270, 0.3240, 0.3260, 1_250_000.0),
    _h1_row(WLD_FIXTURE_START + timedelta(hours=1), 0.3260, 0.3290, 0.3250, 0.3280, 1_500_000.0),
    _h1_row(WLD_FIXTURE_START + timedelta(hours=2), 0.3280, 0.3300, 0.3265, 0.3270, 1_100_000.0),
    _h1_row(WLD_FIXTURE_START + timedelta(hours=3), 0.3270, 0.3280, 0.3230, 0.3240, 1_800_000.0),
]


@pytest.mark.parametrize(
    ("count", "target", "expected"),
    [
        (3, "3h", {
            "open": 0.3250, "high": 0.3300, "low": 0.3240, "close": 0.3270,
            "volume": 3_850_000.0, "range": 0.0060, "body": 0.0020,
            "upper_wick": 0.0030, "lower_wick": 0.0010,
            "body_pct_range": 1 / 3, "upper_wick_pct_range": 1 / 2,
            "lower_wick_pct_range": 1 / 6, "upper_wick_to_body": 1.5,
            "lower_wick_to_body": 0.5, "close_location_pct": 0.5,
        }),
        (4, "4h", {
            "open": 0.3250, "high": 0.3300, "low": 0.3230, "close": 0.3240,
            "volume": 5_650_000.0, "range": 0.0070, "body": 0.0010,
            "upper_wick": 0.0050, "lower_wick": 0.0010,
            "body_pct_range": 1 / 7, "upper_wick_pct_range": 5 / 7,
            "lower_wick_pct_range": 1 / 7, "upper_wick_to_body": 5.0,
            "lower_wick_to_body": 1.0, "close_location_pct": 1 / 7,
        }),
    ],
)
def test_wld_xperp_manual_fixture_h3_h4(count, target, expected):
    result = aggregate_window(
        base_rows=WLD_H1_FIXTURE, base_timeframe="1h", count=count,
        target_timeframe=target, window_start=WLD_FIXTURE_START,
        closed_only=True, include_provisional=False,
        now=WLD_FIXTURE_START + timedelta(hours=4),
    )
    assert WLD_XPERP.endswith("XPERP-310613")
    for field in ("open", "high", "low", "close"):
        assert result[field] == expected[field]  # zero ticks difference
    assert result["volume"] == expected["volume"]
    for field in (
        "range", "body", "upper_wick", "lower_wick", "body_pct_range",
        "upper_wick_pct_range", "lower_wick_pct_range", "upper_wick_to_body",
        "lower_wick_to_body", "close_location_pct",
    ):
        assert abs(result[field] - expected[field]) <= 1e-12


def test_backtest_120_windows_all_supported_offsets_no_divergence_or_lookahead():
    epoch = datetime(2026, 1, 1, tzinfo=timezone.utc)
    windows_checked = 0
    for count, offsets in ((3, (0, 60, 120)), (4, (0, 60, 120, 180))):
        for offset_minutes in offsets:
            grid_start = epoch + timedelta(minutes=offset_minutes)
            for window_index in range(120):
                start = grid_start + timedelta(hours=count * window_index)
                rows = []
                for i in range(count):
                    base = 0.20 + window_index * 0.0001 + offset_minutes * 0.000001 + i * 0.0002
                    rows.append(_h1_row(
                        start + timedelta(hours=i), base, base + 0.0007,
                        base - 0.0004, base + (0.0003 if i % 2 == 0 else -0.0001),
                        1000.0 + window_index + i,
                    ))
                reference = {
                    "open": rows[0]["open"], "high": max(r["high"] for r in rows),
                    "low": min(r["low"] for r in rows), "close": rows[-1]["close"],
                    "volume": sum(r["volume"] for r in rows),
                }
                # A future poison candle proves the calculation cannot look
                # outside this exact constituent window.
                poison = _h1_row(start + timedelta(hours=count), 9.0, 10.0, 8.0, 9.5, 9e9)
                result = aggregate_window(
                    base_rows=rows + [poison], base_timeframe="1h", count=count,
                    target_timeframe=f"{count}h", window_start=start,
                    closed_only=True, include_provisional=False,
                    now=start + timedelta(hours=count),
                )
                assert {key: result[key] for key in reference} == reference
                windows_checked += 1
    assert windows_checked == 840  # 120 windows * (3 H3 offsets + 4 H4 offsets)
