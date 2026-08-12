#!/usr/bin/env python3
"""Deadline-aware HTTP collector for crypto-dashboard analysis rounds.

The analyst invokes this command instead of starting unbounded ``curl``
processes.  Sources run concurrently, each has its own timeout, and cancelling
the collector terminates the complete curl process group.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import signal
import tempfile
import time
from typing import Any


MODE_DEFAULTS = {
    "quick_check": {"deadline": 20.0, "retries": 0},
    "full_analysis": {"deadline": 45.0, "retries": 1},
}
DEFAULT_SOURCE_TIMEOUT = 5.0
CIRCUIT_STATE_FILENAME = "bot-crypto-analysis-circuit.json"
# Sentinel: resolving the default persistence path is deferred to CircuitBreaker
# construction (see _default_circuit_state_path) so importing this module, or
# building argparse defaults, never touches tempfile.gettempdir(). That call can
# raise FileNotFoundError on a read-only/no-/tmp environment, which previously
# crashed the process before any HTTP request was attempted.
USE_DEFAULT_CIRCUIT_STATE = object()


def _default_circuit_state_path() -> Path | None:
    """Best-effort default persistence path; None means "no disk state available"."""
    try:
        return Path(tempfile.gettempdir()) / CIRCUIT_STATE_FILENAME
    except OSError:
        return None


class CircuitBreaker:
    """Per-source failure tracker.

    Persistence is strictly optional: any failure to resolve, read, or write
    the state file downgrades the breaker to an in-memory-only fallback for
    the remainder of the run instead of raising. ``stateless`` reports whether
    that fallback is active so callers can surface it as a warning.
    """

    def __init__(self, path: Path | None, threshold: int = 2, cooldown: float = 30.0):
        self.path = path
        self.threshold = threshold
        self.cooldown = cooldown
        self.stateless = path is None
        self.state = self._load()

    def _load(self) -> dict[str, dict[str, Any]]:
        if self.path is None:
            return {}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}

    def _save(self) -> None:
        if self.path is None or self.stateless:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(f"{self.path.suffix}.{os.getpid()}.tmp")
            temporary.write_text(json.dumps(self.state, separators=(",", ":")), encoding="utf-8")
            temporary.replace(self.path)
        except OSError:
            # Read-only filesystem (or similar): keep tracking in memory only,
            # do not crash the collector over a non-essential persistence step.
            self.stateless = True

    def allow(self, source: str, now: float | None = None) -> tuple[bool, float | None]:
        now = time.time() if now is None else now
        entry = self.state.get(source, {})
        open_until = float(entry.get("open_until", 0.0))
        if open_until > now:
            return False, open_until
        return True, None  # expired circuit is a half-open recovery probe

    def success(self, source: str) -> None:
        if source in self.state:
            self.state.pop(source)
            self._save()

    def failure(self, source: str) -> None:
        entry = self.state.setdefault(source, {"failures": 0, "open_until": 0.0})
        entry["failures"] = int(entry.get("failures", 0)) + 1
        if entry["failures"] >= self.threshold:
            entry["open_until"] = time.time() + self.cooldown
        self._save()


def _is_stale(payload: Any) -> bool:
    if isinstance(payload, dict):
        if payload.get("status") == "stale" or payload.get("is_stale") is True:
            return True
        return any(_is_stale(value) for value in payload.values())
    if isinstance(payload, list):
        return any(_is_stale(value) for value in payload)
    return False


class DeadlineFetcher:
    def __init__(
        self,
        *,
        mode: str,
        source_timeout: float = DEFAULT_SOURCE_TIMEOUT,
        deadline: float | None = None,
        circuit_state: Path | None | object = USE_DEFAULT_CIRCUIT_STATE,
        circuit_threshold: int = 2,
        circuit_cooldown: float = 30.0,
        curl_binary: str = "curl",
    ):
        if mode not in MODE_DEFAULTS:
            raise ValueError(f"unsupported mode: {mode}")
        if source_timeout <= 0:
            raise ValueError("source_timeout must be positive")
        if deadline is not None and mode != "full_analysis":
            raise ValueError("custom deadline is allowed only for full_analysis")
        resolved_deadline = MODE_DEFAULTS[mode]["deadline"] if deadline is None else deadline
        if resolved_deadline <= 0:
            raise ValueError("deadline must be positive")
        self.mode = mode
        self.deadline = float(resolved_deadline)
        self.source_timeout = float(source_timeout)
        self.retries = int(MODE_DEFAULTS[mode]["retries"])
        resolved_circuit_state = (
            _default_circuit_state_path() if circuit_state is USE_DEFAULT_CIRCUIT_STATE else circuit_state
        )
        self.breaker = CircuitBreaker(resolved_circuit_state, circuit_threshold, circuit_cooldown)
        self.curl_binary = curl_binary
        self.active_processes: set[asyncio.subprocess.Process] = set()
        self.started_pids: set[int] = set()

    async def _terminate(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=0.25)
        except asyncio.TimeoutError:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.wait()

    async def _curl(self, url: str, timeout: float) -> dict[str, Any]:
        started = time.monotonic()
        # curl parses --connect-timeout/--max-time with the process locale's
        # decimal separator (LC_NUMERIC); on non-"C" locales (e.g. pl_PL, which
        # uses a comma) it rejects Python's dot-formatted str(float) as "not a
        # proper numerical parameter" and exits before any request is sent.
        # Force a locale-independent numeric parse for the child process only.
        curl_env = dict(os.environ)
        curl_env["LC_NUMERIC"] = "C"
        process = await asyncio.create_subprocess_exec(
            self.curl_binary,
            "-sS",
            "--fail-with-body",
            "--connect-timeout",
            str(timeout),
            "--max-time",
            str(timeout),
            "-w",
            "\n__HTTP_STATUS__:%{http_code}",
            url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            env=curl_env,
        )
        self.active_processes.add(process)
        self.started_pids.add(process.pid)
        try:
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout + 0.15)
            except asyncio.TimeoutError:
                await self._terminate(process)
                return {"status": "timed_out", "error": f"source exceeded {timeout:.3f}s"}
            body, marker, status_text = stdout.rpartition(b"\n__HTTP_STATUS__:")
            http_status = int(status_text) if marker and status_text.isdigit() else None
            if process.returncode == 28:
                return {"status": "timed_out", "http_status": http_status, "error": stderr.decode(errors="replace").strip()}
            if process.returncode != 0:
                return {"status": "error", "http_status": http_status, "error": stderr.decode(errors="replace").strip() or body.decode(errors="replace")[:500]}
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                data = body.decode(errors="replace")
            return {
                "status": "stale" if _is_stale(data) else "ok",
                "http_status": http_status,
                "payload_bytes": len(body),
                "data": data,
            }
        except asyncio.CancelledError:
            await self._terminate(process)
            raise
        finally:
            self.active_processes.discard(process)
            # elapsed is assigned by the caller so every exit path gets it.
            _ = started

    async def _fetch_source(self, name: str, url: str, global_end: float) -> dict[str, Any]:
        allowed, open_until = self.breaker.allow(name)
        if not allowed:
            return {"status": "circuit_open", "attempts": 0, "elapsed_ms": 0.0, "retry_after": open_until}

        started = time.monotonic()
        result: dict[str, Any] = {"status": "error", "error": "not attempted"}
        attempts = 0
        for attempt in range(self.retries + 1):
            remaining = global_end - time.monotonic()
            if remaining <= 0:
                result = {"status": "timed_out", "error": "global deadline exhausted"}
                break
            attempts = attempt + 1
            result = await self._curl(url, min(self.source_timeout, remaining))
            if result["status"] in {"ok", "stale"}:
                self.breaker.success(name)
                break
            if attempt < self.retries and global_end - time.monotonic() > 0:
                continue
        if result["status"] not in {"ok", "stale"}:
            self.breaker.failure(name)
        result["attempts"] = attempts
        result["elapsed_ms"] = round((time.monotonic() - started) * 1000, 2)
        return result

    async def run(self, sources: dict[str, str]) -> dict[str, Any]:
        started = time.monotonic()
        global_end = started + self.deadline
        tasks = {name: asyncio.create_task(self._fetch_source(name, url, global_end)) for name, url in sources.items()}
        try:
            done, pending = await asyncio.wait(tasks.values(), timeout=self.deadline)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        except asyncio.CancelledError:
            for task in tasks.values():
                task.cancel()
            await asyncio.gather(*tasks.values(), return_exceptions=True)
            raise

        results: dict[str, Any] = {}
        for name, task in tasks.items():
            if task.cancelled():
                results[name] = {"status": "timed_out", "attempts": 0, "elapsed_ms": round(self.deadline * 1000, 2), "error": "global deadline exceeded"}
            elif task.exception() is not None:
                results[name] = {"status": "error", "attempts": 0, "elapsed_ms": 0.0, "error": str(task.exception())}
            else:
                results[name] = task.result()

        missing = [name for name, result in results.items() if result["status"] not in {"ok", "stale"}]
        stale = [name for name, result in results.items() if result["status"] == "stale"]
        timed_out = [name for name, result in results.items() if result["status"] == "timed_out"]
        payload_bytes = sum(int(result.get("payload_bytes", 0)) for result in results.values())
        warnings: list[str] = []
        if self.breaker.stateless:
            warnings.append(
                "circuit_breaker_stateless: no writable path for circuit state; "
                "tracking failures in memory only for this run"
            )
        return {
            "mode": self.mode,
            "partial": bool(missing or stale),
            "deadline_seconds": self.deadline,
            "per_source_timeout_seconds": self.source_timeout,
            "elapsed_ms": round((time.monotonic() - started) * 1000, 2),
            "sources": results,
            "missing_sources": missing,
            "stale_sources": stale,
            "timed_out_sources": timed_out,
            "warnings": warnings,
            "cost": {
                "calls": sum(int(result.get("attempts", 0)) for result in results.values()),
                "payload_bytes": payload_bytes,
                "source_time_ms": {name: result["elapsed_ms"] for name, result in results.items()},
                "timeouts": len(timed_out),
                "saved_payload_bytes": None,
                "saved_payload_note": "n/a: upstream Content-Length after cancellation is unknown",
            },
        }


def _source(value: str) -> tuple[str, str]:
    name, separator, url = value.partition("=")
    if not separator or not name or not url:
        raise argparse.ArgumentTypeError("source must have NAME=URL form")
    return name, url


async def _run_with_signal_shutdown(
    fetcher: DeadlineFetcher, sources: dict[str, str]
) -> tuple[dict[str, Any] | None, signal.Signals | None]:
    """Run one round and turn process signals into cooperative cancellation.

    curl runs in a separate session, so merely terminating this collector
    would otherwise orphan it.  Cancelling ``round_task`` enters
    ``DeadlineFetcher.run``'s cleanup and waits until every curl group exits.
    """
    loop = asyncio.get_running_loop()
    round_task = asyncio.create_task(fetcher.run(sources))
    received: signal.Signals | None = None

    def request_shutdown(received_signal: signal.Signals) -> None:
        nonlocal received
        if received is None:
            received = received_signal
            round_task.cancel()

    handled = (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)
    for handled_signal in handled:
        loop.add_signal_handler(handled_signal, request_shutdown, handled_signal)
    try:
        return await round_task, None
    except asyncio.CancelledError:
        if received is None:
            raise
        return None, received
    finally:
        for handled_signal in handled:
            loop.remove_signal_handler(handled_signal)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=MODE_DEFAULTS, required=True)
    parser.add_argument("--source", action="append", type=_source, required=True, metavar="NAME=URL")
    parser.add_argument("--source-timeout", type=float, default=DEFAULT_SOURCE_TIMEOUT)
    parser.add_argument("--deadline", type=float, help="full_analysis only; default 45 seconds")
    parser.add_argument(
        "--circuit-state",
        type=Path,
        default=None,
        help="Optional path for circuit-breaker persistence; omit to resolve the "
        "platform tempdir lazily, falling back to an in-memory-only breaker if no "
        "writable path is available.",
    )
    args = parser.parse_args()
    try:
        fetcher = DeadlineFetcher(
            mode=args.mode,
            source_timeout=args.source_timeout,
            deadline=args.deadline,
            circuit_state=args.circuit_state if args.circuit_state is not None else USE_DEFAULT_CIRCUIT_STATE,
        )
        result, received_signal = asyncio.run(_run_with_signal_shutdown(fetcher, dict(args.source)))
    except ValueError as exc:
        parser.error(str(exc))
    if received_signal is not None:
        return 128 + received_signal.value
    assert result is not None
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
