"""API-contract tests for GET /api/analysis_snapshot (#276, podzadanie #275).

Same convention as test_technical_overlays_api.py: imports `main` directly
and calls the endpoint FUNCTION, monkeypatching main._read_series (the single
lake-reading chokepoint) plus main._okx_client's live-read methods with
deterministic fixtures — no real lake/OKX/network I/O.

Scope (per task #276 "Testy" instruction — full >=90% cost-savings benchmark
and the flow==tape-sum reconciliation test are #278's, NOT here): schema
shape, value agreement with the underlying single-purpose endpoints,
closed-only/no-look-ahead, 400/422/404/503 error semantics, and a payload-size
smoke test.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

import main


@pytest.fixture(autouse=True)
def _clear_analysis_snapshot_cache():
    # The endpoint's TTL cache (main._analysis_snapshot_cache) is module-level
    # and keyed by (instrument_id + all params) — without clearing it between
    # tests, a later test reusing the same symbol/params within the 1-3s live
    # TTL would silently get served a PRIOR test's cached response instead of
    # exercising its own monkeypatches.
    main._analysis_snapshot_cache.clear()
    yield
    main._analysis_snapshot_cache.clear()


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


START = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
SYMBOL = "BTC-USDT-SWAP"


def _ohlcv_fixture(n: int, timeframe: str) -> list[dict]:
    step = {"5m": timedelta(minutes=5), "15m": timedelta(minutes=15), "1h": timedelta(hours=1), "4h": timedelta(hours=4), "1d": timedelta(days=1)}[timeframe]
    rows = []
    for i in range(n):
        price = 100.0 + (i % 10) * 0.5
        t = START + step * i
        rows.append({
            "observed_at": _iso(t),
            "open": price - 0.1, "high": price + 0.3, "low": price - 0.3,
            "close": price, "volume": 10.0 + i % 5,
        })
    return rows


def _oi_fixture(n: int) -> list[dict]:
    rows = []
    for i in range(n):
        t = START + timedelta(minutes=5) * i
        rows.append({"observed_at": _iso(t), "open_interest": 1000.0 + i * 10})
    return rows


def _sr_events_fixture() -> list[dict]:
    base_created = _iso(START)
    return [
        {
            "level_id": "sup-1", "level_type": "support", "price_top": 98.0, "price_bottom": 97.0,
            "status": "holding", "volume": 500.0, "touch_count": 3,
            "created_at": base_created, "last_touched_at": base_created,
            "event": "created", "observed_at": base_created,
        },
        {
            "level_id": "sup-2", "level_type": "support", "price_top": 90.0, "price_bottom": 89.0,
            "status": "holding", "volume": 200.0, "touch_count": 1,
            "created_at": base_created, "last_touched_at": base_created,
            "event": "created", "observed_at": base_created,
        },
        {
            "level_id": "res-1", "level_type": "resistance", "price_top": 106.0, "price_bottom": 105.0,
            "status": "holding", "volume": 400.0, "touch_count": 2,
            "created_at": base_created, "last_touched_at": base_created,
            "event": "created", "observed_at": base_created,
        },
        {
            "level_id": "res-2", "level_type": "resistance", "price_top": 112.0, "price_bottom": 111.0,
            "status": "holding", "volume": 150.0, "touch_count": 1,
            "created_at": base_created, "last_touched_at": base_created,
            "event": "created", "observed_at": base_created,
        },
    ]


def _trade(ts_ms: int, side: str, sz: float) -> dict:
    return {"ts": str(ts_ms), "side": side, "sz": str(sz), "px": "100.0"}


@pytest.fixture
def fake_backend(monkeypatch):
    now = datetime.now(timezone.utc)
    store: dict[tuple[str, str], list[dict]] = {
        ("ohlcv", "5m"): _ohlcv_fixture(300, "5m"),
        ("ohlcv", "15m"): _ohlcv_fixture(100, "15m"),
        ("ohlcv", "1h"): _ohlcv_fixture(260, "1h"),
        ("ohlcv", "4h"): _ohlcv_fixture(80, "4h"),
        ("ohlcv", "1d"): _ohlcv_fixture(30, "1d"),
        ("open_interest", "5m"): _oi_fixture(10),
        ("support_resistance", "5m"): _sr_events_fixture(),
        ("support_resistance", "15m"): _sr_events_fixture(),
        ("support_resistance", "1h"): _sr_events_fixture(),
        ("funding", "1h"): [{"observed_at": _iso(now), "funding_rate": 0.0001}],
    }

    def fake_read_series(data_kind, symbol, timeframe, limit):
        rows = store.get((data_kind, timeframe), [])
        return rows[-limit:] if limit else rows

    monkeypatch.setattr(main, "_read_series", fake_read_series)
    monkeypatch.setattr(main, "_live_provisional_row", lambda instrument_id, timeframe: None)

    now_ms = int(now.timestamp() * 1000)
    trades = [_trade(now_ms - i * 1000, "buy" if i % 2 == 0 else "sell", 1.5) for i in range(50)]

    class _FakeOkxClient:
        def get_trades(self, inst_id, limit=500):
            return {"data": trades}

        def get_orderbook(self, inst_id, sz=20):
            return {"data": [{"ts": str(now_ms), "bids": [["99.5", "1.0", "0", "1"]], "asks": [["100.5", "1.0", "0", "1"]]}]}

    monkeypatch.setattr(main, "_okx_client", _FakeOkxClient())
    return store


# ---------------------------------------------------------------------------
# Schema / AC 1, AC 9
# ---------------------------------------------------------------------------


def test_default_response_has_full_contract(fake_backend):
    result = main.analysis_snapshot(symbol=SYMBOL)
    for key in ("symbol", "instrument_id", "checked_at", "closed_only", "price",
                "candles", "indicators", "open_interest", "taker_flow",
                "support_resistance", "components", "cost"):
        assert key in result, f"missing top-level key {key!r}"
    assert result["instrument_id"] == SYMBOL
    for key in ("upstream_calls", "payload_bytes", "response_bytes", "rows_scanned", "rows_returned", "cache_hits"):
        assert key in result["cost"], f"missing cost.{key}"
    # AC 9: cost metrics are real, not placeholders
    assert result["cost"]["upstream_calls"] > 0
    assert result["cost"]["payload_bytes"] > 0
    assert result["cost"]["response_bytes"] == result["cost"]["payload_bytes"]


def test_default_timeframes_and_indicators(fake_backend):
    result = main.analysis_snapshot(symbol=SYMBOL)
    assert set(result["candles"].keys()) == {"5m", "15m", "1h"}
    for tf, series in result["candles"].items():
        assert len(series) == 3  # closed_candles default
    for tf, indicators in result["indicators"].items():
        assert set(indicators.keys()) == {"bb", "ema21", "ema50", "ema200", "vwap"}


# ---------------------------------------------------------------------------
# AC 2: default payload <= 10 kB
# ---------------------------------------------------------------------------


def test_default_payload_under_10kb(fake_backend):
    result = main.analysis_snapshot(symbol=SYMBOL)
    size = len(json.dumps(result).encode("utf-8"))
    assert size <= 10_240, f"default response is {size} bytes, exceeds 10 kB budget"
    assert result["cost"]["payload_bytes"] <= 10_240


# ---------------------------------------------------------------------------
# AC 3: closed-only default, provisional only when requested
# ---------------------------------------------------------------------------


def test_closed_only_by_default_no_provisional_field(fake_backend):
    result = main.analysis_snapshot(symbol=SYMBOL)
    assert result["closed_only"] is True
    for tf, series in result["candles"].items():
        assert all(c["is_closed"] is True for c in series)


def test_include_provisional_marks_is_closed_false(fake_backend, monkeypatch):
    provisional_row = {"observed_at": "2026-01-05T00:00:00Z", "open": 1.0, "high": 1.1, "low": 0.9, "close": 1.05, "volume": 2.0}
    monkeypatch.setattr(main, "_live_provisional_row", lambda instrument_id, timeframe: dict(provisional_row))
    result = main.analysis_snapshot(symbol=SYMBOL, include_provisional=True)
    assert result["closed_only"] is False
    for tf, series in result["candles"].items():
        assert series[-1]["is_closed"] is False


# ---------------------------------------------------------------------------
# AC 4: source_time on every indicator field, no look-ahead
# ---------------------------------------------------------------------------


def test_every_indicator_field_has_source_time(fake_backend):
    result = main.analysis_snapshot(symbol=SYMBOL, timeframes="5m", indicators="bb,ema21,vwap")
    for name, value in result["indicators"]["5m"].items():
        assert value is not None, f"{name} unexpectedly None with 300-row fixture"
        assert "source_time" in value
        # source_time must be the last CLOSED candle's own time, never later
        assert value["source_time"] == result["candles"]["5m"][-1]["time"]


def test_indicator_source_time_never_ahead_of_last_closed_candle(fake_backend):
    result = main.analysis_snapshot(symbol=SYMBOL, timeframes="1h", indicators="ema21")
    last_candle_time = result["candles"]["1h"][-1]["time"]
    assert result["indicators"]["1h"]["ema21"]["source_time"] == last_candle_time


# ---------------------------------------------------------------------------
# Value agreement with the underlying single-purpose endpoints
# ---------------------------------------------------------------------------


def test_bb_value_matches_bollinger_endpoint(fake_backend):
    snap = main.analysis_snapshot(symbol=SYMBOL, timeframes="5m", indicators="bb", closed_candles=1)
    direct = main.bollinger_endpoint(symbol=SYMBOL, timeframe="5m", limit=1)
    assert snap["indicators"]["5m"]["bb"]["middle"] == pytest.approx(direct["series"][-1]["middle"])
    assert snap["indicators"]["5m"]["bb"]["upper"] == pytest.approx(direct["series"][-1]["upper"])
    assert snap["indicators"]["5m"]["bb"]["lower"] == pytest.approx(direct["series"][-1]["lower"])


def test_ema_value_matches_ema_endpoint(fake_backend):
    snap = main.analysis_snapshot(symbol=SYMBOL, timeframes="5m", indicators="ema21", closed_candles=1)
    direct = main.ema_endpoint(symbol=SYMBOL, timeframe="5m", period=21, limit=1)
    assert snap["indicators"]["5m"]["ema21"]["value"] == pytest.approx(direct["series"][-1]["value"])


def test_vwap_value_matches_vwap_endpoint(fake_backend):
    # VWAP session resets daily and accumulates from the start of its read
    # window (no fixed warmup like BB/EMA) — session_endpoint value only
    # matches analysis_snapshot's own value when both read the SAME window,
    # so compare against a full-history direct call, not vwap_endpoint's own
    # default `limit` (which would cut the accumulation window differently).
    snap = main.analysis_snapshot(symbol=SYMBOL, timeframes="5m", indicators="vwap", closed_candles=1)
    direct = main.vwap_endpoint(symbol=SYMBOL, timeframe="5m", limit=len(fake_backend[("ohlcv", "5m")]))
    assert snap["indicators"]["5m"]["vwap"]["value"] == pytest.approx(direct["series"][-1]["value"])


def test_open_interest_value_matches_open_interest_endpoint(fake_backend):
    snap = main.analysis_snapshot(symbol=SYMBOL, timeframes="5m")
    direct = main.open_interest(symbol=SYMBOL, timeframe="5m", limit=2)
    assert snap["open_interest"]["value"] == direct[-1]["value"]
    assert snap["open_interest"]["delta"] == pytest.approx(direct[-1]["value"] - direct[-2]["value"])


def test_support_resistance_zones_are_nearest_and_ordered(fake_backend):
    # current price ~ last close of 5m fixture (100.0 + i%10*0.5 pattern)
    result = main.analysis_snapshot(symbol=SYMBOL, timeframes="5m", sr_nearest=1)
    sr = result["support_resistance"]
    assert len(sr["support"]) <= 1
    assert len(sr["resistance"]) <= 1
    if sr["support"]:
        assert sr["support"][0]["price_top"] <= result["price"]["value"]
    if sr["resistance"]:
        assert sr["resistance"][0]["price_bottom"] >= result["price"]["value"]


# ---------------------------------------------------------------------------
# AC 5: taker_flow is an aggregate, never raw trades
# ---------------------------------------------------------------------------


def test_taker_flow_is_aggregate_not_raw_trades(fake_backend):
    result = main.analysis_snapshot(symbol=SYMBOL)
    flow = result["taker_flow"]
    for key in ("buy", "sell", "delta", "trade_count", "source_scope"):
        assert key in flow
    assert flow["source_scope"] == "instrument"
    assert "trades" not in flow
    assert "raw_trades" not in flow
    # buy - sell must equal delta (aggregation correctness, not tape-sum
    # reconciliation across the whole benchmark — that's #278's job)
    assert flow["delta"] == pytest.approx(flow["buy"] - flow["sell"])


# ---------------------------------------------------------------------------
# #278: flow == full-tape reconciliation + gaps/partial coverage
# ---------------------------------------------------------------------------


def test_taker_flow_equals_full_manual_sum_over_window(fake_backend, monkeypatch):
    """The aggregate buy/sell/delta/trade_count must equal a manual sum over
    EVERY trade the fake OKX client returns that falls inside flow_window —
    not a sample, not an estimate. Builds a deliberately uneven tape (mixed
    sizes, mixed sides, some OUTSIDE the window) so a subtly wrong filter
    (off-by-one on the cutoff, or aggregating the whole tape ignoring
    flow_window) would fail this."""
    now = datetime.now(timezone.utc)
    now_ms = int(now.timestamp() * 1000)
    flow_window = 120  # seconds
    cutoff_ms = now_ms - flow_window * 1000

    # Deliberately irregular tape: variable sizes, both sides, spanning well
    # past the window on the old end so some trades MUST be excluded.
    raw = [
        (now_ms - 1_000, "buy", 1.25),
        (now_ms - 5_000, "sell", 0.75),
        (now_ms - 15_000, "buy", 3.0),
        (now_ms - 45_000, "sell", 2.2),
        (now_ms - 90_000, "buy", 0.5),
        (now_ms - 119_000, "sell", 1.1),  # just inside window
        (now_ms - 121_000, "buy", 9.0),   # just OUTSIDE window - must be excluded
        (now_ms - 200_000, "sell", 4.0),  # well outside
        (now_ms - 300_000, "buy", 7.0),   # well outside
    ]
    trades = [_trade(ts, side, sz) for ts, side, sz in raw]

    class _FixedTapeOkxClient:
        def get_trades(self, inst_id, limit=500):
            return {"data": trades}

    monkeypatch.setattr(main, "_okx_client", _FixedTapeOkxClient())

    checked_at = now.isoformat().replace("+00:00", "Z")
    flow = main._analysis_snapshot_taker_flow(SYMBOL, flow_window, checked_at)

    # Ground truth: manual sum over the FULL tape, filtered by the same
    # cutoff the endpoint is documented to apply.
    in_window = [(ts, side, sz) for ts, side, sz in raw if ts >= cutoff_ms]
    expected_buy = sum(sz for ts, side, sz in in_window if side == "buy")
    expected_sell = sum(sz for ts, side, sz in in_window if side == "sell")

    assert flow["trade_count"] == len(in_window)
    assert flow["buy"] == pytest.approx(expected_buy)
    assert flow["sell"] == pytest.approx(expected_sell)
    assert flow["delta"] == pytest.approx(expected_buy - expected_sell)
    assert flow["status"] == "ok"
    # Sanity: the excluded trade actually changed the result (proves the
    # window filter is doing real work, not a no-op).
    assert flow["buy"] != pytest.approx(sum(sz for _, side, sz in raw if side == "buy"))


def test_taker_flow_partial_when_tape_pull_capped_before_covering_window(fake_backend, monkeypatch):
    """When OKX's own trades-limit cap (_INSTRUMENT_TRADES_LIMIT=500) is hit
    AND the pulled tape doesn't reach back far enough to fully cover
    flow_window, the aggregate must be flagged status="partial" (coverage
    gap), not silently reported as a complete/"ok" result."""
    now = datetime.now(timezone.utc)
    now_ms = int(now.timestamp() * 1000)
    flow_window = 3600  # 1 hour requested

    # Exactly _INSTRUMENT_TRADES_LIMIT trades, all packed into the most
    # recent 60 seconds (simulates a burst) - the pull hits the OKX cap
    # while covering only 60s of a 3600s requested window: partial coverage.
    trades = [
        _trade(now_ms - i * 100, "buy" if i % 2 == 0 else "sell", 1.0)
        for i in range(main._INSTRUMENT_TRADES_LIMIT)
    ]

    class _CappedTapeOkxClient:
        def get_trades(self, inst_id, limit=500):
            return {"data": trades}

    monkeypatch.setattr(main, "_okx_client", _CappedTapeOkxClient())
    checked_at = now.isoformat().replace("+00:00", "Z")
    flow = main._analysis_snapshot_taker_flow(SYMBOL, flow_window, checked_at)

    assert flow["hit_trades_limit"] is True
    assert flow["status"] == "partial"
    # Component surfaces the gap explicitly instead of a quietly-too-small
    # aggregate presented as complete.
    result = main.analysis_snapshot(symbol=SYMBOL, flow_window=flow_window)
    assert result["taker_flow"]["status"] == "partial"
    assert result["components"]["taker_flow"] == "partial"


def test_taker_flow_unavailable_on_empty_trade_list(fake_backend, monkeypatch):
    """OKX returning a syntactically valid but EMPTY trade list (network
    blip / instrument delisted / no recent activity) must degrade to
    status="unavailable" with buy/sell/delta=None, not a silently-zero
    aggregate that looks like real (zero) flow."""
    class _EmptyTapeOkxClient:
        def get_trades(self, inst_id, limit=500):
            return {"data": []}

    monkeypatch.setattr(main, "_okx_client", _EmptyTapeOkxClient())
    checked_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    flow = main._analysis_snapshot_taker_flow(SYMBOL, 300, checked_at)

    assert flow["status"] == "unavailable"
    assert flow["buy"] is None
    assert flow["sell"] is None
    assert flow["delta"] is None
    assert flow["trade_count"] == 0

    result = main.analysis_snapshot(symbol=SYMBOL)
    assert result["taker_flow"]["status"] == "unavailable"
    assert result["components"]["taker_flow"] == "unavailable"


def test_taker_flow_partial_when_window_older_than_full_tape(fake_backend, monkeypatch):
    """Tape doesn't hit the OKX cap (fewer than _INSTRUMENT_TRADES_LIMIT
    trades returned) but EVERY trade is older than flow_window - a real
    coverage gap (nothing recent happened, or the feed is lagging) that must
    not be silently reported as trade_count=0/status=ok."""
    now = datetime.now(timezone.utc)
    now_ms = int(now.timestamp() * 1000)
    # All trades ~2 hours old; window only asks for the last 60s.
    trades = [_trade(now_ms - 7200_000 - i * 1000, "buy", 1.0) for i in range(20)]

    class _StaleTapeOkxClient:
        def get_trades(self, inst_id, limit=500):
            return {"data": trades}

    monkeypatch.setattr(main, "_okx_client", _StaleTapeOkxClient())
    checked_at = now.isoformat().replace("+00:00", "Z")
    flow = main._analysis_snapshot_taker_flow(SYMBOL, 60, checked_at)

    assert flow["trade_count"] == 0
    assert flow["status"] == "partial"  # not "ok" - the window is unfilled, not genuinely zero-flow


# ---------------------------------------------------------------------------
# AC 6: OI / taker_flow instrument-specific, never silent currency-wide;
# CVD only currency-wide with explicit scope+warning when opted in
# ---------------------------------------------------------------------------


def test_oi_and_flow_are_instrument_scoped(fake_backend):
    result = main.analysis_snapshot(symbol=SYMBOL)
    assert result["open_interest"]["source_scope"] == "instrument"
    assert result["taker_flow"]["source_scope"] == "instrument"


def test_cvd_excluded_by_default(fake_backend):
    result = main.analysis_snapshot(symbol=SYMBOL)
    assert "cvd" not in result


def test_cvd_included_with_explicit_scope_and_warning(fake_backend, monkeypatch):
    monkeypatch.setattr(main, "_read_taker_volume_rows", lambda symbol, timeframe, limit, offset_minutes: [
        {"observed_at": _iso(START), "taker_buy_volume": 5.0, "taker_sell_volume": 3.0},
        {"observed_at": _iso(START + timedelta(minutes=5)), "taker_buy_volume": 6.0, "taker_sell_volume": 2.0},
    ])
    result = main.analysis_snapshot(symbol=SYMBOL, include_cvd=True)
    assert "cvd" in result
    assert result["cvd"]["source_scope"] == "currency_aggregate"
    assert result["cvd"]["warning"]


# ---------------------------------------------------------------------------
# AC 7: partial response per component, never 500, never silent field drop
# ---------------------------------------------------------------------------


def test_missing_open_interest_degrades_to_unavailable_not_500(fake_backend, monkeypatch):
    def fake_read_series(data_kind, symbol, timeframe, limit):
        if data_kind == "open_interest":
            raise HTTPException(status_code=404, detail="no backfilled data")
        return fake_backend.get((data_kind, timeframe), [])[-limit:] if limit else fake_backend.get((data_kind, timeframe), [])

    monkeypatch.setattr(main, "_read_series", fake_read_series)
    result = main.analysis_snapshot(symbol=SYMBOL)  # must not raise
    assert result["open_interest"]["status"] == "unavailable"
    assert result["components"]["open_interest"] == "unavailable"
    # candles/indicators still present (partial, not a silent drop)
    assert result["candles"]


def test_missing_support_resistance_degrades_not_500(fake_backend, monkeypatch):
    def fake_read_series(data_kind, symbol, timeframe, limit):
        if data_kind == "support_resistance":
            raise HTTPException(status_code=404, detail="no backfilled data")
        return fake_backend.get((data_kind, timeframe), [])[-limit:] if limit else fake_backend.get((data_kind, timeframe), [])

    monkeypatch.setattr(main, "_read_series", fake_read_series)
    result = main.analysis_snapshot(symbol=SYMBOL)  # must not raise
    assert result["support_resistance"]["status"] == "unavailable"
    assert result["components"]["support_resistance"] == "unavailable"


def test_taker_flow_unavailable_when_okx_call_fails(fake_backend, monkeypatch):
    class _BrokenOkxClient:
        def get_trades(self, inst_id, limit=500):
            raise RuntimeError("OKX unreachable")

    monkeypatch.setattr(main, "_okx_client", _BrokenOkxClient())
    result = main.analysis_snapshot(symbol=SYMBOL)  # must not raise
    assert result["taker_flow"]["status"] == "unavailable"
    assert result["components"]["taker_flow"] == "unavailable"


# ---------------------------------------------------------------------------
# AC 8: 400/422/404/503 error semantics
# ---------------------------------------------------------------------------


def test_unknown_symbol_404(fake_backend):
    with pytest.raises(HTTPException) as exc_info:
        main.analysis_snapshot(symbol="NOTREAL-USDT-SWAP")
    assert exc_info.value.status_code == 404


def test_invalid_timeframe_422(fake_backend):
    with pytest.raises(HTTPException) as exc_info:
        main.analysis_snapshot(symbol=SYMBOL, timeframes="3m")
    assert exc_info.value.status_code == 422


def test_invalid_closed_candles_422(fake_backend):
    with pytest.raises(HTTPException) as exc_info:
        main.analysis_snapshot(symbol=SYMBOL, closed_candles=6)
    assert exc_info.value.status_code == 422
    with pytest.raises(HTTPException) as exc_info:
        main.analysis_snapshot(symbol=SYMBOL, closed_candles=0)
    assert exc_info.value.status_code == 422


def test_invalid_indicator_422(fake_backend):
    with pytest.raises(HTTPException) as exc_info:
        main.analysis_snapshot(symbol=SYMBOL, indicators="rsi")
    assert exc_info.value.status_code == 422


def test_invalid_sr_nearest_422(fake_backend):
    with pytest.raises(HTTPException) as exc_info:
        main.analysis_snapshot(symbol=SYMBOL, sr_nearest=6)
    assert exc_info.value.status_code == 422


def test_invalid_flow_window_400(fake_backend):
    with pytest.raises(HTTPException) as exc_info:
        main.analysis_snapshot(symbol=SYMBOL, flow_window=0)
    assert exc_info.value.status_code == 400


def test_no_candle_data_at_all_503(fake_backend, monkeypatch):
    monkeypatch.setattr(main, "_read_series", lambda data_kind, symbol, timeframe, limit: [])
    with pytest.raises(HTTPException) as exc_info:
        main.analysis_snapshot(symbol=SYMBOL)
    assert exc_info.value.status_code == 503


# ---------------------------------------------------------------------------
# Cache behavior sanity (TTL for live components, key includes params)
# ---------------------------------------------------------------------------


def test_cache_hit_marks_served_from_cache(fake_backend, monkeypatch):
    main._analysis_snapshot_cache.clear()
    first = main.analysis_snapshot(symbol=SYMBOL)
    assert first["cost"]["served_from_cache"] is False
    second = main.analysis_snapshot(symbol=SYMBOL)
    assert second["cost"]["served_from_cache"] is True
    assert second["cost"]["cache_hits"] >= 1


def test_cache_key_varies_with_parameters(fake_backend):
    main._analysis_snapshot_cache.clear()
    main.analysis_snapshot(symbol=SYMBOL, closed_candles=3)
    different = main.analysis_snapshot(symbol=SYMBOL, closed_candles=5)
    assert different["cost"]["served_from_cache"] is False


# ---------------------------------------------------------------------------
# #278: cache rollover after TTL - no stale data past
# _ANALYSIS_SNAPSHOT_LIVE_TTL_SECONDS
# ---------------------------------------------------------------------------


def test_cache_rollover_after_ttl_sees_mutated_source_data(fake_backend, monkeypatch):
    """Mutates the underlying open_interest source between two calls. Within
    TTL, the second call must still return the FIRST (cached) value (proves
    the cache is actually being used). Once the cache entry is manually
    pushed past _ANALYSIS_SNAPSHOT_LIVE_TTL_SECONDS (simulating elapsed wall
    time without a real sleep), a third call must see the MUTATED value -
    proving TTL rollover invalidates rather than serving stale data
    indefinitely."""
    main._analysis_snapshot_cache.clear()
    oi_store = fake_backend[("open_interest", "5m")]

    first = main.analysis_snapshot(symbol=SYMBOL, timeframes="5m")
    assert first["cost"]["served_from_cache"] is False
    first_oi_value = first["open_interest"]["value"]

    # Mutate the source data lake in place (new latest OI point).
    mutated_point = {"observed_at": oi_store[-1]["observed_at"], "open_interest": oi_store[-1]["open_interest"] + 999.0}
    oi_store.append(mutated_point)

    # Still within TTL -> must be served from cache, i.e. still the OLD value.
    second = main.analysis_snapshot(symbol=SYMBOL, timeframes="5m")
    assert second["cost"]["served_from_cache"] is True
    assert second["open_interest"]["value"] == first_oi_value

    # Force the cache entry's timestamp back past the TTL window without a
    # real sleep - equivalent to "TTL elapsed", same technique the endpoint
    # itself uses (time.monotonic() delta), just backdated here.
    cache_key = main._analysis_snapshot_cache_key(
        instrument_id=SYMBOL, timeframes=("5m",), closed_candles=3,
        indicators=main._ANALYSIS_SNAPSHOT_DEFAULT_INDICATORS, sr_nearest=3, flow_window=300,
        include_orderbook=False, include_funding=False, include_cvd=False, include_provisional=False,
    )
    cached_at, fingerprint, cached_response = main._analysis_snapshot_cache[cache_key]
    backdated_at = cached_at - (main._ANALYSIS_SNAPSHOT_LIVE_TTL_SECONDS + 1.0)
    main._analysis_snapshot_cache[cache_key] = (backdated_at, fingerprint, cached_response)

    third = main.analysis_snapshot(symbol=SYMBOL, timeframes="5m")
    assert third["cost"]["served_from_cache"] is False, "TTL rollover must NOT be served from cache"
    assert third["open_interest"]["value"] != first_oi_value, "stale cached value leaked past TTL rollover"
    assert third["open_interest"]["value"] == pytest.approx(mutated_point["open_interest"])


def test_cache_within_ttl_serves_stale_snapshot_deliberately(fake_backend):
    """Companion to the rollover test above: WITHIN the TTL window, two
    back-to-back calls must be identical (cache doing its job of avoiding
    redundant upstream reads) - this is the expected/deliberate short-lived
    staleness the TTL trades off, not a bug."""
    main._analysis_snapshot_cache.clear()
    first = main.analysis_snapshot(symbol=SYMBOL)
    second = main.analysis_snapshot(symbol=SYMBOL)
    assert first["checked_at"] == second["checked_at"]
    assert second["cost"]["served_from_cache"] is True
