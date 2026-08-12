"""Testy backfillu Open Interest (#164): adapter rubik/stat/contracts/open-interest-volume,
dwutorowy model 1D wstecz + 5m ogon bieżący, oraz multi-symbol as-of join
(CryptoDataLake.read_multi_symbol_as_of_duckdb, #164 PM addendum).

Zero realnych wywołań sieciowych — OkxClient mockowany przez httpx.MockTransport,
analogicznie do test_okx_client.py / test_crypto_backfill.py.
"""
from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest

from services.crypto_data_lake import CryptoDataLake
from services.okx_client import OkxClient

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from crypto_backfill_open_interest import (  # noqa: E402
    OpenInterestHistoryAdapter,
    _symbol_to_ccy,
    _to_okx_period,
)

ALIAS = "test_alias"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for suffix in ("API_KEY", "API_SECRET", "API_PASSPHRASE"):
        monkeypatch.delenv(f"OKX_{ALIAS.upper()}_{suffix}", raising=False)


def _set_creds(monkeypatch):
    monkeypatch.setenv(f"OKX_{ALIAS.upper()}_API_KEY", "key")
    monkeypatch.setenv(f"OKX_{ALIAS.upper()}_API_SECRET", "secret")


def _make_client(handler, **kwargs) -> OkxClient:
    transport = httpx.MockTransport(handler)
    http_client = httpx.Client(base_url="https://my.okx.com", transport=transport)
    return OkxClient(ALIAS, http_client=http_client, **kwargs)


# -- helpers ------------------------------------------------------------------


def test_symbol_to_ccy_strips_usdt_swap_suffix():
    assert _symbol_to_ccy("BTC-USDT-SWAP") == "BTC"
    assert _symbol_to_ccy("DOGE-USDT-SWAP") == "DOGE"


def test_to_okx_period_maps_lowercase_timeframe_to_okx_bar():
    assert _to_okx_period("1d") == "1D"
    assert _to_okx_period("1h") == "1H"
    assert _to_okx_period("5m") == "5m"


# -- OpenInterestHistoryAdapter ------------------------------------------------


def test_adapter_fetches_single_page_and_stops_before_since(monkeypatch):
    _set_creds(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v5/rubik/stat/contracts/open-interest-volume"
        assert "ccy=BTC" in str(request.url)
        assert "period=1D" in str(request.url)
        return httpx.Response(
            200,
            json={
                "code": "0",
                "msg": "",
                "data": [
                    ["1735689600000", "500000.0", "12000.0"],  # 2025-01-01T00:00:00Z
                    ["1735603200000", "480000.0", "11000.0"],  # 2024-12-31T00:00:00Z (older, before since)
                ],
            },
        )

    client = _make_client(handler)
    since = datetime(2024, 12, 31, 12, tzinfo=timezone.utc)
    adapter = OpenInterestHistoryAdapter(client, symbol="BTC-USDT-SWAP", timeframe="1d", since=since)

    rows = list(adapter.fetch())

    assert len(rows) == 1
    row = rows[0]
    assert row["symbol"] == "BTC-USDT-SWAP"
    assert row["timeframe"] == "1d"
    assert row["data_kind"] == "open_interest"
    assert row["open_interest"] == 500000.0
    assert row["volume"] == 12000.0
    # Point sample, not a candle: available_at must equal observed_at.
    assert row["available_at"] == row["observed_at"]


def test_adapter_raises_system_exit_when_no_new_points(monkeypatch):
    _set_creds(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"code": "0", "msg": "", "data": [["1735689600000", "500000.0", "12000.0"]]},
        )

    client = _make_client(handler)
    since = datetime(2026, 1, 1, tzinfo=timezone.utc)  # after the only point returned
    adapter = OpenInterestHistoryAdapter(client, symbol="BTC-USDT-SWAP", timeframe="1d", since=since)

    with pytest.raises(SystemExit):
        list(adapter.fetch())


