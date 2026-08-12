"""Guarded, reproducible research operations for agent-krypto.

The module deliberately keeps data and registries local.  Feature datasets are
content-addressed Parquet snapshots; MLflow and Chroma are embedded clients,
not network services.  Unknown operations are never dispatched.
"""

from __future__ import annotations

import hashlib
import json
import math
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import pyarrow as pa
import pyarrow.parquet as pq


REVIEW_REQUIRED = "REVIEW_REQUIRED"
EXECUTED = "EXECUTED"


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def _digest(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def _validate_request_shape(request: Mapping[str, Any], required: set[str]) -> None:
    missing = required - request.keys()
    if missing:
        raise ValueError(f"request is missing required fields: {sorted(missing)}")


@dataclass(frozen=True)
class ToolDecision:
    status: str
    tool: str
    result: Mapping[str, Any] | None = None
    reason: str | None = None


class ResearchToolGate:
    """Allow-list gate.  Dispatch occurs only after catalog validation."""

    def __init__(self, catalog_path: str | Path):
        self.catalog = _load_json(Path(catalog_path))

    def decide(self, request: Mapping[str, Any]) -> ToolDecision:
        _validate_request_shape(
            request, {"request_id", "requested_by", "tool", "params", "reason"}
        )
        tool = str(request["tool"])
        if tool not in self.catalog["tools"]:
            return ToolDecision(
                status=REVIEW_REQUIRED,
                tool=tool,
                reason="tool is outside the approved research catalog",
            )
        return ToolDecision(status=EXECUTED, tool=tool)

    def execute(self, request: Mapping[str, Any], handlers: Mapping[str, Any]) -> ToolDecision:
        decision = self.decide(request)
        if decision.status != EXECUTED:
            return decision
        handler = handlers.get(decision.tool)
        if handler is None:
            return ToolDecision(
                status=REVIEW_REQUIRED,
                tool=decision.tool,
                reason="approved tool has no configured executor",
            )
        return ToolDecision(
            status=EXECUTED,
            tool=decision.tool,
            result=handler(dict(request["params"])),
        )


class FeatureDatasetStore:
    """Creates immutable derived versions from an immutable market snapshot."""

    def __init__(self, root: str | Path, feature_catalog_path: str | Path):
        self.root = Path(root)
        self.catalog = _load_json(Path(feature_catalog_path))
        self.versions_dir = self.root / "datasets"

    def bollinger_bands(
        self,
        base_dataset_path: str | Path,
        *,
        base_dataset_version: str,
        symbols: Sequence[str] | None = None,
        window: int = 20,
        stddev: float = 2.0,
    ) -> Path:
        spec = self.catalog["features"]["bollinger_bands"]["params"]
        if not spec["window"]["min"] <= window <= spec["window"]["max"]:
            raise ValueError("window outside feature catalog")
        if not spec["stddev"]["min"] <= stddev <= spec["stddev"]["max"]:
            raise ValueError("stddev outside feature catalog")

        source_path = Path(base_dataset_path)
        source_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
        table = pq.read_table(source_path)
        required_columns = {"close", "symbol", "observed_at"}
        missing_columns = required_columns - set(table.column_names)
        if missing_columns:
            raise ValueError(f"base dataset requires columns: {sorted(missing_columns)}")
        dataset_symbols = {str(value) for value in table["symbol"].to_pylist()}
        if symbols is not None:
            missing_symbols = set(symbols) - dataset_symbols
            if missing_symbols:
                raise ValueError(
                    f"requested symbols missing from base dataset: {sorted(missing_symbols)}"
                )
        closes = [float(value) for value in table["close"].to_pylist()]
        rows_by_symbol: dict[str, list[int]] = {}
        raw_symbols = [str(value) for value in table["symbol"].to_pylist()]
        observed_at = [str(value) for value in table["observed_at"].to_pylist()]
        for row_index, symbol in enumerate(raw_symbols):
            rows_by_symbol.setdefault(symbol, []).append(row_index)

        columns: tuple[list[float | None], ...] = tuple(
            [None] * len(closes) for _ in range(5)
        )
        for symbol_indices in rows_by_symbol.values():
            ordered_indices = sorted(symbol_indices, key=lambda index: observed_at[index])
            symbol_closes = [closes[index] for index in ordered_indices]
            for position, row_index in enumerate(ordered_indices):
                close = closes[row_index]
                if position + 1 < window:
                    values = (None, None, None, None, None)
                else:
                    sample = symbol_closes[position + 1 - window : position + 1]
                    mean = sum(sample) / window
                    variance = sum((value - mean) ** 2 for value in sample) / window
                    sigma = math.sqrt(variance)
                    high, low = mean + stddev * sigma, mean - stddev * sigma
                    band_width = high - low
                    values = (
                        mean,
                        high,
                        low,
                        band_width / mean if mean else None,
                        (close - low) / band_width if band_width else 0.5,
                    )
                for target, value in zip(columns, values):
                    target[row_index] = value

        result = table
        for name, values in zip(
            ("bb_mid", "bb_upper", "bb_lower", "bb_width", "bb_percent_b"),
            columns,
        ):
            result = result.append_column(name, pa.array(values, type=pa.float64()))
        lineage = {
            "base_dataset_version": base_dataset_version,
            "base_content_sha256": source_hash,
            "feature": "bollinger_bands",
            "params": {"window": window, "stddev": stddev},
        }
        version_id = f"features-{_digest(lineage)[:16]}"
        destination = self.versions_dir / version_id
        if destination.exists():
            return destination
        self.versions_dir.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=f".{version_id}-", dir=self.versions_dir))
        try:
            pq.write_table(result, temporary / "features.parquet")
            (temporary / "manifest.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "dataset_version": version_id,
                        "created_at": datetime.now(timezone.utc).isoformat(),
                        "lineage": lineage,
                        "row_count": result.num_rows,
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n"
            )
            temporary.rename(destination)
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise
        if hashlib.sha256(source_path.read_bytes()).hexdigest() != source_hash:
            raise RuntimeError("base dataset was mutated")
        return destination

    def rsi(
        self,
        base_dataset_path: str | Path,
        *,
        base_dataset_version: str,
        symbols: Sequence[str] | None = None,
        period: int = 14,
    ) -> Path:
        """Wilder's RSI, one ``rsi_value`` column, over the ``close`` series
        per symbol. Same shape/lineage/versioning contract as
        ``bollinger_bands`` above — this is the second signal_feature family
        (#150 escalation follow-up: bb_percent_b and close/open/high/low both
        failed cost-adjusted walk-forward on BTC-USDT-SWAP, RSI is a
        different indicator family, not a re-tuning of either)."""
        spec = self.catalog["features"]["rsi"]["params"]
        if not spec["period"]["min"] <= period <= spec["period"]["max"]:
            raise ValueError("period outside feature catalog")

        source_path = Path(base_dataset_path)
        source_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
        table = pq.read_table(source_path)
        required_columns = {"close", "symbol", "observed_at"}
        missing_columns = required_columns - set(table.column_names)
        if missing_columns:
            raise ValueError(f"base dataset requires columns: {sorted(missing_columns)}")
        dataset_symbols = {str(value) for value in table["symbol"].to_pylist()}
        if symbols is not None:
            missing_symbols = set(symbols) - dataset_symbols
            if missing_symbols:
                raise ValueError(
                    f"requested symbols missing from base dataset: {sorted(missing_symbols)}"
                )
        closes = [float(value) for value in table["close"].to_pylist()]
        raw_symbols = [str(value) for value in table["symbol"].to_pylist()]
        observed_at = [str(value) for value in table["observed_at"].to_pylist()]
        rows_by_symbol: dict[str, list[int]] = {}
        for row_index, symbol in enumerate(raw_symbols):
            rows_by_symbol.setdefault(symbol, []).append(row_index)

        rsi_values: list[float | None] = [None] * len(closes)
        for symbol_indices in rows_by_symbol.values():
            ordered_indices = sorted(symbol_indices, key=lambda index: observed_at[index])
            symbol_closes = [closes[index] for index in ordered_indices]
            deltas = [
                symbol_closes[i] - symbol_closes[i - 1] for i in range(1, len(symbol_closes))
            ]
            avg_gain: float | None = None
            avg_loss: float | None = None
            for position, row_index in enumerate(ordered_indices):
                if position < period:
                    continue
                if avg_gain is None:
                    window_deltas = deltas[position - period : position]
                    avg_gain = sum(max(d, 0.0) for d in window_deltas) / period
                    avg_loss = sum(max(-d, 0.0) for d in window_deltas) / period
                else:
                    delta = deltas[position - 1]
                    gain = max(delta, 0.0)
                    loss = max(-delta, 0.0)
                    avg_gain = (avg_gain * (period - 1) + gain) / period
                    avg_loss = (avg_loss * (period - 1) + loss) / period
                if avg_loss == 0:
                    rsi_values[row_index] = 100.0
                else:
                    rs = avg_gain / avg_loss
                    rsi_values[row_index] = 100.0 - (100.0 / (1.0 + rs))

        result = table.append_column(
            "rsi_value", pa.array(rsi_values, type=pa.float64())
        )
        lineage = {
            "base_dataset_version": base_dataset_version,
            "base_content_sha256": source_hash,
            "feature": "rsi",
            "params": {"period": period},
        }
        version_id = f"features-{_digest(lineage)[:16]}"
        destination = self.versions_dir / version_id
        if destination.exists():
            return destination
        self.versions_dir.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=f".{version_id}-", dir=self.versions_dir))
        try:
            pq.write_table(result, temporary / "features.parquet")
            (temporary / "manifest.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "dataset_version": version_id,
                        "created_at": datetime.now(timezone.utc).isoformat(),
                        "lineage": lineage,
                        "row_count": result.num_rows,
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n"
            )
            temporary.rename(destination)
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise
        if hashlib.sha256(source_path.read_bytes()).hexdigest() != source_hash:
            raise RuntimeError("base dataset was mutated")
        return destination

    def obv(
        self,
        base_dataset_path: str | Path,
        *,
        base_dataset_version: str,
        symbols: Sequence[str] | None = None,
        slope_window: int = 20,
    ) -> Path:
        """On-Balance Volume with a trailing linear-regression slope, one
        ``obv_value``/``obv_slope`` column pair over the (close, volume)
        series per symbol. Same shape/lineage/versioning contract as
        ``bollinger_bands``/``rsi`` above — third signal_feature family
        (#168, priority per #161 multi-agent consultation: this is one of
        the two indicators — with ADX — that actually drive agent-krypto's
        live trend-following decisions today, so a backtest without them
        would not reproduce the live logic). Formula matches
        ``scripts/analyze_crypto_market_data.py::_volume_indicators`` (OBV =
        cumulative sum of signed volume; slope = OLS slope of the trailing
        ``slope_window`` OBV values), just computed at every row instead of
        only the latest bar.
        """
        spec = self.catalog["features"]["obv"]["params"]
        if not spec["slope_window"]["min"] <= slope_window <= spec["slope_window"]["max"]:
            raise ValueError("slope_window outside feature catalog")

        source_path = Path(base_dataset_path)
        source_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
        table = pq.read_table(source_path)
        required_columns = {"close", "volume", "symbol", "observed_at"}
        missing_columns = required_columns - set(table.column_names)
        if missing_columns:
            raise ValueError(f"base dataset requires columns: {sorted(missing_columns)}")
        dataset_symbols = {str(value) for value in table["symbol"].to_pylist()}
        if symbols is not None:
            missing_symbols = set(symbols) - dataset_symbols
            if missing_symbols:
                raise ValueError(
                    f"requested symbols missing from base dataset: {sorted(missing_symbols)}"
                )
        closes = [float(value) for value in table["close"].to_pylist()]
        volumes = [float(value) for value in table["volume"].to_pylist()]
        raw_symbols = [str(value) for value in table["symbol"].to_pylist()]
        observed_at = [str(value) for value in table["observed_at"].to_pylist()]
        rows_by_symbol: dict[str, list[int]] = {}
        for row_index, symbol in enumerate(raw_symbols):
            rows_by_symbol.setdefault(symbol, []).append(row_index)

        obv_values: list[float | None] = [None] * len(closes)
        obv_slopes: list[float | None] = [None] * len(closes)
        x_mean = (slope_window - 1) / 2.0
        denominator = sum((i - x_mean) ** 2 for i in range(slope_window))
        for symbol_indices in rows_by_symbol.values():
            ordered_indices = sorted(symbol_indices, key=lambda index: observed_at[index])
            symbol_closes = [closes[index] for index in ordered_indices]
            symbol_volumes = [volumes[index] for index in ordered_indices]
            running_obv = 0.0
            symbol_obv: list[float] = []
            for position in range(len(symbol_closes)):
                if position > 0:
                    delta = symbol_closes[position] - symbol_closes[position - 1]
                    direction = 1.0 if delta > 0 else (-1.0 if delta < 0 else 0.0)
                    running_obv += direction * symbol_volumes[position]
                symbol_obv.append(running_obv)
                row_index = ordered_indices[position]
                obv_values[row_index] = running_obv
                if position + 1 >= slope_window:
                    window = symbol_obv[position + 1 - slope_window : position + 1]
                    window_mean = sum(window) / slope_window
                    slope = sum(
                        (i - x_mean) * (window[i] - window_mean) for i in range(slope_window)
                    ) / denominator
                    obv_slopes[row_index] = slope

        result = table
        for name, values in (("obv_value", obv_values), ("obv_slope", obv_slopes)):
            result = result.append_column(name, pa.array(values, type=pa.float64()))
        lineage = {
            "base_dataset_version": base_dataset_version,
            "base_content_sha256": source_hash,
            "feature": "obv",
            "params": {"slope_window": slope_window},
        }
        version_id = f"features-{_digest(lineage)[:16]}"
        destination = self.versions_dir / version_id
        if destination.exists():
            return destination
        self.versions_dir.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=f".{version_id}-", dir=self.versions_dir))
        try:
            pq.write_table(result, temporary / "features.parquet")
            (temporary / "manifest.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "dataset_version": version_id,
                        "created_at": datetime.now(timezone.utc).isoformat(),
                        "lineage": lineage,
                        "row_count": result.num_rows,
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n"
            )
            temporary.rename(destination)
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise
        if hashlib.sha256(source_path.read_bytes()).hexdigest() != source_hash:
            raise RuntimeError("base dataset was mutated")
        return destination

    def adx(
        self,
        base_dataset_path: str | Path,
        *,
        base_dataset_version: str,
        symbols: Sequence[str] | None = None,
        period: int = 14,
    ) -> Path:
        """Wilder's ADX with +DI/-DI, one ``adx_value``/``plus_di``/``minus_di``
        column triple over the (high, low, close) series per symbol. Same
        shape/lineage/versioning contract as ``bollinger_bands``/``rsi``/``obv``
        above — fourth signal_feature family (#168, priority per #161
        multi-agent consultation alongside OBV — see ``obv`` docstring for
        why). Formula matches
        ``scripts/analyze_crypto_market_data.py::_adx`` (Wilder-smoothed ATR,
        +DM/-DM, DX, ADX — all via the same recursive EWM-style update as
        ``rsi`` above uses for avg_gain/avg_loss), computed at every row
        instead of only the latest bar. Needs 2*period warmed-up bars before
        the first non-null ADX value (the DX series itself needs `period`
        bars before its own Wilder smoothing can start).
        """
        spec = self.catalog["features"]["adx"]["params"]
        if not spec["period"]["min"] <= period <= spec["period"]["max"]:
            raise ValueError("period outside feature catalog")

        source_path = Path(base_dataset_path)
        source_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
        table = pq.read_table(source_path)
        required_columns = {"high", "low", "close", "symbol", "observed_at"}
        missing_columns = required_columns - set(table.column_names)
        if missing_columns:
            raise ValueError(f"base dataset requires columns: {sorted(missing_columns)}")
        dataset_symbols = {str(value) for value in table["symbol"].to_pylist()}
        if symbols is not None:
            missing_symbols = set(symbols) - dataset_symbols
            if missing_symbols:
                raise ValueError(
                    f"requested symbols missing from base dataset: {sorted(missing_symbols)}"
                )
        highs = [float(value) for value in table["high"].to_pylist()]
        lows = [float(value) for value in table["low"].to_pylist()]
        closes = [float(value) for value in table["close"].to_pylist()]
        raw_symbols = [str(value) for value in table["symbol"].to_pylist()]
        observed_at = [str(value) for value in table["observed_at"].to_pylist()]
        rows_by_symbol: dict[str, list[int]] = {}
        for row_index, symbol in enumerate(raw_symbols):
            rows_by_symbol.setdefault(symbol, []).append(row_index)

        adx_values: list[float | None] = [None] * len(closes)
        plus_di_values: list[float | None] = [None] * len(closes)
        minus_di_values: list[float | None] = [None] * len(closes)
        for symbol_indices in rows_by_symbol.values():
            ordered_indices = sorted(symbol_indices, key=lambda index: observed_at[index])
            symbol_highs = [highs[index] for index in ordered_indices]
            symbol_lows = [lows[index] for index in ordered_indices]
            symbol_closes = [closes[index] for index in ordered_indices]

            true_ranges: list[float] = [0.0]
            plus_dms: list[float] = [0.0]
            minus_dms: list[float] = [0.0]
            for position in range(1, len(symbol_closes)):
                up_move = symbol_highs[position] - symbol_highs[position - 1]
                down_move = symbol_lows[position - 1] - symbol_lows[position]
                plus_dm = up_move if (up_move > down_move and up_move > 0) else 0.0
                minus_dm = down_move if (down_move > up_move and down_move > 0) else 0.0
                true_range = max(
                    symbol_highs[position] - symbol_lows[position],
                    abs(symbol_highs[position] - symbol_closes[position - 1]),
                    abs(symbol_lows[position] - symbol_closes[position - 1]),
                )
                true_ranges.append(true_range)
                plus_dms.append(plus_dm)
                minus_dms.append(minus_dm)

            avg_tr: float | None = None
            avg_plus_dm: float | None = None
            avg_minus_dm: float | None = None
            dx_values: list[float | None] = [None] * len(symbol_closes)
            for position in range(len(symbol_closes)):
                if position < period:
                    continue
                if avg_tr is None:
                    window = range(position + 1 - period, position + 1)
                    avg_tr = sum(true_ranges[i] for i in window) / period
                    avg_plus_dm = sum(plus_dms[i] for i in window) / period
                    avg_minus_dm = sum(minus_dms[i] for i in window) / period
                else:
                    avg_tr = (avg_tr * (period - 1) + true_ranges[position]) / period
                    avg_plus_dm = (avg_plus_dm * (period - 1) + plus_dms[position]) / period
                    avg_minus_dm = (avg_minus_dm * (period - 1) + minus_dms[position]) / period
                plus_di = 100.0 * avg_plus_dm / avg_tr if avg_tr else 0.0
                minus_di = 100.0 * avg_minus_dm / avg_tr if avg_tr else 0.0
                di_sum = plus_di + minus_di
                dx = 100.0 * abs(plus_di - minus_di) / di_sum if di_sum else 0.0
                dx_values[position] = dx
                row_index = ordered_indices[position]
                plus_di_values[row_index] = plus_di
                minus_di_values[row_index] = minus_di

            avg_dx: float | None = None
            for position in range(len(symbol_closes)):
                if dx_values[position] is None:
                    continue
                if position < 2 * period - 1:
                    continue
                if avg_dx is None:
                    window = [dx_values[i] for i in range(position + 1 - period, position + 1)]
                    avg_dx = sum(window) / period
                else:
                    avg_dx = (avg_dx * (period - 1) + dx_values[position]) / period
                row_index = ordered_indices[position]
                adx_values[row_index] = avg_dx

        result = table
        for name, values in (
            ("adx_value", adx_values),
            ("plus_di", plus_di_values),
            ("minus_di", minus_di_values),
        ):
            result = result.append_column(name, pa.array(values, type=pa.float64()))
        lineage = {
            "base_dataset_version": base_dataset_version,
            "base_content_sha256": source_hash,
            "feature": "adx",
            "params": {"period": period},
        }
        version_id = f"features-{_digest(lineage)[:16]}"
        destination = self.versions_dir / version_id
        if destination.exists():
            return destination
        self.versions_dir.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=f".{version_id}-", dir=self.versions_dir))
        try:
            pq.write_table(result, temporary / "features.parquet")
            (temporary / "manifest.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "dataset_version": version_id,
                        "created_at": datetime.now(timezone.utc).isoformat(),
                        "lineage": lineage,
                        "row_count": result.num_rows,
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n"
            )
            temporary.rename(destination)
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise
        if hashlib.sha256(source_path.read_bytes()).hexdigest() != source_hash:
            raise RuntimeError("base dataset was mutated")
        return destination

    def macd(
        self,
        base_dataset_path: str | Path,
        *,
        base_dataset_version: str,
        symbols: Sequence[str] | None = None,
        fast: int = 12,
        slow: int = 26,
        signal: int = 9,
    ) -> Path:
        """MACD (fast EMA - slow EMA, plus its own EMA signal line), one
        ``macd_line``/``macd_signal``/``macd_histogram`` column triple over
        the ``close`` series per symbol. Same shape/lineage/versioning
        contract as ``bollinger_bands``/``rsi`` above (#168). Formula matches
        ``scripts/analyze_crypto_market_data.py::_macd``
        (``close.ewm(span=N, adjust=False).mean()``), computed at every row
        instead of only the latest bar — ``adjust=False`` EMA is exactly the
        recursive update ``ema = ema + alpha * (price - ema)`` with
        ``alpha = 2 / (span + 1)``, seeded by the first close.
        """
        spec = self.catalog["features"]["macd"]["params"]
        if not spec["fast"]["min"] <= fast <= spec["fast"]["max"]:
            raise ValueError("fast outside feature catalog")
        if not spec["slow"]["min"] <= slow <= spec["slow"]["max"]:
            raise ValueError("slow outside feature catalog")
        if not spec["signal"]["min"] <= signal <= spec["signal"]["max"]:
            raise ValueError("signal outside feature catalog")
        if fast >= slow:
            raise ValueError("fast must be smaller than slow")

        source_path = Path(base_dataset_path)
        source_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
        table = pq.read_table(source_path)
        required_columns = {"close", "symbol", "observed_at"}
        missing_columns = required_columns - set(table.column_names)
        if missing_columns:
            raise ValueError(f"base dataset requires columns: {sorted(missing_columns)}")
        dataset_symbols = {str(value) for value in table["symbol"].to_pylist()}
        if symbols is not None:
            missing_symbols = set(symbols) - dataset_symbols
            if missing_symbols:
                raise ValueError(
                    f"requested symbols missing from base dataset: {sorted(missing_symbols)}"
                )
        closes = [float(value) for value in table["close"].to_pylist()]
        raw_symbols = [str(value) for value in table["symbol"].to_pylist()]
        observed_at = [str(value) for value in table["observed_at"].to_pylist()]
        rows_by_symbol: dict[str, list[int]] = {}
        for row_index, symbol in enumerate(raw_symbols):
            rows_by_symbol.setdefault(symbol, []).append(row_index)

        line_values: list[float | None] = [None] * len(closes)
        signal_values: list[float | None] = [None] * len(closes)
        histogram_values: list[float | None] = [None] * len(closes)
        alpha_fast = 2.0 / (fast + 1)
        alpha_slow = 2.0 / (slow + 1)
        alpha_signal = 2.0 / (signal + 1)
        for symbol_indices in rows_by_symbol.values():
            ordered_indices = sorted(symbol_indices, key=lambda index: observed_at[index])
            symbol_closes = [closes[index] for index in ordered_indices]
            ema_fast: float | None = None
            ema_slow: float | None = None
            ema_signal: float | None = None
            for position, row_index in enumerate(ordered_indices):
                close = symbol_closes[position]
                ema_fast = close if ema_fast is None else ema_fast + alpha_fast * (close - ema_fast)
                ema_slow = close if ema_slow is None else ema_slow + alpha_slow * (close - ema_slow)
                line = ema_fast - ema_slow
                ema_signal = line if ema_signal is None else ema_signal + alpha_signal * (line - ema_signal)
                line_values[row_index] = line
                signal_values[row_index] = ema_signal
                histogram_values[row_index] = line - ema_signal

        result = table
        for name, values in (
            ("macd_line", line_values),
            ("macd_signal", signal_values),
            ("macd_histogram", histogram_values),
        ):
            result = result.append_column(name, pa.array(values, type=pa.float64()))
        lineage = {
            "base_dataset_version": base_dataset_version,
            "base_content_sha256": source_hash,
            "feature": "macd",
            "params": {"fast": fast, "slow": slow, "signal": signal},
        }
        version_id = f"features-{_digest(lineage)[:16]}"
        destination = self.versions_dir / version_id
        if destination.exists():
            return destination
        self.versions_dir.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=f".{version_id}-", dir=self.versions_dir))
        try:
            pq.write_table(result, temporary / "features.parquet")
            (temporary / "manifest.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "dataset_version": version_id,
                        "created_at": datetime.now(timezone.utc).isoformat(),
                        "lineage": lineage,
                        "row_count": result.num_rows,
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n"
            )
            temporary.rename(destination)
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise
        if hashlib.sha256(source_path.read_bytes()).hexdigest() != source_hash:
            raise RuntimeError("base dataset was mutated")
        return destination

    def vwap(
        self,
        base_dataset_path: str | Path,
        *,
        base_dataset_version: str,
        symbols: Sequence[str] | None = None,
        window: int = 20,
    ) -> Path:
        """Rolling VWAP (typical price weighted by volume over a trailing
        window) plus distance of ``close`` from it, one
        ``vwap_value``/``vwap_distance_pct`` column pair over the (high, low,
        close, volume) series per symbol. Same shape/lineage/versioning
        contract as ``bollinger_bands``/``rsi`` above (#168).

        Unlike ``scripts/analyze_crypto_market_data.py::_rolling_vwap``
        (which averages over the *entire* available snapshot window, since
        that script only ever sees one fixed history slice), this uses a
        trailing ``window``-bar rolling window per row — the catalog's
        ``vwap.window`` parameter needs a real rolling size to be meaningful
        over full backfilled history, and a running-since-inception VWAP
        would not be comparable across different points in a long history.
        """
        spec = self.catalog["features"]["vwap"]["params"]
        if not spec["window"]["min"] <= window <= spec["window"]["max"]:
            raise ValueError("window outside feature catalog")

        source_path = Path(base_dataset_path)
        source_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
        table = pq.read_table(source_path)
        required_columns = {"high", "low", "close", "volume", "symbol", "observed_at"}
        missing_columns = required_columns - set(table.column_names)
        if missing_columns:
            raise ValueError(f"base dataset requires columns: {sorted(missing_columns)}")
        dataset_symbols = {str(value) for value in table["symbol"].to_pylist()}
        if symbols is not None:
            missing_symbols = set(symbols) - dataset_symbols
            if missing_symbols:
                raise ValueError(
                    f"requested symbols missing from base dataset: {sorted(missing_symbols)}"
                )
        highs = [float(value) for value in table["high"].to_pylist()]
        lows = [float(value) for value in table["low"].to_pylist()]
        closes = [float(value) for value in table["close"].to_pylist()]
        volumes = [float(value) for value in table["volume"].to_pylist()]
        raw_symbols = [str(value) for value in table["symbol"].to_pylist()]
        observed_at = [str(value) for value in table["observed_at"].to_pylist()]
        rows_by_symbol: dict[str, list[int]] = {}
        for row_index, symbol in enumerate(raw_symbols):
            rows_by_symbol.setdefault(symbol, []).append(row_index)

        vwap_values: list[float | None] = [None] * len(closes)
        distance_values: list[float | None] = [None] * len(closes)
        for symbol_indices in rows_by_symbol.values():
            ordered_indices = sorted(symbol_indices, key=lambda index: observed_at[index])
            symbol_typical = [
                (highs[index] + lows[index] + closes[index]) / 3.0 for index in ordered_indices
            ]
            symbol_volumes = [volumes[index] for index in ordered_indices]
            symbol_closes = [closes[index] for index in ordered_indices]
            for position, row_index in enumerate(ordered_indices):
                if position + 1 < window:
                    continue
                window_slice = range(position + 1 - window, position + 1)
                window_volume = sum(symbol_volumes[i] for i in window_slice)
                if not window_volume:
                    continue
                vwap = sum(symbol_typical[i] * symbol_volumes[i] for i in window_slice) / window_volume
                vwap_values[row_index] = vwap
                distance_values[row_index] = (
                    (symbol_closes[position] - vwap) / vwap * 100.0 if vwap else None
                )

        result = table
        for name, values in (("vwap_value", vwap_values), ("vwap_distance_pct", distance_values)):
            result = result.append_column(name, pa.array(values, type=pa.float64()))
        lineage = {
            "base_dataset_version": base_dataset_version,
            "base_content_sha256": source_hash,
            "feature": "vwap",
            "params": {"window": window},
        }
        version_id = f"features-{_digest(lineage)[:16]}"
        destination = self.versions_dir / version_id
        if destination.exists():
            return destination
        self.versions_dir.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=f".{version_id}-", dir=self.versions_dir))
        try:
            pq.write_table(result, temporary / "features.parquet")
            (temporary / "manifest.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "dataset_version": version_id,
                        "created_at": datetime.now(timezone.utc).isoformat(),
                        "lineage": lineage,
                        "row_count": result.num_rows,
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n"
            )
            temporary.rename(destination)
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise
        if hashlib.sha256(source_path.read_bytes()).hexdigest() != source_hash:
            raise RuntimeError("base dataset was mutated")
        return destination


