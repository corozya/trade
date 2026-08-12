import json

import pytest

from services.crypto_data_lake import CryptoDataLake, SYMBOLS, TIMEFRAMES


def _row(symbol, timeframe, minute=0, **extra):
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "data_kind": "ohlcv",
        "observed_at": f"2026-01-01T00:{minute:02d}:00Z",
        "available_at": f"2026-01-01T00:{minute + 1:02d}:00Z",
        "source": "okx",
        "open": 100.0,
        "high": 102.0,
        "low": 99.0,
        "close": 101.0,
        "volume": 10.0,
        **extra,
    }


def _part_path(dataset_version, index=0):
    manifest = json.loads((dataset_version.path / "manifest.json").read_text())
    part = manifest["storage"]["parts"][index]
    return dataset_version.path / part


def test_publish_is_content_addressed_and_never_mutates_version(tmp_path):
    lake = CryptoDataLake(tmp_path)
    rows = [_row("BTC-USDT-SWAP", "1m")]
    lineage = {"sources": [{"name": "okx", "endpoint": "/history-candles"}]}

    first = lake.publish(rows, lineage=lineage)
    parquet_before = _part_path(first).read_bytes()
    second = lake.publish(rows, lineage=lineage)

    assert second.dataset_id == first.dataset_id
    assert _part_path(first).read_bytes() == parquet_before
    manifest = json.loads((first.path / "manifest.json").read_text())
    assert manifest["dataset_id"] == first.dataset_id
    assert manifest["lineage"] == lineage


def test_changed_content_creates_a_new_version(tmp_path):
    lake = CryptoDataLake(tmp_path)
    lineage = {"sources": [{"name": "okx"}]}
    first = lake.publish([_row("BTC-USDT-SWAP", "1m")], lineage=lineage)
    second = lake.publish([_row("BTC-USDT-SWAP", "1m", close=103.0)], lineage=lineage)

    assert first.dataset_id != second.dataset_id
    assert first.path.exists()
    assert second.path.exists()


def test_as_of_read_excludes_future_available_data(tmp_path):
    lake = CryptoDataLake(tmp_path)
    version = lake.publish(
        [_row("BTC-USDT-SWAP", "1m", minute=0), _row("BTC-USDT-SWAP", "1m", minute=2)],
        lineage={"sources": [{"name": "okx"}]},
    )

    result = lake.read_as_of(version.dataset_id, "2026-01-01T00:01:30Z")

    assert result.num_rows == 1
    assert result["available_at"].to_pylist() == ["2026-01-01T00:01:00Z"]


def test_duckdb_read_is_read_only_and_point_in_time(tmp_path):
    lake = CryptoDataLake(tmp_path)
    version = lake.publish(
        [_row("BTC-USDT-SWAP", "1m", minute=0), _row("BTC-USDT-SWAP", "1m", minute=2)],
        lineage={"sources": [{"name": "okx"}]},
    )
    parquet_before = _part_path(version).read_bytes()

    result = lake.read_as_of_duckdb(
        version.dataset_id,
        "2026-01-01T00:01:30Z",
        columns=["symbol", "available_at"],
    )

    assert result.to_pylist() == [
        {"symbol": "BTC-USDT-SWAP", "available_at": "2026-01-01T00:01:00Z"}
    ]
    assert _part_path(version).read_bytes() == parquet_before


def test_manifest_is_dvc_ready_without_runtime_side_effects(tmp_path):
    lake = CryptoDataLake(tmp_path)
    version = lake.publish(
        [_row("BTC-USDT-SWAP", "1m")],
        lineage={"sources": [{"name": "okx"}]},
    )

    assert version.manifest["dvc"] == {
        "target": f"raw/versions/{version.dataset_id}",
        "content_sha256": version.manifest["content_sha256"],
        "immutable": True,
    }
    assert lake.dvc_add_args(version.dataset_id) == (
        "dvc",
        "add",
        "--",
        str(version.path),
    )
    assert not list(tmp_path.rglob("*.dvc"))


def test_incremental_publish_writes_only_new_rows_to_disk(tmp_path):
    import pyarrow.parquet as pq

    lake = CryptoDataLake(tmp_path)
    lineage = {"sources": [{"name": "okx"}]}
    base_rows = [_row("BTC-USDT-SWAP", "1m", minute=m) for m in range(5)]
    base = lake.publish(base_rows, lineage=lineage)

    new_row = _row("BTC-USDT-SWAP", "1m", minute=5)
    grown = lake.publish(base_rows + [new_row], lineage=lineage, base_dataset_id=base.dataset_id)

    assert grown.dataset_id != base.dataset_id
    # The logical read still returns everything (fasade unchanged for callers).
    assert lake.read_version(grown.dataset_id).num_rows == len(base_rows) + 1

    manifest = json.loads((grown.path / "manifest.json").read_text())
    parts = manifest["storage"]["parts"]
    # The new version's own directory must not contain a full copy of the
    # base rows — only the base's parts (inherited by path) plus one new
    # part holding just the incremental row.
    own_dir_parts = [p for p in parts if not str(p).startswith(str(tmp_path))]
    assert len(own_dir_parts) == 1
    new_part_table = pq.read_table(grown.path / own_dir_parts[0])
    assert new_part_table.num_rows == 1


def test_validation_requires_every_symbol_and_timeframe(tmp_path):
    lake = CryptoDataLake(tmp_path)
    rows = [_row(symbol, timeframe) for symbol in SYMBOLS for timeframe in TIMEFRAMES]
    version = lake.publish(rows, lineage={"sources": [{"name": "okx"}]})

    assert lake.validate(version.dataset_id) == []

    incomplete = lake.publish(rows[:-1], lineage={"sources": [{"name": "okx"}]})
    assert lake.validate(incomplete.dataset_id) == [f"missing ohlcv: {SYMBOLS[-1]}/1d"]


def test_rejects_lookahead_timestamp(tmp_path):
    lake = CryptoDataLake(tmp_path)
    row = _row("BTC-USDT-SWAP", "1m")
    row["available_at"] = "2025-12-31T23:59:00Z"

    with pytest.raises(ValueError, match="available_at"):
        lake.publish([row], lineage={"sources": [{"name": "okx"}]})


def test_supports_research_market_streams(tmp_path):
    lake = CryptoDataLake(tmp_path)
    rows = []
    payloads = {
        "funding": {"funding_rate": 0.0001},
        "open_interest": {"open_interest": 1234.0},
        "costs": {"maker_fee": 0.0002, "taker_fee": 0.0005},
        "market_impact": {"spread_bps": 1.5, "slippage_bps": 2.0},
        "contract_metadata": {"tick_size": 0.1, "lot_size": 0.01},
    }
    for data_kind, payload in payloads.items():
        row = _row("BTC-USDT-SWAP", "1m", **payload)
        row["data_kind"] = data_kind
        rows.append(row)

    version = lake.publish(rows, lineage={"sources": [{"name": "okx"}]})

    assert set(version.manifest["data_kinds"]) == set(payloads)
