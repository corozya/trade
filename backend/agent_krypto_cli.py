"""Operational CLI for the agent-krypto orchestrator over services #94-#99.

Every subcommand emits exactly one JSON object to stdout — no markdown, no
conversational text — consistent with the reporting contract already used by
``.claude/skills/agent-krypto/SKILL.md``. Run this from
``backend/`` so the ``services`` package resolves.

``e2e`` (#105) is the single offline command that drives the whole loop
(ingest -> request -> experiment -> evaluate -> promote -> cycle) against an
in-process synthetic fixture — no network, no external fixture file. It never
places a real order (its `cycle` phase always runs without a TradeIntent).
``research-loop`` (#116/#118) is the observation-mode counterpart: it drives
ingest -> request -> experiment+evaluate -> cycle over REAL ingest data
(``--source-path``/``--local-data-dir``, never a synthetic fixture) for all
5 symbols, never promotes, and its ``cycle`` phase always resolves to WAIT
with no TradeIntent — the intended target of a 15-minute observation cron.
``backup``/``restore``/``verify-backup`` (#105) operate on the local research
workspace (datasets/MLflow/Chroma/SQLite) directly and are not gated by the
versioned orchestrator config. See ``docs/agent-krypto-orchestrator-runbook.md``
for the full operator runbook (first run, status, restart/resume,
backup/restore, rollback/invalidation, troubleshooting, and the OKX Demo
smoke-test plan).
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from services.agent_krypto_backup import (
    BackupError,
    create_backup,
    restore_backup,
    verify_backup_integrity,
)
from services.agent_krypto_cycle import run_agent_krypto_cycle
from services.agent_krypto_artifact_registry import StrategyArtifactRegistry
from services.agent_krypto_orchestrator import dispatch, envelope
from services.agent_krypto_run_store import RunStore
from services.crypto_candidate_cursor import CandidateCursor, CandidateCursorError
from services.crypto_candidate_generator import (
    CandidateGenerator,
    CandidateGeneratorError,
    ParamRange,
    SearchSpace,
)
from services.crypto_champion_registry import (
    ChampionRegistry,
    ChampionRegistryError,
    compare_to_champion,
)
from services.crypto_data_lake import DATA_KINDS, SYMBOLS, TIMEFRAMES, CryptoDataLake
from services.crypto_experiment_runner import (
    ExperimentConfig,
    ExperimentError,
    ExperimentRunner,
    LocalFeatureMomentumEngine,
    _load_feature_rows,
)
from services.crypto_market_ingestion import (
    CryptoMarketIngestor,
    FixtureMarketDataAdapter,
    LocalFeatherMarketDataAdapter,
    readiness_errors,
    require_ready_dataset,
)
from services.crypto_monitoring import (
    MonitoringError,
    MonitoringThresholds,
    build_monitoring_report,
    check_data_quality,
)
from services.crypto_multi_symbol_experiment import run_offline_experiment
from services.crypto_research_insights import (
    InsightReportStore,
    InsightsError,
    build_trial_insight_report,
)
from services.crypto_research import (
    ChromaResearchNotes,
    EXECUTED,
    FeatureDatasetStore,
    MlflowExperimentRegistry,
    REVIEW_REQUIRED,
    ResearchToolGate,
)
from services.crypto_strategy_research import (
    Fold,
    HoldoutClaimStore,
    LabelConfig,
    PromotionPolicy,
    StrategyArtifact,
    build_point_in_time_labels,
    chronological_holdout,
    purged_expanding_walk_forward,
)
from services.paper_execution import derive_point_in_time_decisions, record_research_loop_result

# Repo root (this file lives at backend/). Used to resolve
# the shared catalog configs (feature/tool) so `e2e` works regardless of
# whether it is invoked from the repo root or from backend/
# — those catalogs live at <repo_root>/config/, not under backend/.
_REPO_ROOT = Path(__file__).resolve().parents[1]


def _project_storage_path(env_name: str, default_relative: str) -> Path:
    """Resolve configured storage inside the project unless explicitly absolute."""
    configured = Path(os.environ.get(env_name, default_relative)).expanduser()
    if not configured.is_absolute():
        configured = _REPO_ROOT / configured
    return configured.resolve()


DEFAULT_DATA_ROOT = str(_project_storage_path("CRYPTO_LAKE_ROOT", "data/lake"))
DEFAULT_RUNTIME_ROOT = _project_storage_path("CRYPTO_RUNTIME_ROOT", "data/runtime")
DEFAULT_RUN_DB = str(DEFAULT_RUNTIME_ROOT / "runs" / "orchestrator_runs.db")
DEFAULT_ARTIFACT_REGISTRY_DB = str(DEFAULT_RUNTIME_ROOT / "artifacts" / "registry.db")
DEFAULT_CONFIG_PATH = "config/agent_krypto_orchestrator_config.json"
DEFAULT_CANDIDATE_CURSOR_DB = str(DEFAULT_RUNTIME_ROOT / "candidate_cursor.db")
DEFAULT_INSIGHT_REPORTS_DB = str(DEFAULT_RUNTIME_ROOT / "insight_reports.db")
DEFAULT_CHAMPION_REGISTRY_DB = str(DEFAULT_RUNTIME_ROOT / "champion_registry.db")
DEFAULT_HOLDOUT_CLAIMS_DB = str(DEFAULT_RUNTIME_ROOT / "holdout_claims.db")
DEFAULT_EXPERIMENT_OUTPUT_ROOT = str(DEFAULT_RUNTIME_ROOT / "experiments")
DEFAULT_MLFLOW_TRACKING_URI = f"sqlite:///{DEFAULT_RUNTIME_ROOT / 'mlruns.db'}"
DEFAULT_RAG_PATH = str(DEFAULT_RUNTIME_ROOT / "rag")

# LearningRequest features are only ever dispatched through FeatureDatasetStore
# (content-addressed, versioned) — the ToolRequest catalog/gate is a distinct
# contract (walk_forward, renko_chart, ...) and does not list feature builders.
_LEARNING_REQUEST_HANDLERS = {"bollinger_bands", "rsi"}
_LEARNING_SYMBOLS = {
    "BTC": "BTC-USDT-SWAP",
    "ETH": "ETH-USDT-SWAP",
    "DOGE": "DOGE-USDT-SWAP",
    "SOL": "SOL-USDT-SWAP",
    "XRP": "XRP-USDT-SWAP",
}
_DEFAULT_LOCAL_SYMBOLS = tuple(_LEARNING_SYMBOLS.values())
_LEARNING_SYMBOLS.update({symbol: symbol for symbol in SYMBOLS})
_LEARNING_SYMBOLS.update({symbol.split("-", 1)[0]: symbol for symbol in SYMBOLS})


def _load_json_file(path: str | None) -> dict[str, Any]:
    if not path:
        return {}
    return json.loads(Path(path).read_text())


def _artifact_from_payload(payload: Mapping[str, Any]) -> StrategyArtifact:
    payload = dict(payload)
    label_config = payload.get("label_config")
    if isinstance(label_config, Mapping):
        allowed = {"base_timeframe", "horizon", "label_type", "threshold"}
        payload["label_config"] = LabelConfig(
            **{k: v for k, v in label_config.items() if k in allowed}
        )
    payload.pop("artifact_hash", None)
    return StrategyArtifact(**payload)


def _handle_ingest(args: Mapping[str, Any], config: Mapping[str, Any]) -> dict[str, Any]:
    """Fetch/normalize/dedup/publish via CryptoMarketIngestor (#102) — never a
    raw CryptoDataLake.publish call, which would skip incremental merge,
    conflict detection and adapter lineage. A published version that is
    incomplete or stale for its ``as_of``/``max_age`` window is fail-closed
    here: ``require_ready_dataset`` raises rather than letting an unusable
    dataset silently report DONE."""
    lake = CryptoDataLake(root=args.get("data_root") or DEFAULT_DATA_ROOT)
    required_symbols = tuple(
        str(value) for value in (args.get("required_symbols") or SYMBOLS)
    )
    if args.get("local_data_dir"):
        adapter = LocalFeatherMarketDataAdapter(
            data_dir=Path(str(args["local_data_dir"])),
            symbols=required_symbols,
            timeframe="15m",
        )
    else:
        fixture = _load_json_file(args.get("source_path"))
        adapter = FixtureMarketDataAdapter(
            records=fixture.get("records", []),
            source_metadata={
                "fixture": str(args.get("source_path")),
                "fixture_sha256": fixture.get("sha256", "unspecified"),
            },
            name=str(args.get("adapter_name") or "fixture"),
        )
    version = CryptoMarketIngestor(lake).ingest(
        [adapter], base_dataset_id=args.get("base_dataset_id")
    )
    as_of = args.get("as_of") or datetime.now(timezone.utc).isoformat()
    max_age_minutes = int(args.get("max_age_minutes", 20))
    require_ready_dataset(
        lake, version.dataset_id,
        as_of=as_of, max_age=timedelta(minutes=max_age_minutes),
        required_symbols=required_symbols,
        required_timeframes=("15m",) if args.get("local_data_dir") else TIMEFRAMES,
        required_data_kinds=("ohlcv",) if args.get("local_data_dir") else DATA_KINDS,
    )
    config_updated = None
    if args.get("update_config"):
        config_path = Path(str(args["update_config"]))
        payload = json.loads(config_path.read_text())
        payload["dataset_version"] = version.dataset_id
        temporary = config_path.with_suffix(config_path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, indent=2) + "\n")
        temporary.replace(config_path)
        config_updated = str(config_path)
    return {
        "dataset_id": version.dataset_id,
        "path": str(version.path),
        "config_updated": config_updated,
    }


def _handle_request(args: Mapping[str, Any], config: Mapping[str, Any]) -> dict[str, Any]:
    """Dispatch a LearningRequest (feature builder) or a ToolRequest (tool
    gate) — the two contracts are distinct and must not share one gate: the
    ToolRequest catalog does not (and should not) list feature transforms
    like bollinger_bands, so routing every request through ResearchToolGate
    made every valid feature request fail closed as REVIEW_REQUIRED."""
    request = _load_json_file(args.get("request_file"))
    kind = args.get("request_kind") or ("learning" if "features" in request else "tool")

    if kind == "learning":
        required = {"request_id", "base_dataset_version", "requested_by", "symbols", "hypothesis", "features"}
        missing = required - request.keys()
        if missing:
            raise ValueError(f"LearningRequest missing fields: {sorted(missing)}")
        known_symbols = set(_LEARNING_SYMBOLS) | set(_LEARNING_SYMBOLS.values())
        unknown_symbols = set(request["symbols"]) - known_symbols
        if unknown_symbols:
            raise ValueError(f"LearningRequest has unsupported symbols: {sorted(unknown_symbols)}")
        requested_dataset_symbols = [
            _LEARNING_SYMBOLS.get(symbol, symbol) for symbol in request["symbols"]
        ]
        store = FeatureDatasetStore(
            root=args.get("data_root") or DEFAULT_DATA_ROOT,
            feature_catalog_path=args.get(
                "feature_catalog_path", "config/agent_krypto_feature_catalog.json"
            ),
        )
        outputs = []
        for feature in request["features"]:
            if feature["name"] not in _LEARNING_REQUEST_HANDLERS:
                return {
                    "status": REVIEW_REQUIRED,
                    "tool": feature["name"],
                    "reason": "feature has no configured builder",
                }
            params = feature.get("params", {})
            if feature["name"] == "bollinger_bands":
                destination = store.bollinger_bands(
                    args["base_dataset_path"],
                    base_dataset_version=request["base_dataset_version"],
                    symbols=requested_dataset_symbols,
                    window=params.get("window", 20),
                    stddev=params.get("stddev", 2.0),
                )
            else:
                assert feature["name"] == "rsi"
                destination = store.rsi(
                    args["base_dataset_path"],
                    base_dataset_version=request["base_dataset_version"],
                    symbols=requested_dataset_symbols,
                    period=params.get("period", 14),
                )
            outputs.append(str(destination))
        feature_versions = [Path(output).name for output in outputs]
        config_updated = None
        if args.get("update_config"):
            if len(set(feature_versions)) != 1:
                raise ValueError("config update requires exactly one feature schema version")
            config_path = Path(str(args["update_config"]))
            payload = json.loads(config_path.read_text())
            payload["feature_schema_version"] = feature_versions[0]
            temporary = config_path.with_suffix(config_path.suffix + ".tmp")
            temporary.write_text(json.dumps(payload, indent=2) + "\n")
            temporary.replace(config_path)
            config_updated = str(config_path)
        return {
            "status": EXECUTED,
            "feature_dataset_paths": outputs,
            "feature_schema_versions": feature_versions,
            "config_updated": config_updated,
        }

    gate = ResearchToolGate(
        catalog_path=args.get(
            "tool_catalog_path", "config/agent_krypto_research_tool_catalog.json"
        )
    )
    decision = gate.decide(request)
    return {"status": decision.status, "tool": decision.tool, "reason": decision.reason}


def _handle_experiment(args: Mapping[str, Any], config: Mapping[str, Any]) -> dict[str, Any]:
    """Run one purged walk-forward trial via the #103 ExperimentRunner."""
    payload = _load_json_file(args["experiment_config_file"])
    experiment_config = ExperimentConfig(**payload)
    lake = CryptoDataLake(root=args.get("data_root") or DEFAULT_DATA_ROOT)
    runner = ExperimentRunner(
        lake,
        feature_root=args.get("data_root") or DEFAULT_DATA_ROOT,
        output_root=args.get("experiment_output_root") or DEFAULT_EXPERIMENT_OUTPUT_ROOT,
        experiment_sink=MlflowExperimentRegistry(
            args.get("mlflow_tracking_uri") or DEFAULT_MLFLOW_TRACKING_URI,
            "agent-krypto-research",
        ),
        research_notes=ChromaResearchNotes(
            args.get("rag_path") or DEFAULT_RAG_PATH
        ),
        tool_gate=ResearchToolGate(
            args.get("tool_catalog_path", "config/agent_krypto_research_tool_catalog.json")
        ),
        backtest_engine=LocalFeatureMomentumEngine(),
    )
    return runner.run(experiment_config)


def _fold_from_dict(payload: Mapping[str, Any]) -> Fold:
    return Fold(
        index=payload["index"],
        train_idx=tuple(payload["train_idx"]),
        test_idx=tuple(payload["test_idx"]),
        purged_idx=tuple(payload.get("purged_idx", ())),
        embargo_idx=tuple(payload.get("embargo_idx", ())),
    )


def _handle_evaluate(args: Mapping[str, Any], config: Mapping[str, Any]) -> dict[str, Any]:
    """Evaluate the frozen strategy's holdout partition with the *same*
    accepted trial config, feature dataset and BacktestEngine used for its
    research-partition trial (#103) — never a synthesized raw-market proxy.

    Loads the immutable trial result the artifact's strategy_version was
    frozen against (persisted by ``ExperimentRunner`` under
    ``<output_root>/trials/<trial_id>.json``), rebuilds its exact
    ``ExperimentConfig`` (same ``strategy_params``, ``costs``,
    ``feature_version``), and replays ``LocalFeatureMomentumEngine`` over the
    holdout index range only. A missing/mismatched trial, dataset or feature
    version is fail-closed: nothing here falls back to a raw-market metric.
    """
    claim_store = HoldoutClaimStore(
        db_path=args.get("holdout_claim_db") or DEFAULT_HOLDOUT_CLAIMS_DB
    )
    policy_payload = _load_json_file(args["promotion_policy_file"])
    policy = PromotionPolicy(**policy_payload)
    artifact = _artifact_from_payload(_load_json_file(args["strategy_artifact_file"]))

    output_root = Path(args.get("experiment_output_root") or DEFAULT_EXPERIMENT_OUTPUT_ROOT)
    trial_id = args.get("trial_id") or artifact.strategy_version
    trial_path = output_root / "trials" / f"{trial_id}.json"
    if not trial_path.is_file():
        raise ExperimentError(
            f"no accepted trial found for {trial_id!r}; run `experiment` before `evaluate`"
        )
    trial = json.loads(trial_path.read_text())
    if not trial.get("accepted"):
        raise ExperimentError(f"trial {trial_id!r} was not accepted; cannot evaluate holdout")
    lineage = trial["lineage"]
    if lineage["dataset_version"] != artifact.dataset_version:
        raise ExperimentError("trial dataset_version does not match the artifact")
    if lineage["feature_version"] != artifact.feature_schema_version:
        raise ExperimentError("trial feature_version does not match the artifact")
    if lineage["strategy_version"] != artifact.strategy_version:
        raise ExperimentError("trial strategy_version does not match the artifact")

    experiment_config = ExperimentConfig(
        dataset_version=lineage["dataset_version"],
        feature_version=lineage["feature_version"],
        strategy_version=lineage["strategy_version"],
        symbol=artifact.symbol,
        timeframe=artifact.label_config.base_timeframe,
        seed=lineage["seed"],
        strategy_params=trial["params"],
        costs=trial["costs"],
    )

    lake = CryptoDataLake(root=args.get("data_root") or DEFAULT_DATA_ROOT)
    bars = [
        row
        for row in lake.read_version(experiment_config.dataset_version).to_pylist()
        if row["data_kind"] == "ohlcv"
        and row["symbol"] == experiment_config.symbol
        and row["timeframe"] == experiment_config.timeframe
    ]
    bars.sort(key=lambda row: row["available_at"])
    for row in bars:
        row.setdefault("close_time", row["available_at"])
    if len(bars) < 5:
        raise ExperimentError("not enough bars to compute the frozen holdout")

    labels = build_point_in_time_labels(bars, config=artifact.label_config)
    feature_root = Path(args.get("data_root") or DEFAULT_DATA_ROOT)
    feature_rows = _load_feature_rows(feature_root, experiment_config)
    feature_by_time = {str(row["available_at"]): row for row in feature_rows}
    aligned_features = []
    for label in labels:
        row = feature_by_time.get(str(label["decision_at"]))
        if row is None:
            raise ExperimentError("feature dataset is not point-in-time aligned")
        aligned_features.append(row)

    split = chronological_holdout(len(labels), fraction=artifact.holdout_fraction)
    if not split.holdout_idx:
        raise ExperimentError("holdout split produced no labeled samples")

    # Pass the FULL label/feature arrays (research tail + holdout), with
    # test_idx restricted to the absolute holdout indices. A frozen strategy
    # needs `lookback` bars of history before the first holdout bar to
    # compute momentum there — slicing to holdout-only rows and re-indexing
    # from 0 would silently drop that warm-up context and make the engine
    # skip (or misjudge) the first `lookback` holdout bars. Trades/metrics
    # are still scoped strictly to test_idx, so nothing outside the holdout
    # is scored.
    holdout_fold = Fold(
        index=0,
        train_idx=(),
        test_idx=tuple(split.holdout_idx),
        purged_idx=(),
        embargo_idx=(),
    )
    round_trip_cost = (
        float(experiment_config.costs["fee_bps"])
        + float(experiment_config.costs["spread_bps"])
        + float(experiment_config.costs["slippage_bps"])
    ) / 10_000.0
    _, fold_metrics = LocalFeatureMomentumEngine().run(
        labels=labels,
        feature_rows=aligned_features,
        folds=[holdout_fold],
        config=experiment_config,
        round_trip_cost=round_trip_cost,
    )
    holdout_metrics = fold_metrics[0]

    artifact.run_final_evaluation(holdout_metrics, claim_store=claim_store)
    decision = artifact.evaluate_promotion(
        policy,
        oos_trade_count=int(args.get("oos_trade_count", 0)),
        costs_included=bool(args.get("costs_included", experiment_config.costs)),
        trial_count=int(args.get("trial_count", 1)),
    )
    return {"artifact": artifact.to_dict(), "promotion_decision": decision}


def _handle_promote(args: Mapping[str, Any], config: Mapping[str, Any]) -> dict[str, Any]:
    """Advance and durably audit one legal StrategyArtifact transition."""
    artifact = _artifact_from_payload(_load_json_file(args["strategy_artifact_file"]))
    target_status = args["target_status"]
    if target_status == "PROMOTED" and not (
        artifact.promotion_decision and artifact.promotion_decision.get("passed")
    ):
        raise ValueError(
            "cannot promote without a passing promotion_decision; run `evaluate` first"
        )
    artifact.transition(target_status, reason=args.get("reason", ""))
    if target_status == "PROMOTED":
        artifact.set_valid_until(base_timeframe=artifact.label_config.base_timeframe)
    registry = StrategyArtifactRegistry(args["artifact_registry_db"])
    payload = artifact.to_dict()
    if target_status == "PROMOTED":
        registry.activate(
            payload,
            claim_store=HoldoutClaimStore(
                args.get("holdout_claim_db")
                or DEFAULT_HOLDOUT_CLAIMS_DB
            ),
            expected_dataset_version=config["dataset_version"],
            expected_feature_schema_version=config["feature_schema_version"],
            expected_promotion_policy_version=config["promotion_policy_version"],
            reason=args.get("reason", ""),
        )
    else:
        registry.persist(payload, reason=args.get("reason", ""))
    return {
        "artifact": payload,
        "durable_registry": str(args["artifact_registry_db"]),
    }


def _handle_cycle(args: Mapping[str, Any], config: Mapping[str, Any]) -> dict[str, Any]:
    if args.get("strategy_artifact_file"):
        artifact_payload = _load_json_file(args["strategy_artifact_file"])
    else:
        try:
            artifact_payload = StrategyArtifactRegistry(
                args["artifact_registry_db"]
            ).get_active(
                symbol=args["symbol"],
                expected_dataset_version=config["dataset_version"],
                expected_feature_schema_version=config["feature_schema_version"],
                expected_promotion_policy_version=config["promotion_policy_version"],
            )
        except (ValueError, TypeError):
            # The cycle gate converts an empty/incompatible artifact into WAIT.
            artifact_payload = {}
    trade_intent = _load_json_file(args.get("trade_intent_file")) or None

    def _execute(intent: Mapping[str, Any]) -> Mapping[str, Any]:
        from services.portfolio_client import PortfolioClient

        return PortfolioClient().submit_trade_intent(
            portfolio_id=int(args["portfolio_id"]), intent=intent
        )

    return run_agent_krypto_cycle(
        artifact=artifact_payload,
        symbol=args["symbol"],
        expected_dataset_version=config["dataset_version"],
        expected_feature_schema_version=config["feature_schema_version"],
        expected_promotion_policy_version=config["promotion_policy_version"],
        trade_intent=trade_intent,
        execute=_execute,
    )


def _synthetic_e2e_fixture(as_of: datetime, *, n_bars: int = 220) -> dict[str, Any]:
    """Deterministic, offline-only fixture covering every SYMBOLS x TIMEFRAMES
    ohlcv pair plus every non-ohlcv DATA_KINDS stream (so ``require_ready_dataset``
    accepts it) with a steady uptrend for BTC-USDT-SWAP/15m so the walk-forward
    trial and holdout both produce trades deterministically. No network I/O."""
    from services.crypto_data_lake import DATA_KINDS, SYMBOLS, TIMEFRAMES

    records: list[dict[str, Any]] = []
    for symbol in SYMBOLS:
        for timeframe in TIMEFRAMES:
            if symbol == "BTC-USDT-SWAP" and timeframe == "15m":
                start = as_of - timedelta(minutes=15 * n_bars)
                for index in range(n_bars):
                    available = start + timedelta(minutes=15 * (index + 1))
                    close = 100.0 + index * 0.5
                    records.append({
                        "symbol": symbol, "timeframe": timeframe, "data_kind": "ohlcv",
                        "observed_at": (available - timedelta(minutes=15)).isoformat(),
                        "available_at": available.isoformat(),
                        "source": "offline-e2e-fixture",
                        "open": close - 0.05, "high": close + 0.1, "low": close - 0.1,
                        "close": close, "volume": 10.0,
                    })
            else:
                records.append({
                    "symbol": symbol, "timeframe": timeframe, "data_kind": "ohlcv",
                    "observed_at": (as_of - timedelta(minutes=1)).isoformat(),
                    "available_at": as_of.isoformat(),
                    "source": "offline-e2e-fixture",
                    "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "volume": 10.0,
                })
        for kind in DATA_KINDS:
            if kind == "ohlcv":
                continue
            records.append({
                "symbol": symbol, "timeframe": "1m", "data_kind": kind,
                "observed_at": (as_of - timedelta(minutes=1)).isoformat(),
                "available_at": as_of.isoformat(),
                "source": "offline-e2e-fixture",
                "value": 1.0,
            })
    return {"records": records, "sha256": "offline-e2e-fixture"}


def _handle_e2e(args: Mapping[str, Any], config: Mapping[str, Any]) -> dict[str, Any]:
    """One offline command that drives the *entire* research-to-runtime loop:

        ingest -> LearningRequest (bollinger_bands) -> experiment (walk-forward)
        -> evaluate (holdout) -> promote PAPER/DEMO/PROMOTED -> cycle (WAIT)

    All data is a synthetic, deterministic fixture generated in-process — no
    network call, no external fixture file required (AC: "Offline E2E runs the
    full loop with one command, no network"). Every stage reuses the exact
    same handler function the individual CLI subcommands use, so this cannot
    silently drift from ``ingest``/``request``/``experiment``/``evaluate``/
    ``promote``/``cycle`` behavior.

    The final ``cycle`` call deliberately passes no ``trade_intent`` (WAIT):
    this command never places an order — a "PROMOTED" artifact at the end of
    an *offline* E2E is a fixture-only smoke result, not a claim that the
    strategy would earn edge on a real market. Verifying execution is the
    OKX Demo smoke plan's job (see docs/agent-krypto-runbook.md), and it is
    manual/opt-in on purpose.
    """
    now = datetime.now(timezone.utc)
    data_root = Path(args.get("data_root") or (
        DEFAULT_RUNTIME_ROOT / "e2e" / (args.get("run_bucket") or now.date().isoformat())
    ))
    data_root.mkdir(parents=True, exist_ok=True)
    symbol = "BTC-USDT-SWAP"

    # 1) ingest ---------------------------------------------------------
    fixture = _synthetic_e2e_fixture(now)
    fixture_path = data_root / "fixture.json"
    fixture_path.write_text(json.dumps(fixture))
    ingest_result = _handle_ingest(
        {"data_root": str(data_root), "source_path": str(fixture_path), "as_of": now.isoformat(),
         "max_age_minutes": 60 * 24 * 365},
        config,
    )
    dataset_id = ingest_result["dataset_id"]
    lake = CryptoDataLake(root=data_root)

    # The published dataset version mixes every symbol/timeframe/data_kind in
    # one Parquet file (by design — CryptoDataLake is one immutable table per
    # version). FeatureDatasetStore.bollinger_bands operates on a single
    # `close` series, so it must be given only the BTC-USDT-SWAP/15m OHLCV
    # rows, not the full multi-symbol/multi-kind version file (which has
    # ``close=None`` for non-ohlcv rows and other symbols/timeframes).
    import pyarrow.parquet as _pq
    import pyarrow.compute as _pc

    full_table = lake.read_version(dataset_id)
    ohlcv_mask = _pc.and_(
        _pc.and_(
            _pc.equal(full_table["symbol"], symbol),
            _pc.equal(full_table["timeframe"], "15m"),
        ),
        _pc.equal(full_table["data_kind"], "ohlcv"),
    )
    ohlcv_table = full_table.filter(ohlcv_mask).sort_by("available_at")
    market_parquet = data_root / "btc_15m_ohlcv.parquet"
    _pq.write_table(ohlcv_table, market_parquet)

    # 2) request (LearningRequest: bollinger_bands) ----------------------
    request_path = data_root / "learning_request.json"
    request_path.write_text(json.dumps({
        "request_id": "e2e-req-1", "base_dataset_version": dataset_id,
        "requested_by": "agent-krypto-research", "symbols": [symbol],
        "hypothesis": "offline E2E smoke: bollinger width is computable end to end",
        "features": [{
            "name": "bollinger_bands", "timeframe": "15m",
            "params": {"window": 20, "stddev": 2.0},
            "reason": "offline e2e coverage (#105)",
        }],
    }))
    request_result = _handle_request(
        {"request_file": str(request_path), "request_kind": "learning",
         "base_dataset_path": str(market_parquet), "data_root": str(data_root),
         "feature_catalog_path": str(_REPO_ROOT / "config/agent_krypto_feature_catalog.json")},
        config,
    )
    if request_result["status"] != EXECUTED:
        raise ExperimentError(f"e2e: LearningRequest did not execute: {request_result}")
    feature_version = Path(request_result["feature_dataset_paths"][0]).name

    # 3) experiment (purged walk-forward) --------------------------------
    experiment_output_root = data_root / "experiments"
    experiment_config = ExperimentConfig(
        dataset_version=dataset_id, feature_version=feature_version,
        strategy_version=f"e2e-{dataset_id}", symbol=symbol, timeframe="15m", seed=42,
        strategy_params={"lookback": 3, "threshold": 0.01, "signal_feature": "close"},
        costs={"fee_bps": 0.1, "spread_bps": 0.1, "slippage_bps": 0.1},
        tool_request={
            "request_id": "e2e-tool-1", "requested_by": "agent-krypto-research",
            "tool": "walk_forward", "params": {}, "reason": "offline e2e coverage (#105)",
        },
    )
    runner = ExperimentRunner(
        lake, feature_root=data_root, output_root=experiment_output_root,
        experiment_sink=MlflowExperimentRegistry(
            f"sqlite:///{data_root / 'mlruns.db'}", "agent-krypto-e2e",
        ),
        research_notes=ChromaResearchNotes(str(data_root / "rag")),
        tool_gate=ResearchToolGate(str(_REPO_ROOT / "config/agent_krypto_research_tool_catalog.json")),
        backtest_engine=LocalFeatureMomentumEngine(),
    )
    trial = runner.run(experiment_config)
    if not trial.get("accepted"):
        raise ExperimentError(f"e2e: walk-forward trial was not accepted: {trial.get('reason')}")

    # 4) evaluate (holdout) ----------------------------------------------
    # fold_metrics/n_folds/oos_trade_count come from the *actual accepted
    # trial* the runner just produced, not hand-picked numbers — activate()
    # (used by the promote step below) recomputes and cross-checks these
    # against the promotion decision, so a fabricated fold_metrics evidence
    # set would fail closed there exactly as it must for a real strategy.
    trial_fold_metrics = trial["fold_metrics"]
    oos_trade_count = sum(int(fold["trade_count"]) for fold in trial_fold_metrics)
    policy_path = data_root / "promotion_policy.json"
    policy_path.write_text(json.dumps({
        "min_folds_required": 1, "min_oos_trades": 0,
        "min_positive_fold_fraction": 0.0, "max_drawdown": 1.0,
        "min_profit_factor": 0.0, "require_positive_holdout_expectancy": False,
    }))
    artifact_path = data_root / "artifact.json"
    artifact_path.write_text(json.dumps({
        "strategy_version": experiment_config.strategy_version, "symbol": symbol,
        "dataset_version": dataset_id, "feature_schema_version": feature_version,
        "label_config": {"base_timeframe": "15m", "horizon": 1, "label_type": "log_return", "threshold": 0.0},
        "promotion_policy_version": "e2e-p1", "n_folds": len(trial_fold_metrics), "embargo_bars": 3,
        "holdout_fraction": 0.2, "seed": 42, "costs": experiment_config.costs,
        "status": "CANDIDATE",
        "fold_metrics": trial_fold_metrics,
    }))
    evaluate_result = _handle_evaluate(
        {"strategy_artifact_file": str(artifact_path), "promotion_policy_file": str(policy_path),
         "holdout_claim_db": str(data_root / "holdout_claims.db"), "data_root": str(data_root),
         "experiment_output_root": str(experiment_output_root), "trial_id": trial["trial_id"],
         "oos_trade_count": oos_trade_count, "costs_included": True},
        config,
    )
    evaluated_artifact = evaluate_result["artifact"]
    if not evaluate_result["promotion_decision"]["passed"]:
        raise ExperimentError(
            f"e2e: promotion policy rejected the offline fixture trial: "
            f"{evaluate_result['promotion_decision']['failures']}"
        )
    evaluated_artifact_path = data_root / "evaluated_artifact.json"
    evaluated_artifact_path.write_text(json.dumps(evaluated_artifact))

    # 5) promote CANDIDATE -> PAPER -> DEMO -> PROMOTED -------------------
    # `promote`/`cycle` gate PROMOTED activation against config["dataset_version"]
    # / feature_schema_version / promotion_policy_version — the *environment's*
    # resolved versions. This offline E2E mints its own dataset/feature/policy
    # versions from an in-process fixture, so it must gate promotion/cycle
    # against those e2e-local versions, not the (possibly still "unset")
    # orchestrator config passed in from the CLI. This mirrors exactly what a
    # real deployment does once its own dataset_version/feature_schema_version/
    # promotion_policy_version config fields are resolved to real values.
    e2e_config = {
        **config,
        "dataset_version": dataset_id,
        "feature_schema_version": feature_version,
        "promotion_policy_version": "e2e-p1",
    }
    registry_db = data_root / "artifacts" / "registry.db"
    current_path = evaluated_artifact_path
    promote_results: dict[str, Any] = {}
    for target_status in ("PAPER", "DEMO", "PROMOTED"):
        step = _handle_promote(
            {"strategy_artifact_file": str(current_path), "target_status": target_status,
             "reason": "offline e2e (#105)", "artifact_registry_db": str(registry_db),
             "holdout_claim_db": str(data_root / "holdout_claims.db")},
            e2e_config,
        )
        promote_results[target_status] = step["artifact"]["status"]
        current_path = data_root / f"artifact_{target_status.lower()}.json"
        current_path.write_text(json.dumps(step["artifact"]))

    # 6) cycle (WAIT — this command never places an order) ---------------
    cycle_result = _handle_cycle(
        {"strategy_artifact_file": str(current_path), "artifact_registry_db": str(registry_db),
         "symbol": symbol},
        e2e_config,
    )

    return {
        "phases": {
            "ingest": {"dataset_id": dataset_id},
            "request": {"feature_version": feature_version},
            "experiment": {"trial_id": trial["trial_id"], "accepted": trial["accepted"]},
            "evaluate": {"promotion_decision": evaluate_result["promotion_decision"]},
            "promote": promote_results,
            "cycle": cycle_result,
        },
        "data_root": str(data_root),
        "artifact_registry_db": str(registry_db),
    }


def _handle_research_loop(args: Mapping[str, Any], config: Mapping[str, Any]) -> dict[str, Any]:
    """Observation-mode research loop over REAL market data (#116/#118).

    Drives ingest -> LearningRequest (bollinger_bands, all 5 symbols) ->
    experiment+evaluate (crypto_multi_symbol_experiment: triple-barrier,
    purge/embargo, holdout per symbol) -> cycle, exactly like ``e2e`` reuses
    each phase's own handler function so this cannot silently drift from
    ``ingest``/``request``/``cycle`` behavior. Unlike ``e2e``:

    - ingest reads a real adapter (``--source-path``/``--local-data-dir``),
      never the in-process synthetic fixture;
    - there is no ``promote`` step — evaluate only ever reports
      ACCEPTED/REJECTED, and ``cycle`` is always invoked without a
      TradeIntent/portfolio-id/tracker-db-path, so it can only resolve to
      WAIT with ``execution_result=None``. Nothing here can reach
      Portfolio Manager or create a TradeIntent.

    A failure in any phase raises (fail-closed): the surrounding ``dispatch``
    boundary turns that into one ERROR envelope for the whole run, never a
    partial promote.
    """
    now = datetime.now(timezone.utc)
    data_root = Path(args.get("data_root") or DEFAULT_DATA_ROOT)
    data_root.mkdir(parents=True, exist_ok=True)
    default_symbols = _DEFAULT_LOCAL_SYMBOLS if args.get("local_data_dir") else SYMBOLS
    symbols = tuple(str(value) for value in (args.get("required_symbols") or default_symbols))

    # 1) INGEST — real adapter, never the offline e2e fixture. -----------
    ingest_result = _handle_ingest(
        {
            "data_root": str(data_root),
            "source_path": args.get("source_path"),
            "local_data_dir": args.get("local_data_dir"),
            "base_dataset_id": args.get("base_dataset_id"),
            "adapter_name": args.get("adapter_name"),
            "as_of": args.get("as_of") or now.isoformat(),
            "max_age_minutes": args.get("max_age_minutes", 20),
            "required_symbols": symbols,
        },
        config,
    )
    dataset_id = ingest_result["dataset_id"]
    lake = CryptoDataLake(root=data_root)

    # 2) REQUEST — LearningRequest(bollinger_bands) over every symbol. The
    #    published dataset version mixes every symbol/timeframe/data_kind in
    #    one Parquet file (by design — CryptoDataLake is one immutable table
    #    per version); FeatureDatasetStore.bollinger_bands requires a `close`
    #    value on every row it is given, so non-ohlcv rows (funding,
    #    open_interest, ...) and other timeframes must be filtered out first
    #    — but, unlike the single-symbol offline ``e2e`` fixture, every
    #    symbol's ohlcv/label-timeframe rows are kept (bollinger_bands groups
    #    by symbol internally), producing one shared feature_schema_version.
    import pyarrow.parquet as _pq
    import pyarrow.compute as _pc

    label_timeframe = config.get("label_timeframe", "15m")
    full_table = lake.read_version(dataset_id)
    ohlcv_mask = _pc.and_(
        _pc.equal(full_table["timeframe"], label_timeframe),
        _pc.equal(full_table["data_kind"], "ohlcv"),
    )
    ohlcv_table = full_table.filter(ohlcv_mask).sort_by(
        [("symbol", "ascending"), ("available_at", "ascending")]
    )
    market_parquet = data_root / f"ohlcv_{label_timeframe}_{dataset_id}.parquet"
    _pq.write_table(ohlcv_table, market_parquet)

    request_path = data_root / f"learning_request_{dataset_id}.json"
    request_path.write_text(json.dumps({
        "request_id": f"research-loop-{dataset_id}",
        "base_dataset_version": dataset_id,
        "requested_by": "agent-krypto-research",
        "symbols": list(symbols),
        "hypothesis": "observation-mode research loop: bollinger_percent_b signal (#116)",
        "features": [{
            "name": "bollinger_bands", "timeframe": label_timeframe,
            "params": {"window": 20, "stddev": 2.0},
            "reason": "research-loop observation mode (#116/#118)",
        }],
    }))
    request_result = _handle_request(
        {
            "request_file": str(request_path), "request_kind": "learning",
            "base_dataset_path": str(market_parquet), "data_root": str(data_root),
            "feature_catalog_path": args.get(
                "feature_catalog_path", str(_REPO_ROOT / "config/agent_krypto_feature_catalog.json")
            ),
        },
        config,
    )
    if request_result["status"] != EXECUTED:
        raise ExperimentError(f"research-loop: LearningRequest did not execute: {request_result}")
    feature_version = Path(request_result["feature_dataset_paths"][0]).name

    # 3) FEATURES — feature_schema_version is the content-addressed id
    #    FeatureDatasetStore just minted; nothing further to compute here.

    # 4) EXPERIMENT + 5) EVALUATE — one call: run_offline_experiment already
    #    performs triple-barrier labeling, purge/embargo walk-forward and a
    #    one-shot holdout per symbol, then evaluates the aggregate against
    #    the versioned PromotionPolicy-shaped policy (min OOS trades per
    #    symbol/direction, drawdown, fold stability, multiple-testing
    #    correction) and reports accepted/rejected — there is no separate
    #    per-symbol StrategyArtifact/PromotionPolicy step in observation mode.
    label_config_path = args.get(
        "label_config_path", str(_REPO_ROOT / "config/agent_krypto_label_config.json")
    )
    policy_path = args.get(
        "policy_path", str(_REPO_ROOT / "config/agent_krypto_promotion_policy.json")
    )
    experiment_output_root = data_root / "multi_symbol_experiments"
    experiment_report = run_offline_experiment(
        lake_root=data_root / "raw",
        feature_root=data_root,
        output_root=experiment_output_root,
        dataset_version=dataset_id,
        feature_version=feature_version,
        label_config_path=label_config_path,
        policy_path=policy_path,
        trial_count=int(args.get("trial_count", 1)),
    )
    evaluate_status = "ACCEPTED" if experiment_report["status"] == "accepted" else "REJECTED"

    # 6) PROVIDER — recorded by dispatch() via provider_for_bucket() before
    #    this handler ever runs (#119); this handler does not choose a
    #    provider identity.

    # 7) CYCLE — deliberately no trade_intent_file/portfolio_id/tracker_db_path:
    #    this observation-mode loop can only ever resolve to WAIT.
    cycle_result = _handle_cycle(
        {"artifact_registry_db": args.get("artifact_registry_db", DEFAULT_ARTIFACT_REGISTRY_DB),
         "symbol": symbols[0]},
        config,
    )
    if cycle_result.get("status") != "WAIT" or cycle_result.get("execution_result") is not None:
        raise ExperimentError(
            f"research-loop: cycle did not resolve to WAIT with no execution: {cycle_result}"
        )

    # PAPER ONLY: persist one observation decision per symbol. This adapter
    # consumes serialized loop output and latest closes; it has no exchange
    # client path and therefore cannot place an order.
    latest_prices: dict[str, Any] = {}
    for row in ohlcv_table.to_pylist():
        if row.get("symbol") in symbols and row.get("close") is not None:
            latest_prices[str(row["symbol"])] = row["close"]
    paper_db = Path(args.get("paper_db_path") or (data_root / "paper_execution.sqlite"))
    paper_db.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(paper_db) as paper_conn:
        feature_rows = _pq.read_table(
            data_root / "datasets" / feature_version / "features.parquet"
        ).to_pylist()
        signal_decisions = derive_point_in_time_decisions(
            paper_conn,
            feature_rows=feature_rows,
            market_prices=latest_prices,
            # Keep these parameters aligned with LocalFeatureMomentumEngine;
            # they are the live, point-in-time signal contract, not evaluate status.
            lookback=int(args.get("signal_lookback", 1)),
            threshold=float(args.get("signal_threshold", 0.01)),
            signal_feature=str(args.get("signal_feature", "bb_percent_b")),
            qty=args.get("paper_qty", 1),
        )
        paper_result = record_research_loop_result(
            paper_conn,
            {"run_id": f"research-loop-{dataset_id}", "decisions": [
                {"trade_intent": decision} for decision in signal_decisions
            ]},
            market_prices=latest_prices,
            model_version=feature_version,
            policy_version=str(policy_path),
            dataset_version=dataset_id,
        )

    return {
        "phases": {
            "ingest": {"dataset_id": dataset_id, "symbols": list(symbols)},
            "request": {"feature_version": feature_version},
            "experiment": {
                "run_id": experiment_report["run_id"],
                "status": experiment_report["status"],
                "failures": experiment_report["failures"],
            },
            "evaluate": {"status": evaluate_status},
            "cycle": cycle_result,
            "paper": {"ledger_path": str(paper_db), "decisions": paper_result,
                       "signal": {"feature": str(args.get("signal_feature", "bb_percent_b")),
                                  "lookback": int(args.get("signal_lookback", 1)),
                                  "threshold": float(args.get("signal_threshold", 0.01))}},
        },
        "data_root": str(data_root),
        "experiment_report_path": str(experiment_output_root / f"{experiment_report['run_id']}.json"),
    }


def _search_space_from_payload(payload: Mapping[str, Any]) -> SearchSpace:
    def _range(name: str) -> ParamRange:
        spec = payload[name]
        if "choices" in spec:
            return ParamRange(choices=tuple(spec["choices"]))
        return ParamRange(low=spec["low"], high=spec["high"], step=spec["step"])

    return SearchSpace(
        dataset_version=payload["dataset_version"],
        feature_version=payload["feature_version"],
        lookback=_range("lookback"),
        threshold=_range("threshold"),
        signal_feature=_range("signal_feature"),
        model_variant=_range("model_variant"),
    )


def _handle_candidate_cycle(args: Mapping[str, Any]) -> dict[str, Any]:
    """One candidate-driven experiment cycle — OSOBNA ścieżka od `research-loop`.

    #147: research-loop (istniejący, niezmieniony) drives
    ``crypto_multi_symbol_experiment.run_offline_experiment`` with a fixed
    strategy and never searches parameters. This command instead drives the
    #96/#140 stack (``CandidateGenerator`` -> ``ExperimentRunner`` on the
    RESEARCH partition only) one candidate per invocation, using the durable
    ``CandidateCursor`` (#148) so repeated invocations advance through the
    search space instead of always re-running the first candidate. It has its
    own idempotency (the cursor's ``run_bucket`` reservation) and does not go
    through ``dispatch()``/``RunStore``/``PHASE_FOR`` — it is not a phase of
    the existing orchestrator lifecycle.

    Never promotes, never places a TradeIntent, never calls
    ``ChampionRegistry.promote``/``rollback`` — this handler only generates a
    candidate, runs its trial, records an insight report (#142) and a
    diagnostic monitoring status (#143, data-quality only today). Neither of
    those two additions can change what this handler does with the trial
    result — a CRITICAL monitoring status is surfaced in the returned JSON
    for an operator to read, never acted on here.
    """
    search_space = _search_space_from_payload(_load_json_file(args["search_space_file"]))
    seed = int(args["seed"])
    run_bucket = str(args.get("run_bucket") or "")
    if not run_bucket:
        raise CandidateCursorError("--run-bucket is required for candidate-cycle")

    cursor = CandidateCursor(db_path=args.get("candidate_cursor_db", DEFAULT_CANDIDATE_CURSOR_DB))
    advance_result = cursor.advance(search_space=search_space, seed=seed, run_bucket=run_bucket)

    generator = CandidateGenerator(
        search_space=search_space, seed=seed, trial_budget=search_space.grid_size
    )
    candidate = generator.generate()[advance_result.trial_index]

    data_root = args.get("data_root") or DEFAULT_DATA_ROOT
    lake = CryptoDataLake(root=data_root)
    runner = ExperimentRunner(
        lake,
        feature_root=data_root,
        output_root=args.get("experiment_output_root") or DEFAULT_EXPERIMENT_OUTPUT_ROOT,
        experiment_sink=MlflowExperimentRegistry(
            args.get("mlflow_tracking_uri") or DEFAULT_MLFLOW_TRACKING_URI,
            "agent-krypto-research",
        ),
        research_notes=ChromaResearchNotes(
            args.get("rag_path") or DEFAULT_RAG_PATH
        ),
        tool_gate=ResearchToolGate(
            args.get("tool_catalog_path", "config/agent_krypto_research_tool_catalog.json")
        ),
        backtest_engine=LocalFeatureMomentumEngine(),
    )
    experiment_config = ExperimentConfig(
        dataset_version=search_space.dataset_version,
        feature_version=search_space.feature_version,
        strategy_version=candidate.candidate_id,
        symbol=args["symbol"],
        timeframe=args.get("timeframe", "15m"),
        seed=seed,
        strategy_params=candidate.strategy_params,
        costs=_load_json_file(args["costs_file"]) if args.get("costs_file") else {
            "fee_bps": 1.0, "spread_bps": 0.5, "slippage_bps": 0.5,
        },
        tool_request={
            "request_id": f"candidate-cycle-{candidate.candidate_id}",
            "requested_by": "agent-krypto-research",
            "tool": "walk_forward",
            "params": {},
            "reason": "candidate-driven experiment cycle (#147-149)",
        },
    )
    trial_result = runner.run(experiment_config)

    # #150: insights — a structured, read-only report over the trial JSON
    # just produced. InsightReportStore rejects a duplicate run_id outright,
    # so a retried run_bucket (cursor.replayed=True, same trial_id) must not
    # attempt a second save — the first cycle already recorded it.
    insight_report_run_id = None
    if not advance_result.replayed:
        insight_store = InsightReportStore(
            db_path=args.get("insight_reports_db", DEFAULT_INSIGHT_REPORTS_DB)
        )
        # History for the escalation heuristic (#150 follow-up): prior trial
        # reports for this symbol, newest-first, plus the columns actually
        # present in this run's feature dataset — never invents a feature
        # name outside what the dataset already has.
        history = insight_store.list_recent(symbol=args["symbol"], source="trial")
        try:
            import pyarrow.parquet as _pq

            feature_parquet_path = (
                Path(data_root) / "datasets" / search_space.feature_version / "features.parquet"
            )
            non_feature_columns = {
                "available_at", "close", "data_kind", "high", "low", "observed_at",
                "open", "source", "symbol", "timeframe", "volume",
            }
            all_columns = set(_pq.read_schema(feature_parquet_path).names)
            available_signal_features = sorted(
                (all_columns - non_feature_columns) | {"close", "open", "high", "low"}
            )
        except (OSError, FileNotFoundError):
            available_signal_features = []
        try:
            insight_store.save(
                build_trial_insight_report(
                    trial_result,
                    history=history,
                    available_signal_features=available_signal_features,
                )
            )
            insight_report_run_id = trial_result["trial_id"]
        except InsightsError as exc:
            if "already recorded" not in str(exc):
                raise

    # #150: monitoring — data-quality check over the same dataset this trial
    # just read, purely diagnostic (never gates or stops the cycle: a
    # CRITICAL status is surfaced to the operator in the result, not acted on
    # here). Only data_quality is wired today — signal_effectiveness/
    # pnl_and_errors need paper_execution_ledger data candidate-cycle does
    # not have, and feature_drift needs a reference window this command does
    # not define yet; both remain future work if/when candidate-cycle grows a
    # paper-linked variant.
    dataset_records = lake.read_version(experiment_config.dataset_version).to_pylist()
    monitoring_thresholds = MonitoringThresholds(
        warning_age=timedelta(minutes=20), max_age=timedelta(hours=1),
    )
    data_quality_check = check_data_quality(
        dataset_records,
        as_of=datetime.now(timezone.utc),
        thresholds=monitoring_thresholds,
        required_symbols=(experiment_config.symbol,),
        required_timeframes=(experiment_config.timeframe,),
        required_data_kinds=("ohlcv",),
    )
    monitoring_report = build_monitoring_report([data_quality_check])

    return {
        "candidate": candidate.to_dict(),
        "cursor": {
            "search_space_version": advance_result.search_space_version,
            "seed": advance_result.seed,
            "trial_index": advance_result.trial_index,
            "run_bucket": advance_result.run_bucket,
            "replayed": advance_result.replayed,
        },
        "trial": trial_result,
        "insight_report_run_id": insight_report_run_id,
        "monitoring": monitoring_report.to_dict(),
    }


def _handle_champion_compare(args: Mapping[str, Any]) -> dict[str, Any]:
    """Compare an already-evaluated challenger artifact against the current
    champion — OSOBNA, read-only command (#151).

    Requires a ``StrategyArtifact.to_dict()``-shaped JSON that already
    completed the one-shot holdout and passed ``evaluate_promotion``
    (``holdout_evaluated: true``, a passing ``promotion_decision`` — see
    ``crypto_champion_registry.compare_to_champion``/``_require_passed_decision``,
    #141). Read directly as a plain mapping — unlike ``evaluate``/``promote``,
    this command does not reconstruct a live ``StrategyArtifact`` dataclass
    (which would require every field, including ones irrelevant to a
    comparison like ``label_config``/``embargo_bars``); it only needs the
    subset of fields ``compare_to_champion`` reads. The champion side comes
    either from an explicit ``--champion-artifact-file`` or, if omitted, is
    looked up in ``ChampionRegistry`` by ``--symbol`` (``None`` if no champion
    is registered yet, in which case the challenger automatically wins per
    ``compare_to_champion``).

    This handler NEVER calls ``ChampionRegistry.promote``/``rollback`` — it
    only returns the ``ComparisonResult`` as JSON. Promoting/rolling back
    remains a deliberate, separate operator action.
    """
    challenger_artifact = _load_json_file(args["strategy_artifact_file"])

    symbol = args["symbol"]
    if args.get("champion_artifact_file"):
        champion_artifact = _load_json_file(args["champion_artifact_file"])
    else:
        registry = ChampionRegistry(
            db_path=args.get("champion_registry_db", DEFAULT_CHAMPION_REGISTRY_DB)
        )
        champion_artifact = registry.current_champion(symbol=symbol)

    comparison = compare_to_champion(
        symbol=symbol,
        challenger_artifact=challenger_artifact,
        champion_artifact=champion_artifact,
    )
    return {
        "symbol": comparison.symbol,
        "challenger_strategy_version": comparison.challenger_strategy_version,
        "champion_strategy_version": comparison.champion_strategy_version,
        "challenger_holdout_expectancy": comparison.challenger_holdout_expectancy,
        "champion_holdout_expectancy": comparison.champion_holdout_expectancy,
        "challenger_wins": comparison.challenger_wins,
        "reason": comparison.reason,
    }


def _handle_backup(args: Mapping[str, Any]) -> dict[str, Any]:
    manifest = create_backup(
        source_root=args["source_root"], backup_dir=args["backup_dir"],
    )
    return manifest.to_dict()


def _handle_restore(args: Mapping[str, Any]) -> dict[str, Any]:
    manifest = restore_backup(
        backup_dir=args["backup_dir"], target_root=args["target_root"],
        overwrite=bool(args.get("overwrite")),
    )
    return manifest.to_dict()


def _handle_verify_backup(args: Mapping[str, Any]) -> dict[str, Any]:
    return verify_backup_integrity(args["backup_dir"])


HANDLERS = {
    "ingest": _handle_ingest,
    "request": _handle_request,
    "experiment": _handle_experiment,
    "evaluate": _handle_evaluate,
    "promote": _handle_promote,
    "cycle": _handle_cycle,
    "e2e": _handle_e2e,
    "research-loop": _handle_research_loop,
}


def _handle_status(args: Mapping[str, Any], run_store: RunStore) -> dict[str, Any]:
    if args.get("run_id"):
        row = run_store.get(run_id=args["run_id"])
    elif args.get("phase") and args.get("symbol"):
        row = run_store.find_resumable(phase=args["phase"], symbol=args["symbol"])
    else:
        row = None
    if row is None:
        return envelope("status", status="DONE", reason="no matching run", run_id=None)
    result = json.loads(row["result_json"]) if row.get("result_json") else None
    return envelope(
        "status",
        status=row["status"],
        reason=row.get("reason"),
        run_id=row["run_id"],
        result=result,
        config_version=row.get("config_version"),
        provider=row.get("provider"),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    common = {
        "--config-version": dict(dest="config_version", required=False),
        "--symbol": dict(dest="symbol", required=False),
        "--run-bucket": dict(dest="run_bucket", required=False),
        "--run-db": dict(dest="run_db", default=DEFAULT_RUN_DB),
        "--config-path": dict(dest="config_path", default=DEFAULT_CONFIG_PATH),
        "--data-root": dict(dest="data_root"),
    }

    def add_common(p: argparse.ArgumentParser) -> None:
        for flag, kwargs in common.items():
            p.add_argument(flag, **kwargs)

    ingest = sub.add_parser("ingest")
    add_common(ingest)
    ingest_source = ingest.add_mutually_exclusive_group(required=True)
    ingest_source.add_argument("--source-path", dest="source_path")
    ingest_source.add_argument("--local-data-dir", dest="local_data_dir")
    ingest.add_argument("--base-dataset-id", dest="base_dataset_id")
    ingest.add_argument("--adapter-name", dest="adapter_name")
    ingest.add_argument("--as-of", dest="as_of")
    ingest.add_argument("--max-age-minutes", dest="max_age_minutes", type=int, default=20)
    ingest.add_argument("--required-symbol", dest="required_symbols", action="append")
    ingest.add_argument("--update-config", dest="update_config")

    request = sub.add_parser("request")
    add_common(request)
    request.add_argument("--request-file", dest="request_file", required=True)
    request.add_argument("--request-kind", dest="request_kind", choices=["learning", "tool"])
    request.add_argument("--base-dataset-path", dest="base_dataset_path")
    request.add_argument("--tool-catalog-path", dest="tool_catalog_path")
    request.add_argument("--feature-catalog-path", dest="feature_catalog_path")
    request.add_argument("--update-config", dest="update_config")

    experiment = sub.add_parser("experiment")
    add_common(experiment)
    experiment.add_argument("--experiment-config-file", dest="experiment_config_file", required=True)
    experiment.add_argument("--experiment-output-root", dest="experiment_output_root")
    experiment.add_argument("--mlflow-tracking-uri", dest="mlflow_tracking_uri")
    experiment.add_argument("--rag-path", dest="rag_path")
    experiment.add_argument("--tool-catalog-path", dest="tool_catalog_path")
    experiment.add_argument("--dataset-version", dest="dataset_version")
    experiment.add_argument("--feature-schema-version", dest="feature_schema_version")

    evaluate = sub.add_parser("evaluate")
    add_common(evaluate)
    evaluate.add_argument("--strategy-artifact-file", dest="strategy_artifact_file", required=True)
    evaluate.add_argument("--holdout-claim-db", dest="holdout_claim_db")
    evaluate.add_argument("--promotion-policy-file", dest="promotion_policy_file", required=True)
    evaluate.add_argument("--promotion-policy-version", dest="promotion_policy_version")
    evaluate.add_argument("--trial-id", dest="trial_id")
    evaluate.add_argument("--experiment-output-root", dest="experiment_output_root")
    evaluate.add_argument("--oos-trade-count", dest="oos_trade_count", type=int, default=0)
    evaluate.add_argument("--costs-included", dest="costs_included", action="store_true")

    promote = sub.add_parser("promote")
    add_common(promote)
    promote.add_argument("--strategy-artifact-file", dest="strategy_artifact_file", required=True)
    promote.add_argument("--target-status", dest="target_status", required=True)
    promote.add_argument("--reason", dest="reason", default="")
    promote.add_argument("--promotion-policy-version", dest="promotion_policy_version")
    promote.add_argument("--holdout-claim-db", dest="holdout_claim_db")
    promote.add_argument(
        "--artifact-registry-db", dest="artifact_registry_db",
        default=DEFAULT_ARTIFACT_REGISTRY_DB,
    )

    cycle = sub.add_parser("cycle")
    add_common(cycle)
    cycle.add_argument("--strategy-artifact-file", dest="strategy_artifact_file")
    cycle.add_argument(
        "--artifact-registry-db", dest="artifact_registry_db",
        default=DEFAULT_ARTIFACT_REGISTRY_DB,
    )
    cycle.add_argument("--trade-intent-file", dest="trade_intent_file")
    cycle.add_argument("--portfolio-id", dest="portfolio_id", type=int)
    cycle.add_argument("--tracker-db-path", dest="tracker_db_path")
    cycle.add_argument("--dataset-version", dest="dataset_version")
    cycle.add_argument("--feature-schema-version", dest="feature_schema_version")
    cycle.add_argument("--promotion-policy-version", dest="promotion_policy_version")

    e2e = sub.add_parser(
        "e2e",
        help="Run the full offline loop (ingest->request->experiment->evaluate->"
             "promote->cycle) with a synthetic in-process fixture. No network I/O.",
    )
    add_common(e2e)

    research_loop = sub.add_parser(
        "research-loop",
        help="Observation-mode research loop over REAL market data "
             "(ingest->request->experiment+evaluate->cycle WAIT). Never promotes, "
             "never places a TradeIntent (#116/#118).",
    )
    add_common(research_loop)
    research_loop_source = research_loop.add_mutually_exclusive_group(required=True)
    research_loop_source.add_argument("--source-path", dest="source_path")
    research_loop_source.add_argument("--local-data-dir", dest="local_data_dir")
    research_loop.add_argument("--base-dataset-id", dest="base_dataset_id")
    research_loop.add_argument("--adapter-name", dest="adapter_name")
    research_loop.add_argument("--as-of", dest="as_of")
    research_loop.add_argument("--max-age-minutes", dest="max_age_minutes", type=int, default=20)
    research_loop.add_argument("--required-symbol", dest="required_symbols", action="append")
    research_loop.add_argument("--feature-catalog-path", dest="feature_catalog_path")
    research_loop.add_argument("--label-config-path", dest="label_config_path")
    research_loop.add_argument("--policy-path", dest="policy_path")
    research_loop.add_argument("--trial-count", dest="trial_count", type=int, default=1)
    research_loop.add_argument("--signal-lookback", dest="signal_lookback", type=int, default=1)
    research_loop.add_argument("--signal-threshold", dest="signal_threshold", type=float, default=0.01)
    research_loop.add_argument("--signal-feature", dest="signal_feature", default="bb_percent_b")
    research_loop.add_argument("--paper-qty", dest="paper_qty", type=float, default=1.0)
    research_loop.add_argument("--paper-db-path", dest="paper_db_path")
    research_loop.add_argument(
        "--artifact-registry-db", dest="artifact_registry_db",
        default=DEFAULT_ARTIFACT_REGISTRY_DB,
    )

    candidate_cycle = sub.add_parser(
        "candidate-cycle",
        help="OSOBNA candidate-driven experiment cycle (#147-149): "
             "CandidateGenerator -> ExperimentRunner for ONE candidate per "
             "invocation, using the durable CandidateCursor. Independent from "
             "research-loop/dispatch; never promotes, never places a "
             "TradeIntent, never touches ChampionRegistry.",
    )
    candidate_cycle.add_argument("--search-space-file", dest="search_space_file", required=True)
    candidate_cycle.add_argument("--seed", dest="seed", type=int, required=True)
    candidate_cycle.add_argument("--symbol", dest="symbol", required=True)
    candidate_cycle.add_argument("--timeframe", dest="timeframe", default="15m")
    candidate_cycle.add_argument("--run-bucket", dest="run_bucket", required=True)
    candidate_cycle.add_argument("--data-root", dest="data_root")
    candidate_cycle.add_argument("--experiment-output-root", dest="experiment_output_root")
    candidate_cycle.add_argument("--mlflow-tracking-uri", dest="mlflow_tracking_uri")
    candidate_cycle.add_argument("--rag-path", dest="rag_path")
    candidate_cycle.add_argument("--tool-catalog-path", dest="tool_catalog_path")
    candidate_cycle.add_argument("--costs-file", dest="costs_file")
    candidate_cycle.add_argument(
        "--candidate-cursor-db", dest="candidate_cursor_db",
        default=DEFAULT_CANDIDATE_CURSOR_DB,
    )
    candidate_cycle.add_argument(
        "--insight-reports-db", dest="insight_reports_db",
        default=DEFAULT_INSIGHT_REPORTS_DB,
    )

    champion_compare = sub.add_parser(
        "champion-compare",
        help="OSOBNA, read-only comparison (#151): compare_to_champion for "
             "an already-evaluated StrategyArtifact vs. the current champion. "
             "Never calls ChampionRegistry.promote/rollback.",
    )
    champion_compare.add_argument("--strategy-artifact-file", dest="strategy_artifact_file", required=True)
    champion_compare.add_argument("--symbol", dest="symbol", required=True)
    champion_compare.add_argument("--champion-artifact-file", dest="champion_artifact_file")
    champion_compare.add_argument(
        "--champion-registry-db", dest="champion_registry_db",
        default=DEFAULT_CHAMPION_REGISTRY_DB,
    )

    status = sub.add_parser("status")
    status.add_argument("--run-id", dest="run_id")
    status.add_argument("--phase", dest="phase")
    status.add_argument("--symbol", dest="symbol")
    status.add_argument("--run-db", dest="run_db", default=DEFAULT_RUN_DB)

    backup = sub.add_parser(
        "backup", help="Copy datasets/MLflow/Chroma/SQLite state into a new backup_dir.",
    )
    backup.add_argument("--source-root", dest="source_root", required=True)
    backup.add_argument("--backup-dir", dest="backup_dir", required=True)

    restore = sub.add_parser(
        "restore", help="Restore datasets/MLflow/Chroma/SQLite state from a backup_dir.",
    )
    restore.add_argument("--backup-dir", dest="backup_dir", required=True)
    restore.add_argument("--target-root", dest="target_root", required=True)
    restore.add_argument("--overwrite", dest="overwrite", action="store_true")

    verify_backup = sub.add_parser(
        "verify-backup", help="Verify a backup_dir's contents against its manifest digests.",
    )
    verify_backup.add_argument("--backup-dir", dest="backup_dir", required=True)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    command = args.command
    payload = {k: v for k, v in vars(args).items() if k != "command"}

    if command == "status":
        run_store = RunStore(db_path=payload.pop("run_db"))
        result = _handle_status(payload, run_store)
    elif command in ("backup", "restore", "verify-backup"):
        # These operate directly on the workspace filesystem/registries and
        # are not gated by the versioned orchestrator config — a backup or
        # restore must be runnable even when no dataset/feature/promotion
        # version has ever been resolved yet (e.g. disaster recovery before
        # the first successful ingest).
        try:
            if command == "backup":
                body = _handle_backup(payload)
            elif command == "restore":
                body = _handle_restore(payload)
            else:
                body = _handle_verify_backup(payload)
            result = {"command": command, "status": "DONE", "reason": None, "result": body}
        except BackupError as exc:
            result = {"command": command, "status": "ERROR", "reason": str(exc), "result": None}
    elif command == "candidate-cycle":
        # #147-150: an OSOBNA, independent path from research-loop/dispatch —
        # its own idempotency comes from CandidateCursor's run_bucket
        # reservation (#148), not from RunStore/PHASE_FOR. Never promotes,
        # never places a TradeIntent, never calls ChampionRegistry.promote/
        # rollback (this handler function never imports/references
        # ChampionRegistry at all — see test_candidate_cycle_never_imports_
        # champion_registry_promote_path).
        try:
            body = _handle_candidate_cycle(payload)
            result = {"command": command, "status": "DONE", "reason": None, "result": body}
        except (
            CandidateCursorError, CandidateGeneratorError, ExperimentError,
            InsightsError, MonitoringError,
        ) as exc:
            result = {"command": command, "status": "ERROR", "reason": str(exc), "result": None}
    elif command == "champion-compare":
        # #151: read-only comparison. Never calls ChampionRegistry.promote/
        # rollback — only current_champion() (a read) and compare_to_champion
        # (a pure function). Promoting/rolling back remains a deliberate,
        # separate operator action outside this command.
        try:
            body = _handle_champion_compare(payload)
            result = {"command": command, "status": "DONE", "reason": None, "result": body}
        except (ChampionRegistryError, ExperimentError) as exc:
            result = {"command": command, "status": "ERROR", "reason": str(exc), "result": None}
    else:
        run_db = payload.pop("run_db")
        config_path = payload.pop("config_path")
        run_store = RunStore(db_path=run_db)
        result = dispatch(
            command,
            payload,
            run_store=run_store,
            handler=HANDLERS[command],
            config_path=config_path,
        )

    json.dump(result, sys.stdout, sort_keys=True)
    sys.stdout.write("\n")
    return 0 if result.get("status") != "ERROR" else 1


if __name__ == "__main__":
    sys.exit(main())
