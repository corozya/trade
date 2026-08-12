#!/usr/bin/env python3
"""Read-only, atomic refresh of Bitget futures OHLCV feather files (#128/#129/#130).

Downloads 15m OHLCV for a fixed set of 5 pairs via ``freqtrade download-data
--exchange bitget --trading-mode futures`` into a temp datadir, validates the
result, and atomically replaces the corresponding file in
``data/bitget/futures/`` only when the new data passes validation. This
script is a pure I/O process on market-data files: it never imports or calls
``agent_krypto_orchestrator.py``, ``agent_krypto_cli.py``, or any
execution/promotion module, and it never touches ``config/config.json`` or
the ``research-loop`` cron.

See docs/agent-krypto-bitget-refresh-design.md for the full design this
implements.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

import pyarrow as pa
import pyarrow.feather as feather

ROOT = Path(__file__).resolve().parents[1]

DEFAULT_PAIRS: tuple[str, ...] = (
    "BTC/USDT:USDT",
    "ETH/USDT:USDT",
    "DOGE/USDT:USDT",
    "SOL/USDT:USDT",
    "XRP/USDT:USDT",
)
DEFAULT_TIMEFRAME = "15m"
DEFAULT_DATADIR = ROOT / "data" / "bitget"
DEFAULT_RUNTIME_ROOT = Path(
    os.environ.get("CRYPTO_RUNTIME_ROOT", ROOT / "data" / "runtime")
).expanduser()
DEFAULT_LOG_PATH = DEFAULT_RUNTIME_ROOT / "logs" / "bitget_refresh_versions.jsonl"

DOWNLOAD_TIMEOUT_SECONDS = 60
RETRY_ATTEMPTS = 2
RETRY_DELAY_SECONDS = 5
LOCK_STALE_AFTER_SECONDS = 300
COLD_START_DAYS = 30
OVERLAP = timedelta(hours=2)
INTERVAL = timedelta(minutes=15)

MANIFEST_SCHEMA_VERSION = 1
SOURCE_NAME = "bitget-rest-freqtrade-download-data"


class RefreshError(RuntimeError):
    """Base error for the refresh process; every failure here is fail-closed."""


class NetworkError(RefreshError):
    """A transient/network-ish failure from the download subprocess -> retryable."""


class ValidationError(RefreshError):
    """A data-integrity failure (dup/conflict/regression) -> never retried."""


def pair_to_symbol_key(pair: str) -> str:
    """``BTC/USDT:USDT`` -> ``BTC-USDT-SWAP`` (manifest/lineage key)."""
    base = pair.split("/")[0]
    return f"{base}-USDT-SWAP"


def pair_to_filename(pair: str, timeframe: str) -> str:
    """``BTC/USDT:USDT`` -> ``BTC_USDT_USDT-15m-futures.feather``."""
    base = pair.split("/")[0]
    return f"{base}_USDT_USDT-{timeframe}-futures.feather"


@dataclass
class SymbolResult:
    pair: str
    symbol_key: str
    status: str  # "ok" | "error"
    reason: str | None = None
    warnings: list[str] = field(default_factory=list)
    dataset_version: str | None = None
    sha256: str | None = None
    row_count: int | None = None
    last_candle_ts: str | None = None
    previous_dataset_version: str | None = None


class FileLock:
    """Flock-based lock with self-reclaim after ``stale_after`` seconds.

    Mirrors the RunStore lease pattern (agent_krypto_run_store.py): a lock
    file older than ``stale_after`` is treated as abandoned and reclaimed
    with a logged WARN, rather than blocking forever on a crashed process.
    """

    def __init__(self, path: Path, *, stale_after: int = LOCK_STALE_AFTER_SECONDS):
        self.path = path
        self.stale_after = stale_after
        self._fh: Any = None
        self.reclaimed = False

    def __enter__(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Staleness is judged from the lock file's mtime *before* we touch
        # it: a live holder keeps refreshing mtime (see below), so an old
        # mtime here means either a crashed holder (flock already released
        # by the OS -> acquired immediately below) or a wedged one still
        # holding the OS lock (flock blocks -> BlockingIOError below). Both
        # cases are "stale" and get reclaimed with a logged WARN.
        try:
            pre_age = time.time() - self.path.stat().st_mtime
        except OSError:
            pre_age = None
        stale_before_lock = pre_age is not None and pre_age > self.stale_after

        self._fh = open(self.path, "a+")
        try:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if stale_before_lock:
                self.reclaimed = True
                sys.stderr.write(
                    f"WARN: stale lock reclaimed (age={pre_age:.0f}s > "
                    f"{self.stale_after}s): {self.path}\n"
                )
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
            else:
                self._fh.close()
                raise RefreshError(
                    f"refresh lock held by another process: {self.path}"
                )
        else:
            if stale_before_lock:
                self.reclaimed = True
                sys.stderr.write(
                    f"WARN: stale lock reclaimed (age={pre_age:.0f}s > "
                    f"{self.stale_after}s): {self.path}\n"
                )
        self._fh.seek(0)
        self._fh.truncate()
        self._fh.write(f"{os.getpid()} {datetime.now(timezone.utc).isoformat()}\n")
        self._fh.flush()
        os.utime(self.path, None)
        return self

    def __exit__(self, *exc_info: Any) -> None:
        if self._fh is not None:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            self._fh.close()
            self._fh = None


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_manifest(manifest_path: Path) -> dict[str, Any]:
    if not manifest_path.is_file():
        return {"schema_version": MANIFEST_SCHEMA_VERSION, "entries": {}}
    return json.loads(manifest_path.read_text())


def _write_manifest_atomic(manifest_path: Path, manifest: dict[str, Any]) -> None:
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=".manifest-", suffix=".json.tmp", dir=manifest_path.parent
    )
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(manifest, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp_name, manifest_path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _append_lineage(log_path: Path, entry: dict[str, Any]) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a") as fh:
        fh.write(json.dumps(entry, sort_keys=True) + "\n")


def _table_to_rows(table: pa.Table) -> list[dict[str, Any]]:
    required = {"date", "open", "high", "low", "close", "volume"}
    missing = required - set(table.column_names)
    if missing:
        raise ValidationError(f"downloaded table missing columns: {sorted(missing)}")
    return table.select(sorted(required)).to_pylist()


def _ts_iso(value: Any) -> str:
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def merge_and_validate(
    *,
    existing_rows: list[dict[str, Any]],
    incoming_rows: list[dict[str, Any]],
    previous_last_candle_ts: str | None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Merge existing+incoming rows keyed by timestamp.

    Returns (merged_rows_sorted, warnings). Raises ValidationError on a
    genuine OHLCV conflict at the same timestamp, or on freshness regression.
    """
    warnings: list[str] = []
    by_ts: dict[str, dict[str, Any]] = {}
    order: list[str] = []

    def _key(row: dict[str, Any]) -> tuple[Any, Any, Any, Any, Any]:
        return (row["open"], row["high"], row["low"], row["close"], row["volume"])

    if not incoming_rows:
        raise ValidationError("downloaded package is empty")
    incoming_last_ts = max(_ts_iso(row["date"]) for row in incoming_rows)
    if previous_last_candle_ts is not None and incoming_last_ts < previous_last_candle_ts:
        raise ValidationError(
            f"freshness regression: downloaded last_candle_ts {incoming_last_ts} "
            f"< previous {previous_last_candle_ts}"
        )

    for row in existing_rows:
        ts = _ts_iso(row["date"])
        by_ts[ts] = dict(row)
        order.append(ts)

    for row in incoming_rows:
        ts = _ts_iso(row["date"])
        if ts in by_ts:
            if _key(by_ts[ts]) != _key(row):
                raise ValidationError(
                    "conflicting OHLCV at same timestamp "
                    f"{ts}: existing={_key(by_ts[ts])} incoming={_key(row)}"
                )
            # exact duplicate: keep existing (dedup, last value wins == same value)
            continue
        by_ts[ts] = dict(row)
        order.append(ts)

    sorted_ts = sorted(by_ts)
    if not sorted_ts:
        raise ValidationError("no rows after merge")

    # completeness: gaps in the 15m sequence -> WARN, not ERROR
    prev_dt: datetime | None = None
    for ts in sorted_ts:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if prev_dt is not None:
            delta = dt - prev_dt
            if delta != INTERVAL:
                warnings.append(f"gap in sequence: {prev_dt.isoformat()} -> {dt.isoformat()} ({delta})")
        prev_dt = dt

    merged_rows = [by_ts[ts] for ts in sorted_ts]
    return merged_rows, warnings


