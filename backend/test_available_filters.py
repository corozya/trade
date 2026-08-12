"""Focused contract tests for filtered ``GET /api/available`` (#291)."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import main


FIXED_NOW = datetime(2026, 8, 10, 7, 0, tzinfo=timezone.utc)
DOGE = next(symbol for symbol in main.LAKE_SYMBOLS if symbol.startswith("DOGE-"))
BTC = next(symbol for symbol in main.LAKE_SYMBOLS if symbol.startswith("BTC-"))


class _FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return FIXED_NOW if tz is not None else FIXED_NOW.replace(tzinfo=None)


@pytest.fixture
def available_fixture(monkeypatch):
    registry = {
        f"ohlcv/{DOGE}/5m": "doge-ohlcv-5m",
        f"ohlcv/{DOGE}/1h": "doge-ohlcv-1h",
        f"atr/{DOGE}/5m": "doge-atr-5m",
        f"taker_volume/{DOGE}/5m": "doge-taker-5m",
        f"ohlcv/{BTC}/5m": "btc-ohlcv-5m",
        f"atr/{BTC}/5m": "btc-atr-5m",
    }
    observed = {
        "doge-ohlcv-5m": "2026-08-10T06:55:00Z",
        "doge-ohlcv-1h": "2026-08-10T06:00:00Z",
        "doge-atr-5m": "2026-08-10T06:55:00Z",
        "doge-taker-5m": "2026-08-10T06:50:00Z",
        "btc-ohlcv-5m": "2026-08-10T06:55:00Z",
        "btc-atr-5m": "2026-08-10T06:55:00Z",
    }
    calls = []

    class _FakeLake:
        def latest_observed_at(self, dataset_id):
            calls.append(dataset_id)
            return observed[dataset_id]

    monkeypatch.setattr(main, "_load_registry", lambda: registry)
    monkeypatch.setattr(main, "_lake", lambda: _FakeLake())
    monkeypatch.setattr(main, "datetime", _FrozenDateTime)
    return registry, calls


def test_data_kinds_parser_trims_and_deduplicates():
    assert main._parse_available_data_kinds("ohlcv, atr,ohlcv") == ["ohlcv", "atr"]


@pytest.mark.parametrize(
    ("kwargs", "parameter"),
    [
        ({"symbol": "NOT-A-SYMBOL"}, "symbol"),
        ({"data_kinds": "ohlcv,not_a_kind"}, "data_kinds"),
        ({"data_kinds": ", ,"}, "data_kinds"),
    ],
)
def test_available_validation_is_deterministic_422(kwargs, parameter):
    with pytest.raises(HTTPException) as exc_info:
        main.available(**kwargs)
    assert exc_info.value.status_code == 422
    assert parameter in exc_info.value.detail


def test_filtered_registry_matches_full_fragments_and_limits_io(available_fixture):
    _, calls = available_fixture
    full = main.available()
    calls.clear()

    filtered = main.available(symbol="DOGE", data_kinds="ohlcv,atr")

    assert set(filtered) == {"ohlcv", "atr", "freshness", "computed", "missing_data"}
    assert filtered["computed"] == {}
    assert filtered["missing_data"] == []
    assert filtered["ohlcv"] == {DOGE: full["ohlcv"][DOGE]}
    assert filtered["atr"] == {DOGE: full["atr"][DOGE]}
    assert filtered["freshness"]["ohlcv"] == {DOGE: full["freshness"]["ohlcv"][DOGE]}
    assert filtered["freshness"]["atr"] == {DOGE: full["freshness"]["atr"][DOGE]}
    assert calls == ["doge-ohlcv-5m", "doge-ohlcv-1h", "doge-atr-5m"]


def test_http_query_contract(available_fixture):
    response = TestClient(main.app).get(
        "/api/available", params={"symbol": "doge", "data_kinds": "ohlcv,atr"}
    )
    assert response.status_code == 200
    assert response.json()["ohlcv"] == {DOGE: ["1h", "5m"]}


def test_base_and_instrument_id_are_equivalent(available_fixture):
    by_base = main.available(symbol="DOGE", data_kinds="ohlcv,atr")
    by_instrument = main.available(symbol=DOGE, data_kinds="ohlcv,atr")
    assert by_base == by_instrument


def test_computed_kind_uses_only_backing_freshness_and_keeps_schema(available_fixture):
    _, calls = available_fixture
    result = main.available(symbol="DOGE", data_kinds="ema,cvd")

    assert set(result["computed"]) == {"ema", "cvd"}
    assert result["computed"]["ema"]["symbols"] == [DOGE]
    assert result["computed"]["ema"]["freshness"][DOGE]["5m"]["is_stale"] is False
    assert result["computed"]["cvd"]["freshness"][DOGE]["5m"]["is_stale"] is False
    assert result["freshness"] == {}
    assert calls == ["doge-ohlcv-5m", "doge-ohlcv-1h", "doge-taker-5m"]


def test_supported_kind_without_series_is_explicit_not_500(available_fixture):
    result = main.available(symbol="DOGE", data_kinds="macd")
    assert result["macd"] == {DOGE: []}
    assert result["freshness"]["macd"] == {DOGE: {}}
    assert result["missing_data"] == [
        {"data_kind": "macd", "symbol": DOGE, "status": "no_data"}
    ]


def test_non_file_source_failure_is_propagated(monkeypatch):
    monkeypatch.setattr(main, "_load_registry", lambda: {f"ohlcv/{DOGE}/5m": "broken"})

    class _BrokenLake:
        def latest_observed_at(self, dataset_id):
            raise RuntimeError("duckdb read failed")

    monkeypatch.setattr(main, "_lake", lambda: _BrokenLake())
    with pytest.raises(RuntimeError, match="duckdb read failed"):
        main.available(symbol="DOGE", data_kinds="ohlcv")


def test_missing_file_preserves_freshness_schema(monkeypatch):
    monkeypatch.setattr(main, "_load_registry", lambda: {f"ohlcv/{DOGE}/5m": "missing"})

    class _MissingLake:
        def latest_observed_at(self, dataset_id):
            raise FileNotFoundError(dataset_id)

    monkeypatch.setattr(main, "_lake", lambda: _MissingLake())
    result = main.available(symbol="DOGE", data_kinds="ohlcv")
    entry = result["freshness"]["ohlcv"][DOGE]["5m"]
    assert entry["last_observed_at"] is None
    assert entry["is_stale"] is None


def test_filtered_payload_budget(available_fixture):
    result = main.available(
        symbol="DOGE",
        data_kinds=(
            "ohlcv,atr,rsi,macd,stochastic,risk_indicator,support_resistance,"
            "open_interest,taker_volume,long_short_ratio"
        ),
    )
    assert len(json.dumps(result, separators=(",", ":")).encode()) <= 15_000


def test_unfiltered_contract_has_no_filter_only_metadata(available_fixture):
    result = main.available()
    assert "missing_data" not in result
    assert set(result["computed"]) == set(main._COMPUTED_DATA_SOURCES)
    assert result["ohlcv"][DOGE] == ["1h", "5m"]
