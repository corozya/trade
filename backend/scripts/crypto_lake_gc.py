#!/usr/bin/env python3
"""One-off cleanup for CryptoDataLake's ``raw/versions/`` (#170 follow-up).

Before #170, every ``--incremental`` backfill republished a version's *full*
history as a new, independent copy — 693 versions accumulated to 8.9GB with
no retention. #170 changed ``CryptoDataLake.publish()`` to inherit unchanged
Parquet parts by reference instead of copying them, which stops further
growth for *new* versions — but it does not touch the 693 pre-existing
full-copy versions already on disk. This script is the one-time cleanup for
those.

Safety model: a version is only a deletion candidate if nothing else still
needs it. Three things can need it:

1. ``raw/latest.json`` points at it directly (it is the current version for
   some data_kind/symbol/timeframe key).
2. A newer version's manifest lists one of its part files by absolute path
   (post-#170 incremental publishes inherit parts from their
   ``base_dataset_id`` instead of copying — deleting an inherited-from
   version would silently corrupt every version built on top of it).
3. A ``dataset_id`` string appears anywhere in one of the operational SQLite
   databases that sit alongside the lake (paper_execution ledger,
   orchestrator run history, insight reports, mlruns) — verified in
   production to happen: past experiment/paper-trading runs record the
   ``dataset_version`` they were evaluated against for reproducibility, and
   that reference lives outside ``raw/latest.json`` entirely. A naive GC
   that only checks (1) and (2) undercounts what's safe: on this lake, 132
   of 622 initially-unreachable-by-(1)+(2) versions turned out to be
   referenced by these DBs.

Versions satisfying none of the three are safe to delete. Everything else is
kept, no exceptions, no age-based heuristics — this is a correctness-based
GC, not a size-based one.

This script NEVER deletes anything unless invoked with --apply. The default
is a dry run that reports what *would* be removed and how much space it
would reclaim, so the operator can review before committing (per project
policy: destructive operations require explicit confirmation).

Usage:
  crypto_lake_gc.py --lake-root /path/to/lake            # dry run (default)
  crypto_lake_gc.py --lake-root /path/to/lake --apply     # actually delete
  crypto_lake_gc.py --lake-root /path/to/lake --extra-db /path/to/other.db
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
from pathlib import Path

# Operational SQLite DBs known to record dataset_id/dataset_version strings
# for past runs (paper trading, orchestrator cycles, insight reports, MLflow
# experiment tracking) — checked alongside raw/latest.json and manifest
# part-inheritance before anything is considered safe to delete. Paths are
# relative to the repo root (two levels above this script's scripts/ dir);
# missing files are skipped, not an error (e.g. a lake-only checkout).
_DEFAULT_EXTRA_DB_RELATIVE_PATHS = (
    "research/agent-krypto/insight_reports.db",
    "research/agent-krypto/paper_execution.sqlite",
    "research/agent-krypto/candidate_cursor.db",
    "research/agent-krypto/runs/orchestrator_runs.db",
    "backend/research/agent-krypto/mlruns.db",
    "backend/research/agent-krypto/runs/orchestrator_runs.db",
)


def _load_registry(lake_root: Path) -> dict[str, str]:
    path = lake_root / "raw" / "latest.json"
    if not path.is_file():
        return {}
    return json.loads(path.read_text())


def _dir_size(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def _reachable_dataset_ids(lake_root: Path, versions_dir: Path) -> set[str]:
    """Every dataset_id that must be kept: registry heads plus anything a
    kept version's manifest references by path (inherited parts)."""
    registry = _load_registry(lake_root)
    reachable: set[str] = set(registry.values())

    # Fixed point: a kept version may inherit parts from another version,
    # which must then also be kept, which may itself inherit further, etc.
    changed = True
    while changed:
        changed = False
        for dataset_id in list(reachable):
            manifest_path = versions_dir / dataset_id / "manifest.json"
            if not manifest_path.is_file():
                continue
            manifest = json.loads(manifest_path.read_text())
            for part in manifest.get("storage", {}).get("parts", []):
                if not str(part).startswith(str(versions_dir)):
                    continue
                referenced_id = Path(part).parent.name
                if referenced_id not in reachable:
                    reachable.add(referenced_id)
                    changed = True
    return reachable


