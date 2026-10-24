import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from evolving_harness.bootstrap import bootstrap
from evolving_harness.evolution import Evolution, PiProposer, feedback
from evolving_harness.memory import Memory
from evolving_harness.runtime import redact, run_task
from evolving_harness.store import Store


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.store = Store(self.path / "state.sqlite3")
        bootstrap(self.store, "alice", "repo")
        self.command = [sys.executable, "-u", str(Path(__file__).with_name("fake_pi.py"))]

    async def asyncTearDown(self):
        self.store.close()
        self.temp.cleanup()

    async def test_pi_proposer_uses_active_self_updated_skill_over_rpc(self):
        feedback(self.store, "alice", "repo", "duplicate", "Repeated equivalent memory")
        with patch("evolving_harness.evolution.pi_command", return_value=self.command):
            engine = Evolution(self.store, "alice", "repo", PiProposer("unused", timeout=5))
            meta = await engine.run("improve", apply=True)
            self.assertTrue(meta["applied"])
            policy = await engine.run("memory-policy", apply=True)
            self.assertTrue(policy["applied"])
            self.assertEqual(policy["generated_by"], meta["candidate_id"])
            candidate = self.store.artifact("alice", "repo", policy["candidate_id"])
            self.assertEqual(candidate["backend"], "pi-rpc")

    async def test_task_context_and_authoritative_events_are_persisted(self):
        memory = Memory(self.store)
        selected = memory.add("alice", "repo", "Use unittest", "user", verified=True)
        memory.add("alice", "different", "Use something else", "user", verified=True)
        with patch("evolving_harness.runtime.pi_command", return_value=self.command):
            result = await run_task(self.store, "alice", "repo", "analyze", pi_root="unused",
                                    workspace=str(self.path), timeout=5)
        self.assertEqual(result["text"], "final\u2028answer")
        self.assertEqual(self.store.get_task("alice", "repo", result["task_id"])["state"], "completed")
        events = self.store.events("alice", "repo")
        assembled = next(e for e in events if e["kind"] == "context_assembled")
        self.assertEqual(assembled["data"]["memory_ids"], [selected])
        self.assertFalse(assembled["data"]["allow_write"])

    async def test_model_failure_marks_task_failed(self):
        async def fail_prompt(_self, _message):
            raise ValueError("model failed")

        with patch("evolving_harness.runtime.pi_command", return_value=self.command), \
                patch("evolving_harness.rpc.PiRpc.prompt", fail_prompt):
            with self.assertRaises(ValueError):
                await run_task(self.store, "alice", "repo", "test", pi_root="unused",
                               workspace=str(self.path), timeout=5)
        row = self.store.db.execute("SELECT state FROM tasks").fetchone()
        self.assertEqual(row[0], "failed")


class RedactionTests(unittest.TestCase):
    def test_known_key_shapes_and_environment_secret(self):
        with patch.dict("os.environ", {"EXAMPLE_API_KEY": "private-value-123"}):
            text = redact("sk-123456789abc private-value-123")
            self.assertNotIn("private-value", text)
            self.assertNotIn("123456789abc", text)
        self.assertEqual(redact({"api_key": "anything"})["api_key"], "[redacted]")
