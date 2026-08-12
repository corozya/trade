"""Backup/restore validation for the agent-krypto local research workspace (#105).

AC under test: "Restart/resume i backup/restore zachowują lineage, registry i
idempotency" — a backup/restore round-trip of datasets/MLflow/Chroma/SQLite
must reproduce the exact same content, and a restored orchestrator run store
and StrategyArtifact registry must retain their durable state (idempotent
run rows, active PROMOTED artifact) unchanged.
"""

from __future__ import annotations

import sqlite3

import pytest

from services.agent_krypto_backup import (
    BackupError,
    create_backup,
    read_manifest,
    restore_backup,
    verify_backup_integrity,
)
from services.agent_krypto_run_store import RunStore
from services.agent_krypto_artifact_registry import StrategyArtifactRegistry
from services.crypto_strategy_research import HoldoutClaimStore
from services.crypto_champion_registry import ChampionRegistry, compare_to_champion
from services.crypto_research_insights import InsightReportStore, build_trial_insight_report


def _seeded_workspace(root):
    """Populate a data root with every backup component this module knows
    about, using the real modules that own each store (not hand-written
    fixtures), so the round-trip test exercises the actual schemas."""
    research_root = root / "research" / "agent-krypto"
    research_root.mkdir(parents=True)

    # datasets/ + experiments/ : plain files, stand-ins for Parquet trees.
    (research_root / "datasets" / "features-1").mkdir(parents=True)
    (research_root / "datasets" / "features-1" / "features.parquet").write_bytes(b"parquet-bytes")
    (research_root / "experiments" / "trials").mkdir(parents=True)
    (research_root / "experiments" / "trials" / "trial-1.json").write_text('{"trial_id": "trial-1"}')

    # run_store: real RunStore so the SQLite schema/WAL are realistic.
    run_store = RunStore(db_path=research_root / "runs" / "orchestrator_runs.db")
    run_store.create(
        run_id="run-backup-1", phase="INGEST", symbol="BTC-USDT-SWAP",
        config_version="v1", config_hash="hash-1", request_fingerprint="fp-1",
    )
    run_store.acquire(run_id="run-backup-1", phase="INGEST", symbol="BTC-USDT-SWAP")
    row = run_store.get(run_id="run-backup-1")
    run_store.transition(
        run_id="run-backup-1", new_status="DONE", reason="ok",
        result={"dataset_id": "market-1"}, lease_token=row["lease_token"],
    )

    # artifact_registry: real StrategyArtifactRegistry with one PROMOTED artifact.
    registry = StrategyArtifactRegistry(research_root / "artifacts" / "registry.db")
    claim_store = HoldoutClaimStore(research_root / "holdout_claims.db")
    artifact_payload = {
        "artifact_hash": "hash-artifact-1",
        "strategy_version": "sv-backup-1",
        "symbol": "BTC-USDT-SWAP",
        "status": "PROMOTED",
        "dataset_version": "market-1",
        "feature_schema_version": "features-1",
        "promotion_policy_version": "p1",
        "n_folds": 1,
        "valid_until": "2099-01-01T00:00:00Z",
        "fold_metrics": [
            {"expectancy": 0.01, "max_drawdown": 0.01, "profit_factor": 1.5, "trade_count": 10},
        ],
        "promotion_decision": {
            "passed": True, "failures": [], "policy_version": "p1",
            "artifact_policy_version": "p1", "evaluated_at": "2026-01-01T00:00:00Z",
            "fold_count": 1,
            "fold_metrics_hash": None,  # filled below to match _digest
            "holdout_result_hash": None,  # filled below to match _digest
            "oos_trade_count": 10, "costs_included": True,
        },
        "holdout_result": {"expectancy": 0.01, "trades": 10},
    }
    from services.agent_krypto_artifact_registry import _digest
    artifact_payload["promotion_decision"]["fold_metrics_hash"] = _digest(
        artifact_payload["fold_metrics"]
    )
    artifact_payload["promotion_decision"]["holdout_result_hash"] = _digest(
        artifact_payload["holdout_result"]
    )
    claim_store.claim(
        strategy_version="sv-backup-1", config_hash="hash-artifact-1",
        holdout_result=artifact_payload["holdout_result"],
    )
    registry.activate(
        artifact_payload, claim_store=claim_store,
        expected_dataset_version="market-1", expected_feature_schema_version="features-1",
        expected_promotion_policy_version="p1", reason="seed for backup test",
    )

    # champion_registry (#141): one manually approved promotion.
    champion_registry = ChampionRegistry(research_root / "champion_registry.db")
    challenger_artifact = {
        "strategy_version": "sv-backup-1",
        "symbol": "BTC-USDT-SWAP",
        "holdout_evaluated": True,
        "holdout_result": {"expectancy": 0.01},
        "promotion_decision": {"passed": True, "failures": [], "policy_version": "p1", "evaluated_at": "2026-01-01T00:00:00Z"},
    }
    comparison = compare_to_champion(
        symbol="BTC-USDT-SWAP", challenger_artifact=challenger_artifact, champion_artifact=None
    )
    champion_registry.promote(comparison=comparison, challenger_artifact=challenger_artifact, approved_by="backup-test")

    # insight_reports (#142): one persisted trial insight report.
    insight_store = InsightReportStore(research_root / "insight_reports.db")
    trial_result = {
        "trial_id": "trial-backup-1",
        "accepted": True,
        "reason": None,
        "lineage": {"dataset_version": "market-1", "strategy_version": "sv-backup-1", "seed": 1, "holdout": {"accessed": False}},
        "params": {"lookback": 3, "threshold": 0.001, "signal_feature": "bb_percent_b"},
        "fold_metrics": [{"fold": 0, "expectancy": 0.01, "max_drawdown": 0.01, "profit_factor": 1.2, "trade_count": 5}],
        "symbol_metrics": {"BTC-USDT-SWAP": {"expectancy": 0.01}},
        "bootstrap_ci": {"lower": 0.001, "upper": 0.02, "confidence": 0.95},
    }
    insight_store.save(build_trial_insight_report(trial_result))

    return research_root