def _text_columns(db_path: Path, table: str) -> list[str]:
    con = sqlite3.connect(str(db_path))
    try:
        cur = con.cursor()
        cur.execute(f"PRAGMA table_info('{table}')")
        return [row[1] for row in cur.fetchall() if "CHAR" in row[2].upper() or row[2].upper() in ("TEXT", "")]
    finally:
        con.close()


def _referenced_in_db(db_path: Path, candidate_ids: set[str]) -> set[str]:
    """Return the subset of ``candidate_ids`` that appear as a substring in
    any text column of any table in ``db_path``."""
    con = sqlite3.connect(str(db_path))
    try:
        cur = con.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [row[0] for row in cur.fetchall()]
    finally:
        con.close()

    hits: set[str] = set()
    for table in tables:
        cols = _text_columns(db_path, table)
        if not cols:
            continue
        con = sqlite3.connect(str(db_path))
        try:
            cur = con.cursor()
            col_list = ", ".join(f'"{c}"' for c in cols)
            cur.execute(f'SELECT {col_list} FROM "{table}"')
            for row in cur.fetchall():
                for value in row:
                    if not isinstance(value, str):
                        continue
                    for candidate in candidate_ids:
                        if candidate in value:
                            hits.add(candidate)
        finally:
            con.close()
    return hits


def _externally_referenced_dataset_ids(
    candidate_ids: set[str], *, extra_db_paths: list[Path]
) -> set[str]:
    referenced: set[str] = set()
    for db_path in extra_db_paths:
        if not db_path.is_file():
            continue
        referenced |= _referenced_in_db(db_path, candidate_ids - referenced)
    return referenced


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lake-root", required=True)
    parser.add_argument(
        "--repo-root",
        default=None,
        help="Root to resolve the default extra-DB paths against. Defaults to four "
        "levels above this script (scripts/ -> backend/ -> portfolio-tracker/ -> BOT/).",
    )
    parser.add_argument(
        "--extra-db",
        action="append",
        default=[],
        help="Additional SQLite DB path to check for dataset_id references. "
        "Repeatable. Adds to (does not replace) the built-in default list.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually delete unreachable versions. Without this flag, only report.",
    )
    args = parser.parse_args(argv)

    lake_root = Path(args.lake_root)
    versions_dir = lake_root / "raw" / "versions"
    if not versions_dir.is_dir():
        print(f"no versions directory at {versions_dir}, nothing to do")
        return 0

    repo_root = (
        Path(args.repo_root) if args.repo_root else Path(__file__).resolve().parents[2]
    )
    extra_db_paths = [repo_root / rel for rel in _DEFAULT_EXTRA_DB_RELATIVE_PATHS]
    extra_db_paths += [Path(p) for p in args.extra_db]

    all_ids = {p.name for p in versions_dir.iterdir() if p.is_dir()}
    reachable = _reachable_dataset_ids(lake_root, versions_dir) & all_ids
    candidates = all_ids - reachable

    externally_referenced = _externally_referenced_dataset_ids(candidates, extra_db_paths=extra_db_paths)
    safe_to_delete = sorted(candidates - externally_referenced)

    total_before = _dir_size(versions_dir)
    reclaimable = sum(_dir_size(versions_dir / did) for did in safe_to_delete)

    checked_dbs = [str(p) for p in extra_db_paths if p.is_file()]
    print(f"lake root: {lake_root}")
    print(f"extra DBs checked ({len(checked_dbs)}):")
    for p in checked_dbs:
        print(f"  {p}")
    print(
        f"versions total: {len(all_ids)}, reachable (keep, latest.json/manifest): {len(reachable)}, "
        f"externally referenced (keep, other DBs): {len(externally_referenced)}, "
        f"safe to delete: {len(safe_to_delete)}"
    )
    print(f"current size: {total_before / 1e9:.2f} GB")
    print(f"reclaimable: {reclaimable / 1e9:.2f} GB")

    if not args.apply:
        print()
        print("dry run only — nothing deleted. Re-run with --apply to delete the")
        print(f"{len(safe_to_delete)} version(s) listed above's total size.")
        if safe_to_delete:
            print("\nfirst 20 candidates:")
            for did in safe_to_delete[:20]:
                print(f"  {did}")
        return 0

    for dataset_id in safe_to_delete:
        shutil.rmtree(versions_dir / dataset_id)
    print(f"deleted {len(safe_to_delete)} version(s), reclaimed {reclaimable / 1e9:.2f} GB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
