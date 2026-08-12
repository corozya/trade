"""Local backup/restore for the agent-krypto research workspace (#105).

Scope is deliberately the *local, filesystem-owned* state that the
orchestrator itself writes and that must survive a disk copy/restore with its
lineage, registry and idempotency intact:

- the CryptoDataLake `datasets/` tree (versioned Parquet market/feature data)
- the MLflow SQLite tracking store (`mlruns.db` or any `sqlite:///...` URI)
- the Chroma RAG persistent directory (SQLite + index files)
- the orchestrator run store (`orchestrator_runs.db`)
- the StrategyArtifact registry (`registry.db`)
- the holdout-claim store (`holdout_claims.db`)
- the champion/challenger registry (`champion_registry.db`, #141)
- the insight-report store (`insight_reports.db`, #142)

Nothing here talks to OKX or any network service. A backup is a plain
directory/file copy (tar-free, so it stays diffable and greppable); `restore`
is the inverse copy back into a target root. Both operations are fail-closed:
a missing required source, a destination that already has divergent content
without `overwrite=True`, or a post-restore verification mismatch raises
rather than silently producing a partial/corrupt workspace.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Relative-to-data-root paths this module knows how to back up/restore.
# `required=False` entries are skipped (not an error) when they do not yet
# exist in the source workspace, e.g. a fresh install with no RAG notes yet.
_COMPONENTS: tuple[tuple[str, str, bool], ...] = (
    # (component_name, relative_path, required)
    ("datasets", "data/lake/datasets", False),
    ("experiments", "data/runtime/experiments", False),
    ("mlflow", "data/runtime/mlruns.db", False),
    ("chroma", "data/runtime/rag", False),
    ("run_store", "data/runtime/runs/orchestrator_runs.db", False),
    ("artifact_registry", "data/runtime/artifacts/registry.db", False),
    ("holdout_claims", "data/runtime/holdout_claims.db", False),
    ("champion_registry", "data/runtime/champion_registry.db", False),
    ("insight_reports", "data/runtime/insight_reports.db", False),
)


class BackupError(ValueError):
    """Fail-closed: an inconsistent or partial backup/restore was refused."""


@dataclass(frozen=True)
class ComponentManifestEntry:
    component: str
    relative_path: str
    kind: str  # "dir" | "file" | "missing"
    sha256: str | None  # directory digest (files+contents) or file digest
    file_count: int


@dataclass(frozen=True)
class BackupManifest:
    created_at: str
    source_root: str
    entries: list[ComponentManifestEntry] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "created_at": self.created_at,
            "source_root": self.source_root,
            "entries": [
                {
                    "component": e.component,
                    "relative_path": e.relative_path,
                    "kind": e.kind,
                    "sha256": e.sha256,
                    "file_count": e.file_count,
                }
                for e in self.entries
            ],
        }


def _sqlite_checkpoint(path: Path) -> None:
    """Fold any pending WAL frames into the main DB file before copying it, so
    a plain file copy of a WAL-mode SQLite DB is not missing recent commits
    that are still only in the `-wal` sidecar."""
    if not path.exists():
        return
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _dir_sha256(path: Path) -> tuple[str, int]:
    """Deterministic digest over every regular file's relative path + content,
    independent of filesystem iteration order."""
    digest = hashlib.sha256()
    files = sorted(p for p in path.rglob("*") if p.is_file())
    for file_path in files:
        rel = file_path.relative_to(path).as_posix()
        digest.update(rel.encode())
        digest.update(_file_sha256(file_path).encode())
    return digest.hexdigest(), len(files)


def create_backup(
    *,
    source_root: str | Path,
    backup_dir: str | Path,
    now: datetime | None = None,
) -> BackupManifest:
    """Copy every known component under ``source_root`` into ``backup_dir``.

    ``backup_dir`` must not already exist (fail-closed against silently
    merging into/overwriting an unrelated directory); callers pick a fresh,
    timestamped destination.
    """
    source_root = Path(source_root)
    backup_dir = Path(backup_dir)
    if backup_dir.exists():
        raise BackupError(f"backup_dir already exists: {backup_dir}")

    entries: list[ComponentManifestEntry] = []
    backup_dir.mkdir(parents=True)
    now = now or datetime.now(timezone.utc)

    for component, relative, required in _COMPONENTS:
        source_path = source_root / relative
        destination_path = backup_dir / relative
        if source_path.is_dir():
            _checkpoint_sqlite_siblings(source_path)
            destination_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(source_path, destination_path)
            digest, count = _dir_sha256(destination_path)
            entries.append(ComponentManifestEntry(component, relative, "dir", digest, count))
        elif source_path.is_file():
            _sqlite_checkpoint(source_path)
            destination_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, destination_path)
            entries.append(
                ComponentManifestEntry(component, relative, "file", _file_sha256(destination_path), 1)
            )
        else:
            if required:
                raise BackupError(f"required component missing at source: {source_path}")
            entries.append(ComponentManifestEntry(component, relative, "missing", None, 0))

    manifest = BackupManifest(
        created_at=now.isoformat(), source_root=str(source_root), entries=entries
    )
    (backup_dir / "manifest.json").write_text(
        json.dumps(manifest.to_dict(), indent=2, sort_keys=True) + "\n"
    )
    return manifest


def _checkpoint_sqlite_siblings(directory: Path) -> None:
    for db_path in directory.rglob("*.db"):
        _sqlite_checkpoint(db_path)


def read_manifest(backup_dir: str | Path) -> BackupManifest:
    backup_dir = Path(backup_dir)
    manifest_path = backup_dir / "manifest.json"
    if not manifest_path.is_file():
        raise BackupError(f"no manifest.json in backup_dir: {backup_dir}")
    payload = json.loads(manifest_path.read_text())
    entries = [
        ComponentManifestEntry(
            e["component"], e["relative_path"], e["kind"], e.get("sha256"), e.get("file_count", 0)
        )
        for e in payload["entries"]
    ]
    return BackupManifest(payload["created_at"], payload["source_root"], entries)


def restore_backup(
    *,
    backup_dir: str | Path,
    target_root: str | Path,
    overwrite: bool = False,
) -> BackupManifest:
    """Restore every component from ``backup_dir`` into ``target_root``.

    Fail-closed: refuses to touch a target component that already exists
    unless ``overwrite=True`` — a restore must never silently merge into (and
    therefore corrupt the idempotency/lineage of) an already-populated
    workspace. After copying, every component's digest is recomputed from the
    *restored* target and compared against the backup manifest; any mismatch
    raises ``BackupError`` rather than reporting a silently incomplete
    restore.
    """
    backup_dir = Path(backup_dir)
    target_root = Path(target_root)
    manifest = read_manifest(backup_dir)

    for entry in manifest.entries:
        source_path = backup_dir / entry.relative_path
        destination_path = target_root / entry.relative_path
        if entry.kind == "missing":
            continue
        if destination_path.exists():
            if not overwrite:
                raise BackupError(
                    f"restore target already exists (pass overwrite=True to replace): "
                    f"{destination_path}"
                )
            if destination_path.is_dir():
                shutil.rmtree(destination_path)
            else:
                destination_path.unlink()
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        if entry.kind == "dir":
            shutil.copytree(source_path, destination_path)
        else:
            shutil.copy2(source_path, destination_path)

    _verify_restore(manifest, target_root)
    return manifest


def _verify_restore(manifest: BackupManifest, target_root: Path) -> None:
    mismatches: list[str] = []
    for entry in manifest.entries:
        if entry.kind == "missing":
            continue
        destination_path = target_root / entry.relative_path
        if entry.kind == "dir":
            digest, count = _dir_sha256(destination_path)
            if digest != entry.sha256 or count != entry.file_count:
                mismatches.append(entry.relative_path)
        else:
            if _file_sha256(destination_path) != entry.sha256:
                mismatches.append(entry.relative_path)
    if mismatches:
        raise BackupError(
            "restore verification failed for components: " + ", ".join(sorted(mismatches))
        )


def verify_backup_integrity(backup_dir: str | Path) -> dict[str, Any]:
    """Recompute digests of the backup contents themselves (not a restore
    target) and compare against the manifest — detects a backup that was
    corrupted/truncated at rest, independent of any restore."""
    backup_dir = Path(backup_dir)
    manifest = read_manifest(backup_dir)
    mismatches: list[str] = []
    for entry in manifest.entries:
        if entry.kind == "missing":
            continue
        path = backup_dir / entry.relative_path
        if entry.kind == "dir":
            digest, count = _dir_sha256(path)
            ok = digest == entry.sha256 and count == entry.file_count
        else:
            ok = _file_sha256(path) == entry.sha256
        if not ok:
            mismatches.append(entry.relative_path)
    return {"ok": not mismatches, "mismatched_components": mismatches}
