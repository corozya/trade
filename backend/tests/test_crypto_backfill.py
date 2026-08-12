import argparse

import pytest

from services.crypto_backfill import (
    BackfillMode,
    BackfillRunner,
    add_mode_arguments,
    resolve_since,
    resume_cursor,
    retry_read,
)
from services.crypto_data_lake import CryptoDataLake
from services.okx_client import OkxRateLimitError


def _row(symbol, timeframe, data_kind, observed_minute, **extra):
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "data_kind": data_kind,
        "observed_at": f"2026-01-01T00:{observed_minute:02d}:00Z",
        "available_at": f"2026-01-01T00:{observed_minute + 1:02d}:00Z",
        "source": "okx",
        **extra,
    }


# -- retry_read ---------------------------------------------------------------


def test_retry_read_succeeds_after_transient_rate_limit_errors():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise OkxRateLimitError("rate limited")
        return "ok"

    result = retry_read(
        flaky,
        retry_exceptions=(OkxRateLimitError,),
        max_attempts=5,
        backoff_seconds=0,
    )

    assert result == "ok"
    assert calls["n"] == 3


def test_retry_read_raises_after_exhausting_attempts():
    def always_fails():
        raise OkxRateLimitError("still limited")

    with pytest.raises(OkxRateLimitError):
        retry_read(
            always_fails,
            retry_exceptions=(OkxRateLimitError,),
            max_attempts=2,
            backoff_seconds=0,
        )


def test_retry_read_does_not_retry_unlisted_exceptions():
    calls = {"n": 0}

    def raises_value_error():
        calls["n"] += 1
        raise ValueError("not retryable")

    with pytest.raises(ValueError):
        retry_read(
            raises_value_error,
            retry_exceptions=(OkxRateLimitError,),
            max_attempts=5,
            backoff_seconds=0,
        )
    assert calls["n"] == 1


# -- resume_cursor --------------------------------------------------------------


def test_resume_cursor_is_none_without_base_dataset(tmp_path):
    lake = CryptoDataLake(tmp_path)
    cursor = resume_cursor(
        lake,
        base_dataset_id=None,
        symbol="BTC-USDT-SWAP",
        timeframe="1d",
        data_kind="funding",
    )
    assert cursor is None


def test_resume_cursor_is_none_for_missing_dataset(tmp_path):
    lake = CryptoDataLake(tmp_path)
    cursor = resume_cursor(
        lake,
        base_dataset_id="market-doesnotexist0000",
        symbol="BTC-USDT-SWAP",
        timeframe="1d",
        data_kind="funding",
    )
    assert cursor is None


def test_resume_cursor_returns_latest_observed_at_for_matching_key(tmp_path):
    lake = CryptoDataLake(tmp_path)
    version = lake.publish(
        [
            _row("BTC-USDT-SWAP", "1d", "funding", 0, funding_rate=0.0001),
            _row("BTC-USDT-SWAP", "1d", "funding", 5, funding_rate=0.0002),
            # different data_kind for the same symbol/timeframe must not leak in.
            _row("BTC-USDT-SWAP", "1d", "open_interest", 9, open_interest=1.0),
            # different symbol must not leak in.
            _row("ETH-USDT-SWAP", "1d", "funding", 9, funding_rate=0.0003),
        ],
        lineage={"sources": [{"name": "okx"}]},
    )

    cursor = resume_cursor(
        lake,
        base_dataset_id=version.dataset_id,
        symbol="BTC-USDT-SWAP",
        timeframe="1d",
        data_kind="funding",
    )

    assert cursor == "2026-01-01T00:05:00Z"


# -- full vs incremental CLI contract -------------------------------------------


def test_add_mode_arguments_requires_full_or_incremental():
    parser = argparse.ArgumentParser()
    add_mode_arguments(parser)

    with pytest.raises(SystemExit):
        parser.parse_args(["--lake-root", "/tmp/lake"])


