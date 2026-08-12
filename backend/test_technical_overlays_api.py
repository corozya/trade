"""API-contract tests for the 5 new /api/* endpoints (#242): bollinger, ema,
ema_projection, vwap, cvd — plus their /api/available integration.

Imports `main` directly and calls the endpoint FUNCTIONS (not a live HTTP
server), same convention as test_autotrader_api.py. `_read_series` (main.py's
single lake-reading chokepoint) is monkeypatched with deterministic fixture
rows so these tests never touch the real data lake, OKX, or network — pure
API-shape/validation/integration coverage on top of test_technical_overlays.py's
pure-function math coverage.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

import main


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


START = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


def _ohlcv_fixture(n: int, timeframe: str = "5m") -> list[dict]:
    step = {"5m": timedelta(minutes=5), "15m": timedelta(minutes=15), "1h": timedelta(hours=1), "4h": timedelta(hours=4)}[timeframe]
    rows = []
    for i in range(n):
        price = 100.0 + (i % 10) * 0.5
        t = START + step * i
        rows.append(
            {
                "observed_at": _iso(t),
                "open": price - 0.1,
                "high": price + 0.3,
                "low": price - 0.3,
                "close": price,
                "volume": 10.0,
            }
        )
    return rows


def _taker_fixture(n: int, timeframe: str = "5m") -> list[dict]:
    step = {"5m": timedelta(minutes=5), "1h": timedelta(hours=1)}[timeframe]
    rows = []
    for i in range(n):
        t = START + step * i
        rows.append({"observed_at": _iso(t), "taker_buy_volume": 5.0 + i % 3, "taker_sell_volume": 3.0 + i % 2})
    return rows


@pytest.fixture
def fake_lake(monkeypatch):
    """Patches main._read_series so every endpoint call is served from an
    in-memory fixture keyed by (data_kind, timeframe) — no lake/OKX I/O."""
    store: dict[tuple[str, str], list[dict]] = {
        ("ohlcv", "5m"): _ohlcv_fixture(300, "5m"),
        ("ohlcv", "15m"): _ohlcv_fixture(100, "15m"),
        ("ohlcv", "1h"): _ohlcv_fixture(260, "1h"),
        ("ohlcv", "4h"): _ohlcv_fixture(80, "4h"),
        ("taker_volume", "5m"): _taker_fixture(300, "5m"),
        ("taker_volume", "1h"): _taker_fixture(260, "1h"),
    }

    def fake_read_series(data_kind, symbol, timeframe, limit):
        rows = store.get((data_kind, timeframe), [])
        return rows[-limit:] if limit else rows

    monkeypatch.setattr(main, "_read_series", fake_read_series)
    return store


# ---------------------------------------------------------------------------
# /api/bollinger
# ---------------------------------------------------------------------------


def test_bollinger_endpoint_shape(fake_lake):
    result = main.bollinger_endpoint(symbol="BTC-USDT-SWAP", timeframe="5m", limit=50)
    assert "series" in result and "period" in result and "percentile_window" in result
    assert result["period"] == 20
    point = result["series"][-1]
    for field in ("time", "middle", "upper", "lower", "bandwidth", "percent_b", "bandwidth_percentile"):
        assert field in point


def test_bollinger_endpoint_rejects_invalid_percentile_window(fake_lake):
    with pytest.raises(HTTPException) as exc_info:
        main.bollinger_endpoint(symbol="BTC-USDT-SWAP", timeframe="5m", limit=50, percentile_window=42)
    assert exc_info.value.status_code == 422


def test_bollinger_endpoint_warns_on_short_history(fake_lake, monkeypatch):
    monkeypatch.setitem(fake_lake, ("ohlcv", "5m"), _ohlcv_fixture(5, "5m"))
    result = main.bollinger_endpoint(symbol="BTC-USDT-SWAP", timeframe="5m", limit=5)
    assert "warning" in result


# ---------------------------------------------------------------------------
# /api/ema
# ---------------------------------------------------------------------------


def test_ema_endpoint_shape_and_period_validation(fake_lake):
    result = main.ema_endpoint(symbol="BTC-USDT-SWAP", timeframe="5m", period=50, limit=100)
    assert result["period"] == 50
    assert result["series"][-1]["period"] == 50

    with pytest.raises(HTTPException) as exc_info:
        main.ema_endpoint(symbol="BTC-USDT-SWAP", timeframe="5m", period=9, limit=100)
    assert exc_info.value.status_code == 422


def test_ema_endpoint_warmup_warning_for_short_history(fake_lake, monkeypatch):
    monkeypatch.setitem(fake_lake, ("ohlcv", "5m"), _ohlcv_fixture(10, "5m"))
    result = main.ema_endpoint(symbol="BTC-USDT-SWAP", timeframe="5m", period=200, limit=10)
    assert "warning" in result
    assert all(p["value"] is None for p in result["series"])


# ---------------------------------------------------------------------------
# /api/ema_projection
# ---------------------------------------------------------------------------


def test_ema_projection_endpoint_shape(fake_lake):
    result = main.ema_projection_endpoint(
        symbol="BTC-USDT-SWAP", target_timeframe="5m", source_timeframe="1h", period=21, limit=50
    )
    assert result["source_timeframe"] == "1h"
    assert result["target_timeframe"] == "5m"
    point = result["series"][-1]
    for field in ("target_time", "value", "period", "source_timeframe", "source_candle_close_time", "target_timeframe", "last_updated_at"):
        assert field in point


def test_ema_projection_endpoint_rejects_disallowed_source_for_target(fake_lake):
    with pytest.raises(HTTPException) as exc_info:
        main.ema_projection_endpoint(
            symbol="BTC-USDT-SWAP", target_timeframe="5m", source_timeframe="4h", period=21, limit=50
        )
    assert exc_info.value.status_code == 422


def test_ema_projection_endpoint_rejects_unknown_target_timeframe(fake_lake):
    with pytest.raises(HTTPException) as exc_info:
        main.ema_projection_endpoint(
            symbol="BTC-USDT-SWAP", target_timeframe="1d", source_timeframe="1h", period=21, limit=50
        )
    assert exc_info.value.status_code == 422


def test_ema_projection_endpoint_15m_target_allows_1h_and_4h_sources(fake_lake):
    for src in ("1h", "4h"):
        result = main.ema_projection_endpoint(
            symbol="BTC-USDT-SWAP", target_timeframe="15m", source_timeframe=src, period=21, limit=30
        )
        assert result["source_timeframe"] == src


# ---------------------------------------------------------------------------
# /api/vwap
# ---------------------------------------------------------------------------


def test_vwap_endpoint_session_default(fake_lake):
    result = main.vwap_endpoint(symbol="BTC-USDT-SWAP", timeframe="5m", limit=50)
    assert result["mode"] == "session"
    assert result["session_timezone"] == "UTC"
    assert result["anchor_time"] is None
    assert result["series"][-1]["value"] is not None


def test_vwap_endpoint_anchored_requires_anchor_time(fake_lake):
    with pytest.raises(HTTPException) as exc_info:
        main.vwap_endpoint(symbol="BTC-USDT-SWAP", timeframe="5m", mode="anchored", limit=50)
    assert exc_info.value.status_code == 422


def test_vwap_endpoint_anchored_rejects_unknown_anchor(fake_lake):
    with pytest.raises(HTTPException) as exc_info:
        main.vwap_endpoint(
            symbol="BTC-USDT-SWAP", timeframe="5m", mode="anchored", anchor_time="2099-01-01T00:00:00Z", limit=50
        )
    assert exc_info.value.status_code == 422


def test_vwap_endpoint_anchored_valid_anchor(fake_lake):
    rows = fake_lake[("ohlcv", "5m")]
    anchor = rows[-10]["observed_at"]  # must fall within the endpoint's own limit=50 read window
    result = main.vwap_endpoint(symbol="BTC-USDT-SWAP", timeframe="5m", mode="anchored", anchor_time=anchor, limit=50)
    assert result["anchor_time"] == anchor


def test_vwap_endpoint_rejects_bad_mode(fake_lake):
    with pytest.raises(HTTPException) as exc_info:
        main.vwap_endpoint(symbol="BTC-USDT-SWAP", timeframe="5m", mode="bogus", limit=50)
    assert exc_info.value.status_code == 422


# ---------------------------------------------------------------------------
# /api/cvd
# ---------------------------------------------------------------------------


def test_cvd_endpoint_session_default(fake_lake):
    result = main.cvd_endpoint(symbol="BTC-USDT-SWAP", timeframe="5m", limit=50)
    assert result["mode"] == "session"
    assert result["source"] == "okx"
    point = result["series"][-1]
    for field in ("time", "value", "delta", "taker_buy", "taker_sell", "source"):
        assert field in point


def test_cvd_endpoint_anchored_requires_anchor_time(fake_lake):
    with pytest.raises(HTTPException) as exc_info:
        main.cvd_endpoint(symbol="BTC-USDT-SWAP", timeframe="5m", mode="anchored", limit=50)
    assert exc_info.value.status_code == 422


# ---------------------------------------------------------------------------
# /api/cvd — #265 source_scope contract (currency_aggregate vs instrument)
# ---------------------------------------------------------------------------


def test_cvd_endpoint_default_scope_is_currency_aggregate_and_backward_compatible(fake_lake):
    """Task #265 test 1 + 7: contract test for scope/units, AND backward
    compatibility — every field the original #242 contract returned
    (series/mode/anchor_time/source, plus the _response_envelope fields) must
    still be present and unchanged when source_scope is omitted."""
    result = main.cvd_endpoint(symbol="BTC-USDT-SWAP", timeframe="5m", limit=50)
    # #242 original contract, untouched:
    assert result["mode"] == "session"
    assert result["source"] == "okx"
    assert "series" in result and "anchor_time" in result
    for envelope_field in ("symbol", "timeframe", "offset_minutes", "data_kind", "closed_only", "last_closed_candle_time", "last_updated_at", "checked_at", "is_stale"):
        assert envelope_field in result, f"backward-compat break: {envelope_field} missing"
    # #265 new explicit-scope fields, additive:
    assert result["source_scope"] == "currency_aggregate"
    assert result["source_endpoint"] == "/api/v5/rubik/stat/taker-volume"
    assert result["source_ccy"] == "BTC"
    assert result["source_inst_type"] == "CONTRACTS"
    assert result["units"] == "quote_currency_notional_per_bucket"
    assert result["requested_symbol"] == "BTC-USDT-SWAP"
    assert result["resolved_instrument_id"] == "BTC-USDT-SWAP"
    assert "warning" in result and "currency_aggregate" in result["warning"]
    assert "data_as_of" in result
    assert "freshness_seconds" in result


def test_cvd_endpoint_xperp_currency_aggregate_ccy_not_swapped_for_swap(fake_lake, monkeypatch):
    """Task #265 test 4: XPERP must not be silently relabeled as a SWAP
    instrument, and the currency-aggregate scope must be explicitly and
    correctly tagged — this is the exact WLD X-Perp misreading #265 reports."""
    monkeypatch.setitem(fake_lake, ("taker_volume", "5m"), _taker_fixture(300, "5m"))
    result = main.cvd_endpoint(symbol="WLD-USD_UM_XPERP-310613", timeframe="5m", limit=50)
    assert result["resolved_instrument_id"] == "WLD-USD_UM_XPERP-310613"
    assert result["source_ccy"] == "WLD"
    assert result["source_scope"] == "currency_aggregate"
    assert "coverage_status" in result and result["coverage_status"] == "currency_wide"


