import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import crypto_analysis_fetch
from crypto_analysis_fetch import CircuitBreaker, DeadlineFetcher, USE_DEFAULT_CIRCUIT_STATE


class Handler(BaseHTTPRequestHandler):
    calls: dict[str, int] = {}

    def do_GET(self):
        self.calls[self.path] = self.calls.get(self.path, 0) + 1
        if self.path == "/slow":
            time.sleep(1)
        if self.path == "/flaky" and self.calls[self.path] == 1:
            self.send_response(503)
            self.end_headers()
            return
        if self.path == "/error":
            self.send_response(503)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        try:
            self.wfile.write(json.dumps({"status": "ok", "path": self.path}).encode())
        except BrokenPipeError:
            pass

    def log_message(self, *_args):
        pass


class DeadlineFetcherTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        Handler.calls = {}
        self.tempdir = tempfile.TemporaryDirectory()
        self.state = Path(self.tempdir.name) / "circuit.json"

    def tearDown(self):
        self.tempdir.cleanup()

    def fetcher(self, mode="quick_check", **kwargs):
        return DeadlineFetcher(mode=mode, circuit_state=self.state, **kwargs)

    async def test_fast_sources_regression(self):
        result = await self.fetcher().run({"a": f"{self.base}/ok", "b": f"{self.base}/other"})
        self.assertFalse(result["partial"])
        self.assertEqual(result["missing_sources"], [])
        self.assertEqual(result["cost"]["calls"], 2)
        self.assertLess(result["elapsed_ms"], 20_000)

    async def test_nested_stale_component_is_reported(self):
        original = Handler.do_GET

        def stale(handler):
            handler.send_response(200)
            handler.send_header("Content-Type", "application/json")
            handler.end_headers()
            handler.wfile.write(json.dumps({"open_interest": {"status": "stale"}}).encode())

        Handler.do_GET = stale
        try:
            result = await self.fetcher().run({"snapshot": f"{self.base}/stale"})
        finally:
            Handler.do_GET = original
        self.assertEqual(result["stale_sources"], ["snapshot"])
        self.assertTrue(result["partial"])

    async def test_quick_timeout_is_partial_and_has_no_retry(self):
        fetcher = self.fetcher(source_timeout=0.1)
        result = await fetcher.run({"slow": f"{self.base}/slow"})
        self.assertTrue(result["partial"])
        self.assertEqual(result["timed_out_sources"], ["slow"])
        self.assertEqual(result["sources"]["slow"]["attempts"], 1)
        self.assertLess(result["elapsed_ms"], 1_000)
        self.assertEqual(fetcher.active_processes, set())

    async def test_two_unavailable_sources_are_named(self):
        result = await self.fetcher().run({"one": f"{self.base}/error", "two": "http://127.0.0.1:1/nope"})
        self.assertEqual(set(result["missing_sources"]), {"one", "two"})
        self.assertTrue(all(item["status"] == "error" for item in result["sources"].values()))

    async def test_full_analysis_retries_once(self):
        result = await self.fetcher(mode="full_analysis", deadline=1, source_timeout=0.2).run({"flaky": f"{self.base}/flaky"})
        self.assertEqual(result["sources"]["flaky"]["status"], "ok")
        self.assertEqual(result["sources"]["flaky"]["attempts"], 2)

    async def test_cancellation_terminates_curl(self):
        fetcher = self.fetcher(source_timeout=5)
        task = asyncio.create_task(fetcher.run({"slow": f"{self.base}/slow"}))
        await asyncio.sleep(0.05)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(fetcher.active_processes, set())
        self.assertTrue(fetcher.started_pids)
        for pid in fetcher.started_pids:
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)

    async def test_external_sigterm_reaps_child_curl_process(self):
        script = Path(__file__).with_name("crypto_analysis_fetch.py")
        process = subprocess.Popen(
            [
                sys.executable,
                str(script),
                "--mode",
                "quick_check",
                "--source",
                f"slow={self.base}/slow",
                "--circuit-state",
                str(self.state),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        child_pid = None
        try:
            children_path = Path(f"/proc/{process.pid}/task/{process.pid}/children")
            for _ in range(100):
                if process.poll() is not None:
                    break
                try:
                    children = children_path.read_text(encoding="utf-8").split()
                except FileNotFoundError:
                    children = []
                if children:
                    child_pid = int(children[0])
                    break
                await asyncio.sleep(0.01)
            self.assertIsNotNone(child_pid, "collector did not start curl child")

            process.send_signal(signal.SIGTERM)
            returncode = await asyncio.wait_for(asyncio.to_thread(process.wait), timeout=2)
            self.assertEqual(returncode, 128 + signal.SIGTERM)
            with self.assertRaises(ProcessLookupError):
                os.kill(child_pid, 0)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=2)
            if child_pid is not None:
                try:
                    os.killpg(child_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    async def test_full_global_deadline_returns_partial_before_limit(self):
        fetcher = self.fetcher(mode="full_analysis", deadline=0.15, source_timeout=5)
        result = await fetcher.run({"slow": f"{self.base}/slow"})
        self.assertTrue(result["partial"])
        self.assertEqual(result["timed_out_sources"], ["slow"])
        self.assertLess(result["elapsed_ms"], 500)

    async def test_circuit_opens_and_expires_to_recovery_probe(self):
        fetcher = self.fetcher(circuit_threshold=2, circuit_cooldown=0.05)
        await fetcher.run({"bad": f"{self.base}/error"})
        await fetcher.run({"bad": f"{self.base}/error"})
        opened = await fetcher.run({"bad": f"{self.base}/ok"})
        self.assertEqual(opened["sources"]["bad"]["status"], "circuit_open")
        await asyncio.sleep(0.06)
        recovered = await fetcher.run({"bad": f"{self.base}/ok"})
        self.assertEqual(recovered["sources"]["bad"]["status"], "ok")


class ReadOnlyTempdirTests(unittest.IsolatedAsyncioTestCase):
    """Task #297: no writable tempdir must not crash the collector before HTTP."""

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        Handler.calls = {}

    def test_module_import_does_not_touch_tempfile(self):
        # Regression guard for the baseline crash: DEFAULT_CIRCUIT_STATE used to
        # be a module-level constant built from tempfile.gettempdir(), which
        # raised FileNotFoundError before main() ever ran. Constructing the
        # module's public surface must not reference a resolved default path.
        self.assertFalse(hasattr(crypto_analysis_fetch, "DEFAULT_CIRCUIT_STATE"))

    def test_gettempdir_failure_falls_back_to_stateless_breaker(self):
        with mock.patch("tempfile.gettempdir", side_effect=FileNotFoundError("no /tmp")):
            fetcher = DeadlineFetcher(mode="quick_check", circuit_state=USE_DEFAULT_CIRCUIT_STATE)
        self.assertTrue(fetcher.breaker.stateless)
        self.assertIsNone(fetcher.breaker.path)

    def test_circuit_breaker_save_failure_downgrades_to_stateless(self):
        unwritable = Path("/proc/does-not-exist/circuit.json")
        breaker = CircuitBreaker(unwritable)
        self.assertFalse(breaker.stateless)  # load failure alone does not flip it
        breaker.failure("some-source")  # triggers _save(), which must fail silently
        self.assertTrue(breaker.stateless)

    async def test_run_completes_and_warns_when_tempdir_unavailable(self):
        with mock.patch("tempfile.gettempdir", side_effect=FileNotFoundError("no /tmp")):
            fetcher = DeadlineFetcher(mode="quick_check", circuit_state=USE_DEFAULT_CIRCUIT_STATE)
            result = await fetcher.run({"a": f"{self.base}/ok"})
        self.assertFalse(result["partial"])
        self.assertEqual(result["cost"]["calls"], 1)
        self.assertIn(
            "circuit_breaker_stateless: no writable path for circuit state; "
            "tracking failures in memory only for this run",
            result["warnings"],
        )

    async def test_run_completes_when_state_dir_is_read_only(self):
        # integration: simulate a read-only filesystem by pointing circuit_state
        # at a path whose parent directory cannot be created/written to.
        readonly_path = Path("/proc/self/cannot-create-here/circuit.json")
        fetcher = DeadlineFetcher(mode="quick_check", circuit_state=readonly_path)
        result = await fetcher.run({"a": f"{self.base}/ok", "b": f"{self.base}/error"})
        self.assertEqual(result["cost"]["calls"], 2)
        self.assertEqual(set(result["missing_sources"]), {"b"})
        self.assertTrue(fetcher.breaker.stateless)
        self.assertIn("circuit_breaker_stateless: no writable path for circuit state; "
                       "tracking failures in memory only for this run", result["warnings"])

    async def test_reference_parity_stateless_vs_writable_output_shape(self):
        # Same sources/mode, only circuit-state persistence differs: JSON schema
        # and outcome per source must match (contract in task #297 section 2).
        with tempfile.TemporaryDirectory() as tempdir:
            writable_state = Path(tempdir) / "circuit.json"
            writable_result = await DeadlineFetcher(mode="quick_check", circuit_state=writable_state).run(
                {"a": f"{self.base}/ok", "b": f"{self.base}/other"}
            )
        stateless_result = await DeadlineFetcher(mode="quick_check", circuit_state=None).run(
            {"a": f"{self.base}/ok", "b": f"{self.base}/other"}
        )
        for result in (writable_result, stateless_result):
            self.assertEqual(
                set(result.keys()),
                {
                    "mode", "partial", "deadline_seconds", "per_source_timeout_seconds",
                    "elapsed_ms", "sources", "missing_sources", "stale_sources",
                    "timed_out_sources", "warnings", "cost",
                },
            )
            self.assertEqual(set(result["cost"].keys()), {
                "calls", "payload_bytes", "source_time_ms", "timeouts",
                "saved_payload_bytes", "saved_payload_note",
            })
        self.assertEqual(writable_result["partial"], stateless_result["partial"])
        self.assertEqual(writable_result["missing_sources"], stateless_result["missing_sources"])
        self.assertEqual(writable_result["cost"]["calls"], stateless_result["cost"]["calls"])
        self.assertEqual(writable_result["warnings"], [])
        self.assertIn("circuit_breaker_stateless", stateless_result["warnings"][0])

    async def test_source_time_alignment_unaffected_by_stateless_breaker(self):
        # time-alignment: per-source elapsed_ms/source_time_ms must still reflect
        # actual request duration when the breaker has no disk to write to.
        fetcher = DeadlineFetcher(mode="quick_check", circuit_state=None, source_timeout=2)
        result = await fetcher.run({"slow": f"{self.base}/slow", "fast": f"{self.base}/ok"})
        self.assertGreaterEqual(result["sources"]["slow"]["elapsed_ms"], 900)
        self.assertLess(result["sources"]["fast"]["elapsed_ms"], 900)
        self.assertEqual(result["cost"]["source_time_ms"]["slow"], result["sources"]["slow"]["elapsed_ms"])
        self.assertEqual(result["cost"]["source_time_ms"]["fast"], result["sources"]["fast"]["elapsed_ms"])

    def test_cli_main_runs_without_writable_tempdir(self):
        # end-to-end AC check: reproduce the baseline crash directly -
        # tempfile.gettempdir() raising FileNotFoundError because no candidate
        # directory is writable (its actual failure mode; see tempfile
        # source's _get_default_tempdir) - and confirm `main()` still starts,
        # queries the source over HTTP, and returns schema-compatible JSON
        # instead of crashing before any request is made.
        import io
        import contextlib

        argv = [
            "crypto_analysis_fetch.py",
            "--mode",
            "quick_check",
            "--source",
            f"a={self.base}/ok",
        ]
        stdout = io.StringIO()
        with mock.patch.object(sys, "argv", argv), \
             mock.patch("tempfile.gettempdir", side_effect=FileNotFoundError("no usable tempdir")), \
             contextlib.redirect_stdout(stdout):
            exit_code = crypto_analysis_fetch.main()
        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["cost"]["calls"], 1)
        self.assertFalse(payload["partial"])
        self.assertIn("circuit_breaker_stateless", payload["warnings"][0])


if __name__ == "__main__":
    unittest.main()
