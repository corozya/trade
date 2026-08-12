"""Deterministic, offline-testable ingestion for agent-krypto market data."""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol, Sequence

import pyarrow.feather as feather

from services.crypto_data_lake import (
    DATA_KINDS,
    SYMBOLS,
    TIMEFRAMES,
    CryptoDataLake,
    DatasetVersion,
    _utc_iso,
)


class MarketDataAdapter(Protocol):
    """A source adapter. Network policy belongs to the concrete implementation."""

    name: str

    def fetch(self) -> Iterable[Mapping[str, Any]]: ...

    def lineage(self) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class FixtureMarketDataAdapter:
    """JSON/in-memory adapter used by tests and reproducible research fixtures."""

    records: Sequence[Mapping[str, Any]]
    source_metadata: Mapping[str, Any]
    name: str = "fixture"

    def fetch(self) -> Iterable[Mapping[str, Any]]:
        return [dict(row) for row in self.records]

    def lineage(self) -> Mapping[str, Any]:
        return {"name": self.name, **dict(self.source_metadata)}


@dataclass(frozen=True)
class LocalFeatherMarketDataAdapter:
    """Read-only adapter for the project's Freqtrade/Bitget OHLCV history."""

    data_dir: Path
    symbols: Sequence[str]
    timeframe: str = "15m"
    name: str = "local-feather"

    def _path(self, symbol: str) -> Path:
        base = symbol.removesuffix("-SWAP").replace("-", "_")
        return self.data_dir / f"{base}_USDT-{self.timeframe}-futures.feather"

    def _files(self) -> list[Path]:
        files = [self._path(symbol) for symbol in self.symbols]
        missing = [path.name for path in files if not path.is_file()]
        if missing:
            raise IngestionError(f"missing local market-data files: {missing}")
        return files

    def fetch(self) -> Iterable[Mapping[str, Any]]:
        rows: list[dict[str, Any]] = []
        interval = timedelta(minutes=int(self.timeframe.removesuffix("m")))
        for symbol, path in zip(self.symbols, self._files()):
            table = feather.read_table(path)
            required = {"date", "open", "high", "low", "close", "volume"}
            missing = required - set(table.column_names)
            if missing:
                raise IngestionError(f"{path.name}: missing columns: {sorted(missing)}")
            for source_row in table.select(sorted(required)).to_pylist():
                observed_at = source_row.pop("date")
                rows.append(
                    {
                        "symbol": symbol,
                        "timeframe": self.timeframe,
                        "data_kind": "ohlcv",
                        "observed_at": observed_at,
                        "available_at": observed_at + interval,
                        "source": self.name,
                        **source_row,
                    }
                )
        return rows

    def lineage(self) -> Mapping[str, Any]:
        files = self._files()
        return {
            "name": self.name,
            "provider": "Bitget",
            "mode": "local-read-only",
            "timeframe": self.timeframe,
            "files": [
                {
                    "name": path.name,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
                for path in files
            ],
        }


class IngestionError(RuntimeError):
    """Fail-closed ingestion or dataset readiness error."""


def _record_key(row: Mapping[str, Any]) -> tuple[str, ...]:
    return (
        str(row["symbol"]),
        str(row["timeframe"]),
        str(row["data_kind"]),
        _utc_iso(row["observed_at"]),
        _utc_iso(row["available_at"]),
        str(row["source"]),
    )


def _canonical_payload(row: Mapping[str, Any]) -> str:
    # Arrow materializes absent union-schema fields as null when reading Parquet.
    # Treat those fields exactly like absent keys so a persisted row equals a retry.
    return json.dumps(
        {key: value for key, value in row.items() if value is not None},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def merge_records(
    existing: Iterable[Mapping[str, Any]],
    incoming: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Merge retries/increments, rejecting conflicting values for one source event."""
    merged: dict[tuple[str, ...], dict[str, Any]] = {}
    payloads: dict[tuple[str, ...], str] = {}
    for source_row in [*existing, *incoming]:
        row = dict(source_row)
        row["observed_at"] = _utc_iso(row["observed_at"])
        row["available_at"] = _utc_iso(row["available_at"])
        key = _record_key(row)
        payload = _canonical_payload(row)
        if key in payloads and payloads[key] != payload:
            raise IngestionError(f"conflicting duplicate record: {'/'.join(key)}")
        merged[key] = row
        payloads[key] = payload
    return [merged[key] for key in sorted(merged)]


def readiness_errors(
    records: Iterable[Mapping[str, Any]],
    *,
    as_of: str | datetime,
    max_age: timedelta,
    required_symbols: Sequence[str] = SYMBOLS,
    required_timeframes: Sequence[str] = TIMEFRAMES,
    required_data_kinds: Sequence[str] = DATA_KINDS,
) -> list[str]:
    """Return completeness/freshness errors that must block research and runtime."""
    cutoff = datetime.fromisoformat(_utc_iso(as_of).replace("Z", "+00:00"))
    rows = [
        dict(row)
        for row in records
        if datetime.fromisoformat(_utc_iso(row["available_at"]).replace("Z", "+00:00"))
        <= cutoff
    ]
    errors: list[str] = []

    ohlcv_pairs = {
        (str(row["symbol"]), str(row["timeframe"]))
        for row in rows
        if row["data_kind"] == "ohlcv"
    }
    for symbol in required_symbols:
        for timeframe in required_timeframes:
            if (symbol, timeframe) not in ohlcv_pairs:
                errors.append(f"missing ohlcv: {symbol}/{timeframe}")

    for symbol in required_symbols:
        present = {
            str(row["data_kind"]) for row in rows if str(row["symbol"]) == symbol
        }
        for data_kind in required_data_kinds:
            if data_kind not in present:
                errors.append(f"missing {data_kind}: {symbol}")

    for symbol in required_symbols:
        for timeframe in required_timeframes:
            available = [
                datetime.fromisoformat(
                    _utc_iso(row["available_at"]).replace("Z", "+00:00")
                )
                for row in rows
                if str(row["symbol"]) == symbol
                and str(row["timeframe"]) == timeframe
                and row["data_kind"] == "ohlcv"
            ]
            if available and cutoff - max(available) > max_age:
                errors.append(f"stale ohlcv: {symbol}/{timeframe}")
    return errors


def require_ready_dataset(
    lake: CryptoDataLake,
    dataset_id: str,
    *,
    as_of: str | datetime,
    max_age: timedelta,
    required_symbols: Sequence[str] = SYMBOLS,
    required_timeframes: Sequence[str] = TIMEFRAMES,
    required_data_kinds: Sequence[str] = DATA_KINDS,
) -> None:
    errors = readiness_errors(
        lake.read_version(dataset_id).to_pylist(),
        as_of=as_of,
        max_age=max_age,
        required_symbols=required_symbols,
        required_timeframes=required_timeframes,
        required_data_kinds=required_data_kinds,
    )
    if errors:
        raise IngestionError("; ".join(errors))


class CryptoMarketIngestor:
    """Fetch, normalize, deduplicate and publish immutable dataset versions."""

    def __init__(self, lake: CryptoDataLake):
        self.lake = lake

    def ingest(
        self,
        adapters: Sequence[MarketDataAdapter],
        *,
        base_dataset_id: str | None = None,
    ) -> DatasetVersion:
        if not adapters:
            raise IngestionError("at least one adapter is required")
        existing = (
            self.lake.read_version(base_dataset_id).to_pylist()
            if base_dataset_id
            else []
        )
        incoming: list[Mapping[str, Any]] = []
        sources: list[Mapping[str, Any]] = []
        for adapter in adapters:
            source_rows = list(adapter.fetch())
            if not source_rows:
                raise IngestionError(f"adapter returned no data: {adapter.name}")
            incoming.extend(source_rows)
            sources.append(dict(adapter.lineage()))
        lineage = {
            "sources": sorted(sources, key=_canonical_payload),
            "base_dataset_id": base_dataset_id,
            "operation": "incremental-ingest",
        }
        return self.lake.publish(
            merge_records(existing, incoming), lineage=lineage, base_dataset_id=base_dataset_id
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Publish an offline market-data fixture")
    parser.add_argument("--lake-root", required=True)
    parser.add_argument("--fixture", required=True)
    parser.add_argument("--base-dataset-id")
    args = parser.parse_args(argv)

    fixture_path = Path(args.fixture)
    payload = json.loads(fixture_path.read_text())
    adapter = FixtureMarketDataAdapter(
        records=payload["records"],
        source_metadata={
            "fixture": fixture_path.name,
            "fixture_sha256": payload.get("sha256", "unspecified"),
        },
    )
    version = CryptoMarketIngestor(CryptoDataLake(args.lake_root)).ingest(
        [adapter], base_dataset_id=args.base_dataset_id
    )
    print(version.dataset_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