def build_renko(
    closes: Sequence[float], *, brick_size: float
) -> dict[str, Any]:
    """Build close-only, causal Renko bricks and a self-contained SVG chart."""
    if not closes or brick_size <= 0:
        raise ValueError("non-empty closes and positive brick_size are required")
    level = float(closes[0])
    bricks: list[dict[str, Any]] = []
    for source_index, raw_close in enumerate(closes[1:], start=1):
        close = float(raw_close)
        while close >= level + brick_size:
            opening = level
            level += brick_size
            bricks.append(
                {"source_index": source_index, "open": opening, "close": level, "direction": "up"}
            )
        while close <= level - brick_size:
            opening = level
            level -= brick_size
            bricks.append(
                {"source_index": source_index, "open": opening, "close": level, "direction": "down"}
            )
    width = max(120, len(bricks) * 12 + 20)
    if bricks:
        levels = [value for brick in bricks for value in (brick["open"], brick["close"])]
        low, high = min(levels), max(levels)
        span = high - low or brick_size
        rects = []
        for index, brick in enumerate(bricks):
            top = 10 + (high - max(brick["open"], brick["close"])) / span * 80
            height = max(2.0, abs(brick["close"] - brick["open"]) / span * 80)
            color = "#16a34a" if brick["direction"] == "up" else "#dc2626"
            rects.append(
                f'<rect x="{10 + index * 12}" y="{top:.2f}" width="10" '
                f'height="{height:.2f}" fill="{color}"/>'
            )
        body = "".join(rects)
    else:
        body = ""
    chart = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="100" '
        f'viewBox="0 0 {width} 100">{body}</svg>'
    )
    return {"brick_table": bricks, "chart": chart, "brick_size": brick_size}


