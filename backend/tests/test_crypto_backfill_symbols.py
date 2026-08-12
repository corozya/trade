"""Tests for the shared crypto backfill symbol configuration and CLI helper."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


BACKEND_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_ROOT))
sys.path.insert(0, str(BACKEND_ROOT / "scripts"))

import crypto_backfill_symbols as symbol_cli  # noqa: E402
from services.crypto_data_lake import _load_backfill_symbols  # noqa: E402


def _write_config(path: Path, symbols: list[str]) -> None:
    path.write_text(
        json.dumps({"schema_version": 1, "symbols": symbols}), encoding="utf-8"
    )


class _FakeClient:
    def __init__(self, payload: dict | None = None, error: Exception | None = None):
        self.payload = payload
        self.error = error
        self.requests: list[str] = []

    def get_instruments(self, instrument_type: str):
        self.requests.append(instrument_type)
        if self.error is not None:
            raise self.error
        return self.payload


def _instrument(symbol: str, **overrides: str) -> dict[str, str]:
    row = {"instId": symbol, "instType": "SWAP", "state": "live"}
    row.update(overrides)
    return row


def test_load_backfill_symbols_preserves_valid_order(tmp_path):
    config = tmp_path / "symbols.json"
    expected = ["ETH-USDT-SWAP", "BTC-USDT-SWAP"]
    _write_config(config, expected)

    assert _load_backfill_symbols(config) == tuple(expected)


@pytest.mark.parametrize(
    "payload, message",
    [
        ({"schema_version": 2, "symbols": ["BTC-USDT-SWAP"]}, "schema_version"),
        ({"schema_version": 1, "symbols": []}, "non-empty"),
        (
            {"schema_version": 1, "symbols": ["BTC-USDT-SWAP", "BTC-USDT-SWAP"]},
            "unique",
        ),
        ({"schema_version": 1, "symbols": ["btc/usdt"]}, "canonical"),
    ],
)
def test_load_backfill_symbols_rejects_invalid_config(tmp_path, payload, message):
    config = tmp_path / "symbols.json"
    config.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        _load_backfill_symbols(config)


@pytest.mark.parametrize(
    "symbol, payload, expected_type",
    [
        ("ETH-USDT-SWAP", {"data": [_instrument("ETH-USDT-SWAP")]}, "SWAP"),
        (
            "WLD-USD_UM_XPERP-310613",
            {
                "data": [
                    _instrument(
                        "WLD-USD_UM_XPERP-310613",
                        instType="FUTURES",
                        instFamily="WLD-USD_UM_XPERP",
                    )
                ]
            },
            "FUTURES",
        ),
    ],
)
def test_add_symbol_uses_fake_api_and_replaces_config_atomically(
    tmp_path, monkeypatch, capsys, symbol, payload, expected_type
):
    config = tmp_path / "symbols.json"
    _write_config(config, ["BTC-USDT-SWAP"])
    client = _FakeClient(payload)
    real_replace = symbol_cli.os.replace
    replace_calls: list[tuple[Path, Path]] = []

    def tracked_replace(source, destination):
        replace_calls.append((Path(source), Path(destination)))
        real_replace(source, destination)

    monkeypatch.setattr(symbol_cli.os, "replace", tracked_replace)

    assert symbol_cli.add_symbol(symbol.lower(), config_path=config, client=client) == 0
    result = json.loads(capsys.readouterr().out)

    assert result == {"status": "added", "symbol": symbol, "exit_code": 0}
    assert client.requests == [expected_type]
    assert _load_backfill_symbols(config) == ("BTC-USDT-SWAP", symbol)
    assert len(replace_calls) == 1
    temporary, destination = replace_calls[0]
    assert temporary.parent == config.parent
    assert destination == config
    assert not temporary.exists()


def test_add_duplicate_leaves_config_unchanged(tmp_path, capsys):
    config = tmp_path / "symbols.json"
    _write_config(config, ["BTC-USDT-SWAP"])
    original = config.read_bytes()
    client = _FakeClient({"data": [_instrument("BTC-USDT-SWAP")]})

    assert symbol_cli.add_symbol(
        "BTC-USDT-SWAP", config_path=config, client=client
    ) == 4
    result = json.loads(capsys.readouterr().out)

    assert result["status"] == "duplicate"
    assert config.read_bytes() == original


def test_add_rejects_missing_or_not_live_instrument(tmp_path, capsys):
    config = tmp_path / "symbols.json"
    _write_config(config, ["BTC-USDT-SWAP"])
    original = config.read_bytes()
    client = _FakeClient(
        {"data": [_instrument("ETH-USDT-SWAP", state="suspend")]}
    )

    assert symbol_cli.add_symbol(
        "ETH-USDT-SWAP", config_path=config, client=client
    ) == 2
    result = json.loads(capsys.readouterr().out)

    assert result["status"] == "invalid"
    assert config.read_bytes() == original


def test_add_maps_fake_api_error_without_changing_config(tmp_path, capsys):
    config = tmp_path / "symbols.json"
    _write_config(config, ["BTC-USDT-SWAP"])
    original = config.read_bytes()
    client = _FakeClient(error=OSError("offline"))

    assert symbol_cli.add_symbol(
        "ETH-USDT-SWAP", config_path=config, client=client
    ) == 3
    captured = capsys.readouterr()
    result = json.loads(captured.out)

    assert result["status"] == "api_error"
    assert "offline" in captured.err
    assert config.read_bytes() == original