def test_add_mode_arguments_parses_full_and_incremental():
    parser = argparse.ArgumentParser()
    add_mode_arguments(parser)

    full_args = parser.parse_args(["--full", "--lake-root", "/tmp/lake"])
    assert full_args.mode is BackfillMode.FULL

    incremental_args = parser.parse_args(
        ["--incremental", "--lake-root", "/tmp/lake", "--base-dataset-id", "market-abc123"]
    )
    assert incremental_args.mode is BackfillMode.INCREMENTAL
    assert incremental_args.base_dataset_id == "market-abc123"


def test_add_mode_arguments_rejects_both_flags_together():
    parser = argparse.ArgumentParser()
    add_mode_arguments(parser)

    with pytest.raises(SystemExit):
        parser.parse_args(["--full", "--incremental", "--lake-root", "/tmp/lake"])


# -- resolve_since --------------------------------------------------------------


def _dt(iso):
    from datetime import datetime

    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def test_resolve_since_full_mode_always_uses_horizon_start(tmp_path):
    lake = CryptoDataLake(tmp_path)
    version = lake.publish(
        [_row("BTC-USDT-SWAP", "1d", "funding", 0, funding_rate=0.0001)],
        lineage={"sources": [{"name": "okx"}]},
    )
    horizon = _dt("2025-01-01T00:00:00Z")

    since = resolve_since(
        mode=BackfillMode.FULL,
        lake=lake,
        base_dataset_id=version.dataset_id,
        symbol="BTC-USDT-SWAP",
        timeframe="1d",
        data_kind="funding",
        full_horizon_start=horizon,
    )

    assert since == horizon


def test_resolve_since_incremental_resumes_from_last_published_point(tmp_path):
    lake = CryptoDataLake(tmp_path)
    version = lake.publish(
        [_row("BTC-USDT-SWAP", "1d", "funding", 5, funding_rate=0.0001)],
        lineage={"sources": [{"name": "okx"}]},
    )
    horizon = _dt("2025-01-01T00:00:00Z")

    since = resolve_since(
        mode=BackfillMode.INCREMENTAL,
        lake=lake,
        base_dataset_id=version.dataset_id,
        symbol="BTC-USDT-SWAP",
        timeframe="1d",
        data_kind="funding",
        full_horizon_start=horizon,
    )

    assert since == _dt("2026-01-01T00:05:00Z")


def test_resolve_since_incremental_falls_back_to_horizon_without_prior_data(tmp_path):
    lake = CryptoDataLake(tmp_path)
    horizon = _dt("2025-01-01T00:00:00Z")

    since = resolve_since(
        mode=BackfillMode.INCREMENTAL,
        lake=lake,
        base_dataset_id=None,
        symbol="BTC-USDT-SWAP",
        timeframe="1d",
        data_kind="funding",
        full_horizon_start=horizon,
    )

    assert since == horizon


# -- BackfillRunner (integration with CryptoMarketIngestor) --------------------


class _FixtureAdapter:
    name = "fixture"

    def __init__(self, records, source_metadata=None):
        self._records = records
        self._source_metadata = source_metadata or {}

    def fetch(self):
        return [dict(row) for row in self._records]

    def lineage(self):
        return {"name": self.name, **self._source_metadata}


def test_backfill_runner_publishes_via_ingestor(tmp_path):
    lake = CryptoDataLake(tmp_path)
    runner = BackfillRunner(lake)
    adapter = _FixtureAdapter(
        [_row("BTC-USDT-SWAP", "1d", "funding", 0, funding_rate=0.0001)]
    )

    version = runner.run(adapter, base_dataset_id=None)

    assert version.manifest["data_kinds"] == ["funding"]
    assert version.manifest["row_count"] == 1


def test_backfill_runner_incremental_merges_with_base_dataset(tmp_path):
    lake = CryptoDataLake(tmp_path)
    runner = BackfillRunner(lake)
    first = runner.run(
        _FixtureAdapter([_row("BTC-USDT-SWAP", "1d", "funding", 0, funding_rate=0.0001)]),
        base_dataset_id=None,
    )

    second = runner.run(
        _FixtureAdapter([_row("BTC-USDT-SWAP", "1d", "funding", 5, funding_rate=0.0002)]),
        base_dataset_id=first.dataset_id,
    )

    assert second.manifest["row_count"] == 2
    assert second.dataset_id != first.dataset_id
