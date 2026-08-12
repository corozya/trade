"""Deterministic offline experiment runner for the agent-krypto research loop.

The runner intentionally exposes only the research partition.  The final
chronological holdout is represented in lineage by its boundary/count, but its
rows and metrics never enter tuning output.  Final evaluation remains the
one-shot responsibility of ``StrategyArtifact.run_final_evaluation``.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import random
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

import pyarrow.parquet as pq

from services.crypto_data_lake import CryptoDataLake
from services.crypto_research import (
    ChromaResearchNotes,
    EXECUTED,
    MlflowExperimentRegistry,
    ResearchToolGate,
)
from services.crypto_strategy_research import (
    LabelConfig,
    TrialRecord,
    TrialRegistry,
    build_point_in_time_labels,
    chronological_holdout,
    purged_expanding_walk_forward,
)


class ExperimentError(RuntimeError):
    """Fail-closed configuration, data or registry error."""


class _TrialLedger:
    """Cross-process reservation guarding non-transactional external sinks."""

    def __init__(self, output_root: Path, config: "ExperimentConfig"):
        self.directory = output_root / "trials"
        self.result_path = self.directory / f"{config.trial_id}.json"
        self.state_path = self.directory / f"{config.trial_id}.state.json"
        self.lock_path = self.directory / f"{config.trial_id}.lock"
        self.config = config

    def _write_state(self, state: str, **extra: Any) -> None:
        payload = {
            "schema_version": 1,
            "trial_id": self.config.trial_id,
            "config_hash": self.config.config_hash,
            "state": state,
            **extra,
        }
        temporary = self.state_path.with_suffix(
            f".{os.getpid()}.{id(self)}.tmp"
        )
        with temporary.open("w") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.state_path)

    def transition(self, expected: str, target: str, **extra: Any) -> None:
        current = json.loads(self.state_path.read_text())
        if (
            current.get("config_hash") != self.config.config_hash
            or current.get("state") != expected
        ):
            raise ExperimentError(
                f"trial ledger transition rejected: {self.config.trial_id}"
            )
        self._write_state(target, **extra)

    @contextmanager
    def reserve(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            if self.result_path.exists():
                persisted = json.loads(self.result_path.read_text())
                if persisted.get("lineage", {}).get("config_hash") != self.config.config_hash:
                    raise ExperimentError(
                        f"immutable trial collision: {self.config.trial_id}"
                    )
                yield persisted, False
                return
            if self.state_path.exists():
                reservation = json.loads(self.state_path.read_text())
                if reservation.get("config_hash") != self.config.config_hash:
                    raise ExperimentError(
                        f"immutable trial collision: {self.config.trial_id}"
                    )
                state = reservation.get("state")
                if state != "RESERVED":
                    raise ExperimentError(
                        f"incomplete trial requires operator recovery "
                        f"({state}): {self.config.trial_id}"
                    )
            else:
                self._write_state("RESERVED")
            yield None, True


class ExperimentSink(Protocol):
    def record(
        self,
        *,
        lineage: Mapping[str, Any],
        params: Mapping[str, Any],
        costs: Mapping[str, float],
        results: Mapping[str, float],
    ) -> str: ...

class ResearchNotesSink(Protocol):
    def append(
        self, note_id: str, document: str, metadata: Mapping[str, Any]
    ) -> None: ...


class BacktestEngine(Protocol):
    name: str

    def run(
        self,
        *,
        labels: Sequence[Mapping[str, Any]],
        feature_rows: Sequence[Mapping[str, Any]],
        folds: Sequence[Any],
        config: "ExperimentConfig",
        round_trip_cost: float,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]: ...


@dataclass(frozen=True)
class ExperimentConfig:
    dataset_version: str
    feature_version: str
    strategy_version: str
    symbol: str
    timeframe: str
    seed: int
    strategy_params: Mapping[str, Any]
    costs: Mapping[str, float]
    learning_request: Mapping[str, Any] | None = None
    tool_request: Mapping[str, Any] | None = None
    n_folds: int = 5
    holdout_fraction: float = 0.2
    bootstrap_samples: int = 1000
    code_version: str = "crypto-experiment-runner-v1"

    def __post_init__(self) -> None:
        if self.n_folds < 1:
            raise ExperimentError("n_folds must be positive")
        if not 0 < self.holdout_fraction < 1:
            raise ExperimentError("holdout_fraction must be in (0, 1)")
        if self.bootstrap_samples < 100:
            raise ExperimentError("bootstrap_samples must be >= 100")
        if int(self.strategy_params.get("lookback", 0)) < 1:
            raise ExperimentError("strategy_params.lookback must be >= 1")
        if float(self.strategy_params.get("threshold", -1)) < 0:
            raise ExperimentError("strategy_params.threshold must be >= 0")
        for name in ("fee_bps", "spread_bps", "slippage_bps"):
            if float(self.costs.get(name, -1)) < 0:
                raise ExperimentError(f"costs.{name} must be >= 0")
        if self.learning_request is not None:
            required = {
                "request_id",
                "base_dataset_version",
                "requested_by",
                "symbols",
                "hypothesis",
                "features",
            }
            missing = required - self.learning_request.keys()
            if missing:
                raise ExperimentError(f"LearningRequest missing fields: {sorted(missing)}")
            if self.learning_request["base_dataset_version"] != self.dataset_version:
                raise ExperimentError("LearningRequest dataset version does not match experiment")
            if self.learning_request["requested_by"] != "agent-krypto-research":
                raise ExperimentError("LearningRequest has an unsupported requester")
        if self.tool_request is not None:
            required = {"request_id", "requested_by", "tool", "params", "reason"}
            missing = required - self.tool_request.keys()
            if missing:
                raise ExperimentError(f"ToolRequest missing fields: {sorted(missing)}")
            if self.tool_request["requested_by"] != "agent-krypto-research":
                raise ExperimentError("ToolRequest has an unsupported requester")

    @property
    def config_hash(self) -> str:
        payload = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(payload.encode()).hexdigest()

    @property
    def trial_id(self) -> str:
        return f"trial-{self.config_hash[:16]}"


def _load_feature_rows(
    feature_root: Path, config: ExperimentConfig
) -> list[dict[str, Any]]:
    version_dir = feature_root / "datasets" / config.feature_version
    manifest_path = version_dir / "manifest.json"
    parquet_path = version_dir / "features.parquet"
    if not manifest_path.is_file() or not parquet_path.is_file():
        raise ExperimentError(f"unknown feature version: {config.feature_version}")
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("dataset_version") != config.feature_version:
        raise ExperimentError("feature manifest version mismatch")
    if manifest.get("lineage", {}).get("base_dataset_version") != config.dataset_version:
        raise ExperimentError("feature dataset base version mismatch")
    rows = [
        row
        for row in pq.read_table(parquet_path).to_pylist()
        if row.get("data_kind", "ohlcv") == "ohlcv"
        and row.get("symbol") == config.symbol
        and row.get("timeframe") == config.timeframe
    ]
    rows.sort(key=lambda row: row["available_at"])
    signal_feature = str(config.strategy_params.get("signal_feature", "bb_percent_b"))
    if not rows or signal_feature not in rows[0]:
        raise ExperimentError(f"feature dataset lacks configured feature: {signal_feature}")
    return rows


def _metric(returns: Sequence[float]) -> dict[str, float | int]:
    if not returns:
        return {
            "expectancy": 0.0,
            "max_drawdown": 0.0,
            "profit_factor": 0.0,
            "trade_count": 0,
        }
    equity = peak = 0.0
    max_drawdown = 0.0
    for value in returns:
        equity += value
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
    gains = sum(value for value in returns if value > 0)
    losses = -sum(value for value in returns if value < 0)
    profit_factor = gains / losses if losses else (1_000_000.0 if gains else 0.0)
    return {
        "expectancy": sum(returns) / len(returns),
        "max_drawdown": max_drawdown,
        "profit_factor": profit_factor,
        "trade_count": len(returns),
    }


def _bootstrap_ci(
    values: Sequence[float], *, seed: int, samples: int
) -> dict[str, float]:
    if not values:
        return {"lower": 0.0, "upper": 0.0, "confidence": 0.95}
    rng = random.Random(seed)
    means = sorted(
        sum(rng.choice(values) for _ in values) / len(values) for _ in range(samples)
    )
    lower_index = max(0, math.floor(samples * 0.025))
    upper_index = min(samples - 1, math.ceil(samples * 0.975) - 1)
    return {
        "lower": means[lower_index],
        "upper": means[upper_index],
        "confidence": 0.95,
    }


def _regime(momentum: float, threshold: float) -> str:
    if momentum > threshold:
        return "bull"
    if momentum < -threshold:
        return "bear"
    return "sideways"


class LocalFeatureMomentumEngine:
    """Approved deterministic local adapter; no network or implicit fallback."""

    name = "local-feature-momentum-v1"

    def run(
        self,
        *,
        labels: Sequence[Mapping[str, Any]],
        feature_rows: Sequence[Mapping[str, Any]],
        folds: Sequence[Any],
        config: ExperimentConfig,
        round_trip_cost: float,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        lookback = int(config.strategy_params["lookback"])
        threshold = float(config.strategy_params["threshold"])
        signal_feature = str(config.strategy_params.get("signal_feature", "bb_percent_b"))
        if len(feature_rows) != len(labels):
            raise ExperimentError("feature rows do not align with labeled market rows")
        fold_metrics: list[dict[str, Any]] = []
        trades: list[dict[str, Any]] = []
        for fold in folds:
            fold_returns: list[float] = []
            for index in fold.test_idx:
                if index < lookback:
                    continue
                current = feature_rows[index].get(signal_feature)
                reference = feature_rows[index - lookback].get(signal_feature)
                if current is None or reference is None:
                    continue
                momentum = float(current) - float(reference)
                direction = (
                    "LONG"
                    if momentum > threshold
                    else "SHORT"
                    if momentum < -threshold
                    else "WAIT"
                )
                if direction == "WAIT":
                    continue
                gross = float(labels[index]["raw_log_return"])
                net_return = (gross if direction == "LONG" else -gross) - round_trip_cost
                trade = {
                    "fold": fold.index,
                    "symbol": config.symbol,
                    "direction": direction,
                    "regime": _regime(momentum, threshold),
                    "decision_at": labels[index]["decision_at"],
                    "net_return": net_return,
                }
                trades.append(trade)
                fold_returns.append(net_return)
            fold_metrics.append(
                {"fold": fold.index, "symbol": config.symbol, **_metric(fold_returns)}
            )
        return trades, fold_metrics


class ExperimentRunner:
    """Runs a cost-aware walk-forward trial and persists an immutable result."""

    def __init__(
        self,
        lake: CryptoDataLake,
        feature_root: str | Path,
        output_root: str | Path,
        *,
        experiment_sink: ExperimentSink,
        research_notes: ResearchNotesSink,
        tool_gate: ResearchToolGate,
        backtest_engine: BacktestEngine,
    ):
        self.lake = lake
        self.feature_root = Path(feature_root)
        self.output_root = Path(output_root)
        self.experiment_sink = experiment_sink
        self.research_notes = research_notes
        self.tool_gate = tool_gate
        self.backtest_engine = backtest_engine
        self.registry = TrialRegistry()

    def run(self, config: ExperimentConfig) -> dict[str, Any]:
        if config.tool_request is None:
            raise ExperimentError("ToolRequest is required")
        tool_decision = self.tool_gate.decide(config.tool_request)
        if tool_decision.status != EXECUTED or tool_decision.tool != "walk_forward":
            raise ExperimentError(
                f"ToolRequest is not executable: {tool_decision.status}"
            )
        table = self.lake.read_version(config.dataset_version)
        bars = [
            row
            for row in table.to_pylist()
            if row["data_kind"] == "ohlcv"
            and row["symbol"] == config.symbol
            and row["timeframe"] == config.timeframe
        ]
        bars.sort(key=lambda row: row["available_at"])
        if len(bars) < 30:
            raise ExperimentError("at least 30 closed OHLCV bars are required")
        for row in bars:
            if row["available_at"] != row["observed_at"]:
                # ``close_time`` is the actual availability boundary, never
                # the candle's opening/observation timestamp.
                row["close_time"] = row["available_at"]
            else:
                row["close_time"] = row["available_at"]

        labels = build_point_in_time_labels(
            bars, config=LabelConfig(base_timeframe=config.timeframe, horizon=1)
        )
        feature_rows = _load_feature_rows(self.feature_root, config)
        feature_by_time = {str(row["available_at"]): row for row in feature_rows}
        aligned_features = []
        for label in labels:
            row = feature_by_time.get(str(label["decision_at"]))
            if row is None:
                raise ExperimentError("feature dataset is not point-in-time aligned")
            aligned_features.append(row)
        split = chronological_holdout(len(labels), fraction=config.holdout_fraction)
        research_labels = [labels[index] for index in split.research_idx]
        research_features = [aligned_features[index] for index in split.research_idx]
        lookback = int(config.strategy_params["lookback"])
        if len(research_labels) <= lookback + config.n_folds:
            raise ExperimentError("research partition is too small for strategy and folds")
        folds = purged_expanding_walk_forward(
            len(research_labels),
            label_horizon=1,
            n_folds=config.n_folds,
            min_train_size=max(lookback + 2, len(research_labels) // (config.n_folds + 1)),
        )
        round_trip_cost = (
            float(config.costs["fee_bps"])
            + float(config.costs["spread_bps"])
            + float(config.costs["slippage_bps"])
        ) / 10_000.0

        trades, fold_metrics = self.backtest_engine.run(
            labels=research_labels,
            feature_rows=research_features,
            folds=folds,
            config=config,
            round_trip_cost=round_trip_cost,
        )

        net_returns = [float(trade["net_return"]) for trade in trades]
        grouped: dict[str, dict[str, float | int]] = {}
        for dimension in ("direction", "regime"):
            values = sorted({str(trade[dimension]) for trade in trades})
            for value in values:
                grouped[f"{dimension}:{value}"] = _metric(
                    [
                        float(trade["net_return"])
                        for trade in trades
                        if trade[dimension] == value
                    ]
                )
        slice_metrics: dict[str, dict[str, float | int]] = {}
        slices = sorted(
            {
                (
                    int(trade["fold"]),
                    str(trade["symbol"]),
                    str(trade["direction"]),
                    str(trade["regime"]),
                )
                for trade in trades
            }
        )
        for fold, symbol, direction, regime in slices:
            key = f"fold:{fold}|symbol:{symbol}|direction:{direction}|regime:{regime}"
            slice_metrics[key] = _metric(
                [
                    float(trade["net_return"])
                    for trade in trades
                    if trade["fold"] == fold
                    and trade["symbol"] == symbol
                    and trade["direction"] == direction
                    and trade["regime"] == regime
                ]
            )
        overall = _metric(net_returns)
        bootstrap_ci = _bootstrap_ci(
            net_returns, seed=config.seed, samples=config.bootstrap_samples
        )
        accepted = bool(
            overall["trade_count"]
            and overall["expectancy"] > 0
            and bootstrap_ci["lower"] > 0
        )
        reason = None if accepted else "non-positive cost-adjusted bootstrap lower bound"
        lineage = {
            "dataset_version": config.dataset_version,
            "feature_version": config.feature_version,
            "strategy_version": config.strategy_version,
            "code_version": config.code_version,
            "backtest_engine": self.backtest_engine.name,
            "time_range": {
                "start": research_labels[0]["decision_at"],
                "end": research_labels[-1]["label_end"],
            },
            "data_stage": "RESEARCH",
            "seed": config.seed,
            "config_hash": config.config_hash,
            "learning_request_id": (
                config.learning_request.get("request_id") if config.learning_request else None
            ),
            "tool_request_id": (
                config.tool_request.get("request_id") if config.tool_request else None
            ),
            "holdout": {
                "fraction": config.holdout_fraction,
                "start_index": split.holdout_idx[0],
                "sample_count": len(split.holdout_idx),
                "accessed": False,
            },
        }
        result = {
            "schema_version": 1,
            "trial_id": config.trial_id,
            "accepted": accepted,
            "reason": reason,
            "lineage": lineage,
            "params": dict(config.strategy_params),
            "costs": dict(config.costs),
            "fold_metrics": fold_metrics,
            "symbol_metrics": {config.symbol: overall},
            "dimension_metrics": grouped,
            "slice_metrics": slice_metrics,
            "bootstrap_ci": bootstrap_ci,
        }
        ledger = _TrialLedger(self.output_root, config)
        with ledger.reserve() as (persisted, owner):
            if not owner:
                return persisted
            self.registry.record(
                TrialRecord(
                    trial_id=config.trial_id,
                    config_hash=config.config_hash,
                    fold_metrics=tuple(fold_metrics),
                    accepted=accepted,
                    reason=reason,
                )
            )
            ledger.transition("RESERVED", "MLFLOW_IN_FLIGHT")
            result["experiment_id"] = self.experiment_sink.record(
                lineage=lineage,
                params={
                    **dict(config.strategy_params),
                    "feature_version": config.feature_version,
                    "seed": config.seed,
                },
                costs=config.costs,
                results={
                    "expectancy": float(overall["expectancy"]),
                    "max_drawdown": float(overall["max_drawdown"]),
                    "profit_factor": float(overall["profit_factor"]),
                    "trade_count": float(overall["trade_count"]),
                    "bootstrap_ci_lower": bootstrap_ci["lower"],
                    "bootstrap_ci_upper": bootstrap_ci["upper"],
                },
            )
            ledger.transition(
                "MLFLOW_IN_FLIGHT",
                "MLFLOW_DONE",
                experiment_id=result["experiment_id"],
            )
            ledger.transition(
                "MLFLOW_DONE",
                "RAG_IN_FLIGHT",
                experiment_id=result["experiment_id"],
            )
            self.research_notes.append(
                config.trial_id,
                f"Trial {config.trial_id}: "
                f"{'accepted' if accepted else 'rejected'}; {reason or 'passed'}",
                {
                    "experiment_id": result["experiment_id"],
                    "dataset_version": config.dataset_version,
                    "strategy_version": config.strategy_version,
                    "time_range": json.dumps(lineage["time_range"], sort_keys=True),
                    "data_stage": "RESEARCH",
                },
            )
            ledger.transition(
                "RAG_IN_FLIGHT",
                "RAG_DONE",
                experiment_id=result["experiment_id"],
            )
            encoded = json.dumps(result, indent=2, sort_keys=True) + "\n"
            try:
                descriptor = os.open(
                    ledger.result_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644
                )
            except FileExistsError as exc:
                raise ExperimentError(
                    f"immutable trial collision: {config.trial_id}"
                ) from exc
            with os.fdopen(descriptor, "w") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            ledger.transition(
                "RAG_DONE",
                "COMPLETED",
                experiment_id=result["experiment_id"],
            )
            return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one offline agent-krypto experiment")
    parser.add_argument("--lake-root", required=True)
    parser.add_argument("--feature-root", required=True)
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--tool-catalog", required=True)
    parser.add_argument("--mlflow-tracking-uri", required=True)
    parser.add_argument("--rag-path", required=True)
    parser.add_argument(
        "--backtest-engine", required=True, choices=["local-feature-momentum-v1"]
    )
    args = parser.parse_args(argv)
    payload = json.loads(Path(args.config).read_text())
    result = ExperimentRunner(
        CryptoDataLake(args.lake_root),
        args.feature_root,
        args.output_root,
        experiment_sink=MlflowExperimentRegistry(
            args.mlflow_tracking_uri, "agent-krypto-research"
        ),
        research_notes=ChromaResearchNotes(args.rag_path),
        tool_gate=ResearchToolGate(args.tool_catalog),
        backtest_engine=LocalFeatureMomentumEngine(),
    ).run(ExperimentConfig(**payload))
    print(result["trial_id"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