def _rows_to_table(rows: list[dict[str, Any]]) -> pa.Table:
    dates = [
        r["date"] if isinstance(r["date"], datetime) else
        datetime.fromisoformat(_ts_iso(r["date"]).replace("Z", "+00:00"))
        for r in rows
    ]
    return pa.table(
        {
            "date": pa.array(dates, type=pa.timestamp("ns", tz="UTC")),
            "open": pa.array([r["open"] for r in rows], type=pa.float64()),
            "high": pa.array([r["high"] for r in rows], type=pa.float64()),
            "low": pa.array([r["low"] for r in rows], type=pa.float64()),
            "close": pa.array([r["close"] for r in rows], type=pa.float64()),
            "volume": pa.array([r["volume"] for r in rows], type=pa.float64()),
        }
    )


def download_pair(
    *,
    pair: str,
    timeframe: str,
    tmp_datadir: Path,
    timerange: str,
    timeout: int = DOWNLOAD_TIMEOUT_SECONDS,
    retry_attempts: int = RETRY_ATTEMPTS,
    retry_delay: int = RETRY_DELAY_SECONDS,
    runner: Any = subprocess.run,
) -> None:
    """Invoke ``freqtrade download-data`` for one pair, retrying network errors.

    Always passes an explicit ``--datadir`` (never relies on freqtrade's
    default ``user_data/data/...``) and an explicit ``--user-data-dir`` so
    the call never depends on the caller's current working directory
    (freqtrade otherwise looks for ``<cwd>/user_data`` and errors out under
    cron, whose cwd is not the repo root).
    """
    cmd = [
        "freqtrade",
        "download-data",
        "--exchange",
        "bitget",
        "--trading-mode",
        "futures",
        "--datadir",
        str(tmp_datadir),
        "--user-data-dir",
        str(ROOT / "user_data"),
        "-p",
        pair,
        "--timeframes",
        timeframe,
        "--timerange",
        timerange,
    ]
    last_error: Exception | None = None
    for attempt in range(1, retry_attempts + 1):
        try:
            result = runner(cmd, timeout=timeout, capture_output=True, text=True)
        except subprocess.TimeoutExpired as exc:
            last_error = NetworkError(f"download-data timed out for {pair}: {exc}")
        except OSError as exc:
            last_error = NetworkError(f"download-data failed to start for {pair}: {exc}")
        else:
            if result.returncode == 0:
                return
            last_error = NetworkError(
                f"download-data exited {result.returncode} for {pair}: {result.stderr}"
            )
        if attempt < retry_attempts:
            time.sleep(retry_delay)
    assert last_error is not None
    raise last_error