class MlflowExperimentRegistry:
    """Small adapter enforcing complete MLflow lineage for every run."""

    REQUIRED_LINEAGE = {
        "dataset_version",
        "strategy_version",
        "code_version",
        "time_range",
        "data_stage",
    }

    def __init__(self, tracking_uri: str, experiment_name: str):
        try:
            import mlflow
        except ImportError as exc:
            raise RuntimeError("install pinned mlflow in the project environment") from exc
        self.mlflow = mlflow
        mlflow.set_tracking_uri(tracking_uri)
        mlflow.set_experiment(experiment_name)

    def record(
        self,
        *,
        lineage: Mapping[str, Any],
        params: Mapping[str, Any],
        costs: Mapping[str, float],
        results: Mapping[str, float],
    ) -> str:
        missing = self.REQUIRED_LINEAGE - lineage.keys()
        if missing:
            raise ValueError(f"missing experiment lineage: {sorted(missing)}")
        with self.mlflow.start_run() as run:
            self.mlflow.log_params({f"lineage.{key}": value for key, value in lineage.items()})
            self.mlflow.log_params(dict(params))
            self.mlflow.log_metrics({f"cost.{key}": value for key, value in costs.items()})
            self.mlflow.log_metrics({f"result.{key}": value for key, value in results.items()})
            return run.info.run_id


class ChromaResearchNotes:
    REQUIRED_METADATA = {
        "experiment_id",
        "dataset_version",
        "strategy_version",
        "time_range",
        "data_stage",
    }

    def __init__(self, path: str | Path):
        import chromadb

        self.collection = chromadb.PersistentClient(path=str(path)).get_or_create_collection(
            "agent_krypto_research"
        )

    def append(self, note_id: str, document: str, metadata: Mapping[str, Any]) -> None:
        missing = self.REQUIRED_METADATA - metadata.keys()
        if missing:
            raise ValueError(f"missing RAG metadata: {sorted(missing)}")
        self.collection.add(ids=[note_id], documents=[document], metadatas=[dict(metadata)])
