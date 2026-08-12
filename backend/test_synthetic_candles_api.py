"""REST contract and error mapping tests for #264 synthetic candles."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import main


INSTRUMENT = "WLD-USD_UM_XPERP-310613"
START = datetime(2026, 8, 10, 8, 0, tzinfo=timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _row(hour: int, o: float, h: float, l: float, c: float, v: float = 1000.0) -> dict:
    return {
        "observed_at": _iso(START + timedelta(hours=hour)),
        "open": o, "high": h, "low": l, "close": c, "volume": v,
    }


ROWS = [
    _row(0, 0.325, 0.327, 0.324, 0.326),
    _row(1, 0.326, 0.329, 0.325, 0.328),
    _row(2, 0.328, 0.330, 0.3265, 0.327),
    _row(3, 0.327, 0.328, 0.323, 0.324),
]


class _FrozenDatetime(datetime):
    current = START + timedelta(hours=4)

    @classmethod
    def now(cls, tz=None):
        value = cls.current
        return value if tz is None else value.astimezone(tz)


@pytest.fixture
def client(monkeypatch):
    _FrozenDatetime.current = START + timedelta(hours=4)
    monkeypatch.setattr(main, "datetime", _FrozenDatetime)
    monkeypatch.setattr(main, "_read_series", lambda *args, **kwargs: list(ROWS))
    monkeypatch.setattr(main, "_live_provisional_row", lambda *args, **kwargs: None)
    return TestClient(main.app)


def test_rest_contract_wld_h3_0800_anchor_and_freshness(client):
    response = client.get("/api/synthetic_candles", params={
        "symbol": "WLD", "base_timeframe": "1h", "count": 3,
        "anchor_time": _iso(START),
    })
    assert response.status_code == 200
    body = response.json()
    assert body["instrument_id"] == INSTRUMENT
    assert body["symbol"] == "WLD"
    assert body["open_time"] == _iso(START)
    assert body["close_time"] == _iso(START + timedelta(hours=3))
    assert body["is_closed"] is True
    assert isinstance(body["freshness_seconds"], (int, float))
    assert body["freshness_seconds"] >= 0
    assert body["data_as_of"] == ROWS[-1]["observed_at"]
    assert body["computed_at"].endswith("Z")
    assert body["is_stale"] is False


def test_closed_only_appears_at_confirm_without_historical_repaint(client):
    params = {"symbol": "WLD", "count": 3, "anchor_time": _iso(START)}
    _FrozenDatetime.current = START + timedelta(hours=2, minutes=59)
    before = client.get("/api/synthetic_candles", params=params)
    assert before.status_code == 409
    assert "not closed yet" in before.json()["detail"]

    _FrozenDatetime.current = START + timedelta(hours=3)
    after = client.get("/api/synthetic_candles", params=params)
    assert after.status_code == 200
    assert {key: after.json()[key] for key in ("open", "high", "low", "close", "volume")} == {
        "open": 0.325, "high": 0.330, "low": 0.324, "close": 0.327, "volume": 3000.0,
    }


def test_provisional_contract_uses_only_final_live_h1(client, monkeypatch):
    _FrozenDatetime.current = START + timedelta(hours=2, minutes=30)
    monkeypatch.setattr(main, "_read_series", lambda *args, **kwargs: list(ROWS[:2]))
    monkeypatch.setattr(main, "_live_provisional_row", lambda *args, **kwargs: ROWS[2])
    response = client.get("/api/synthetic_candles", params={
        "symbol": "WLD", "count": 3, "anchor_time": _iso(START),
        "closed_only": "false", "include_provisional": "true",
    })
    assert response.status_code == 200
    body = response.json()
    assert body["is_closed"] is False
    assert body["close_time"] == _iso(START + timedelta(hours=3))
    assert body["constituents"][:2] == [
        {"open_time": _iso(START), "close_time": _iso(START + timedelta(hours=1)), "is_closed": True},
        {"open_time": _iso(START + timedelta(hours=1)), "close_time": _iso(START + timedelta(hours=2)), "is_closed": True},
    ]


def test_instrument_identity_never_maps_explicit_wld_swap_to_xperp(client):
    xperp = client.get("/api/synthetic_candles", params={
        "symbol": INSTRUMENT, "count": 3, "anchor_time": _iso(START),
    })
    swap = client.get("/api/synthetic_candles", params={
        "symbol": "WLD-USDT-SWAP", "count": 3, "anchor_time": _iso(START),
    })
    assert xperp.status_code == 200
    assert xperp.json()["instrument_id"] == INSTRUMENT
    assert swap.status_code == 404
    assert "unrecognized symbol/instrument" in swap.json()["detail"]


@pytest.mark.parametrize(
    ("params", "status"),
    [
        ({"symbol": "WLD", "count": 2, "anchor_time": _iso(START)}, 400),
        ({"symbol": "WLD", "count": 3, "anchor_time": "2026-08-10T08:30:00Z"}, 400),
        ({"symbol": "WLD", "count": "not-an-int", "anchor_time": _iso(START)}, 422),
    ],
)
def test_parameter_errors_have_fastapi_detail_schema(client, params, status):
    response = client.get("/api/synthetic_candles", params=params)
    assert response.status_code == status
    assert "detail" in response.json()


def test_gap_maps_to_409_and_never_returns_two_of_three(client, monkeypatch):
    monkeypatch.setattr(main, "_read_series", lambda *args, **kwargs: [ROWS[0], ROWS[2]])
    response = client.get("/api/synthetic_candles", params={
        "symbol": "WLD", "count": 3, "anchor_time": _iso(START),
    })
    assert response.status_code == 409
    assert "missing constituent" in response.json()["detail"]
    assert "open" not in response.json()


def test_stale_base_data_maps_to_503_with_last_available_time(client, monkeypatch):
    _FrozenDatetime.current = START + timedelta(hours=12)
    stale = [_row(0, 0.325, 0.327, 0.324, 0.326)]
    monkeypatch.setattr(main, "_read_series", lambda *args, **kwargs: stale)
    response = client.get("/api/synthetic_candles", params={
        "symbol": "WLD", "count": 3, "anchor_time": _iso(START),
    })
    assert response.status_code == 503
    detail = response.json()["detail"]
    assert "stale" in detail["message"]
    assert detail["last_available_time"] == stale[-1]["observed_at"]


def test_doji_ratios_are_json_null(client, monkeypatch):
    doji = [
        _row(0, 0.325, 0.327, 0.324, 0.326),
        _row(1, 0.326, 0.329, 0.325, 0.328),
        _row(2, 0.328, 0.330, 0.323, 0.325),
    ]
    monkeypatch.setattr(main, "_read_series", lambda *args, **kwargs: doji)
    response = client.get("/api/synthetic_candles", params={
        "symbol": "WLD", "count": 3, "anchor_time": _iso(START),
    })
    assert response.status_code == 200
    body = response.json()
    assert body["is_doji"] is True
    assert body["upper_wick_to_body"] is None
    assert body["lower_wick_to_body"] is None
