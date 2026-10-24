import sys
import tempfile
import unittest
from pathlib import Path

from evolving_harness.rpc import PiRpc, RpcError


class RpcTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.command = [sys.executable, "-u", str(Path(__file__).with_name("fake_pi.py"))]

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_fast_settle_unicode_and_authoritative_message(self):
        events = []
        async with PiRpc(self.command, self.temp.name, 5, events.append) as rpc:
            result = await rpc.prompt("normal")
            self.assertEqual(result["text"], "final\u2028answer")
            self.assertTrue(any(e["type"] == "agent_settled" for e in events))
            state = await rpc.request("get_state")
            self.assertFalse(state["isStreaming"])

    async def test_handled_does_not_wait_for_a_run(self):
        async with PiRpc(self.command, self.temp.name, 5) as rpc:
            self.assertTrue((await rpc.prompt("handled"))["handled"])

    async def test_retry_error_not_mistaken_for_final_failure(self):
        async with PiRpc(self.command, self.temp.name, 5) as rpc:
            self.assertEqual((await rpc.prompt("retry"))["text"], "final\u2028answer")

    async def test_provider_error_and_abort_are_not_success(self):
        for mode in ("error", "abort", "reject"):
            with self.subTest(mode=mode):
                async with PiRpc(self.command, self.temp.name, 5) as rpc:
                    with self.assertRaises(RpcError):
                        await rpc.prompt(mode)

    async def test_exit_and_malformed_protocol_fail_without_hanging(self):
        for mode in ("exit", "malformed"):
            with self.subTest(mode=mode):
                async with PiRpc(self.command, self.temp.name, 5) as rpc:
                    with self.assertRaises(RpcError):
                        await rpc.prompt(mode)

    async def test_timeout_aborts_and_closes(self):
        async with PiRpc(self.command, self.temp.name, 2) as rpc:
            rpc.timeout = 0.15
            with self.assertRaises(TimeoutError):
                await rpc.prompt("stall")

    async def test_missing_executable_reports_startup_failure(self):
        with self.assertRaises(OSError):
            async with PiRpc(["missing-pi-executable-123456"], self.temp.name, 1):
                pass
