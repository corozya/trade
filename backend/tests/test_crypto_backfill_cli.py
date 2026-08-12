"""#162 — OHLCV backfill CLI: batch symbols x timeframes, idempotency, resume.

Exercises ``scripts/crypto_backfill_cli.py`` end-to-end against a fake OKX
client (no network) and a real ``CryptoDataLake`` on ``tmp_path``, covering
the scope #162 actually adds on top of #167's single-pair proof of concept:
every ``CryptoDataLake.SYMBOLS`` x every requested timeframe in one
invocation, --symbols/--timeframes overrides, legacy --symbol/--timeframe
compatibility, and --full -> --incremental idempotency across a batch.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import crypto_backfill_cli as cli
from services.crypto_data_lake import SYMBOLS, TIMEFRAMES, CryptoDataLake


def _candle(ts: datetime, price: float) -> list[str]:
    ms = int(ts.timestamp() * 1000)
    return [str(ms), str(price), str(price + 1), str(price - 1), str(price), "10"]


def _taker_point(ts: datetime, sell: float, buy: float) -> list[str]:
    ms = int(ts.timestamp() * 1000)
    return [str(ms), str(sell), str(buy)]


class _FakeOkxClient:
    """Stand-in for OkxClient.get_candles(history=True) — no network.

    Each (instId, bar) key gets a fixed page of candles served once (OKX
    real behaviour: subsequent ``after`` pagination past the seeded page
    returns an empty ``data`` list, ending the adapter's while loop).
    """

    def __init__(self, alias, *args, **kwargs):
        self.alias = alias
        self.calls: list[tuple[str, str]] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_candles(self, inst_id, bar="15m", limit=100, after=None, before=None, history=False):
        self.calls.append((inst_id, bar))
        if after is not None:
            return {"data": []}
        now = datetime.now(timezone.utc).replace(microsecond=0)
        candles = [
            _candle(now - timedelta(minutes=idx), 100.0 + idx) for idx in range(5)
        ]
        return {"data": candles}

    def get_taker_volume_history(
        self, ccy, inst_type="CONTRACTS", period="5m", limit=None, after=None, before=None
    ):
        self.calls.append((ccy, period))
        if after is not None:
            return {"data": []}
        now = datetime.now(timezone.utc).replace(microsecond=0)
        points = [
            _taker_point(now - timedelta(minutes=idx), 10.0 + idx, 20.0 + idx)
            for idx in range(5)
        ]
        return {"data": points}


@pytest.fixture(autouse=True)
def _patch_okx_client(monkeypatch):
    monkeypatch.setattr(cli, "OkxClient", _FakeOkxClient)


def _run(tmp_path, extra_args):
    lake_root = tmp_path / "lake"
    argv = [
        "--full",
        "--data-kind",
        "ohlcv",
        "--lake-root",
        str(lake_root),
        "--alias",
        "demo",
        *extra_args,
    ]
    exit_code = cli.main(argv)
    return exit_code, lake_root


def test_defaults_backfill_every_symbol_and_timeframe(tmp_path, capsys):
    exit_code, lake_root = _run(tmp_path, [])
    out = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert out["ok"] is True
    assert not out["failures"]
    pairs = {(row["symbol"], row["timeframe"]) for row in out["results"]}
    assert pairs == {(symbol, tf) for symbol in SYMBOLS for tf in TIMEFRAMES}


def test_symbols_and_timeframes_csv_overrides_restrict_the_batch(tmp_path, capsys):
    exit_code, _ = _run(
        tmp_path,
        ["--symbols", "BTC-USDT-SWAP,ETH-USDT-SWAP", "--timeframes", "1h,1d"],
    )
    out = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    pairs = {(row["symbol"], row["timeframe"]) for row in out["results"]}
    assert pairs == {
        ("BTC-USDT-SWAP", "1h"),
        ("BTC-USDT-SWAP", "1d"),
        ("ETH-USDT-SWAP", "1h"),
        ("ETH-USDT-SWAP", "1d"),
    }


def test_legacy_singular_symbol_and_timeframe_still_work(tmp_path, capsys):
    exit_code, _ = _run(tmp_path, ["--symbol", "BTC-USDT-SWAP", "--timeframe", "1d"])
    out = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert len(out["results"]) == 1
    assert out["results"][0]["symbol"] == "BTC-USDT-SWAP"
    assert out["results"][0]["timeframe"] == "1d"


def test_symbol_and_symbols_are_mutually_exclusive(tmp_path):
    lake_root = tmp_path / "lake"
    argv = [
        "--full",
        "--data-kind",
        "ohlcv",
        "--lake-root",
        str(lake_root),
        "--alias",
        "demo",
        "--symbol",
        "BTC-USDT-SWAP",
        "--symbols",
        "BTC-USDT-SWAP,ETH-USDT-SWAP",
    ]
    with pytest.raises(SystemExit):
        cli.main(argv)


def test_incremental_after_full_is_idempotent_across_the_batch(tmp_path, capsys):
    exit_code, lake_root = _run(
        tmp_path, ["--symbols", "BTC-USDT-SWAP", "--timeframes", "1d"]
    )
    assert exit_code == 0
    capsys.readouterr()

    argv = [
        "--incremental",
        "--data-kind",
        "ohlcv",
        "--lake-root",
        str(lake_root),
        "--alias",
        "demo",
        "--symbols",
        "BTC-USDT-SWAP",
        "--timeframes",
        "1d",
    ]
    exit_code = cli.main(argv)
    out = json.loads(capsys.readouterr().out)

    # The fake client re-serves the same fixed candle page; every candle's
    # observed_at is now <= the resumed cursor, so the adapter raises
    # SystemExit("no new candles ...") — this must show up as a clean
    # failure entry, not crash the process. Exit code is non-zero here
    # because this batch has exactly one pair and it produced zero
    # results (see main()'s "failures and not results" contract) — a
    # multi-pair batch where only *some* pairs have nothing new still
    # exits 0, exercised by test_one_pair_failing_does_not_abort_the_rest_of_the_batch.
    assert exit_code == 1
    assert out["failures"]
    assert "no new candles" in out["failures"][0]["error"]


def test_one_pair_failing_does_not_abort_the_rest_of_the_batch(tmp_path, monkeypatch, capsys):
    class _PartiallyBrokenClient(_FakeOkxClient):
        def get_candles(self, inst_id, bar="15m", limit=100, after=None, before=None, history=False):
            if inst_id == "ETH-USDT-SWAP":
                return {"data": []}
            return super().get_candles(
                inst_id, bar=bar, limit=limit, after=after, before=before, history=history
            )

    monkeypatch.setattr(cli, "OkxClient", _PartiallyBrokenClient)

    exit_code, _ = _run(
        tmp_path,
        ["--symbols", "BTC-USDT-SWAP,ETH-USDT-SWAP", "--timeframes", "1d"],
    )
    out = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert {row["symbol"] for row in out["results"]} == {"BTC-USDT-SWAP"}
    assert {row["symbol"] for row in out["failures"]} == {"ETH-USDT-SWAP"}


def test_registry_persists_dataset_id_per_symbol_timeframe_pair(tmp_path, capsys):
    _, lake_root = _run(tmp_path, ["--symbols", "BTC-USDT-SWAP,ETH-USDT-SWAP", "--timeframes", "1h,1d"])
    capsys.readouterr()

    registry = json.loads((lake_root / "raw" / "latest.json").read_text())
    assert set(registry.keys()) == {
        "ohlcv/BTC-USDT-SWAP/1h",
        "ohlcv/BTC-USDT-SWAP/1d",
        "ohlcv/ETH-USDT-SWAP/1h",
        "ohlcv/ETH-USDT-SWAP/1d",
    }


def test_published_rows_are_readable_back_from_the_lake(tmp_path, capsys):
    _, lake_root = _run(tmp_path, ["--symbols", "BTC-USDT-SWAP", "--timeframes", "1d"])
    out = json.loads(capsys.readouterr().out)

    lake = CryptoDataLake(lake_root)
    dataset_id = out["results"][0]["dataset_id"]
    table = lake.read_version(dataset_id)
    rows = table.to_pylist()
    assert all(row["data_kind"] == "ohlcv" for row in rows)
    assert all(row["symbol"] == "BTC-USDT-SWAP" for row in rows)
    assert all(row["timeframe"] == "1d" for row in rows)
