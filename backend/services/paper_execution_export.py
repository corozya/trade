"""Read-only export of ``paper_execution_ledger`` (SQLite) to Parquet, plus an
as-of join against ``CryptoDataLake`` for historical trade analysis (#169,
podzadanie #161).

Does not touch ``services.paper_execution`` — that module remains the sole
writer of the ledger. This module only reads it.

Unlike ``CryptoDataLake``/``FeatureDatasetStore``, the ledger export is a
plain overwritable dump, not a content-hash-versioned immutable snapshot:
the ledger is a live, growing operational log (new rows on every decision),
so "one immutable version per distinct content" would mean writing a new
Parquet file after every single trade — the versioning that makes sense for
static backfilled market data does not fit a continuously appended table.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Sequence

import pyarrow as pa
import pyarrow.parquet as pq

from services.crypto_data_lake import _utc_iso

LEDGER_COLUMNS = (
    "id",
    "run_id",
    "symbol",
    "decision",
    "side",
    "qty",
    "entry_price",
    "exit_price",
    "stop_loss_price",
    "take_profit_price",
    "fee",
    "pnl",
    "model_version",
    "policy_version",
    "dataset_version",
    "event_json",
    "created_at",
)


def export_paper_ledger_to_parquet(
    conn: sqlite3.Connection, destination: str | Path
) -> Path:
    """Dump the full ``paper_execution_ledger`` table to one Parquet file.

    ``model_version``/``policy_version``/``dataset_version`` are carried
    through unchanged (#169 AC point 3) — grouping/comparing results across
    strategy versions is the reason this export exists, so those columns are
    never dropped or flattened away.
    """
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        f"SELECT {', '.join(LEDGER_COLUMNS)} FROM paper_execution_ledger ORDER BY id"
    ).fetchall()
    columns_data = {column: [row[column] for row in rows] for column in LEDGER_COLUMNS}
    # Normalize to the same "...Z" UTC format CryptoDataLake uses for
    # available_at/observed_at (services.crypto_data_lake._utc_iso) — the
    # as-of join below compares these as plain strings, and Python's
    # isoformat() default ("+00:00") sorts *after* "Z" at an identical
    # instant, which would silently drop an exact-match row from a `<=` join.
    columns_data["created_at"] = [_utc_iso(value) for value in columns_data["created_at"]]
    table = pa.Table.from_pydict(columns_data)
    destination_path = Path(destination)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, destination_path)
    return destination_path


def join_paper_ledger_with_market_data(
    ledger_parquet_path: str | Path,
    market_data_parquet_path: str | Path | Sequence[str | Path],
    *,
    symbols: Sequence[str] | None = None,
    columns: Sequence[str] | None = None,
) -> pa.Table:
    """As-of join: for each ledger row, attach the most recent market_data
    row at-or-before ``created_at`` for the same ``symbol``.

    Same no-lookahead contract as ``CryptoDataLake.read_as_of_duckdb``
    (``available_at <= as_of``) — a trade decision only ever sees market data
    that was actually available at ``created_at``, never a later bar. This is
    plain SQL (no ``CryptoDataLake`` instance required) so it works directly
    against Parquet path(s) already published by #162-166's backfills.
    ``market_data_parquet_path`` accepts either a single path or a sequence
    of paths — a version's content may be split across multiple part files
    (see ``CryptoDataLake._version_parquet_paths``); callers can pass
    ``lake._version_parquet_paths(dataset_id)`` directly.
    """
    import duckdb

    market_paths = (
        [str(market_data_parquet_path)]
        if isinstance(market_data_parquet_path, (str, Path))
        else [str(path) for path in market_data_parquet_path]
    )
    market_columns = set(pq.read_schema(market_paths[0]).names)
    requested_market_columns = list(columns) if columns else sorted(market_columns)
    unknown = set(requested_market_columns) - market_columns
    if unknown:
        raise ValueError(f"unknown market_data columns: {sorted(unknown)}")
    market_projection = ", ".join(f'm."{name}" AS "market_{name}"' for name in requested_market_columns)

    connection = duckdb.connect(database=":memory:")
    try:
        symbol_filter = ""
        params: list[Any] = [str(ledger_parquet_path), market_paths]
        if symbols:
            placeholders = ", ".join("?" for _ in symbols)
            symbol_filter = f"WHERE l.symbol IN ({placeholders})"
            params.extend(symbols)
        query = f"""
            SELECT l.* EXCLUDE (event_json), {market_projection}
            FROM read_parquet(?) AS l
            ASOF LEFT JOIN read_parquet(?) AS m
                ON m.symbol = l.symbol AND m.available_at <= l.created_at
            {symbol_filter}
            ORDER BY l.created_at
        """
        result = connection.execute(query, params).fetch_arrow_table()
    finally:
        connection.close()
    return result
