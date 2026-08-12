#!/usr/bin/env python3
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from prepare_agent_krypto_snapshots import prepare


def _snapshot(symbol: str, analyzed_at: datetime) -> dict:
    return {
        "symbol": f"{symbol}-USD_UM_XPERP-310404",
        "analyzed_at": analyzed_at.isoformat().replace("+00:00", "Z"),
        "price": {"last": 1},
        "indicators_15m": {"marker": f"{symbol}_SNAPSHOT_MARKER"},
        "higher_tf_context": {"trend_1h": "range"},
        "orderbook": None,
        "futures": {"funding_rate": 0},
    }


class SnapshotPrepareTests(unittest.TestCase):
    def test_complete_set_is_returned_without_truncation(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as raw_dir:
            data_dir = Path(raw_dir)
            for symbol in ("BTC", "ETH", "DOGE"):
                (data_dir / f"{symbol}_analysis.json").write_text(json.dumps(_snapshot(symbol, now)))
            result = prepare(data_dir, 1200, cycle_started_at=now, now=now)
        self.assertEqual(set(result), {"BTC", "ETH", "DOGE"})
        self.assertEqual(result["DOGE"]["indicators_15m"]["marker"], "DOGE_SNAPSHOT_MARKER")

    def test_missing_invalid_stale_and_mismatch_are_rejected(self) -> None:
        now = datetime.now(timezone.utc)
        cases = {
            "missing": None,
            "invalid": "not-json",
            "stale": json.dumps(_snapshot("DOGE", now - timedelta(hours=1))),
            "mismatch": json.dumps(_snapshot("BTC", now)),
        }
        for name, doge_value in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as raw_dir:
                data_dir = Path(raw_dir)
                for symbol in ("BTC", "ETH"):
                    (data_dir / f"{symbol}_analysis.json").write_text(json.dumps(_snapshot(symbol, now)))
                if doge_value is not None:
                    (data_dir / "DOGE_analysis.json").write_text(doge_value)
                with self.assertRaises(ValueError):
                    prepare(data_dir, 1200, cycle_started_at=now, now=now)

    def test_snapshot_younger_than_max_age_but_from_previous_cycle_is_rejected(self) -> None:
        now = datetime.now(timezone.utc)
        previous_cycle = now - timedelta(minutes=5)
        with tempfile.TemporaryDirectory() as raw_dir:
            data_dir = Path(raw_dir)
            for symbol in ("BTC", "ETH", "DOGE"):
                (data_dir / f"{symbol}_analysis.json").write_text(
                    json.dumps(_snapshot(symbol, previous_cycle))
                )
            with self.assertRaisesRegex(ValueError, "pre-cycle snapshot"):
                prepare(data_dir, 1200, cycle_started_at=now, now=now)


if __name__ == "__main__":
    unittest.main()
