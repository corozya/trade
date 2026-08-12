#!/usr/bin/env python3
"""Deklaratywna pętla cech dla researchu agent-krypto.

Agent składa LearningRequest. Ten moduł waliduje go względem allowlistowanego
katalogu i wylicza wyłącznie deterministyczne, point-in-time cechy. Nie
wykonuje kodu dostarczonego przez agenta i nie dotyka runtime tradingowego.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CATALOG = ROOT / "config/agent_krypto_feature_catalog.json"
DEFAULT_SCHEMA = ROOT / "config/agent_krypto_learning_request.schema.json"

OHLCV_COLUMNS = ("date", "open", "high", "low", "close", "volume")


class LearningRequestError(ValueError):
    pass


def _load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise LearningRequestError(f"{path}: oczekiwano obiektu JSON")
    return value


def _validate_number(value: Any, spec: dict[str, Any], label: str) -> None:
    expected = spec["type"]
    if expected == "integer":
        if not isinstance(value, int) or isinstance(value, bool):
            raise LearningRequestError(f"{label} musi być integer")
    elif expected == "number":
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise LearningRequestError(f"{label} musi być number")
    else:
        raise LearningRequestError(f"nieobsługiwany typ katalogu: {expected}")
    numeric = float(value)
    if not math.isfinite(numeric):
        raise LearningRequestError(f"{label} musi być skończone")
    if numeric < float(spec["min"]) or numeric > float(spec["max"]):
        raise LearningRequestError(
            f"{label}={value} poza zakresem [{spec['min']}, {spec['max']}]"
        )


def validate_learning_request(
    request: dict[str, Any], catalog: dict[str, Any]
) -> dict[str, Any]:
    required = {
        "request_id",
        "base_dataset_version",
        "requested_by",
        "symbols",
        "hypothesis",
        "features",
    }
    missing = sorted(required - request.keys())
    extra = sorted(request.keys() - required)
    if missing:
        raise LearningRequestError(f"brak pól LearningRequest: {', '.join(missing)}")
    if extra:
        raise LearningRequestError(f"nieznane pola LearningRequest: {', '.join(extra)}")
    if request["requested_by"] != "agent-krypto-research":
        raise LearningRequestError("requested_by musi być agent-krypto-research")
    if not isinstance(request["hypothesis"], str) or len(request["hypothesis"].strip()) < 10:
        raise LearningRequestError("hypothesis musi zawierać konkretną hipotezę")
    symbols = request["symbols"]
    if not isinstance(symbols, list) or not symbols or len(set(symbols)) != len(symbols):
        raise LearningRequestError("symbols musi być niepustą listą bez duplikatów")
    invalid_symbols = sorted(set(symbols) - {"BTC", "ETH", "DOGE"})
    if invalid_symbols:
        raise LearningRequestError(f"niedozwolone symbole: {', '.join(invalid_symbols)}")

    features = request["features"]
    if not isinstance(features, list) or not 1 <= len(features) <= 20:
        raise LearningRequestError("features musi zawierać od 1 do 20 pozycji")
    allowed_tfs = set(catalog["allowed_timeframes"])
    available = catalog["features"]
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, feature in enumerate(features):
        if not isinstance(feature, dict):
            raise LearningRequestError(f"features[{index}] musi być obiektem")
        if set(feature) != {"name", "timeframe", "params", "reason"}:
            raise LearningRequestError(
                f"features[{index}] wymaga wyłącznie name,timeframe,params,reason"
            )
        name = feature["name"]
        timeframe = feature["timeframe"]
        params = feature["params"]
        if name not in available:
            raise LearningRequestError(
                f"cecha {name!r} nie istnieje w allowlistowanym katalogu"
            )
        if timeframe not in allowed_tfs:
            raise LearningRequestError(f"timeframe {timeframe!r} nie jest dozwolony")
        if not isinstance(params, dict):
            raise LearningRequestError(f"{name}.params musi być obiektem")
        expected_params = available[name]["params"]
        if set(params) != set(expected_params):
            raise LearningRequestError(
                f"{name}.params: oczekiwano {sorted(expected_params)}, otrzymano {sorted(params)}"
            )
        for param_name, param_spec in expected_params.items():
            _validate_number(
                params[param_name], param_spec, f"{name}.{param_name}"
            )
        if name == "macd" and not params["fast"] < params["slow"]:
            raise LearningRequestError("macd.fast musi być mniejsze niż macd.slow")
        canonical = json.dumps(
            {"name": name, "timeframe": timeframe, "params": params},
            sort_keys=True,
            separators=(",", ":"),
        )
        if canonical in seen:
            raise LearningRequestError(f"zduplikowana cecha: {canonical}")
        seen.add(canonical)
        normalized.append({**feature, "params": dict(sorted(params.items()))})

    return {**request, "features": normalized}


def _prefix(feature: dict[str, Any]) -> str:
    params = "_".join(f"{key}{value:g}" for key, value in feature["params"].items())
    return f"{feature['timeframe']}__{feature['name']}__{params}"


def _rsi(close: pd.Series, period: int) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def compute_feature(frame: pd.DataFrame, feature: dict[str, Any]) -> pd.DataFrame:
    """Zwraca wyłącznie nowe kolumny o indeksie zgodnym z wejściem."""
    missing = [column for column in OHLCV_COLUMNS if column not in frame.columns]
    if missing:
        raise LearningRequestError(f"brak kolumn OHLCV: {', '.join(missing)}")
    close = frame["close"].astype(float)
    high = frame["high"].astype(float)
    low = frame["low"].astype(float)
    volume = frame["volume"].astype(float)
    name = feature["name"]
    params = feature["params"]
    prefix = _prefix(feature)
    out = pd.DataFrame(index=frame.index)

    if name == "bollinger_bands":
        window = params["window"]
        mid = close.rolling(window, min_periods=window).mean()
        std = close.rolling(window, min_periods=window).std(ddof=0)
        upper = mid + params["stddev"] * std
        lower = mid - params["stddev"] * std
        width = (upper - lower) / mid.replace(0, np.nan)
        out[f"{prefix}__mid"] = mid
        out[f"{prefix}__upper"] = upper
        out[f"{prefix}__lower"] = lower
        out[f"{prefix}__width"] = width
        out[f"{prefix}__percent_b"] = (close - lower) / (upper - lower).replace(0, np.nan)
    elif name == "ema":
        ema = close.ewm(span=params["period"], adjust=False, min_periods=params["period"]).mean()
        out[f"{prefix}__value"] = ema
        out[f"{prefix}__slope_3"] = ema.pct_change(3)
    elif name == "rsi":
        out[f"{prefix}__value"] = _rsi(close, params["period"])
    elif name == "atr":
        previous = close.shift(1)
        true_range = pd.concat(
            [(high - low), (high - previous).abs(), (low - previous).abs()], axis=1
        ).max(axis=1)
        atr = true_range.ewm(
            alpha=1 / params["period"],
            adjust=False,
            min_periods=params["period"],
        ).mean()
        out[f"{prefix}__value"] = atr
        out[f"{prefix}__pct"] = atr / close.replace(0, np.nan)
    elif name == "macd":
        fast = close.ewm(span=params["fast"], adjust=False, min_periods=params["fast"]).mean()
        slow = close.ewm(span=params["slow"], adjust=False, min_periods=params["slow"]).mean()
        line = fast - slow
        signal = line.ewm(
            span=params["signal"], adjust=False, min_periods=params["signal"]
        ).mean()
        out[f"{prefix}__line"] = line
        out[f"{prefix}__signal"] = signal
        out[f"{prefix}__histogram"] = line - signal
    elif name == "relative_volume":
        mean = volume.rolling(params["window"], min_periods=params["window"]).mean()
        out[f"{prefix}__value"] = volume / mean.replace(0, np.nan)
    elif name == "obv":
        direction = np.sign(close.diff()).fillna(0)
        obv = (direction * volume).cumsum()
        out[f"{prefix}__value"] = obv
        out[f"{prefix}__slope"] = obv.diff(params["slope_window"])
    elif name == "vwap":
        typical = (high + low + close) / 3
        window = params["window"]
        denominator = volume.rolling(window, min_periods=window).sum()
        value = (typical * volume).rolling(window, min_periods=window).sum() / denominator.replace(0, np.nan)
        out[f"{prefix}__value"] = value
        out[f"{prefix}__distance_pct"] = close / value - 1
    elif name == "returns":
        out[f"{prefix}__value"] = close.pct_change(params["periods"])
    elif name == "realized_volatility":
        returns = np.log(close / close.shift(1))
        out[f"{prefix}__value"] = returns.rolling(
            params["window"], min_periods=params["window"]
        ).std(ddof=0)
    else:  # pragma: no cover - validator prevents this
        raise LearningRequestError(f"brak implementacji cechy {name}")
    return out


def add_requested_features(
    frames: dict[str, pd.DataFrame],
    request: dict[str, Any],
    catalog: dict[str, Any],
) -> tuple[dict[str, pd.DataFrame], dict[str, Any]]:
    request = validate_learning_request(request, catalog)
    outputs = {timeframe: frame.copy() for timeframe, frame in frames.items()}
    generated_columns: list[str] = []
    for feature in request["features"]:
        timeframe = feature["timeframe"]
        if timeframe not in outputs:
            raise LearningRequestError(f"brak danych wejściowych timeframe={timeframe}")
        computed = compute_feature(outputs[timeframe], feature)
        overlap = sorted(set(computed.columns) & set(outputs[timeframe].columns))
        if overlap:
            raise LearningRequestError(f"kolizja kolumn: {', '.join(overlap)}")
        outputs[timeframe] = outputs[timeframe].join(computed)
        generated_columns.extend(computed.columns)

    request_bytes = json.dumps(
        request, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    version = hashlib.sha256(request_bytes).hexdigest()[:16]
    manifest = {
        "request_id": request["request_id"],
        "base_dataset_version": request["base_dataset_version"],
        "dataset_version": f"{request['base_dataset_version']}+features-{version}",
        "request_sha256": hashlib.sha256(request_bytes).hexdigest(),
        "generated_columns": generated_columns,
        "feature_count": len(request["features"]),
        "status": "COMPUTED",
    }
    return outputs, manifest


def _read_frame(path: Path) -> pd.DataFrame:
    if path.suffix == ".feather":
        return pd.read_feather(path)
    if path.suffix in {".parquet", ".pq"}:
        return pd.read_parquet(path)
    if path.suffix == ".csv":
        return pd.read_csv(path)
    raise LearningRequestError(f"nieobsługiwany format danych: {path}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("request", type=Path)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument(
        "--frame",
        action="append",
        default=[],
        metavar="TF=PATH",
        help="Dane OHLCV, np. --frame 15m=data.feather",
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    request = _load_json(args.request)
    catalog = _load_json(args.catalog)
    frames: dict[str, pd.DataFrame] = {}
    for item in args.frame:
        if "=" not in item:
            raise LearningRequestError("--frame wymaga TF=PATH")
        timeframe, raw_path = item.split("=", 1)
        frames[timeframe] = _read_frame(Path(raw_path))
    outputs, manifest = add_requested_features(frames, request, catalog)
    args.output_dir.mkdir(parents=True, exist_ok=False)
    for timeframe, frame in outputs.items():
        frame.to_parquet(args.output_dir / f"{timeframe}.parquet", index=False)
    (args.output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(manifest, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
