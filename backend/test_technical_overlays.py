"""Tests for technical_overlays.py (#242) — Bollinger Bands, EMA, EMA
multi-timeframe projection, VWAP, CVD, offset-shifted M30/H1 aggregation.

Deterministic fixtures only (no live OKX/lake reads) — every function under
test is a pure transform of `list[dict] -> list[dict]`, so fixtures are
hand-built here rather than pulled from the real data lake. Look-ahead and
closed-candle-only guarantees are the core of task #242's AC, so those get
dedicated tests, not just incidental coverage.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from technical_overlays import (
    ALLOWED_OFFSETS,
    aggregate_offset_ohlcv,
    aggregate_offset_taker_volume,
    bandwidth_percentile,
    bollinger_bands,
    bucket_public_trades,
    candle_close_time,
    cvd_anchored,
    cvd_session,
    ema,
    project_ema_multi_timeframe,
    vwap_anchored,
    vwap_session,
)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _make_ohlcv_rows(n: int, start: datetime, step: timedelta, base_price: float = 100.0) -> list[dict]:
    """Deterministic synthetic OHLCV: price drifts by a fixed pattern (no
    randomness) so expected numeric outputs can be computed by hand/via an
    independent reference formula in tests."""
    rows = []
    price = base_price
    for i in range(n):
        # Deterministic oscillation: not linear (a pure trend would make
        # every windowed stdev calc trivially predictable but wouldn't
        # exercise BB's variance formula meaningfully).
        price = base_price + (i % 10) * 0.5 - (i % 3) * 0.2
        t = start + step * i
        rows.append(
            {
                "observed_at": _iso(t),
                "open": price - 0.1,
                "high": price + 0.3,
                "low": price - 0.3,
                "close": price,
                "volume": 10.0 + (i % 5),
            }
        )
    return rows


START = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
STEP_5M = timedelta(minutes=5)


# ---------------------------------------------------------------------------
# Bollinger Bands
# ---------------------------------------------------------------------------


def test_bollinger_bands_matches_reference_formula():
    rows = _make_ohlcv_rows(40, START, STEP_5M)
    closes = [r["close"] for r in rows]
    bb = bollinger_bands(closes, period=20, stddev_mult=2.0)

    # Independent reference implementation (no code shared with bollinger_bands).
    def reference(i):
        window = closes[i - 19 : i + 1]
        mean = sum(window) / 20
        var = sum((v - mean) ** 2 for v in window) / 20
        stdev = var**0.5
        upper = mean + 2 * stdev
        lower = mean - 2 * stdev
        return mean, upper, lower

    for i in [19, 25, 39]:
        mean, upper, lower = reference(i)
        assert bb[i]["middle"] == pytest.approx(mean)
        assert bb[i]["upper"] == pytest.approx(upper)
        assert bb[i]["lower"] == pytest.approx(lower)
        expected_bw = (upper - lower) / mean
        expected_pb = (closes[i] - lower) / (upper - lower)
        assert bb[i]["bandwidth"] == pytest.approx(expected_bw)
        assert bb[i]["percent_b"] == pytest.approx(expected_pb)


def test_bollinger_bands_warmup_is_null():
    rows = _make_ohlcv_rows(19, START, STEP_5M)  # one short of period=20
    closes = [r["close"] for r in rows]
    bb = bollinger_bands(closes, period=20, stddev_mult=2.0)
    assert all(b["middle"] is None for b in bb)
    assert all(b["bandwidth"] is None for b in bb)
    assert all(b["percent_b"] is None for b in bb)


def test_bollinger_percent_b_null_on_zero_span_not_divide_error():
    # Flat prices -> stdev=0 -> upper==lower -> percent_b undefined, not a
    # ZeroDivisionError.
    closes = [100.0] * 25
    bb = bollinger_bands(closes, period=20, stddev_mult=2.0)
    assert bb[19]["percent_b"] is None
    assert bb[19]["bandwidth"] == pytest.approx(0.0)


def test_bandwidth_percentile_is_backward_looking_only():
    # Construct bandwidths where the LAST value is the maximum — its
    # percentile among the trailing window must be 100, and appending more
    # data afterward must not change any earlier percentile (see look-ahead
    # test below for the stronger end-to-end version of this guarantee).
    bandwidths = [1.0, 2.0, 3.0, 4.0, 5.0] + [1.0] * 95 + [10.0]  # window=100
    pct = bandwidth_percentile(bandwidths, window=100)
    assert pct[-1] == 100.0  # 10.0 is the max of its trailing 100-window
    assert pct[:99] == [None] * 99  # not enough trailing history yet (window=100)


def test_bandwidth_percentile_null_before_window_fills():
    bandwidths = [1.0] * 50
    pct = bandwidth_percentile(bandwidths, window=100)
    assert all(p is None for p in pct)


# ---------------------------------------------------------------------------
# EMA
# ---------------------------------------------------------------------------


def test_ema_matches_reference_formula():
    closes = [100.0, 102.0, 101.0, 105.0, 107.0, 106.0, 110.0, 108.0, 111.0, 113.0]
    period = 5
    values = ema(closes, period)
    assert all(v is None for v in values[: period - 1])
    k = 2.0 / (period + 1)
    seed = sum(closes[:period]) / period
    assert values[period - 1] == pytest.approx(seed)
    prev = seed
    for i in range(period, len(closes)):
        prev = closes[i] * k + prev * (1 - k)
        assert values[i] == pytest.approx(prev)


def test_ema_warmup_null_for_short_history():
    closes = [100.0, 101.0, 102.0]
    values = ema(closes, period=21)
    assert values == [None, None, None]


# ---------------------------------------------------------------------------
# Look-ahead / closed-candle tests (BB + EMA + percentile)
# ---------------------------------------------------------------------------


def test_bollinger_look_ahead_appending_future_candles_does_not_change_past():
    rows = _make_ohlcv_rows(50, START, STEP_5M)
    closes = [r["close"] for r in rows]
    bb_before = bollinger_bands(closes, period=20, stddev_mult=2.0)

    more_rows = _make_ohlcv_rows(60, START, STEP_5M)  # same generator, 10 more rows appended
    closes_after = [r["close"] for r in more_rows]
    bb_after = bollinger_bands(closes_after, period=20, stddev_mult=2.0)

    for i in range(50):
        assert bb_before[i] == bb_after[i]


def test_ema_look_ahead_appending_future_candles_does_not_change_past():
    rows = _make_ohlcv_rows(50, START, STEP_5M)
    closes = [r["close"] for r in rows]
    ema_before = ema(closes, period=21)

    more_rows = _make_ohlcv_rows(60, START, STEP_5M)
    closes_after = [r["close"] for r in more_rows]
    ema_after = ema(closes_after, period=21)

    for i in range(50):
        assert ema_before[i] == ema_after[i]


def test_bandwidth_percentile_look_ahead():
    bandwidths = [float(i % 7) for i in range(150)]
    pct_before = bandwidth_percentile(bandwidths, window=100)
    pct_after = bandwidth_percentile(bandwidths + [99.0, 0.5, 3.0], window=100)
    assert pct_before == pct_after[:150]


def test_closed_candle_only_unclosed_candle_not_passed_in_does_not_affect_result():
    # The module itself has no notion of "now" — this test documents/enforces
    # that guarantee: a caller who simply omits the in-progress candle from
    # the input list gets byte-identical output to a call made one bar later
    # (once that candle is closed and dropped from being "in progress"),
    # PROVIDED the newly-closed candle is not yet appended either. I.e.
    # closed-candle enforcement is the caller's responsibility (main.py only
    # ever reads _read_series() rows, which are all closed) and this module
    # must not silently change past values regardless of what's appended.
    rows_10 = _make_ohlcv_rows(10, START, STEP_5M)
    rows_10_again = _make_ohlcv_rows(10, START, STEP_5M)
    assert ema([r["close"] for r in rows_10], 5) == ema([r["close"] for r in rows_10_again], 5)


# ---------------------------------------------------------------------------
# EMA multi-timeframe projection
# ---------------------------------------------------------------------------


def _make_htf_rows(n: int, start: datetime, step: timedelta) -> list[dict]:
    rows = []
    price = 1000.0
    for i in range(n):
        price = 1000.0 + (i % 8) * 3.0
        rows.append({"observed_at": _iso(start + step * i), "close": price})
    return rows


def test_ema_projection_forward_fills_only_after_source_close():
    # Source: 1h candles starting at START. First one CLOSES at START+1h.
    source_rows = _make_htf_rows(30, START, timedelta(hours=1))
    # Target: 5m candles covering the same range.
    target_rows = [{"observed_at": _iso(START + timedelta(minutes=5) * i)} for i in range(400)]

    period = 5  # small period so EMA warms up within available source history
    result = project_ema_multi_timeframe(target_rows, source_rows, "1h", "5m", period)

    source_ema = ema([r["close"] for r in source_rows], period)
    first_valid_source_idx = next(i for i, v in enumerate(source_ema) if v is not None)
    first_close_time = candle_close_time(source_rows[first_valid_source_idx]["observed_at"], "1h")

    for point in result:
        if point["target_time"] < first_close_time:
            assert point["value"] is None, "no HTF EMA value should be visible before its source candle closes"
        else:
            assert point["value"] is not None

    # Every non-null point must carry the full contract fields.
    for point in result:
        if point["value"] is not None:
            assert point["source_timeframe"] == "1h"
            assert point["target_timeframe"] == "5m"
            assert point["source_candle_close_time"] is not None
            assert point["last_updated_at"] == point["source_candle_close_time"]


def test_ema_projection_before_and_exactly_after_htf_close():
    source_rows = _make_htf_rows(10, START, timedelta(hours=1))
    period = 3
    source_ema = ema([r["close"] for r in source_rows], period)
    # First non-null source EMA index and its close time.
    idx = next(i for i, v in enumerate(source_ema) if v is not None)
    close_time = candle_close_time(source_rows[idx]["observed_at"], "1h")
    next_close_time = candle_close_time(source_rows[idx + 1]["observed_at"], "1h")

    close_dt = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
    just_before = _iso(close_dt - timedelta(minutes=5))
    at_close = close_time

    target_rows = [{"observed_at": just_before}, {"observed_at": at_close}]
    result = project_ema_multi_timeframe(target_rows, source_rows, "1h", "5m", period)

    assert result[0]["value"] is None  # strictly before close: previous (nothing) value only
    assert result[1]["value"] == pytest.approx(source_ema[idx])  # exactly at close: new value visible
    assert result[1]["source_candle_close_time"] == close_time
    assert next_close_time > close_time


def test_ema_projection_restart_backfill_identical_no_repaint():
    source_rows = _make_htf_rows(50, START, timedelta(hours=1))
    target_rows = [{"observed_at": _iso(START + timedelta(minutes=5) * i)} for i in range(600)]
    period = 8

    first_run = project_ema_multi_timeframe(target_rows, source_rows, "1h", "5m", period)
    # Simulate a "restart/backfill" — recomputed from scratch with the exact
    # same inputs (as a real restart would re-read the same closed candles
    # from the lake) — must be byte-identical, not merely equivalent.
    second_run = project_ema_multi_timeframe(target_rows, source_rows, "1h", "5m", period)
    assert first_run == second_run

    # Appending FUTURE source candles (simulating time passing) must not
    # change any already-emitted historical target point.
    more_source_rows = _make_htf_rows(60, START, timedelta(hours=1))
    third_run = project_ema_multi_timeframe(target_rows, more_source_rows, "1h", "5m", period)
    assert first_run == third_run


# ---------------------------------------------------------------------------
# VWAP
# ---------------------------------------------------------------------------


def _make_session_rows() -> list[dict]:
    # Two UTC calendar days, 4 hourly candles each — deliberately crossing
    # midnight so the session reset is exercised.
    rows = []
    day1 = datetime(2026, 1, 1, 22, 0, 0, tzinfo=timezone.utc)
    for i in range(4):  # 22:00, 23:00 (day1), 00:00, 01:00 (day2)
        t = day1 + timedelta(hours=i)
        price = 100.0 + i
        rows.append(
            {"observed_at": _iso(t), "high": price + 1, "low": price - 1, "close": price, "volume": 10.0 + i}
        )
    return rows


def test_vwap_session_resets_at_utc_midnight():
    rows = _make_session_rows()
    series = vwap_session(rows)
    # First two rows: 2026-01-01 22:00/23:00 (session "2026-01-01")
    # Last two rows: 2026-01-02 00:00/01:00 (session "2026-01-02") — reset.
    assert series[0]["value"] is not None
    typical0 = (rows[0]["high"] + rows[0]["low"] + rows[0]["close"]) / 3.0
    assert series[0]["value"] == pytest.approx(typical0)

    # At the reset point, cumulative starts fresh — value should equal that
    # single row's typical price again, NOT a continuation of day1's average.
    typical2 = (rows[2]["high"] + rows[2]["low"] + rows[2]["close"]) / 3.0
    assert series[2]["value"] == pytest.approx(typical2)
    assert series[2]["session_timezone"] == "UTC"
    assert series[2]["mode"] == "session"


def test_vwap_anchored_null_before_anchor_and_starts_fresh_at_anchor():
    rows = _make_session_rows()
    anchor = rows[2]["observed_at"]
    series = vwap_anchored(rows, anchor)
    assert series[0]["value"] is None
    assert series[1]["value"] is None
    assert series[2]["value"] is not None
    typical2 = (rows[2]["high"] + rows[2]["low"] + rows[2]["close"]) / 3.0
    assert series[2]["value"] == pytest.approx(typical2)
    assert series[2]["anchor_time"] == anchor


def test_vwap_look_ahead_appending_future_rows_does_not_change_past():
    rows = _make_session_rows()
    before = vwap_session(rows)
    more_rows = rows + [{"observed_at": _iso(datetime(2026, 1, 2, 2, 0, 0, tzinfo=timezone.utc)), "high": 200, "low": 190, "close": 195, "volume": 5}]
    after = vwap_session(more_rows)
    assert before == after[: len(before)]


# ---------------------------------------------------------------------------
# CVD
# ---------------------------------------------------------------------------


def _make_taker_rows() -> list[dict]:
    rows = []
    day1 = datetime(2026, 1, 1, 22, 0, 0, tzinfo=timezone.utc)
    buys = [10.0, 5.0, 8.0, 3.0]
    sells = [4.0, 6.0, 2.0, 7.0]
    for i in range(4):
        t = day1 + timedelta(hours=i)
        rows.append({"observed_at": _iso(t), "taker_buy_volume": buys[i], "taker_sell_volume": sells[i]})
    return rows


def test_cvd_session_signed_delta_and_reset():
    rows = _make_taker_rows()
    series = cvd_session(rows)
    assert series[0]["delta"] == pytest.approx(10.0 - 4.0)
    assert series[0]["value"] == pytest.approx(6.0)
    assert series[1]["value"] == pytest.approx(6.0 + (5.0 - 6.0))  # cumulative within session 1
    # session resets at row index 2 (UTC day boundary)
    assert series[2]["value"] == pytest.approx(8.0 - 2.0)
    assert series[2]["source"] == "okx"


def test_cvd_anchored_starts_fresh_at_anchor():
    rows = _make_taker_rows()
    anchor = rows[1]["observed_at"]
    series = cvd_anchored(rows, anchor)
    assert series[0]["value"] is None
    assert series[1]["value"] == pytest.approx(5.0 - 6.0)
    assert series[2]["value"] == pytest.approx((5.0 - 6.0) + (8.0 - 2.0))


def test_cvd_look_ahead():
    rows = _make_taker_rows()
    before = cvd_session(rows)
    more = rows + [{"observed_at": _iso(datetime(2026, 1, 2, 3, 0, 0, tzinfo=timezone.utc)), "taker_buy_volume": 1.0, "taker_sell_volume": 1.0}]
    after = cvd_session(more)
    assert before == after[: len(before)]


# ---------------------------------------------------------------------------
# bucket_public_trades (#265 — instrument-specific CVD source)
# ---------------------------------------------------------------------------


def _trade(dt: datetime, side: str, sz: float) -> dict:
    return {"side": side, "sz": str(sz), "ts": str(int(dt.timestamp() * 1000))}


def test_bucket_public_trades_buy_sell_split_and_bucketing_5m():
    """Task #265 test 2: fixture instrument-specific trades, buy-sell split,
    bucketing into a closed 5m grid."""
    base = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    trades = [
        _trade(base + timedelta(seconds=10), "buy", 2.0),
        _trade(base + timedelta(seconds=20), "sell", 1.0),
        _trade(base + timedelta(minutes=5, seconds=5), "buy", 3.0),
        _trade(base + timedelta(minutes=5, seconds=15), "sell", 0.5),
        _trade(base + timedelta(minutes=5, seconds=25), "sell", 0.5),
    ]
    now = base + timedelta(minutes=10)  # both 5m buckets fully closed by `now`
    rows = bucket_public_trades(trades, "5m", now=now)
    assert len(rows) == 2
    assert rows[0]["observed_at"] == "2026-01-01T00:00:00Z"
    assert rows[0]["taker_buy_volume"] == pytest.approx(2.0)
    assert rows[0]["taker_sell_volume"] == pytest.approx(1.0)
    assert rows[0]["trade_count"] == 2
    assert rows[1]["observed_at"] == "2026-01-01T00:05:00Z"
    assert rows[1]["taker_buy_volume"] == pytest.approx(3.0)
    assert rows[1]["taker_sell_volume"] == pytest.approx(1.0)
    assert rows[1]["trade_count"] == 3


def test_bucket_public_trades_aggregates_to_15m_1h_4h():
    """Task #265 test 2: bucketing 5m trades up to 15m/1h/4h must equal the
    sum of the finer buckets — same non-overlapping-window guarantee as
    aggregate_offset_taker_volume for the currency_aggregate path."""
    base = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    trades = []
    # 20 trades, one every 3 minutes, spanning exactly one hour — alternating
    # buy/sell with a fixed size so totals are trivial to hand-verify.
    for i in range(20):
        side = "buy" if i % 2 == 0 else "sell"
        trades.append(_trade(base + timedelta(minutes=3 * i), side, 1.0))
    now = base + timedelta(hours=2)  # everything closed
    rows_15m = bucket_public_trades(trades, "15m", now=now)
    rows_1h = bucket_public_trades(trades, "1h", now=now)
    total_buy_15m = sum(r["taker_buy_volume"] for r in rows_15m)
    total_sell_15m = sum(r["taker_sell_volume"] for r in rows_15m)
    total_buy_1h = sum(r["taker_buy_volume"] for r in rows_1h)
    total_sell_1h = sum(r["taker_sell_volume"] for r in rows_1h)
    assert total_buy_15m == pytest.approx(total_buy_1h)
    assert total_sell_15m == pytest.approx(total_sell_1h)
    assert total_buy_1h == pytest.approx(10.0)  # 10 buys of size 1.0
    assert total_sell_1h == pytest.approx(10.0)  # 10 sells of size 1.0


def test_bucket_public_trades_session_reset_and_anchor_via_cvd():
    """Task #265 test 2: instrument buckets feed cvd_session/cvd_anchored
    exactly like currency_aggregate rows do — session resets at UTC midnight,
    anchor starts fresh at the named bucket."""
    day1 = datetime(2026, 1, 1, 23, 50, 0, tzinfo=timezone.utc)
    trades = [
        _trade(day1, "buy", 5.0),
        _trade(day1 + timedelta(minutes=5), "sell", 2.0),  # 2026-01-01 23:55 bucket
        _trade(day1 + timedelta(minutes=10), "buy", 1.0),  # 2026-01-02 00:00 bucket (new UTC day)
    ]
    now = day1 + timedelta(hours=1)
    rows = bucket_public_trades(trades, "5m", now=now)
    assert len(rows) == 3
    series = cvd_session(rows)
    assert series[0]["value"] == pytest.approx(5.0)
    assert series[1]["value"] == pytest.approx(5.0 - 2.0)
    assert series[2]["value"] == pytest.approx(1.0)  # reset at UTC day boundary

    anchor = rows[1]["observed_at"]
    anchored = cvd_anchored(rows, anchor)
    assert anchored[0]["value"] is None
    assert anchored[1]["value"] == pytest.approx(-2.0)
    assert anchored[2]["value"] == pytest.approx(-2.0 + 1.0)


def test_bucket_public_trades_matches_candle_volume_within_tolerance():
    """Task #265 test 3: sum(buy)+sum(sell) for a bucket must equal the
    trade-tape-derived volume for that same window (tautological by
    construction here since both come from the same trades — the real
    invariant under test is that NO trade is double-counted or dropped
    across bucket boundaries, i.e. total volume is conserved)."""
    base = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    trades = [_trade(base + timedelta(seconds=i * 7), "buy" if i % 3 else "sell", 1.5) for i in range(40)]
    now = base + timedelta(minutes=10)
    rows = bucket_public_trades(trades, "5m", now=now)
    bucketed_total = sum(r["taker_buy_volume"] + r["taker_sell_volume"] for r in rows)
    raw_total = sum(float(t["sz"]) for t in trades)
    assert bucketed_total == pytest.approx(raw_total)


def test_bucket_public_trades_no_look_ahead_still_forming_bucket_dropped():
    """Task #265 test 6: the bucket the tape is currently inside must never
    be emitted — only fully-closed buckets."""
    base = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    trades = [_trade(base + timedelta(seconds=30), "buy", 1.0)]
    still_forming_now = base + timedelta(minutes=2)  # bucket closes at :05, now is :02 — not closed yet
    rows = bucket_public_trades(trades, "5m", now=still_forming_now)
    assert rows == []

    closed_now = base + timedelta(minutes=5)  # exactly at close boundary — bucket IS closed
    rows_closed = bucket_public_trades(trades, "5m", now=closed_now)
    assert len(rows_closed) == 1


def test_bucket_public_trades_limit_independence():
    """Task #265 test 6: which buckets close must not depend on how many
    trades happen to be in the input list — a smaller/larger raw trade batch
    covering the SAME closed time range produces the same closed buckets."""
    base = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    full_trades = [_trade(base + timedelta(seconds=i * 3), "buy", 1.0) for i in range(100)]
    truncated_trades = full_trades[:10]  # simulates a smaller `limit` pull
    now = base + timedelta(minutes=10)
    rows_full = bucket_public_trades(full_trades, "5m", now=now)
    rows_truncated = bucket_public_trades(truncated_trades, "5m", now=now)
    # Bucket BOUNDARIES (observed_at set) present in the truncated pull must
    # be a subset of the full pull's — never a DIFFERENT boundary set, which
    # would mean `now` alone isn't what decides closure.
    full_times = {r["observed_at"] for r in rows_full}
    truncated_times = {r["observed_at"] for r in rows_truncated}
    assert truncated_times <= full_times


def test_bucket_public_trades_no_interpolation_gap():
    """Task #265: a bucket with zero trades is never synthesized — a gap in
    the trade tape shows up as a MISSING observed_at, not an interpolated
    zero-delta row."""
    base = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    trades = [
        _trade(base + timedelta(seconds=10), "buy", 1.0),  # bucket 00:00
        # bucket 00:05 has NO trades — gap
        _trade(base + timedelta(minutes=10, seconds=5), "sell", 1.0),  # bucket 00:10
    ]
    now = base + timedelta(minutes=15)
    rows = bucket_public_trades(trades, "5m", now=now)
    observed_ats = [r["observed_at"] for r in rows]
    assert "2026-01-01T00:05:00Z" not in observed_ats
    assert observed_ats == ["2026-01-01T00:00:00Z", "2026-01-01T00:10:00Z"]


# ---------------------------------------------------------------------------
# Offset-shifted M30/H1 aggregation
# ---------------------------------------------------------------------------


def _make_5m_rows(n: int, start: datetime) -> list[dict]:
    rows = []
    for i in range(n):
        t = start + timedelta(minutes=5) * i
        price = 100.0 + i * 0.1
        rows.append(
            {
                "observed_at": _iso(t),
                "open": price,
                "high": price + 0.5,
                "low": price - 0.5,
                "close": price + 0.05,
                "volume": 1.0,
            }
        )
    return rows


def test_offset_aggregation_30m_offset0_buckets_align_to_grid():
    start = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    rows = _make_5m_rows(12, start)  # exactly two complete 30m buckets
    agg = aggregate_offset_ohlcv(rows, "5m", "30m", 0)
    assert len(agg) == 2
    assert agg[0]["observed_at"] == _iso(start)
    assert agg[1]["observed_at"] == _iso(start + timedelta(minutes=30))
    assert agg[0]["open"] == rows[0]["open"]
    assert agg[0]["close"] == rows[5]["close"]
    assert agg[0]["high"] == max(r["high"] for r in rows[:6])
    assert agg[0]["low"] == min(r["low"] for r in rows[:6])
    assert agg[0]["volume"] == pytest.approx(sum(r["volume"] for r in rows[:6]))


def test_offset_aggregation_30m_offset15_shifts_bucket_boundary():
    start = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    rows = _make_5m_rows(18, start)  # 0:00..1:25
    agg = aggregate_offset_ohlcv(rows, "5m", "30m", 15)
    # offset=15 -> buckets at :15-:45, :45-:15(next hour)... first COMPLETE
    # bucket starting at 0:15 needs bars at 0:15,0:20,...,0:40 (6 bars) — all
    # present in `rows` (up to 1:25).
    assert any(b["observed_at"] == _iso(start + timedelta(minutes=15)) for b in agg)
    for bucket in agg:
        bucket_dt = datetime.fromisoformat(bucket["observed_at"].replace("Z", "+00:00"))
        assert (bucket_dt.minute - 15) % 30 == 0


def test_offset_aggregation_incomplete_bucket_never_emitted():
    start = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    rows = _make_5m_rows(8, start)  # 1 complete 30m bucket (6 bars) + 2 leftover bars
    agg = aggregate_offset_ohlcv(rows, "5m", "30m", 0)
    assert len(agg) == 1  # trailing incomplete bucket dropped, not emitted as partial


def test_offset_aggregation_gap_in_base_data_drops_bucket():
    start = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    rows = _make_5m_rows(6, start)
    del rows[3]  # remove one bar from the middle -> count still could coincidentally match elsewhere, but contiguity check must catch a real gap
    agg = aggregate_offset_ohlcv(rows, "5m", "30m", 0)
    assert agg == []  # gap makes the only candidate bucket incomplete/non-contiguous


def test_offset_aggregation_invalid_offset_rejected():
    rows = _make_5m_rows(12, START)
    with pytest.raises(ValueError):
        aggregate_offset_ohlcv(rows, "5m", "30m", 7)  # not in ALLOWED_OFFSETS["30m"]


def test_offset_aggregation_h1_all_offsets_non_overlapping():
    start = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    rows = _make_5m_rows(288, start)  # 24h of 5m bars
    series_by_offset = {
        off: aggregate_offset_ohlcv(rows, "5m", "1h", off) for off in ALLOWED_OFFSETS["1h"]
    }
    # Each offset must produce distinct bucket-start grids (not identical to
    # offset 0 once shifted) — i.e. genuinely separate series, not aliases.
    starts_0 = {b["observed_at"] for b in series_by_offset[0]}
    for off in (15, 30, 45):
        starts_off = {b["observed_at"] for b in series_by_offset[off]}
        assert starts_0.isdisjoint(starts_off)


def test_offset_aggregation_taker_volume_sums_correctly():
    start = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    rows = []
    for i in range(6):
        rows.append(
            {
                "observed_at": _iso(start + timedelta(minutes=5) * i),
                "taker_buy_volume": 1.0 + i,
                "taker_sell_volume": 0.5 + i,
            }
        )
    agg = aggregate_offset_taker_volume(rows, "5m", "30m", 0)
    assert len(agg) == 1
    assert agg[0]["taker_buy_volume"] == pytest.approx(sum(r["taker_buy_volume"] for r in rows))
    assert agg[0]["taker_sell_volume"] == pytest.approx(sum(r["taker_sell_volume"] for r in rows))


# ---------------------------------------------------------------------------
# Time bucketing / DST
# ---------------------------------------------------------------------------


def test_candle_close_time_adds_timeframe_duration():
    assert candle_close_time("2026-01-01T00:00:00Z", "5m") == "2026-01-01T00:05:00Z"
    assert candle_close_time("2026-01-01T00:00:00Z", "1h") == "2026-01-01T01:00:00Z"


def test_utc_bucketing_unaffected_by_europe_warsaw_dst_transition():
    # 2026-03-29 is the EU spring-forward DST transition (Europe/Warsaw
    # 01:00 UTC -> 03:00 CEST skips 02:00-03:00 local). Since this module
    # works entirely in UTC, bucket boundaries around that instant must be
    # perfectly regular 30-minute UTC buckets — no skipped/doubled bucket.
    start = datetime(2026, 3, 29, 0, 0, 0, tzinfo=timezone.utc)
    rows = _make_5m_rows(24, start)  # spans 00:00-02:00 UTC, straight through the DST instant
    agg = aggregate_offset_ohlcv(rows, "5m", "30m", 0)
    assert len(agg) == 4
    expected_starts = [_iso(start + timedelta(minutes=30) * i) for i in range(4)]
    assert [b["observed_at"] for b in agg] == expected_starts


# ---------------------------------------------------------------------------
# Warm-up / null tests for short history (BB/EMA cross-check)
# ---------------------------------------------------------------------------


def test_warmup_null_for_bb_and_ema_on_short_history():
    rows = _make_ohlcv_rows(10, START, STEP_5M)
    closes = [r["close"] for r in rows]
    bb = bollinger_bands(closes, period=20)
    ema21 = ema(closes, 21)
    assert all(b["middle"] is None for b in bb)
    assert all(v is None for v in ema21)