def test_adapter_paginates_across_pages_via_after_cursor(monkeypatch):
    _set_creds(monkeypatch)
    calls = {"n": 0, "afters": []}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        after = httpx.QueryParams(request.url.query.decode()).get("after")
        calls["afters"].append(after)
        if calls["n"] == 1:
            # Exactly 100 points -> adapter must request a second page.
            data = [[str(1735689600000 - i * 300000), "1.0", "1.0"] for i in range(100)]
            return httpx.Response(200, json={"code": "0", "msg": "", "data": data})
        return httpx.Response(200, json={"code": "0", "msg": "", "data": []})

    client = _make_client(handler)
    since = datetime(2000, 1, 1, tzinfo=timezone.utc)
    adapter = OpenInterestHistoryAdapter(client, symbol="BTC-USDT-SWAP", timeframe="5m", since=since)

    rows = list(adapter.fetch())

    assert calls["n"] == 2
    assert calls["afters"][0] is None
    assert calls["afters"][1] is not None
    assert len(rows) == 100


def test_adapter_lineage_reports_endpoint_and_since():
    client = OkxClient(ALIAS)
    since = datetime(2025, 1, 1, tzinfo=timezone.utc)
    adapter = OpenInterestHistoryAdapter(client, symbol="ETH-USDT-SWAP", timeframe="1d", since=since)

    lineage = adapter.lineage()

    assert lineage["endpoint"] == "/api/v5/rubik/stat/contracts/open-interest-volume"
    assert lineage["symbol"] == "ETH-USDT-SWAP"
    assert lineage["since"] == "2025-01-01T00:00:00Z"


# -- multi-symbol as-of join (#164 PM addendum) --------------------------------


def _oi_row(symbol, minute, oi, **extra):
    ts = f"2026-01-01T00:{minute:02d}:00Z"
    return {
        "symbol": symbol,
        "timeframe": "5m",
        "data_kind": "open_interest",
        "observed_at": ts,
        "available_at": ts,
        "source": "okx-open-interest-volume",
        "open_interest": oi,
        "volume": 100.0,
        **extra,
    }


def test_read_multi_symbol_as_of_duckdb_joins_two_symbols_at_matching_timestamps(tmp_path):
    lake = CryptoDataLake(tmp_path)
    version = lake.publish(
        [
            _oi_row("BTC-USDT-SWAP", 0, 500000.0),
            _oi_row("BTC-USDT-SWAP", 5, 510000.0),
            _oi_row("ETH-USDT-SWAP", 0, 200000.0),
            _oi_row("ETH-USDT-SWAP", 5, 205000.0),
        ],
        lineage={"sources": [{"name": "okx-open-interest-volume"}]},
    )

    joined = lake.read_multi_symbol_as_of_duckdb(
        version.dataset_id,
        "2026-01-01T00:10:00Z",
        data_kind="open_interest",
        timeframe="5m",
        symbols=("BTC-USDT-SWAP", "ETH-USDT-SWAP"),
        value_column="open_interest",
    )
    rows = joined.to_pylist()

    assert len(rows) == 2
    last = rows[-1]
    assert last["open_interest_btc_usdt_swap"] == 510000.0
    assert last["open_interest_eth_usdt_swap"] == 205000.0


def test_read_multi_symbol_as_of_duckdb_respects_as_of_cutoff(tmp_path):
    lake = CryptoDataLake(tmp_path)
    version = lake.publish(
        [
            _oi_row("BTC-USDT-SWAP", 0, 500000.0),
            _oi_row("BTC-USDT-SWAP", 5, 999999.0),  # in the "future" relative to the cutoff below
        ],
        lineage={"sources": [{"name": "okx-open-interest-volume"}]},
    )

    joined = lake.read_multi_symbol_as_of_duckdb(
        version.dataset_id,
        "2026-01-01T00:02:00Z",  # before the second point's available_at
        data_kind="open_interest",
        timeframe="5m",
        symbols=("BTC-USDT-SWAP",),
        value_column="open_interest",
    )
    rows = joined.to_pylist()

    assert len(rows) == 1
    assert rows[0]["open_interest_btc_usdt_swap"] == 500000.0
