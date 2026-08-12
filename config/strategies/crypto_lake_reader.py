"""Read-only access from freqtrade strategies to CryptoDataLake
(research/agent-krypto/raw/), mounted read-only into the container at
/freqtrade/user_data/crypto_lake (see docker-compose.yml).

CryptoDataLake stores every data_kind (ohlcv, funding, open_interest,
taker_volume, long_short_ratio, costs, market_impact, contract_metadata, and
the precomputed rsi/macd/stochastic/risk_indicator/atr/support_resistance
series) as immutable, content-addressed Parquet datasets, tracked by
raw/latest.json (a "{data_kind}/{symbol}/{timeframe}" -> dataset_id
registry). Freqtrade pairs use ccxt's unified format ("BTC/USDT:USDT");
CryptoDataLake uses the OKX instId ("BTC-USDT-SWAP") — the two must be
mapped explicitly, there is no reliable string transform for every symbol
(e.g. WLD-USD_UM_XPERP-310613 in research/agent-krypto/backend/config/
crypto_backfill_symbols.json), so only pairs actually in use are listed.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq

LAKE_ROOT = Path("/freqtrade/user_data/crypto_lake")

# ccxt unified pair -> OKX instId, extend as new freqtrade pairs are added.
PAIR_TO_INST_ID = {
    "BTC/USDT:USDT": "BTC-USDT-SWAP",
    "ETH/USDT:USDT": "ETH-USDT-SWAP",
}


def _registry() -> dict[str, str]:
    return json.loads((LAKE_ROOT / "raw" / "latest.json").read_text())


def _version_dir(dataset_id: str) -> Path:
    return LAKE_ROOT / "raw" / "versions" / dataset_id


def _parquet_paths(dataset_id: str) -> list[str]:
    """Resolve a dataset's Parquet part files under the *current* LAKE_ROOT.

    Inherited parts (see CryptoDataLake._split_against_base) are recorded in
    the manifest as absolute host paths pointing at another version's
    directory. Those host paths don't exist inside the freqtrade container
    (the lake is mounted at a different mountpoint) — every part, inherited
    or not, is re-rooted here to "raw/versions/<id>/<name>" under this
    process's own LAKE_ROOT rather than trusted as an absolute path.
    """
    manifest = json.loads((_version_dir(dataset_id) / "manifest.json").read_text())
    parts = manifest.get("storage", {}).get("parts") or ["market_data.parquet"]
    resolved = []
    for name in parts:
        rel = Path(*Path(name).parts[-2:]) if Path(name).is_absolute() else Path(name)
        if len(rel.parts) == 2 and rel.parts[0].startswith("market-"):
            resolved.append(str(LAKE_ROOT / "raw" / "versions" / rel))
        else:
            resolved.append(str(_version_dir(dataset_id) / rel))
    return resolved


@lru_cache(maxsize=None)
def _read_dataset(dataset_id: str) -> pd.DataFrame:
    table = pq.read_table(_parquet_paths(dataset_id))
    return table.to_pandas()


def read_lake_series(pair: str, timeframe: str, data_kind: str) -> pd.DataFrame | None:
    """Return the latest published dataset for (pair, timeframe, data_kind),
    or None if the pair isn't mapped or nothing has been published yet.

    Columns always include ``observed_at``/``available_at`` (UTC ISO
    strings) plus whatever fields that data_kind carries (e.g. ``rsi``,
    ``atr``, ``funding_rate``, ``taker_buy_volume``/``taker_sell_volume``).
    Callers must join on ``available_at`` (not ``observed_at``) to avoid
    look-ahead — a bar's derived indicators become available only once the
    bar itself has closed.
    """
    inst_id = PAIR_TO_INST_ID.get(pair)
    if inst_id is None:
        return None
    key = f"{data_kind}/{inst_id}/{timeframe}"
    dataset_id = _registry().get(key)
    if dataset_id is None:
        return None
    return _read_dataset(dataset_id).copy()


def merge_lake_series(
    dataframe: pd.DataFrame,
    pair: str,
    timeframe: str,
    data_kind: str,
    *,
    value_columns: list[str],
    prefix: str | None = None,
) -> pd.DataFrame:
    """As-of merge ``value_columns`` from a CryptoDataLake series onto
    ``dataframe`` (a freqtrade OHLCV dataframe with a ``date`` column),
    matching each candle to the latest lake row whose ``available_at`` is
    not after the candle's own timestamp. No-op (columns left absent) if the
    series isn't published for this pair/timeframe/data_kind yet.
    """
    series = read_lake_series(pair, timeframe, data_kind)
    if series is None or series.empty:
        return dataframe

    series = series.sort_values("available_at").copy()
    series["available_at"] = pd.to_datetime(series["available_at"], utc=True)
    rename = {col: f"{prefix or data_kind}_{col}" for col in value_columns}
    series = series.rename(columns=rename)

    left = dataframe.sort_values("date").copy()
    left_date_col = "date"
    if left["date"].dt.tz is None:
        left[left_date_col] = left["date"].dt.tz_localize("UTC")

    merged = pd.merge_asof(
        left,
        series[["available_at", *rename.values()]],
        left_on=left_date_col,
        right_on="available_at",
        direction="backward",
    )
    return merged.drop(columns=["available_at"])