def refresh_symbol(
    *,
    pair: str,
    timeframe: str,
    datadir: Path,
    manifest: dict[str, Any],
    now: datetime,
    runner: Any = subprocess.run,
    timeout: int = DOWNLOAD_TIMEOUT_SECONDS,
    retry_attempts: int = RETRY_ATTEMPTS,
    retry_delay: int = RETRY_DELAY_SECONDS,
) -> SymbolResult:
    symbol_key = pair_to_symbol_key(pair)
    filename = pair_to_filename(pair, timeframe)
    futures_dir = datadir / "futures"
    target_path = futures_dir / filename
    entry_key = f"{symbol_key}/{timeframe}"
    previous_entry = manifest.get("entries", {}).get(entry_key)
    previous_last_ts = previous_entry["last_candle_ts"] if previous_entry else None

    existing_rows: list[dict[str, Any]] = []
    if target_path.is_file():
        existing_rows = _table_to_rows(feather.read_table(target_path))
        if previous_last_ts is None and existing_rows:
            # No manifest entry yet for a file that already exists on disk
            # (e.g. pre-existing dataset before this mechanism's first run).
            # Freshness must still be judged against what is actually on
            # disk, not silently skipped just because the manifest is empty.
            previous_last_ts = max(_ts_iso(r["date"]) for r in existing_rows)

    if existing_rows:
        last_existing = max(_ts_iso(r["date"]) for r in existing_rows)
        since = datetime.fromisoformat(last_existing.replace("Z", "+00:00")) - OVERLAP
    else:
        since = now - timedelta(days=COLD_START_DAYS)
    # freqtrade's --timerange only supports 8-digit (day), 10-digit (unix
    # seconds) or 13-digit (unix ms) endpoints -- a day-only range collapses
    # to zero width whenever `since` and `now` fall on the same calendar day
    # (the common case when refreshing every 10 minutes), silently yielding
    # an empty download. Unix seconds give sub-day precision on both ends.
    since_ts = int(since.timestamp())
    now_ts = int(now.timestamp()) + 1
    timerange = f"{since_ts}-{now_ts}"

    tmp_root = Path(tempfile.mkdtemp(prefix=f".refresh-{symbol_key}-", dir=datadir.parent))
    try:
        try:
            download_pair(
                pair=pair,
                timeframe=timeframe,
                tmp_datadir=tmp_root,
                timerange=timerange,
                timeout=timeout,
                retry_attempts=retry_attempts,
                retry_delay=retry_delay,
                runner=runner,
            )
        except NetworkError as exc:
            return SymbolResult(
                pair=pair, symbol_key=symbol_key, status="error", reason=str(exc)
            )

        downloaded_path = tmp_root / "futures" / filename
        if not downloaded_path.is_file():
            return SymbolResult(
                pair=pair,
                symbol_key=symbol_key,
                status="error",
                reason=f"download-data did not produce expected file: {downloaded_path}",
            )

        try:
            incoming_rows = _table_to_rows(feather.read_table(downloaded_path))
            merged_rows, warnings = merge_and_validate(
                existing_rows=existing_rows,
                incoming_rows=incoming_rows,
                previous_last_candle_ts=previous_last_ts,
            )
        except ValidationError as exc:
            return SymbolResult(
                pair=pair, symbol_key=symbol_key, status="error", reason=str(exc)
            )

        merged_table = _rows_to_table(merged_rows)
        last_candle_ts = _ts_iso(merged_rows[-1]["date"])

        futures_dir.mkdir(parents=True, exist_ok=True)
        tmp_target = futures_dir / f"{filename}.tmp-{uuid.uuid4().hex}"
        feather.write_feather(merged_table, tmp_target)
        content_sha256 = _sha256_bytes(tmp_target.read_bytes())
        dataset_version = f"{pair.split('/')[0].lower()}-{timeframe}-{content_sha256[:16]}"

        previous_dataset_version = previous_entry["dataset_version"] if previous_entry else None
        if target_path.is_file():
            prev_path = futures_dir / f"{filename}.prev"
            shutil.copy2(target_path, prev_path)
        os.replace(tmp_target, target_path)

        return SymbolResult(
            pair=pair,
            symbol_key=symbol_key,
            status="ok",
            warnings=warnings,
            dataset_version=dataset_version,
            sha256=content_sha256,
            row_count=len(merged_rows),
            last_candle_ts=last_candle_ts,
            previous_dataset_version=previous_dataset_version,
        )
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


