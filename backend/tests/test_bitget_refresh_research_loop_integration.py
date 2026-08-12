"""Integration verification for #131: refresh_bitget_ohlcv.py -> ingest ->
research-loop, on a fully sandboxed tmp_path (never touches the real repo
``data/bitget/futures``, never calls a real exchange, never invokes
promotion/execution).

Covers the 5 acceptance criteria from #131 in one file, each as its own test:

1. ``test_ingest_passes_without_stale_ohlcv_after_refresh`` — after a
   mocked-subprocess ``refresh_bitget_ohlcv.run_refresh``, the resulting
   feather files pass ``crypto_market_ingestion.require_ready_dataset``
   with no ``stale ohlcv`` error.
2. ``test_research_loop_reaches_evaluate_and_ends_wait`` — the CLI
   ``research-loop`` command, driven with ``--local-data-dir`` pointed at
   the freshly-refreshed sandbox directory, reaches experiment/evaluate and
   the ``cycle`` phase resolves to WAIT with ``execution_result=None``.
3. ``test_refresh_retry_and_research_loop_are_idempotent`` — running the
   refresh twice back-to-back on the same data is a safe no-op/second write,
   and running research-loop twice for the same 15-min run_bucket returns
   the same cached ``run_id``.
4. ``test_corrupted_refresh_does_not_become_active`` — a simulated OHLCV
   conflict (same timestamp, different values) on retry must not replace
   the on-disk file: the existing ``.feather`` stays byte-identical.
5. ``test_rollback_to_previous_version_restores_prior_state`` — after a
   successful refresh (which produces ``.feather.prev`` and a manifest
   entry), the design's rollback procedure (``mv X.feather.prev X.feather``
   + restoring the manifest entry from the previous
   ``bitget_refresh_versions.jsonl`` line) brings the data back to the
   pre-refresh state.

All of this uses the real ``refresh_bitget_ohlcv.run_refresh``,
``services.crypto_market_ingestion``, and ``agent_krypto_cli.main`` code
paths -- only the ``freqtrade download-data`` subprocess call is mocked
(same technique as ``scripts/test_refresh_bitget_ohlcv.py``), so this stays
fully offline and deterministic.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.feather as feather
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import refresh_bitget_ohlcv as refresh  # noqa: E402

import agent_krypto_cli  # noqa: E402
from services.crypto_market_ingestion import (  # noqa: E402
    LocalFeatherMarketDataAdapter,
    require_ready_dataset,
)
from services.crypto_data_lake import CryptoDataLake  # noqa: E402
from services.crypto_market_ingestion import CryptoMarketIngestor  # noqa: E402


PAIRS = list(refresh.DEFAULT_PAIRS)
TF = "15m"
REQUIRED_SYMBOLS = tuple(refresh.pair_to_symbol_key(p) for p in PAIRS)


def _candles(start: datetime, count: int, *, price: float = 100.0) -> list[dict[str, Any]]:
    rows = []
    for i in range(count):
        ts = start + timedelta(minutes=15 * i)
        rows.append({
            "date": ts,
            "open": price + i, "high": price + i + 1, "low": price + i - 1,
            "close": price + i + 0.5, "volume": 10.0 + i,
        })
    return rows


def _fake_runner(behaviors: dict[str, Any]):
    def runner(cmd: list[str], **kwargs: Any) -> "subprocess.CompletedProcess[str]":
        pair = cmd[cmd.index("-p") + 1]
        behavior = behaviors[pair]
        outcome = behavior(cmd)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
    return runner


def _success_behavior(rows: list[dict[str, Any]]):
    def _behavior(cmd: list[str]) -> "subprocess.CompletedProcess[str]":
        datadir = Path(cmd[cmd.index("--datadir") + 1])
        pair = cmd[cmd.index("-p") + 1]
        filename = refresh.pair_to_filename(pair, TF)
        table = refresh._rows_to_table(rows)
        target = datadir / "futures" / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        feather.write_feather(table, target)
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")
    return _behavior


def _refresh_all(datadir: Path, log_path: Path, now: datetime, *, n_bars: int = 200):
    """Run refresh_bitget_ohlcv.run_refresh for all 5 pairs with a mocked
    subprocess that writes ``n_bars`` fresh 15m candles ending at ``now``."""
    start = now - timedelta(minutes=15 * n_bars)
    behaviors = {pair: _success_behavior(_candles(start, n_bars)) for pair in PAIRS}
    runner = _fake_runner(behaviors)
    return refresh.run_refresh(
        pairs=PAIRS, timeframe=TF, datadir=datadir, log_path=log_path,
        now=now, runner=runner,
    )


def _run_cli(argv) -> tuple[int, dict]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        exit_code = agent_krypto_cli.main(argv)
    lines = [line for line in buf.getvalue().splitlines() if line.strip()]
    assert len(lines) == 1, f"expected exactly one JSON line on stdout, got: {lines!r}"
    return exit_code, json.loads(lines[0])


# --------------------------------------------------------------------------
# AC 1: ingest passes without "stale ohlcv" after refresh_bitget_ohlcv.py
# --------------------------------------------------------------------------

def test_ingest_passes_without_stale_ohlcv_after_refresh(tmp_path):
    datadir = tmp_path / "sandbox_data" / "bitget"
    log_path = tmp_path / "logs" / "bitget_refresh_versions.jsonl"
    now = datetime.now(timezone.utc)

    outcome = _refresh_all(datadir, log_path, now)
    assert outcome["status"] == "OK"

    futures_dir = datadir / "futures"
    adapter = LocalFeatherMarketDataAdapter(futures_dir, REQUIRED_SYMBOLS, timeframe=TF)
    lake = CryptoDataLake(tmp_path / "lake")
    version = CryptoMarketIngestor(lake).ingest([adapter])

    # Must not raise, and specifically must not raise for "stale ohlcv".
    require_ready_dataset(
        lake, version.dataset_id,
        as_of=now.isoformat(), max_age=timedelta(minutes=20),
        required_symbols=REQUIRED_SYMBOLS, required_timeframes=(TF,),
        required_data_kinds=("ohlcv",),
    )


# --------------------------------------------------------------------------
# AC 2: research-loop reaches experiment/evaluate and ends cycle WAIT
# --------------------------------------------------------------------------

def test_research_loop_reaches_evaluate_and_ends_wait(tmp_path):
    datadir = tmp_path / "sandbox_data" / "bitget"
    log_path = tmp_path / "logs" / "bitget_refresh_versions.jsonl"
    now = datetime.now(timezone.utc)

    outcome = _refresh_all(datadir, log_path, now, n_bars=2000)
    assert outcome["status"] == "OK"

    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({
        "config_version": "v1", "dataset_version": "unset",
        "feature_schema_version": "unset", "promotion_policy_version": "unset",
        "label_config_version": "unset", "credential_alias": "okx-demo-1",
    }))

    exit_code, result = _run_cli([
        "research-loop", "--config-version", "v1",
        "--data-root", str(tmp_path / "research" / "agent-krypto"),
        "--config-path", str(config_path),
        "--run-db", str(tmp_path / "research" / "agent-krypto" / "runs" / "orchestrator_runs.db"),
        "--local-data-dir", str(datadir / "futures"),
        "--as-of", now.isoformat(),
        "--max-age-minutes", "20",
        "--run-bucket", "t131-ac2-bucket",
    ])

    assert exit_code == 0, result
    assert result["status"] == "DONE", result
    phases = result["result"]["phases"]
    assert phases["ingest"]["dataset_id"]
    assert set(phases["ingest"]["symbols"]) == set(REQUIRED_SYMBOLS)
    assert phases["request"]["feature_version"]
    assert phases["experiment"]["status"] in ("accepted", "rejected")
    assert phases["evaluate"]["status"] in ("ACCEPTED", "REJECTED")
    assert phases["cycle"]["status"] == "WAIT"
    assert phases["cycle"]["execution_result"] is None
    assert "promote" not in phases


# --------------------------------------------------------------------------
# AC 3: refresh retry + research-loop are idempotent
# --------------------------------------------------------------------------

def test_refresh_retry_and_research_loop_are_idempotent(tmp_path):
    datadir = tmp_path / "sandbox_data" / "bitget"
    log_path = tmp_path / "logs" / "bitget_refresh_versions.jsonl"
    now = datetime.now(timezone.utc)

    first_refresh = _refresh_all(datadir, log_path, now, n_bars=2000)
    assert first_refresh["status"] == "OK"

    futures_dir = datadir / "futures"
    manifest_after_first = json.loads((futures_dir / "_manifest.json").read_text())
    files_after_first = {
        p.name: p.read_bytes() for p in futures_dir.glob("*.feather")
    }

    # Retry the *exact same* download (same candles, same `now`) -- freqtrade
    # would return the same data on a re-run within the same 10-min tick, or
    # simply because the exchange has not published a newer candle yet since
    # the previous refresh (routine under a 10-min cron with 15-min candles).
    # This must be a safe no-op: same last_candle_ts is NOT a regression
    # (only an older one is), so run_refresh must report OK and existing
    # files/manifest must remain byte-identical (nothing new to publish).
    behaviors = {
        pair: _success_behavior(_candles(now - timedelta(minutes=15 * 2000), 2000))
        for pair in PAIRS
    }
    runner = _fake_runner(behaviors)
    second_refresh = refresh.run_refresh(
        pairs=PAIRS, timeframe=TF, datadir=datadir, log_path=log_path,
        now=now, runner=runner,
    )
    assert second_refresh["status"] == "OK"
    files_after_second = {
        p.name: p.read_bytes() for p in futures_dir.glob("*.feather")
    }
    assert files_after_second == files_after_first
    manifest_after_second = json.loads((futures_dir / "_manifest.json").read_text())
    assert manifest_after_second == manifest_after_first

    # research-loop idempotency: same run_bucket -> same cached run_id.
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({
        "config_version": "v1", "dataset_version": "unset",
        "feature_schema_version": "unset", "promotion_policy_version": "unset",
        "label_config_version": "unset", "credential_alias": "okx-demo-1",
    }))
    argv = [
        "research-loop", "--config-version", "v1",
        "--data-root", str(tmp_path / "research" / "agent-krypto"),
        "--config-path", str(config_path),
        "--run-db", str(tmp_path / "research" / "agent-krypto" / "runs" / "orchestrator_runs.db"),
        "--local-data-dir", str(futures_dir),
        "--as-of", now.isoformat(),
        "--max-age-minutes", "20",
        "--run-bucket", "t131-ac3-bucket",
    ]
    first_exit, first = _run_cli(argv)
    second_exit, second = _run_cli(argv)

    assert first_exit == 0 and second_exit == 0
    assert first["run_id"] == second["run_id"]
    assert first["result"] == second["result"]
    assert second["reason"] == "ok"


# --------------------------------------------------------------------------
# AC 4: a corrupted/conflicting refresh never becomes the active dataset
# --------------------------------------------------------------------------

def test_corrupted_refresh_does_not_become_active(tmp_path):
    datadir = tmp_path / "sandbox_data" / "bitget"
    log_path = tmp_path / "logs" / "bitget_refresh_versions.jsonl"
    now = datetime.now(timezone.utc)

    good_refresh = _refresh_all(datadir, log_path, now, n_bars=200)
    assert good_refresh["status"] == "OK"

    futures_dir = datadir / "futures"
    target_pair = "BTC/USDT:USDT"
    filename = refresh.pair_to_filename(target_pair, TF)
    target_path = futures_dir / filename
    good_bytes = target_path.read_bytes()
    good_manifest = json.loads((futures_dir / "_manifest.json").read_text())

    # Simulate a corrupted/conflicting next refresh: same timestamps as what
    # is already on disk but different OHLCV values (a genuine data
    # conflict, not a duplicate) plus one new bar so freshness alone would
    # otherwise pass -- the conflict check must be what actually fires.
    later_now = now + timedelta(minutes=20)
    start = now - timedelta(minutes=15 * 200)
    conflicting_rows = _candles(start, 200, price=999.0) + _candles(
        start + timedelta(minutes=15 * 200), 1
    )
    # Other 4 pairs get genuinely fresh, non-overlapping data (continues
    # right after the existing on-disk candles) so their refresh succeeds
    # cleanly -- only the target pair's download is corrupted/conflicting.
    fresh_start = start + timedelta(minutes=15 * 200)
    behaviors = {pair: _success_behavior(_candles(fresh_start, 50)) for pair in PAIRS}
    behaviors[target_pair] = _success_behavior(conflicting_rows)
    runner = _fake_runner(behaviors)

    bad_refresh = refresh.run_refresh(
        pairs=PAIRS, timeframe=TF, datadir=datadir, log_path=log_path,
        now=later_now, runner=runner,
    )

    assert bad_refresh["status"] == "PARTIAL"
    by_pair = {r["pair"]: r for r in bad_refresh["results"]}
    assert by_pair[target_pair]["status"] == "error"
    assert "conflict" in by_pair[target_pair]["reason"].lower()

    # The target file must be byte-identical to the last good version --
    # the corrupted download never became active.
    assert target_path.read_bytes() == good_bytes
    manifest_after_bad = json.loads((futures_dir / "_manifest.json").read_text())
    key = f"{refresh.pair_to_symbol_key(target_pair)}/{TF}"
    assert manifest_after_bad["entries"][key] == good_manifest["entries"][key]

    # ...and it still passes ingest/readiness (the good, untouched data).
    adapter = LocalFeatherMarketDataAdapter(futures_dir, REQUIRED_SYMBOLS, timeframe=TF)
    lake = CryptoDataLake(tmp_path / "lake")
    version = CryptoMarketIngestor(lake).ingest([adapter])
    require_ready_dataset(
        lake, version.dataset_id,
        as_of=later_now.isoformat(), max_age=timedelta(minutes=60),
        required_symbols=REQUIRED_SYMBOLS, required_timeframes=(TF,),
        required_data_kinds=("ohlcv",),
    )


# --------------------------------------------------------------------------
# AC 5: rollback to the previous good version works (design doc §6)
# --------------------------------------------------------------------------

def test_rollback_to_previous_version_restores_prior_state(tmp_path):
    datadir = tmp_path / "sandbox_data" / "bitget"
    log_path = tmp_path / "logs" / "bitget_refresh_versions.jsonl"
    now = datetime.now(timezone.utc)

    first_refresh = _refresh_all(datadir, log_path, now, n_bars=200)
    assert first_refresh["status"] == "OK"

    futures_dir = datadir / "futures"
    target_pair = "BTC/USDT:USDT"
    filename = refresh.pair_to_filename(target_pair, TF)
    target_path = futures_dir / filename
    v1_bytes = target_path.read_bytes()
    v1_manifest_entry = json.loads(
        (futures_dir / "_manifest.json").read_text()
    )["entries"][f"{refresh.pair_to_symbol_key(target_pair)}/{TF}"]

    # A second, successful refresh (new version v2).
    later_now = now + timedelta(minutes=20)
    second_refresh = _refresh_all(datadir, log_path, later_now, n_bars=210)
    assert second_refresh["status"] == "OK"
    v2_bytes = target_path.read_bytes()
    assert v2_bytes != v1_bytes  # genuinely a new version

    prev_path = futures_dir / f"{filename}.prev"
    assert prev_path.is_file()
    assert prev_path.read_bytes() == v1_bytes  # .prev holds the pre-v2 state

    # --- Rollback procedure per docs/agent-krypto-bitget-refresh-design.md §6 ---
    # 1) mv X.feather.prev X.feather
    prev_path.replace(target_path)
    # 2) restore the manifest entry from the previous lineage line.
    lineage_lines = [json.loads(l) for l in log_path.read_text().splitlines()]
    btc_key = refresh.pair_to_symbol_key(target_pair)
    btc_lineage = [e for e in lineage_lines if e["symbol"] == btc_key and e["status"] == "ok"]
    assert len(btc_lineage) == 2  # v1 then v2
    previous_lineage_entry = btc_lineage[0]  # the entry before the current (v2) one
    assert previous_lineage_entry["dataset_version"] == v1_manifest_entry["dataset_version"]

    manifest_path = futures_dir / "_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["entries"][f"{btc_key}/{TF}"] = {
        "sha256": previous_lineage_entry["sha256"],
        "row_count": previous_lineage_entry["row_count"],
        "last_candle_ts": previous_lineage_entry["last_candle_ts"],
        "refreshed_at": previous_lineage_entry["refreshed_at"],
        "dataset_version": previous_lineage_entry["dataset_version"],
        "status": "ok",
        "source": previous_lineage_entry["source"],
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")

    # --- Verify: data is back to the v1 state ---
    assert target_path.read_bytes() == v1_bytes
    restored_manifest_entry = json.loads(manifest_path.read_text())["entries"][f"{btc_key}/{TF}"]
    assert restored_manifest_entry["dataset_version"] == v1_manifest_entry["dataset_version"]
    assert restored_manifest_entry["sha256"] == v1_manifest_entry["sha256"]

    # And the rolled-back dataset still ingests/reads cleanly (sanity check
    # that rollback doesn't leave the file in a state readiness rejects).
    adapter = LocalFeatherMarketDataAdapter(futures_dir, REQUIRED_SYMBOLS, timeframe=TF)
    lake = CryptoDataLake(tmp_path / "lake")
    version = CryptoMarketIngestor(lake).ingest([adapter])
    require_ready_dataset(
        lake, version.dataset_id,
        as_of=later_now.isoformat(), max_age=timedelta(minutes=60),
        required_symbols=REQUIRED_SYMBOLS, required_timeframes=(TF,),
        required_data_kinds=("ohlcv",),
    )