def test_cvd_endpoint_2026_08_09_snapshot_not_presented_as_instrument_specific(fake_lake):
    """Task #265 AC: the control test for the 2026-08-09 11:25-11:30 CEST
    window — a Rubik-sourced (currency_aggregate) response must never claim
    source_scope=instrument, regardless of the delta's sign/magnitude."""
    result = main.cvd_endpoint(symbol="WLD-USD_UM_XPERP-310613", timeframe="5m", limit=50, source_scope="currency_aggregate")
    assert result["source_scope"] != "instrument"
    assert result["source_scope"] == "currency_aggregate"
    assert result["source_endpoint"] == "/api/v5/rubik/stat/taker-volume"


def test_cvd_endpoint_rejects_unknown_source_scope(fake_lake):
    with pytest.raises(HTTPException) as exc_info:
        main.cvd_endpoint(symbol="BTC-USDT-SWAP", timeframe="5m", limit=50, source_scope="whole_market")
    assert exc_info.value.status_code == 422


def _fake_trades_payload(trades: list[dict]) -> dict:
    return {"code": "0", "msg": "", "data": trades}


def test_cvd_endpoint_instrument_scope_session_and_schema(fake_lake, monkeypatch):
    """Task #265 test 1 + 7: instrument-scope contract/units/schema."""
    base = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    trades = [
        {"side": "buy", "sz": "2.0", "ts": str(int((base + timedelta(seconds=10)).timestamp() * 1000))},
        {"side": "sell", "sz": "1.0", "ts": str(int((base + timedelta(seconds=20)).timestamp() * 1000))},
    ]
    monkeypatch.setattr(main._okx_client, "get_trades", lambda inst_id, limit=100: _fake_trades_payload(trades))
    fixed_now = base + timedelta(minutes=10)

    class _FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now if tz else fixed_now.replace(tzinfo=None)

    monkeypatch.setattr(main, "datetime", _FixedDatetime)
    result = main.cvd_endpoint(symbol="WLD-USD_UM_XPERP-310613", timeframe="5m", limit=50, source_scope="instrument")
    assert result["source_scope"] == "instrument"
    assert result["source_endpoint"] == "/api/v5/market/trades"
    assert result["source_ccy"] is None
    assert result["source_inst_type"] is None
    assert result["units"] == "contract_size_per_bucket"
    assert result["resolved_instrument_id"] == "WLD-USD_UM_XPERP-310613"
    assert result["requested_symbol"] == "WLD-USD_UM_XPERP-310613"
    assert result["coverage_status"] in ("complete", "partial", "no_data")
    assert result["series"]
    assert result["series"][0]["value"] == pytest.approx(2.0 - 1.0)
    for field in ("raw_trade_count", "first_trade_time", "last_trade_time", "expected_close_time", "is_stale", "data_as_of", "estimated_coverage_seconds"):
        assert field in result
    # Task #266: estimated_coverage_seconds is the first-to-last-trade span
    # of the current ~500-trade pull (here: 10s to 20s after `base` == 10s).
    assert result["estimated_coverage_seconds"] == pytest.approx(10.0)


