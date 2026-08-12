import json
from datetime import timedelta

import pytest
import pyarrow as pa
import pyarrow.feather as feather

from services.crypto_data_lake import DATA_KINDS, SYMBOLS, TIMEFRAMES, CryptoDataLake
from services.crypto_market_ingestion import (
    CryptoMarketIngestor,
    FixtureMarketDataAdapter,
    IngestionError,
    LocalFeatherMarketDataAdapter,
    main,
    merge_records,
    require_ready_dataset,
)


NOW = "2026-07-23T12:00:00Z"


def _row(symbol, timeframe, kind, **extra):
    payload = {
        "symbol": symbol,
        "timeframe": timeframe,
        "data_kind": kind,
        "observed_at": "2026-07-23T11:58:00Z",
        "available_at": "2026-07-23T11:59:00Z",
        "source": "offline-okx-fixture",
    }
    payload.update(
        {
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.5,
            "volume": 10.0,
        }
        if kind == "ohlcv"
        else {"value": 1.0}
    )
    payload.update(extra)
    return payload


def _complete_fixture():
    rows = [
        _row(symbol, timeframe, "ohlcv")
        for symbol in SYMBOLS
        for timeframe in TIMEFRAMES
    ]
    for symbol in SYMBOLS:
        for kind in DATA_KINDS:
            if kind != "ohlcv":
                rows.append(_row(symbol, "1m", kind))
    return rows


def _adapter(rows):
    return FixtureMarketDataAdapter(
        records=rows,
        source_metadata={
            "provider": "OKX",
            "environment": "offline-fixture",
            "streams": list(DATA_KINDS),
        },
    )


def test_offline_fixture_publishes_complete_dataset_with_lineage(tmp_path):
    lake = CryptoDataLake(tmp_path)
    version = CryptoMarketIngestor(lake).ingest([_adapter(_complete_fixture())])

    assert lake.validate(version.dataset_id) == []
    assert version.manifest["symbols"] == sorted(SYMBOLS)
    assert set(version.manifest["data_kinds"]) == set(DATA_KINDS)
    assert version.manifest["lineage"]["sources"][0]["provider"] == "OKX"
    assert version.manifest["lineage"]["sources"][0]["streams"] == list(DATA_KINDS)
    require_ready_dataset(lake, version.dataset_id, as_of=NOW, max_age=timedelta(minutes=5))


def test_retry_is_deduplicated_and_deterministic(tmp_path):
    lake = CryptoDataLake(tmp_path)
    ingestor = CryptoMarketIngestor(lake)
    first = ingestor.ingest([_adapter(_complete_fixture())])
    retried = ingestor.ingest(
        [_adapter(_complete_fixture())],
        base_dataset_id=first.dataset_id,
    )
    retried_again = ingestor.ingest(
        [_adapter(_complete_fixture())],
        base_dataset_id=first.dataset_id,
    )

    assert retried.dataset_id == retried_again.dataset_id
    assert lake.read_version(retried.dataset_id).num_rows == len(_complete_fixture())
    assert lake.read_version(first.dataset_id).num_rows == len(_complete_fixture())


def test_incremental_update_adds_only_new_source_events(tmp_path):
    lake = CryptoDataLake(tmp_path)
    ingestor = CryptoMarketIngestor(lake)
    first = ingestor.ingest([_adapter(_complete_fixture())])
    new_row = _row(
        "BTC-USDT-SWAP",
        "1m",
        "ohlcv",
        observed_at="2026-07-23T11:59:00Z",
        available_at="2026-07-23T12:00:00Z",
    )

    second = ingestor.ingest([_adapter([new_row, new_row])], base_dataset_id=first.dataset_id)

    assert second.dataset_id != first.dataset_id
    assert lake.read_version(second.dataset_id).num_rows == len(_complete_fixture()) + 1


def test_offline_ingest_command_publishes_fixture(tmp_path, capsys):
    fixture = tmp_path / "market.json"
    fixture.write_text(json.dumps({"records": _complete_fixture(), "sha256": "fixture-v1"}))

    assert main(["--lake-root", str(tmp_path / "lake"), "--fixture", str(fixture)]) == 0

    dataset_id = capsys.readouterr().out.strip()
    lake = CryptoDataLake(tmp_path / "lake")
    assert dataset_id.startswith("market-")
    assert lake.validate(dataset_id) == []
    assert lake.read_version(dataset_id).num_rows == len(_complete_fixture())


def test_conflicting_duplicate_fails_closed():
    original = _row("BTC-USDT-SWAP", "1m", "ohlcv")
    conflicting = {**original, "close": 999.0}

    with pytest.raises(IngestionError, match="conflicting duplicate"):
        merge_records([original], [conflicting])


@pytest.mark.parametrize("mode", ["missing", "stale"])
def test_missing_or_stale_data_blocks_consumers(tmp_path, mode):
    rows = _complete_fixture()
    if mode == "missing":
        rows = [
            row
            for row in rows
            if not (
                row["symbol"] == "DOGE-USDT-SWAP"
                and row["timeframe"] == "1d"
                and row["data_kind"] == "ohlcv"
            )
        ]
    else:
        for row in rows:
            row["observed_at"] = "2026-07-23T10:00:00Z"
            row["available_at"] = "2026-07-23T10:01:00Z"
    lake = CryptoDataLake(tmp_path)
    version = CryptoMarketIngestor(lake).ingest([_adapter(rows)])

    with pytest.raises(IngestionError, match=mode):
        require_ready_dataset(
            lake,
            version.dataset_id,
            as_of=NOW,
            max_age=timedelta(minutes=5),
        )


def _write_local_feather(root, symbol, *, close=100.5, at="2026-07-23T11:45:00Z"):
    path = root / f"{symbol}_USDT_USDT-15m-futures.feather"
    feather.write_feather(
        pa.Table.from_pylist(
            [{
                "date": __import__("datetime").datetime.fromisoformat(
                    at.replace("Z", "+00:00")
                ),
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": close,
                "volume": 10.0,
            }]
        ),
        path,
    )


def test_local_feather_adapter_ingests_five_symbols_15m_offline(tmp_path):
    symbols = ("BTC", "ETH", "DOGE", "SOL", "XRP")
    for symbol in symbols:
        _write_local_feather(tmp_path, symbol)
    required = tuple(f"{symbol}-USDT-SWAP" for symbol in symbols)
    adapter = LocalFeatherMarketDataAdapter(tmp_path, required)
    lake = CryptoDataLake(tmp_path / "lake")

    first = CryptoMarketIngestor(lake).ingest([adapter])
    retry = CryptoMarketIngestor(lake).ingest([adapter])

    assert first.dataset_id == retry.dataset_id
    assert first.manifest["symbols"] == sorted(required)
    assert first.manifest["timeframes"] == ["15m"]
    assert first.manifest["lineage"]["sources"][0]["mode"] == "local-read-only"
    require_ready_dataset(
        lake,
        first.dataset_id,
        as_of=NOW,
        max_age=timedelta(minutes=5),
        required_symbols=required,
        required_timeframes=("15m",),
        required_data_kinds=("ohlcv",),
    )


def test_local_feather_adapter_missing_symbol_fails_closed(tmp_path):
    _write_local_feather(tmp_path, "BTC")
    adapter = LocalFeatherMarketDataAdapter(
        tmp_path, ("BTC-USDT-SWAP", "XRP-USDT-SWAP")
    )

    with pytest.raises(IngestionError, match="XRP_USDT_USDT-15m"):
        list(adapter.fetch())
