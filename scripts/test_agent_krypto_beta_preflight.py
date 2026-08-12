from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from scripts import agent_krypto_beta_preflight as preflight


def test_manifest_is_safe_and_current() -> None:
    manifest = preflight.load_manifest(preflight.DEFAULT_MANIFEST)
    result = preflight.validate_allowlist(manifest)
    assert result["unexpected"] == []
    assert len(result["manifest_sha256"]) == 64


def test_manifest_rejects_unexpected_scoped_file(monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = preflight.load_manifest(preflight.DEFAULT_MANIFEST)
    monkeypatch.setattr(
        preflight,
        "scoped_dirty_paths",
        lambda _manifest: ["backend/services/agent_krypto_surprise.py"],
    )
    with pytest.raises(preflight.PreflightError, match="unexpected scoped dirty"):
        preflight.validate_allowlist(manifest)


def test_manifest_rejects_database(tmp_path: Path) -> None:
    forbidden = tmp_path / "state.db"
    forbidden.write_bytes(b"")
    payload = {
        "files": [str(forbidden.relative_to(tmp_path))],
        "forbidden_suffixes": [".db"],
    }
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(payload))
    old_root = preflight.ROOT
    try:
        preflight.ROOT = tmp_path
        with pytest.raises(preflight.PreflightError, match="forbidden artifact"):
            preflight.load_manifest(manifest)
    finally:
        preflight.ROOT = old_root


def test_compatible_promoted_is_no_go_signal(tmp_path: Path) -> None:
    db = tmp_path / "registry.db"
    payload = {
        "dataset_version": "d1",
        "feature_schema_version": "f1",
        "promotion_policy_version": "p1",
    }
    with sqlite3.connect(db) as conn:
        conn.executescript(
            """
            CREATE TABLE strategy_artifacts (
              artifact_hash TEXT, status TEXT, payload_json TEXT
            );
            CREATE TABLE active_strategy_artifacts (
              symbol TEXT, artifact_hash TEXT
            );
            """
        )
        conn.execute(
            "INSERT INTO strategy_artifacts VALUES ('h','PROMOTED',?)",
            (json.dumps(payload),),
        )
        conn.execute("INSERT INTO active_strategy_artifacts VALUES ('BTC','h')")
    assert preflight.active_compatible_promoted(db, payload) == 1
