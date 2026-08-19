"""Unit tests for timeframe-selection logic in crypto_backfill_indicators.py.

Exercises only the pure argument-parsing / TF-resolution logic — no data lake
I/O, no real backfill runs.  The test uses the script's ``main()`` entry point
with patched ``_backfill_one_pair`` so the backfill body is never executed.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import crypto_backfill_indicators as cli
from services.crypto_data_lake import TIMEFRAMES


def _run(argv: list[str]) -> list[str]:
    """Call main() with a fake lake and capture which timeframes were used."""
    captured: list[str] = []

    def _fake_backfill_one_pair(*, timeframe: str, **_kwargs):
        captured.append(timeframe)
        return {
            "ok": True,
            "mode": "incremental",
            "indicator": "rsi",
            "data_kind": "rsi",
            "symbol": "BTC-USDT-SWAP",
            "timeframe": timeframe,
            "ohlcv_dataset_id": "ds-ohlcv",
            "dataset_id": "ds-out",
            "row_count": 1,
            "new_points_this_run": 1,
        }

    with (
        patch.object(cli, "_backfill_one_pair", side_effect=_fake_backfill_one_pair),
        patch.object(cli, "_load_registry", return_value={}),
        patch.object(cli, "_save_registry"),
        patch("services.crypto_data_lake.CryptoDataLake.__init__", return_value=None),
    ):
        cli.main(argv)

    return captured


class TestDefaultTimeframesSelection:
    """Timeframe selection when --timeframes/--timeframe is omitted."""

    BASE_ARGV = ["--incremental", "--lake-root", "/fake/lake", "--symbol", "BTC-USDT-SWAP"]

    def test_rsi_defaults_to_all_timeframes(self):
        tfs = _run([*self.BASE_ARGV, "--indicator", "rsi"])
        assert tfs == list(TIMEFRAMES), (
            f"rsi without --timeframes should use full TIMEFRAMES={list(TIMEFRAMES)}, got {tfs}"
        )

    def test_support_resistance_defaults_to_narrow_set(self):
        tfs = _run([*self.BASE_ARGV, "--indicator", "support_resistance"])
        assert tfs == list(cli._SUPPORT_RESISTANCE_DEFAULT_TIMEFRAMES), (
            f"support_resistance without --timeframes should use "
            f"{list(cli._SUPPORT_RESISTANCE_DEFAULT_TIMEFRAMES)}, got {tfs}"
        )
        assert "1m" not in tfs
        assert "5m" not in tfs
        assert "15m" not in tfs

    def test_support_resistance_explicit_timeframes_override(self):
        """Explicit --timeframes must win over the narrow default."""
        tfs = _run([*self.BASE_ARGV, "--indicator", "support_resistance", "--timeframes", "1m,5m"])
        assert tfs == ["1m", "5m"], (
            f"explicit --timeframes=1m,5m must override default, got {tfs}"
        )

    def test_support_resistance_explicit_legacy_timeframe(self):
        """Legacy --timeframe (singular) must also override the narrow default."""
        tfs = _run([*self.BASE_ARGV, "--indicator", "support_resistance", "--timeframe", "1m"])
        assert tfs == ["1m"], f"explicit --timeframe=1m must override default, got {tfs}"