def run_refresh(
    *,
    pairs: Sequence[str],
    timeframe: str,
    datadir: Path,
    log_path: Path,
    now: datetime | None = None,
    runner: Any = subprocess.run,
    lock_stale_after: int = LOCK_STALE_AFTER_SECONDS,
    timeout: int = DOWNLOAD_TIMEOUT_SECONDS,
    retry_attempts: int = RETRY_ATTEMPTS,
    retry_delay: int = RETRY_DELAY_SECONDS,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    futures_dir = datadir / "futures"
    manifest_path = futures_dir / "_manifest.json"
    lock_path = futures_dir / ".refresh.lock"

    results: list[SymbolResult] = []
    with FileLock(lock_path, stale_after=lock_stale_after):
        manifest = _read_manifest(manifest_path)
        manifest.setdefault("schema_version", MANIFEST_SCHEMA_VERSION)
        manifest.setdefault("entries", {})

        for pair in pairs:
            result = refresh_symbol(
                pair=pair,
                timeframe=timeframe,
                datadir=datadir,
                manifest=manifest,
                now=now,
                runner=runner,
                timeout=timeout,
                retry_attempts=retry_attempts,
                retry_delay=retry_delay,
            )
            results.append(result)

            if result.status == "ok":
                entry_key = f"{result.symbol_key}/{timeframe}"
                manifest["entries"][entry_key] = {
                    "sha256": result.sha256,
                    "row_count": result.row_count,
                    "last_candle_ts": result.last_candle_ts,
                    "refreshed_at": _ts_iso(now),
                    "dataset_version": result.dataset_version,
                    "status": "ok",
                    "source": SOURCE_NAME,
                }
                _write_manifest_atomic(manifest_path, manifest)
                _append_lineage(
                    log_path,
                    {
                        "symbol": result.symbol_key,
                        "timeframe": timeframe,
                        "pair": pair,
                        "dataset_version": result.dataset_version,
                        "previous_dataset_version": result.previous_dataset_version,
                        "sha256": result.sha256,
                        "row_count": result.row_count,
                        "last_candle_ts": result.last_candle_ts,
                        "refreshed_at": _ts_iso(now),
                        "status": "ok",
                        "warnings": result.warnings,
                        "source": SOURCE_NAME,
                    },
                )
            else:
                _append_lineage(
                    log_path,
                    {
                        "symbol": result.symbol_key,
                        "timeframe": timeframe,
                        "pair": pair,
                        "refreshed_at": _ts_iso(now),
                        "status": "error",
                        "reason": result.reason,
                        "source": SOURCE_NAME,
                    },
                )

    ok_count = sum(1 for r in results if r.status == "ok")
    error_count = len(results) - ok_count
    if error_count == 0:
        run_status = "OK"
    elif ok_count == 0:
        run_status = "ERROR"
    else:
        run_status = "PARTIAL"

    return {
        "status": run_status,
        "results": [
            {
                "pair": r.pair,
                "symbol": r.symbol_key,
                "status": r.status,
                "reason": r.reason,
                "warnings": r.warnings,
                "dataset_version": r.dataset_version,
            }
            for r in results
        ],
    }


def _parse_pairs(value: str) -> list[str]:
    return [p.strip() for p in value.split(",") if p.strip()]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pairs", type=_parse_pairs, default=list(DEFAULT_PAIRS))
    parser.add_argument("--timeframe", default=DEFAULT_TIMEFRAME)
    parser.add_argument("--datadir", type=Path, default=DEFAULT_DATADIR)
    parser.add_argument("--log-path", type=Path, default=DEFAULT_LOG_PATH)
    args = parser.parse_args(argv)

    outcome = run_refresh(
        pairs=args.pairs,
        timeframe=args.timeframe,
        datadir=args.datadir,
        log_path=args.log_path,
    )
    print(json.dumps(outcome, indent=2, sort_keys=True))
    return 0 if outcome["status"] != "ERROR" else 1


if __name__ == "__main__":
    raise SystemExit(main())
