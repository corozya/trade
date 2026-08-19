"""API-contract tests for GET /api/support_resistance (analyst brief 2026-08-16).

Same convention as test_analysis_snapshot_api.py: imports `main` directly and
calls the endpoint FUNCTION, monkeypatching main._read_series (the single
lake-reading chokepoint) with deterministic fixtures — no real lake I/O.

Scope: the decision-ready extension (reference_price, price_position,
nearest_resistances/supports) and that `zones` stays byte-for-byte identical to
the pre-extension #233 contract.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

import main
from sr_levels import price_position_summary


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


START = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
SYMBOL = "BTC-USDT-SWAP"
BASE = _iso(START)


def _sr_events() -> list[dict]:
    # Two supports (98/97, 90/89) and two resistances (106/105, 112/111).
    return [
        {"level_id": "sup-1", "level_type": "support", "price_top": 98.0, "price_bottom": 97.0,
         "status": "holding", "volume": 500.0, "touch_count": 3,
         "created_at": BASE, "last_touched_at": BASE, "event": "created", "observed_at": BASE},
        {"level_id": "sup-2", "level_type": "support", "price_top": 90.0, "price_bottom": 89.0,
         "status": "holding", "volume": 200.0, "touch_count": 1,
         "created_at": BASE, "last_touched_at": BASE, "event": "created", "observed_at": BASE},
        {"level_id": "res-1", "level_type": "resistance", "price_top": 106.0, "price_bottom": 105.0,
         "status": "holding", "volume": 400.0, "touch_count": 2,
         "created_at": BASE, "last_touched_at": BASE, "event": "created", "observed_at": BASE},
        {"level_id": "res-2", "level_type": "resistance", "price_top": 112.0, "price_bottom": 111.0,
         "status": "holding", "volume": 150.0, "touch_count": 1,
         "created_at": BASE, "last_touched_at": BASE, "event": "created", "observed_at": BASE},
    ]


def _ohlcv(close: float, *, n: int = 3) -> list[dict]:
    rows = []
    for i in range(n):
        t = START + timedelta(hours=i)
        c = close if i == n - 1 else close - 1.0
        rows.append({"observed_at": _iso(t), "open": c - 0.1, "high": c + 0.3,
                     "low": c - 0.3, "close": c, "volume": 10.0})
    return rows


@pytest.fixture
def fake_lake(monkeypatch):
    store: dict[tuple[str, str], list[dict]] = {
        ("support_resistance", "1h"): _sr_events(),
        ("ohlcv", "1h"): _ohlcv(100.0),  # price 100: between sup 98 and res 105
    }

    def fake_read_series(data_kind, symbol, timeframe, limit):
        key = (data_kind, timeframe)
        if key not in store:
            raise HTTPException(status_code=404, detail=f"no data for {key}")
        rows = store[key]
        return rows[-limit:] if limit else rows

    monkeypatch.setattr(main, "_read_series", fake_read_series)
    return store


# --- backward compatibility: zones unchanged ------------------------------


_LEGACY_ZONE_KEYS = {"price_top", "price_bottom", "type", "status", "volume",
                     "touch_count", "created_at", "last_touched_at"}


def test_zones_field_matches_legacy_contract(fake_lake):
    resp = main.support_resistance_endpoint(SYMBOL, "1h")
    assert isinstance(resp, dict) and "zones" in resp
    zones = resp["zones"]
    # highest price_top first (unchanged #233 ordering)
    assert [z["price_top"] for z in zones] == [112.0, 106.0, 98.0, 90.0]
    for z in zones:
        assert set(z.keys()) == _LEGACY_ZONE_KEYS


# --- reference_price -------------------------------------------------------


def test_reference_price_is_last_closed_ohlcv_close(fake_lake):
    resp = main.support_resistance_endpoint(SYMBOL, "1h")
    rp = resp["reference_price"]
    assert rp["value"] == 100.0
    assert rp["source"] == "ohlcv_close"
    assert rp["timeframe"] == "1h"
    assert rp["candle_time"] == _iso(START + timedelta(hours=2))  # last row


def test_reference_price_none_when_no_ohlcv(fake_lake):
    del fake_lake[("ohlcv", "1h")]
    resp = main.support_resistance_endpoint(SYMBOL, "1h")
    assert resp["reference_price"] is None
    assert "price_position" not in resp  # no position block without a price


# --- price_position + nearest_* -------------------------------------------


def test_between_zones_nearest_both_sides(fake_lake):
    # price 100 sits between support top 98 and resistance bottom 105
    resp = main.support_resistance_endpoint(SYMBOL, "1h", near=3)
    assert resp["price_position"] == {"status": "between_zones", "zone_index": None}

    # zones order: [112/111(0), 106/105(1), 98/97(2), 90/89(3)]
    res = resp["nearest_resistances"]
    assert [r["zone_index"] for r in res] == [1, 0]  # 105 closer than 111
    assert res[0]["distance_abs"] == pytest.approx(5.0)   # 105 - 100
    assert res[0]["distance_pct"] == pytest.approx(5.0)
    assert res[1]["distance_abs"] == pytest.approx(11.0)  # 111 - 100

    sup = resp["nearest_supports"]
    assert [s["zone_index"] for s in sup] == [2, 3]  # 98 closer than 90
    assert sup[0]["distance_abs"] == pytest.approx(-2.0)  # 98 - 100
    assert sup[0]["distance_pct"] == pytest.approx(-2.0)


def test_near_caps_per_side(fake_lake):
    resp = main.support_resistance_endpoint(SYMBOL, "1h", near=1)
    assert len(resp["nearest_resistances"]) == 1
    assert len(resp["nearest_supports"]) == 1
    assert resp["nearest_resistances"][0]["zone_index"] == 1


def test_near_zero_skips_position_block(fake_lake):
    resp = main.support_resistance_endpoint(SYMBOL, "1h", near=0)
    assert resp["reference_price"] is not None
    assert "price_position" not in resp
    assert "nearest_resistances" not in resp


def test_near_negative_is_422(fake_lake):
    with pytest.raises(HTTPException) as exc:
        main.support_resistance_endpoint(SYMBOL, "1h", near=-1)
    assert exc.value.status_code == 422


def test_above_all_zones_empty_resistances(fake_lake):
    fake_lake[("ohlcv", "1h")] = _ohlcv(200.0)  # above every zone
    resp = main.support_resistance_endpoint(SYMBOL, "1h")
    assert resp["price_position"]["status"] == "above_all_zones"
    assert resp["nearest_resistances"] == []
    assert len(resp["nearest_supports"]) == 3  # capped at near


def test_below_all_zones_empty_supports(fake_lake):
    fake_lake[("ohlcv", "1h")] = _ohlcv(50.0)  # below every zone
    resp = main.support_resistance_endpoint(SYMBOL, "1h")
    assert resp["price_position"]["status"] == "below_all_zones"
    assert resp["nearest_supports"] == []
    assert len(resp["nearest_resistances"]) == 3


def test_inside_support_zone(fake_lake):
    fake_lake[("ohlcv", "1h")] = _ohlcv(97.5)  # inside 98/97 support band
    resp = main.support_resistance_endpoint(SYMBOL, "1h")
    pos = resp["price_position"]
    assert pos["status"] == "inside_support_zone"
    assert resp["zones"][pos["zone_index"]]["type"] == "support"
    # the zone price is inside is excluded from nearest lists
    assert pos["zone_index"] not in [r["zone_index"] for r in resp["nearest_resistances"]]
    assert pos["zone_index"] not in [s["zone_index"] for s in resp["nearest_supports"]]


def test_inside_resistance_zone(fake_lake):
    fake_lake[("ohlcv", "1h")] = _ohlcv(105.5)  # inside 106/105 resistance band
    resp = main.support_resistance_endpoint(SYMBOL, "1h")
    assert resp["price_position"]["status"] == "inside_resistance_zone"


# --- pure helper edge cases -----------------------------------------------


def test_summary_empty_zone_map():
    out = price_position_summary([], 100.0, near=3)
    assert out["price_position"] == {"status": "between_zones", "zone_index": None}
    assert out["nearest_resistances"] == []
    assert out["nearest_supports"] == []


def test_extras_payload_is_small_relative_to_zones():
    # sanity on the "extras << zones" claim: build 68 zones, the position block
    # must stay a small fraction of the zones payload.
    import json

    zones = []
    for i in range(68):
        top = 50.0 + i
        zones.append({"price_top": top, "price_bottom": top - 0.5, "type": "support" if top < 100 else "resistance",
                      "status": "holding", "volume": 100.0, "touch_count": 1,
                      "created_at": BASE, "last_touched_at": BASE})
    summary = price_position_summary(zones, 100.0, near=3)
    extras_bytes = len(json.dumps({k: summary[k] for k in ("price_position", "nearest_resistances", "nearest_supports")}))
    zones_bytes = len(json.dumps(zones))
    assert extras_bytes < 0.05 * zones_bytes