def test_cvd_endpoint_instrument_scope_partial_coverage_when_trades_limit_hit(fake_lake, monkeypatch):
    """Task #265 test 5: partial/gap coverage — hitting OKX's own trades
    cap must mark coverage_status=partial, never silently presented as
    complete."""
    base = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    trades = [
        {"side": "buy", "sz": "1.0", "ts": str(int((base + timedelta(seconds=i)).timestamp() * 1000))}
        for i in range(main._INSTRUMENT_TRADES_LIMIT)
    ]
    monkeypatch.setattr(main._okx_client, "get_trades", lambda inst_id, limit=100: _fake_trades_payload(trades))
    fixed_now = base + timedelta(minutes=10)

    class _FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now if tz else fixed_now.replace(tzinfo=None)

    monkeypatch.setattr(main, "datetime", _FixedDatetime)
    result = main.cvd_endpoint(symbol="BTC-USDT-SWAP", timeframe="5m", limit=50, source_scope="instrument")
    assert result["coverage_status"] == "partial"
    # Task #266: on a liquid pair the ~500-trade cap can be consumed in
    # seconds — estimated_coverage_seconds surfaces that span explicitly
    # (here: one trade per second across the full limit).
    assert result["estimated_coverage_seconds"] == pytest.approx(main._INSTRUMENT_TRADES_LIMIT - 1)


