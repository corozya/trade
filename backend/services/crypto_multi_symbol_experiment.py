"""Auditable, offline-only five-symbol research experiment (#111).

This module deliberately has no execution or artifact-promotion adapter.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from collections import Counter
from pathlib import Path
from statistics import pstdev
from typing import Any, Mapping, Sequence

import pyarrow.parquet as pq

from services.crypto_data_lake import CryptoDataLake
from services.crypto_research_policy import canonical_version, corrected_expectancy_threshold
from services.crypto_strategy_research import (
    LabelConfig,
    build_point_in_time_labels,
    chronological_holdout,
    purged_expanding_walk_forward,
)


SYMBOLS = ("BTC-USDT-SWAP", "ETH-USDT-SWAP", "DOGE-USDT-SWAP", "SOL-USDT-SWAP", "XRP-USDT-SWAP")


def _metric(values: Sequence[float]) -> dict[str, float | int]:
    if not values:
        return {"expectancy": 0.0, "max_drawdown": 0.0, "trade_count": 0}
    equity = peak = drawdown = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return {
        "expectancy": sum(values) / len(values),
        "max_drawdown": drawdown,
        "trade_count": len(values),
    }


def _load_json(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text())


def run_offline_experiment(
    *,
    lake_root: str | Path,
    feature_root: str | Path,
    output_root: str | Path,
    dataset_version: str,
    feature_version: str,
    label_config_path: str | Path,
    policy_path: str | Path,
    trial_count: int = 1,
) -> dict[str, Any]:
    label_payload = _load_json(label_config_path)
    policy = _load_json(policy_path)
    label_config = LabelConfig(**label_payload)
    if policy["min_oos_trades_per_symbol_direction"] <= 0:
        raise ValueError("policy minimum per symbol/direction must be > 0")
    if not policy["multiple_testing"]["include_rejected_trials"]:
        raise ValueError("policy must count rejected trials")
    if policy["max_degradation_vs_train"] < 0:
        raise ValueError("policy requires a degradation limit")

    feature_path = Path(feature_root) / "datasets" / feature_version / "features.parquet"
    raw_rows = CryptoDataLake(Path(lake_root).parent).read_version(dataset_version).to_pylist()
    feature_rows = pq.read_table(feature_path).to_pylist()
    feature_by_key = {
        (row["symbol"], str(row["available_at"])): row for row in feature_rows
        if row.get("timeframe") == label_config.base_timeframe
    }
    round_trip_cost = sum(float(value) for value in policy["costs"].values()) / 10_000
    fold_metrics: list[dict[str, Any]] = []
    holdouts: dict[str, Any] = {}
    counts: Counter[str] = Counter()
    all_returns: list[float] = []

    for symbol in SYMBOLS:
        bars = [
            {**row, "close_time": row["available_at"]}
            for row in raw_rows
            if row.get("data_kind") == "ohlcv"
            and row.get("symbol") == symbol
            and row.get("timeframe") == label_config.base_timeframe
        ]
        bars.sort(key=lambda row: row["available_at"])
        labels = build_point_in_time_labels(bars, config=label_config)
        split = chronological_holdout(len(labels), fraction=0.2)
        holdouts[symbol] = {
            "start": labels[split.holdout_idx[0]]["decision_at"],
            "end": labels[split.holdout_idx[-1]]["label_end"],
            "sample_count": len(split.holdout_idx),
            "accessed": False,
            "claim": "SEALED_ONE_SHOT",
        }
        research = [labels[index] for index in split.research_idx]
        folds = purged_expanding_walk_forward(
            len(research),
            label_horizon=label_config.horizon,
            embargo_bars=label_config.horizon,
            n_folds=int(policy["n_folds"]),
        )
        for fold in folds:
            returns: list[float] = []
            for index in fold.test_idx:
                label = research[index]
                feature = feature_by_key.get((symbol, label["decision_at"]))
                if not feature:
                    continue
                signal = feature.get("bb_percent_b")
                if signal is None:
                    continue
                direction = "LONG" if float(signal) >= 0.5 else "SHORT"
                gross = float(label["raw_log_return"])
                net = (gross if direction == "LONG" else -gross) - round_trip_cost
                returns.append(net)
                all_returns.append(net)
                counts[f"{symbol}|{direction}"] += 1
            fold_metrics.append({"fold": fold.index, "symbol": symbol, **_metric(returns)})

    corrected_threshold = corrected_expectancy_threshold(
        base_threshold=float(policy["multiple_testing"]["base_expectancy_threshold"]),
        trial_count=trial_count,
        oos_trade_count=max(1, len(all_returns)),
    )
    overall = _metric(all_returns)
    expected_keys = {f"{symbol}|{direction}" for symbol in SYMBOLS for direction in ("LONG", "SHORT")}
    failures = []
    if len(all_returns) < policy["min_oos_trades"]:
        failures.append("insufficient total OOS trades")
    if any(counts[key] < policy["min_oos_trades_per_symbol_direction"] for key in expected_keys):
        failures.append("insufficient OOS trades per symbol/direction")
    if float(overall["expectancy"]) < corrected_threshold:
        failures.append("expectancy below multiple-testing-corrected threshold")
    if float(overall["max_drawdown"]) > policy["max_drawdown"]:
        failures.append("drawdown limit exceeded")
    positive_fraction = sum(m["expectancy"] > 0 for m in fold_metrics) / len(fold_metrics)
    if positive_fraction < policy["min_positive_fold_fraction"]:
        failures.append("fold stability: positive fraction below policy")
    if pstdev(float(m["expectancy"]) for m in fold_metrics) > policy["stability_max_expectancy_stddev"]:
        failures.append("fold stability: expectancy dispersion above policy")

    label_version = canonical_version("labels", label_payload)
    policy_version = canonical_version("policy", policy)
    identity = {
        "dataset_version": dataset_version,
        "feature_version": feature_version,
        "label_config_version": label_version,
        "promotion_policy_version": policy_version,
        "trial_count": trial_count,
    }
    run_id = "research-" + hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:16]
    report = {
        "schema_version": 1,
        "run_id": run_id,
        **identity,
        "status": "accepted" if not failures else "rejected",
        "failures": failures,
        "higher_timeframes_used": False,
        "costs": policy["costs"],
        "multiple_testing": {
            **policy["multiple_testing"],
            "trial_count": trial_count,
            "corrected_expectancy_threshold": corrected_threshold,
        },
        "holdout_boundaries": holdouts,
        "fold_metrics": fold_metrics,
        "symbol_metrics": {
            symbol: {
                "fold_count": len([m for m in fold_metrics if m["symbol"] == symbol]),
                "trade_count": sum(int(m["trade_count"]) for m in fold_metrics if m["symbol"] == symbol),
                "mean_fold_expectancy": sum(
                    float(m["expectancy"]) for m in fold_metrics if m["symbol"] == symbol
                ) / int(policy["n_folds"]),
            }
            for symbol in SYMBOLS
        },
        "oos_trades_per_symbol_direction": dict(sorted(counts.items())),
        "overall": overall,
        "strategy_artifact_status_changed": False,
        "execution_path_available": False,
        "promotion_path_available": False,
    }
    target = Path(output_root) / f"{run_id}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if target.exists():
        persisted = target.read_text()
        if persisted != encoded:
            raise RuntimeError("immutable experiment collision")
        return json.loads(persisted)
    temporary = target.with_suffix(f".{os.getpid()}.tmp")
    temporary.write_text(encoded)
    os.replace(temporary, target)
    return report