def test_backup_restore_round_trip_preserves_registry_and_run_store(tmp_path):
    source_root = tmp_path / "source"
    _seeded_workspace(source_root)

    backup_dir = tmp_path / "backup1"
    manifest = create_backup(source_root=source_root, backup_dir=backup_dir)
    assert {e.component for e in manifest.entries} == {
        "datasets", "experiments", "mlflow", "chroma",
        "run_store", "artifact_registry", "holdout_claims",
        "champion_registry", "insight_reports",
    }
    # mlflow/chroma were never created in this seed workspace — a component
    # genuinely absent from the source must be recorded as "missing", not
    # silently dropped from the manifest or treated as an error.
    kinds = {e.component: e.kind for e in manifest.entries}
    assert kinds["mlflow"] == "missing"
    assert kinds["chroma"] == "missing"
    assert kinds["run_store"] == "file"
    assert kinds["artifact_registry"] == "file"
    assert kinds["champion_registry"] == "file"
    assert kinds["insight_reports"] == "file"

    assert verify_backup_integrity(backup_dir)["ok"] is True

    target_root = tmp_path / "restored"
    restore_backup(backup_dir=backup_dir, target_root=target_root)

    restored_research_root = target_root / "research" / "agent-krypto"
    assert (restored_research_root / "datasets" / "features-1" / "features.parquet").read_bytes() == b"parquet-bytes"
    assert (restored_research_root / "experiments" / "trials" / "trial-1.json").is_file()

    # Idempotency (run_store): the restored DB has the exact same DONE run row.
    restored_run_store = RunStore(db_path=restored_research_root / "runs" / "orchestrator_runs.db")
    restored_row = restored_run_store.get(run_id="run-backup-1")
    assert restored_row["status"] == "DONE"
    assert restored_row["result_json"] == '{"dataset_id": "market-1"}'

    # Registry (lineage): the restored registry still resolves the same
    # active PROMOTED artifact for the symbol with unchanged compatibility.
    restored_conn = sqlite3.connect(restored_research_root / "artifacts" / "registry.db")
    try:
        persisted = restored_conn.execute(
            "SELECT strategy_version, status FROM strategy_artifacts WHERE strategy_version='sv-backup-1'"
        ).fetchone()
    finally:
        restored_conn.close()
    assert persisted == ("sv-backup-1", "PROMOTED")

    restored_registry = StrategyArtifactRegistry(restored_research_root / "artifacts" / "registry.db")
    active = restored_registry.get_active(
        symbol="BTC-USDT-SWAP", expected_dataset_version="market-1",
        expected_feature_schema_version="features-1", expected_promotion_policy_version="p1",
    )
    assert active["strategy_version"] == "sv-backup-1"

    # champion_registry (#141): restored DB still resolves the same champion
    # and its full promotion history.
    restored_champion_registry = ChampionRegistry(restored_research_root / "champion_registry.db")
    restored_champion = restored_champion_registry.current_champion(symbol="BTC-USDT-SWAP")
    assert restored_champion["strategy_version"] == "sv-backup-1"
    restored_history = restored_champion_registry.history(symbol="BTC-USDT-SWAP")
    assert [event["event_type"] for event in restored_history] == ["PROMOTE"]
    assert restored_history[0]["approved_by"] == "backup-test"

    # insight_reports (#142): restored DB still has the persisted report.
    restored_insight_store = InsightReportStore(restored_research_root / "insight_reports.db")
    restored_report = restored_insight_store.get(run_id="trial-backup-1")
    assert restored_report["run_id"] == "trial-backup-1"
    assert restored_report["source"] == "trial"


