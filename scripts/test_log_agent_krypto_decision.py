#!/usr/bin/env python3
import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scripts.log_agent_krypto_decision import build_entry


class BuildEntryTests(unittest.TestCase):
    def test_normalizes_claude_structured_output(self) -> None:
        report = {
            "status": "completed",
            "round_id": 12,
            "decisions": [{"symbol": "BTC", "decision": "WAIT", "reason": "test"}],
        }
        entry = build_entry(
            "key",
            "claude",
            99,
            {
                "structured_output": report,
                "duration_ms": 7,
                "total_cost_usd": 0.01,
                "is_error": False,
            },
            None,
        )

        self.assertEqual(entry["provider"], "claude")
        self.assertEqual(entry["duration_ms"], 7)
        self.assertEqual(entry["cost_usd"], 0.01)
        self.assertEqual(entry["status"], "completed")
        self.assertEqual(entry["round_id"], 12)
        self.assertEqual(entry["decisions"], report["decisions"])

    def test_normalizes_direct_codex_report(self) -> None:
        report = {
            "status": "completed",
            "round_id": 13,
            "decisions": [{"symbol": "ETH", "decision": "WAIT", "reason": "test"}],
        }
        entry = build_entry("key", "codex", 42, report, None)

        self.assertEqual(entry["provider"], "codex")
        self.assertEqual(entry["duration_ms"], 42)
        self.assertIsNone(entry["cost_usd"])
        self.assertEqual(entry["status"], "completed")
        self.assertEqual(entry["round_id"], 13)
        self.assertEqual(entry["decisions"], report["decisions"])


if __name__ == "__main__":
    unittest.main()
