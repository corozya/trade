"""Immutable, point-in-time market data store for agent-krypto research.

Parquet files are the source of truth.  DuckDB may query them directly, while
DVC versions the ``raw`` directory and manifests outside this module.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq


_BACKFILL_SYMBOLS_CONFIG = (
    Path(__file__).resolve().parents[1] / "config" / "crypto_backfill_symbols.json"
)
_BACKFILL_SYMBOL_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9_-]*(?:-[A-Z0-9_]+)+$")


def _load_backfill_symbols(config_path: Path = _BACKFILL_SYMBOLS_CONFIG) -> tuple[str, ...]:
    """Load and validate the ordered backfill symbol configuration."""
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("backfill symbol configuration must be a JSON object")
    if set(payload) != {"schema_version", "symbols"}:
        raise ValueError(
            "backfill symbol configuration must contain only schema_version and symbols"
        )
    if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
        raise ValueError("unsupported backfill symbol configuration schema_version")

    symbols = payload["symbols"]
    if not isinstance(symbols, list) or not symbols:
        raise ValueError("backfill symbols must be a non-empty JSON array")
    if any(not isinstance(symbol, str) for symbol in symbols):
        raise ValueError("every backfill symbol must be a string")
    if any(not _BACKFILL_SYMBOL_PATTERN.fullmatch(symbol) for symbol in symbols):
        raise ValueError("backfill symbols must use the canonical OKX instrument format")
    if len(symbols) != len(set(symbols)):
        raise ValueError("backfill symbols must be unique")
    return tuple(symbols)


SYMBOLS = _load_backfill_symbols()
TIMEFRAMES = ("1m", "5m", "15m", "1h", "4h", "1d")
DATA_KINDS = (
    "ohlcv",
    "funding",
    "open_interest",
    "taker_volume",
    "long_short_ratio",
    "costs",
    "market_impact",
    "contract_metadata",
    # #228 (podzadanie #227): precomputed indicator series, derived locally
    # from an already-backfilled "ohlcv" series (see services.crypto_indicator_backfill)
    # rather than fetched from OKX — added here so downstream endpoints
    # (#229 rsi/macd, #230 stochastic/atr, #231 risk_indicator) can publish
    # into the same lake/registry/_read_series machinery as every other
    # data_kind, without a schema change of their own.
    "rsi",
    "macd",
    "stochastic",
    "risk_indicator",
    "atr",
    # #233: fractal pivot support/resistance zones (1:1 port of TV/sr.pine),
    # published as one row per level-state-change EVENT (created/holding/
    # broken/flipped), not one row per input bar — see
    # crypto-dashboard/backend/sr_levels.py module docstring for why this
    # data_kind's shape differs from rsi/macd/atr/stochastic/risk_indicator's
    # 1-value-per-bar convention.
    "support_resistance",
)
REQUIRED_FIELDS = {
    "symbol",
    "timeframe",
    "data_kind",
    "observed_at",
    "available_at",
    "source",
}


def _utc_iso(value: Any) -> str:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamps must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_records(records: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    normalized = []
    for source_record in records:
        missing = REQUIRED_FIELDS - source_record.keys()
        if missing:
            raise ValueError(f"record is missing required fields: {sorted(missing)}")
        record = dict(source_record)
        if record["symbol"] not in SYMBOLS:
            raise ValueError(f"unsupported symbol: {record['symbol']}")
        if record["timeframe"] not in TIMEFRAMES:
            raise ValueError(f"unsupported timeframe: {record['timeframe']}")
        if record["data_kind"] not in DATA_KINDS:
            raise ValueError(f"unsupported data_kind: {record['data_kind']}")
        record["observed_at"] = _utc_iso(record["observed_at"])
        record["available_at"] = _utc_iso(record["available_at"])
        if record["available_at"] < record["observed_at"]:
            raise ValueError("available_at cannot precede observed_at")
        normalized.append(record)
    if not normalized:
        raise ValueError("cannot publish an empty dataset")
    return sorted(
        normalized,
        key=lambda row: (
            row["symbol"],
            row["timeframe"],
            row["data_kind"],
            row["observed_at"],
            row["available_at"],
        ),
    )


def _digest(records: Sequence[Mapping[str, Any]], lineage: Mapping[str, Any]) -> str:
    payload = json.dumps(
        {"records": records, "lineage": lineage},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class DatasetVersion:
    dataset_id: str
    path: Path
    manifest: Mapping[str, Any]


class CryptoDataLake:
    """Publishes content-addressed versions and exposes safe as-of reads."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.versions_dir = self.root / "raw" / "versions"

    def publish(
        self,
        records: Iterable[Mapping[str, Any]],
        *,
        lineage: Mapping[str, Any],
        base_dataset_id: str | None = None,
    ) -> DatasetVersion:
        """Publish ``records`` (the full logical content) as a new version.

        ``dataset_id``/content hash is always derived from the *full* logical
        row set, exactly as before — callers (e.g. ``CryptoMarketIngestor``)
        keep passing ``merge_records(existing, incoming)`` and idempotency is
        unaffected. What changes is how many bytes actually get written to
        disk: when ``base_dataset_id`` names an already-published version,
        its Parquet parts are inherited by reference (listed in the new
        manifest, not copied) and only the rows that are new relative to
        that base are written as a fresh part file. This is what keeps
        repeated ``--incremental`` publishes from re-writing the entire
        history on every run (#170 follow-up — a full-history rewrite every
        15 minutes was making the lake grow ~linearly with run count, 8.9GB
        across 693 versions before this fix).
        """
        rows = _canonical_records(records)
        if not lineage.get("sources"):
            raise ValueError("lineage.sources is required")
        digest = _digest(rows, lineage)
        dataset_id = f"market-{digest[:16]}"
        destination = self.versions_dir / dataset_id
        manifest_path = destination / "manifest.json"
        if destination.exists():
            return DatasetVersion(dataset_id, destination, json.loads(manifest_path.read_text()))

        inherited_parts, new_rows = self._split_against_base(rows, base_dataset_id=base_dataset_id)

        self.versions_dir.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=f".{dataset_id}-", dir=self.versions_dir))
        try:
            part_files = list(inherited_parts)
            if new_rows or not part_files:
                # First-ever publish for this key (no base) always gets at
                # least one part, even if ``new_rows`` happens to be empty,
                # so every version directory is independently readable.
                part_name = f"part-{len(part_files)}.parquet"
                # ``Table.from_pylist`` infers its schema from the first record
                # and would silently discard fields present only in another
                # stream — build columns explicitly across the full row set
                # written into this part.
                write_rows = new_rows if part_files else rows
                columns = sorted({key for row in write_rows for key in row})
                table = pa.Table.from_pydict(
                    {column: [row.get(column) for row in write_rows] for column in columns}
                )
                pq.write_table(table, temporary / part_name)
                part_files.append(part_name)

            manifest = {
                "schema_version": 2,
                "dataset_id": dataset_id,
                "content_sha256": digest,
                "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "row_count": len(rows),
                "symbols": sorted({row["symbol"] for row in rows}),
                "timeframes": sorted({row["timeframe"] for row in rows}),
                "data_kinds": sorted({row["data_kind"] for row in rows}),
                "lineage": dict(lineage),
                "storage": {"format": "parquet", "parts": part_files},
                "dvc": {
                    "target": f"raw/versions/{dataset_id}",
                    "content_sha256": digest,
                    "immutable": True,
                },
            }
            (temporary / "manifest.json").write_text(
                json.dumps(manifest, indent=2, sort_keys=True) + "\n"
            )
            try:
                os.rename(temporary, destination)
            except FileExistsError:
                shutil.rmtree(temporary)
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise
        return DatasetVersion(dataset_id, destination, manifest)

    def _split_against_base(
        self,
        rows: list[dict[str, Any]],
        *,
        base_dataset_id: str | None,
    ) -> tuple[list[str], list[dict[str, Any]]]:
        """Return (inherited part *paths*, rows not already in the base version).

        Inherited paths are absolute so the new version's manifest can
        reference a sibling version directory directly — the storage layer
        never copies another version's Parquet files.
        """
        if not base_dataset_id:
            return [], rows
        try:
            base_dir = self._version_dir(base_dataset_id)
        except FileNotFoundError:
            return [], rows
        base_manifest = json.loads((base_dir / "manifest.json").read_text())
        base_parts = base_manifest.get("storage", {}).get("parts")
        if base_parts is None:
            # schema_version 1 (pre-#170) versions still use a single
            # ``market_data.parquet`` file — inherit it under its legacy name.
            base_parts = ["market_data.parquet"]
        inherited_paths = [str(base_dir / name) for name in base_parts]
        base_table = pq.read_table([str(base_dir / name) for name in base_parts])
        base_keys = {
            (sym, tf, kind, obs, avail)
            for sym, tf, kind, obs, avail in zip(
                base_table["symbol"].to_pylist(),
                base_table["timeframe"].to_pylist(),
                base_table["data_kind"].to_pylist(),
                base_table["observed_at"].to_pylist(),
                base_table["available_at"].to_pylist(),
            )
        }
        new_rows = [
            row
            for row in rows
            if (
                row["symbol"],
                row["timeframe"],
                row["data_kind"],
                row["observed_at"],
                row["available_at"],
            )
            not in base_keys
        ]
        return inherited_paths, new_rows

    def dvc_add_args(self, dataset_id: str) -> tuple[str, ...]:
        """Return reproducible argv for tracking one immutable version with DVC.

        DVC remains a project CLI concern; the data service never starts a
        daemon or mutates repository metadata during publication.
        """
        version_dir = self._version_dir(dataset_id)
        return ("dvc", "add", "--", str(version_dir))

    def _version_dir(self, dataset_id: str) -> Path:
        version_dir = self.versions_dir / dataset_id
        if (
            not dataset_id.startswith("market-")
            or "/" in dataset_id
            or "\\" in dataset_id
            or version_dir.parent != self.versions_dir
            or not version_dir.is_dir()
        ):
            raise FileNotFoundError(f"unknown dataset version: {dataset_id}")
        return version_dir

    def _version_parquet_paths(self, dataset_id: str) -> list[str]:
        """Return every Parquet part making up ``dataset_id``'s logical content.

        A version's parts may live outside its own directory (inherited,
        by reference, from an earlier ``base_dataset_id`` — see
        :meth:`_split_against_base`), so every read path goes through this
        instead of assuming a single ``market_data.parquet`` file.
        """
        version_dir = self._version_dir(dataset_id)
        manifest = json.loads((version_dir / "manifest.json").read_text())
        parts = manifest.get("storage", {}).get("parts")
        if parts is None:
            return [str(version_dir / "market_data.parquet")]
        return [
            name if os.path.isabs(name) else str(version_dir / name)
            for name in parts
        ]

    def read_as_of(
        self,
        dataset_id: str,
        as_of: str | datetime,
        *,
        columns: Sequence[str] | None = None,
    ) -> pa.Table:
        parquet_paths = self._version_parquet_paths(dataset_id)
        cutoff = _utc_iso(as_of)
        requested = list(columns) if columns else None
        scan_columns = requested
        if requested is not None and "available_at" not in requested:
            scan_columns = [*requested, "available_at"]
        table = pq.read_table(parquet_paths, columns=scan_columns)
        filtered = table.filter(pc.less_equal(table["available_at"], pa.scalar(cutoff)))
        return filtered.select(requested) if requested is not None else filtered

    def read_version(
        self,
        dataset_id: str,
        *,
        columns: Sequence[str] | None = None,
    ) -> pa.Table:
        """Read one immutable version without applying a point-in-time cutoff."""
        return pq.read_table(
            self._version_parquet_paths(dataset_id),
            columns=list(columns) if columns else None,
        )

    def read_as_of_duckdb(
        self,
        dataset_id: str,
        as_of: str | datetime,
        *,
        columns: Sequence[str] | None = None,
    ) -> pa.Table:
        """Query a published Parquet version through embedded, in-memory DuckDB."""
        import duckdb

        parquet_paths = self._version_parquet_paths(dataset_id)
        available = set(pq.read_schema(parquet_paths[0]).names)
        requested = list(columns) if columns else sorted(available)
        unknown = set(requested) - available
        if unknown:
            raise ValueError(f"unknown columns: {sorted(unknown)}")
        projection = ", ".join(f'"{name}"' for name in requested)
        cutoff = _utc_iso(as_of)
        connection = duckdb.connect(database=":memory:")
        try:
            result = connection.execute(
                f"""
                SELECT {projection}
                FROM read_parquet(?)
                WHERE available_at <= ?
                ORDER BY available_at
                """,
                [parquet_paths, cutoff],
            ).fetch_arrow_table()
        finally:
            connection.close()
        return result

    def latest_observed_at(self, dataset_id: str) -> str | None:
        """Return the max ``observed_at`` for a published version without
        reading the full table (#208): DuckDB's Parquet reader answers a bare
        MAX() from per-row-group statistics, so this scans metadata rather
        than every row even for large versions."""
        import duckdb

        parquet_paths = self._version_parquet_paths(dataset_id)
        connection = duckdb.connect(database=":memory:")
        try:
            row = connection.execute(
                "SELECT MAX(observed_at) FROM read_parquet(?)",
                [parquet_paths],
            ).fetchone()
        finally:
            connection.close()
        return row[0] if row else None

    def read_multi_symbol_as_of_duckdb(
        self,
        dataset_id: str,
        as_of: str | datetime,
        *,
        data_kind: str,
        timeframe: str,
        symbols: Sequence[str] = SYMBOLS,
        value_column: str = "open_interest",
    ) -> pa.Table:
        """As-of join of one point-sample ``data_kind`` across multiple symbols.

        Added for #164 (PM consultation w/ Zarządca-Ryzyka in #161): OI from
        ``open-interest-volume`` is aggregated per base currency (``ccy``), so
        each symbol's history lives as an independent time series in the
        lake. Assessing cross-symbol sentiment correlation at one instant (a
        stress moment) requires a *deliberate* join — it will not happen on
        its own from 5 separate series. This wraps
        :meth:`read_as_of_duckdb` with an as-of ``ASOF`` self-join per symbol
        so callers get one wide table: one row per distinct ``observed_at``
        actually present for the first symbol in ``symbols``, with each
        other symbol's most recent value at-or-before that timestamp in its
        own column (``{value_column}_{symbol}``, symbol lowercased/underscored).

        This is a plain as-of lookup (not a ``shift(1)`` guard like OHLCV
        joins need) because OI rows are point samples, not candles — see
        #164 scope notes. ``available_at`` on each underlying row is what
        keeps this point-in-time-safe: the ``as_of`` cutoff is applied by
        :meth:`read_as_of_duckdb` before any join happens.
        """
        import duckdb

        if len(symbols) < 1:
            raise ValueError("symbols must be non-empty")
        base = self.read_as_of_duckdb(
            dataset_id,
            as_of,
            columns=["symbol", "timeframe", "data_kind", "observed_at", "available_at", value_column],
        )
        connection = duckdb.connect(database=":memory:")
        try:
            connection.register("rows", base)
            filtered = connection.execute(
                """
                SELECT symbol, observed_at, "%s" AS value
                FROM rows
                WHERE data_kind = ? AND timeframe = ?
                ORDER BY symbol, observed_at
                """
                % value_column,
                [data_kind, timeframe],
            ).fetch_arrow_table()
            connection.register("filtered", filtered)

            anchor_symbol = symbols[0]
            select_cols = [f'"{anchor_symbol}"."observed_at" AS observed_at']
            joins = []
            for symbol in symbols:
                alias = symbol.lower().replace("-", "_")
                column = f"{value_column}_{alias}"
                select_cols.append(f'"{symbol}".value AS "{column}"')
                side = (
                    f'(SELECT * FROM filtered WHERE symbol = \'{symbol}\') AS "{symbol}"'
                )
                if symbol == anchor_symbol:
                    joins.append(f"FROM {side}")
                else:
                    joins.append(
                        f'ASOF LEFT JOIN {side} ON "{symbol}".observed_at <= "{anchor_symbol}".observed_at'
                    )
            query = f"SELECT {', '.join(select_cols)} " + " ".join(joins) + f' ORDER BY "{anchor_symbol}".observed_at'
            result = connection.execute(query).fetch_arrow_table()
        finally:
            connection.close()
        return result

    def validate(
        self,
        dataset_id: str,
        *,
        required_symbols: Sequence[str] = SYMBOLS,
        required_timeframes: Sequence[str] = TIMEFRAMES,
    ) -> list[str]:
        table = pq.read_table(self._version_parquet_paths(dataset_id))
        rows = table.to_pylist()
        errors = []
        ohlcv_pairs = {
            (row["symbol"], row["timeframe"])
            for row in rows
            if row["data_kind"] == "ohlcv"
        }
        for symbol in required_symbols:
            for timeframe in required_timeframes:
                if (symbol, timeframe) not in ohlcv_pairs:
                    errors.append(f"missing ohlcv: {symbol}/{timeframe}")
        for index, row in enumerate(rows):
            if _utc_iso(row["available_at"]) < _utc_iso(row["observed_at"]):
                errors.append(f"lookahead timestamp at row {index}")
        return errors