def test_restore_refuses_to_overwrite_existing_target_without_flag(tmp_path):
    source_root = tmp_path / "source"
    _seeded_workspace(source_root)
    backup_dir = tmp_path / "backup1"
    create_backup(source_root=source_root, backup_dir=backup_dir)

    target_root = tmp_path / "restored"
    restore_backup(backup_dir=backup_dir, target_root=target_root)

    with pytest.raises(BackupError, match="already exists"):
        restore_backup(backup_dir=backup_dir, target_root=target_root)

    # With overwrite=True the same restore must succeed and reproduce the
    # identical manifest digests (a second restore is a legal idempotent op).
    manifest = restore_backup(backup_dir=backup_dir, target_root=target_root, overwrite=True)
    assert manifest.entries


def test_backup_refuses_existing_backup_dir(tmp_path):
    source_root = tmp_path / "source"
    _seeded_workspace(source_root)
    backup_dir = tmp_path / "backup1"
    create_backup(source_root=source_root, backup_dir=backup_dir)

    with pytest.raises(BackupError, match="already exists"):
        create_backup(source_root=source_root, backup_dir=backup_dir)


def test_verify_backup_integrity_detects_corruption_at_rest(tmp_path):
    source_root = tmp_path / "source"
    _seeded_workspace(source_root)
    backup_dir = tmp_path / "backup1"
    create_backup(source_root=source_root, backup_dir=backup_dir)

    # Corrupt a backed-up file directly (simulating bit rot / manual tampering)
    corrupted = backup_dir / "research" / "agent-krypto" / "datasets" / "features-1" / "features.parquet"
    corrupted.write_bytes(b"corrupted-bytes")

    result = verify_backup_integrity(backup_dir)
    assert result["ok"] is False
    assert "research/agent-krypto/datasets" in result["mismatched_components"]


def test_restore_verifies_and_rejects_a_manifest_content_mismatch(tmp_path):
    source_root = tmp_path / "source"
    _seeded_workspace(source_root)
    backup_dir = tmp_path / "backup1"
    create_backup(source_root=source_root, backup_dir=backup_dir)

    # Tamper with the backup's payload after the manifest was written; a
    # restore must not silently succeed with content that no longer matches
    # the recorded digest — that would be a silent lineage break.
    tampered = backup_dir / "research" / "agent-krypto" / "datasets" / "features-1" / "features.parquet"
    tampered.write_bytes(b"tampered")

    target_root = tmp_path / "restored"
    with pytest.raises(BackupError, match="verification failed"):
        restore_backup(backup_dir=backup_dir, target_root=target_root)


def test_read_manifest_missing_file_is_fail_closed(tmp_path):
    empty_dir = tmp_path / "not-a-backup"
    empty_dir.mkdir()
    with pytest.raises(BackupError, match="no manifest.json"):
        read_manifest(empty_dir)
