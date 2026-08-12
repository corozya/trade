"""Testy skryptu fetch_crypto_market_data.py (#78): fetch per-symbol, zapis JSON,
odporność na częściowy błąd sekcji. Zero realnych wywołań sieciowych.
"""
import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import fetch_crypto_market_data as fcmd
from services import okx_client as ok

ALIAS = "test_alias"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for suffix in ("API_KEY", "API_SECRET", "API_PASSPHRASE"):
        monkeypatch.delenv(f"OKX_{ALIAS.upper()}_{suffix}", raising=False)


def _set_creds(monkeypatch, alias=ALIAS):
    monkeypatch.setenv(f"OKX_{alias.upper()}_API_KEY", "k")
    monkeypatch.setenv(f"OKX_{alias.upper()}_API_SECRET", "s")


def _make_client(handler, alias=ALIAS, **kwargs):
    transport = httpx.MockTransport(handler)
    http_client = httpx.Client(transport=transport, base_url=ok.OKX_BASE_URL)
    return ok.OkxClient(alias, http_client=http_client, **kwargs)


def _ok_json(data):
    return httpx.Response(200, json={"code": "0", "msg": "", "data": data})


def test_fetch_symbol_all_sections_success(monkeypatch):
    _set_creds(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path in ("/api/v5/market/candles", "/api/v5/market/history-candles"):
            return _ok_json([["1", "2", "3", "4", "5"]])
        if path == "/api/v5/market/books":
            return _ok_json([{"bids": [], "asks": []}])
        if path == "/api/v5/public/funding-rate":
            return _ok_json([{"fundingRate": "0.0001"}])
        if path == "/api/v5/public/open-interest":
            return _ok_json([{"oi": "123"}])
        raise AssertionError(f"unexpected path {path}")

    client = _make_client(handler, max_read_attempts=1, retry_backoff_seconds=0)
    payload = fcmd.fetch_symbol(client, "BTC-USD-SWAP")

    assert payload["inst_id"] == "BTC-USD-SWAP"
    assert set(payload["candles"].keys()) == {"15m", "5m", "1H", "4H"}
    assert all(v["ok"] for v in payload["candles"].values())
    assert payload["orderbook"]["ok"] is True
    assert payload["funding_rate"]["ok"] is True
    assert payload["open_interest"]["ok"] is True


def test_fetch_symbol_partial_failure_does_not_raise(monkeypatch):
    _set_creds(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path in ("/api/v5/market/candles", "/api/v5/market/history-candles"):
            return httpx.Response(500, json={"code": "51000", "msg": "boom", "data": []})
        if path == "/api/v5/market/books":
            return _ok_json([{"bids": [], "asks": []}])
        if path == "/api/v5/public/funding-rate":
            return _ok_json([{"fundingRate": "0.0001"}])
        if path == "/api/v5/public/open-interest":
            return _ok_json([{"oi": "123"}])
        raise AssertionError(f"unexpected path {path}")

    client = _make_client(handler, max_read_attempts=1, retry_backoff_seconds=0)
    payload = fcmd.fetch_symbol(client, "BTC-USD-SWAP")

    assert all(not v["ok"] for v in payload["candles"].values())
    assert "error" in payload["candles"]["15m"]
    assert payload["orderbook"]["ok"] is True


def test_main_writes_json_files_and_resolves_inst_id(monkeypatch, tmp_path):
    _set_creds(monkeypatch)
    monkeypatch.setattr(fcmd, "DATA_DIR", tmp_path)
    monkeypatch.setattr(fcmd, "SYMBOLS", ["BTC"])

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v5/public/instruments":
            return _ok_json([{"instId": "BTC-USD_UM_XPERP-310328", "state": "live"}])
        if path in ("/api/v5/market/candles", "/api/v5/market/history-candles"):
            return _ok_json([["1"]])
        if path == "/api/v5/market/books":
            return _ok_json([{"bids": [], "asks": []}])
        if path == "/api/v5/public/funding-rate":
            return _ok_json([{"fundingRate": "0"}])
        if path == "/api/v5/public/open-interest":
            return _ok_json([{"oi": "1"}])
        raise AssertionError(f"unexpected path {path}")

    transport = httpx.MockTransport(handler)
    http_client = httpx.Client(transport=transport, base_url=ok.OKX_BASE_URL)
    monkeypatch.setattr(
        fcmd, "OkxClient",
        lambda alias, simulated_trading=False: ok.OkxClient(alias, http_client=http_client),
    )
    monkeypatch.setattr(sys, "argv", ["fetch_crypto_market_data.py", ALIAS])

    exit_code = fcmd.main()
    assert exit_code == 0

    out_file = tmp_path / "BTC_latest.json"
    assert out_file.exists()
    data = json.loads(out_file.read_text())
    assert data["inst_id"] == "BTC-USD_UM_XPERP-310328"


def test_main_missing_credentials_returns_1(monkeypatch, tmp_path):
    monkeypatch.setattr(fcmd, "DATA_DIR", tmp_path)
    monkeypatch.setattr(fcmd, "SYMBOLS", ["BTC", "ETH"])
    monkeypatch.setattr(sys, "argv", ["fetch_crypto_market_data.py", "nonexistent_alias_xyz"])

    exit_code = fcmd.main()
    assert exit_code == 1
    assert list(tmp_path.glob("*.json")) == []
