#!/usr/bin/env python3
"""#83/#92: normalizuje raport Claude/Codex i dopisuje jedną linię JSONL per cykl do
scripts/agent_krypto_decisions.jsonl — czytelna historia decyzji, osobno od
surowego technicznego logu (scripts/agent_krypto_cron.log).

Wejście: JSON Claude (`--output-format json`) albo bezpośredni raport JSON
Codexa (`--output-last-message`) na stdin.

Format wyjścia (jedna linia JSONL per cykl):
{
  "ts": "2026-07-21T18:00:24.000Z",
  "idempotency_key": "agent-krypto-20260721T180024000Z",
  "provider": "claude",
  "duration_ms": 23000,
  "cost_usd": 0.217,
  "status": "completed",
  "round_id": 12,
  "decisions": [
    {"symbol": "BTC", "decision": "WAIT", "reason": "..."},
    {"symbol": "ETH", "decision": "TRADE", "side": "LONG", "qty": 1, ...}
  ]
}

Jeśli agent nie zwróci żadnego bloku ```json``` zgodnego z kontraktem (bywa —
patrz agent-krypto SKILL.md), "decisions" zawiera jeden wpis
{"raw_summary": "<pełny tekst odpowiedzi>"} zamiast pustej listy.

Odporność: brak/niepełny JSON na wejściu nie wywala skryptu — zapisuje wpis
z "decisions": [] i "parse_error" zamiast rzucać wyjątek (wrapper nie ma się
wywalić przez błąd logowania).
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

LOG_PATH = Path(__file__).resolve().parent / "agent_krypto_decisions.jsonl"

_JSON_BLOCK_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)


def _now_iso() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def extract_decisions(result_text: str) -> list[dict]:
    """Wyciąga wszystkie bloki ```json {...}``` z tekstu odpowiedzi agenta —
    kontrakt WAIT/TRADE zdefiniowany w .claude/skills/agent-krypto/SKILL.md.

    Agent nie zawsze trzyma się dosłownie tego formatu (obserwowane: wolny
    tekst z podsumowaniem zamiast czystych bloków JSON) — gdy nie znaleziono
    ani jednego poprawnego bloku, zwraca cały result_text jako fallback, żeby
    log nigdy nie wyglądał na "brak decyzji" przy faktycznym sukcesie."""
    decisions = []
    for match in _JSON_BLOCK_RE.finditer(result_text or ""):
        try:
            decisions.append(json.loads(match.group(1)))
        except json.JSONDecodeError:
            continue

    if not decisions and result_text:
        decisions.append({"raw_summary": result_text.strip()})

    return decisions


def _report_from_payload(payload: dict, provider: str) -> dict | None:
    """Return the common schema report from either provider's envelope."""
    if provider == "codex" and isinstance(payload.get("decisions"), list):
        return payload

    structured = payload.get("structured_output")
    if isinstance(structured, dict):
        return structured

    result = payload.get("result")
    if isinstance(result, dict):
        return result
    if isinstance(result, str):
        try:
            parsed = json.loads(result)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def build_entry(
    idempotency_key: str,
    provider: str,
    duration_ms: int | None,
    payload: dict | None,
    parse_error: str | None,
) -> dict:
    entry: dict = {
        "ts": _now_iso(),
        "idempotency_key": idempotency_key,
        "provider": provider,
    }
    if parse_error:
        entry["parse_error"] = parse_error
        entry["decisions"] = []
        return entry

    assert payload is not None
    report = _report_from_payload(payload, provider)
    entry["duration_ms"] = payload.get("duration_ms", duration_ms)
    entry["cost_usd"] = payload.get("total_cost_usd")
    entry["is_error"] = payload.get("is_error", False)
    if report is not None:
        entry["status"] = report.get("status")
        entry["round_id"] = report.get("round_id")
        entry["decisions"] = report.get("decisions", [])
    else:
        result = payload.get("result", "")
        entry["decisions"] = extract_decisions(result if isinstance(result, str) else "")
    return entry


def main() -> int:
    idempotency_key = sys.argv[1] if len(sys.argv) > 1 else "unknown"
    provider = sys.argv[2] if len(sys.argv) > 2 else "claude"
    try:
        duration_ms = int(sys.argv[3]) if len(sys.argv) > 3 else None
    except ValueError:
        duration_ms = None
    raw = sys.stdin.read()

    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            parse_error = "stdin JSON nie jest obiektem"
            payload = None
        else:
            parse_error = None
    except json.JSONDecodeError as exc:
        payload = None
        parse_error = f"stdin nie jest poprawnym JSON: {exc}"

    entry = build_entry(idempotency_key, provider, duration_ms, payload, parse_error)

    with LOG_PATH.open("a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