def test_cvd_endpoint_instrument_scope_no_data_when_tape_empty(fake_lake, monkeypatch):
    """Task #265 test 5: empty tape must surface coverage_status=no_data, not
    a silent empty-but-'complete' response."""
    monkeypatch.setattr(main._okx_client, "get_trades", lambda inst_id, limit=100: _fake_trades_payload([]))
    result = main.cvd_endpoint(symbol="BTC-USDT-SWAP", timeframe="5m", limit=50, source_scope="instrument")
    assert result["coverage_status"] == "no_data"
    assert result["series"] == []
    assert result["is_stale"] is None
    # Task #266: no trades at all means no span to estimate.
    assert result["estimated_coverage_seconds"] is None


def test_cvd_endpoint_instrument_scope_anchored_unknown_anchor_rejects(fake_lake, monkeypatch):
    """Task #265: instrument scope never silently falls back to
    currency_aggregate when the requested anchor isn't covered by the tape —
    explicit 422 instead."""
    base = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    trades = [{"side": "buy", "sz": "1.0", "ts": str(int((base + timedelta(seconds=10)).timestamp() * 1000))}]
    monkeypatch.setattr(main._okx_client, "get_trades", lambda inst_id, limit=100: _fake_trades_payload(trades))
    fixed_now = base + timedelta(minutes=10)

    class _FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now if tz else fixed_now.replace(tzinfo=None)

    monkeypatch.setattr(main, "datetime", _FixedDatetime)
    with pytest.raises(HTTPException) as exc_info:
        main.cvd_endpoint(
            symbol="BTC-USDT-SWAP", timeframe="5m", mode="anchored", anchor_time="2099-01-01T00:00:00Z",
            limit=50, source_scope="instrument",
        )
    assert exc_info.value.status_code == 422


def test_cvd_endpoint_instrument_scope_rejects_offset_minutes(fake_lake):
    """Task #265: offset_minutes has no meaning for the live-trade-tape
    instrument scope — must 422, not silently ignore the parameter."""
    with pytest.raises(HTTPException) as exc_info:
        main.cvd_endpoint(symbol="BTC-USDT-SWAP", timeframe="30m", limit=50, source_scope="instrument", offset_minutes=15)
    assert exc_info.value.status_code == 422


# ---------------------------------------------------------------------------
# offset_minutes plumbing at the endpoint layer
# ---------------------------------------------------------------------------


def test_bollinger_endpoint_offset_minutes_rejects_disallowed_value(fake_lake):
    with pytest.raises(HTTPException) as exc_info:
        main.bollinger_endpoint(symbol="BTC-USDT-SWAP", timeframe="30m", limit=10, offset_minutes=7)
    assert exc_info.value.status_code == 422


def test_ema_endpoint_offset_minutes_builds_synthetic_series(fake_lake):
    result = main.ema_endpoint(symbol="BTC-USDT-SWAP", timeframe="30m", period=21, limit=5, offset_minutes=15)
    assert result["series"]  # non-empty: synthetic 30m@offset15 built from the 5m fixture


# ---------------------------------------------------------------------------
# /api/available integration
# ---------------------------------------------------------------------------


def test_available_reports_new_computed_data_kinds(monkeypatch, tmp_path):
    fake_registry = {
        "ohlcv/BTC-USDT-SWAP/5m": "market-fake1",
        "taker_volume/BTC-USDT-SWAP/5m": "market-fake2",
    }
    monkeypatch.setattr(main, "_load_registry", lambda: fake_registry)

    class _FakeLake:
        def latest_observed_at(self, dataset_id):
            return "2026-08-09T06:00:00Z"

    monkeypatch.setattr(main, "_lake", lambda: _FakeLake())

    result = main.available()
    for kind in ("bollinger", "ema", "ema_projection", "vwap", "cvd"):
        assert kind in result["computed"], f"{kind} missing from /api/available computed section"
        assert "symbols" in result["computed"][kind]
        assert "freshness" in result["computed"][kind]

    # #255: freshness entries are {last_observed_at, checked_at, is_stale} objects.
    # bollinger/ema/ema_projection/vwap mirror ohlcv freshness; cvd mirrors taker_volume.
    assert result["computed"]["bollinger"]["freshness"]["BTC-USDT-SWAP"]["5m"]["last_observed_at"] == "2026-08-09T06:00:00Z"
    assert result["computed"]["cvd"]["freshness"]["BTC-USDT-SWAP"]["5m"]["last_observed_at"] == "2026-08-09T06:00:00Z"
