import sys
import signal
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from evolving_harness.chat import chat_loop, interactive_input
from evolving_harness.cli import execute, parser
from evolving_harness.memory import Memory
from evolving_harness.store import Store


class ChatTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.store = Store(self.path / "state.sqlite3")
        self.command = [sys.executable, "-u", str(Path(__file__).with_name("fake_pi.py"))]
        self.lines = []

    async def asyncTearDown(self):
        self.store.close()
        self.temp.cleanup()

    async def chat(self, commands, **kwargs):
        inputs = iter(commands)
        with patch("evolving_harness.chat.pi_command", return_value=self.command.copy()):
            return await chat_loop(self.store, "alice", "repo", pi_root="unused", workspace=str(self.path),
                                   read_line=lambda _: next(inputs), write_line=self.lines.append, **kwargs)

    async def test_turns_reuse_one_pi_session_and_persist_each_task(self):
        result = await self.chat(["fixture:set apple", "fixture:recall", "/exit"])
        self.assertEqual(result["turns"], 2)
        self.assertIn("Agent> apple", self.lines)
        sessions = self.store.events("alice", "repo", "pi_session")
        self.assertEqual(len(sessions), 2)
        self.assertEqual(sessions[0]["data"]["session_id"], sessions[1]["data"]["session_id"])
        self.assertNotEqual(sessions[0]["task_id"], sessions[1]["task_id"])

    async def test_new_resets_context_but_preserves_explicit_memory(self):
        await self.chat(["/remember use unittest", "fixture:set apple", "/new", "fixture:recall", "/exit"])
        self.assertIn("Agent> no previous context", self.lines)
        self.assertEqual(Memory(self.store).list("alice", "repo")[0]["content"], "use unittest")
        sessions = self.store.events("alice", "repo", "pi_session")
        self.assertNotEqual(sessions[0]["data"]["session_id"], sessions[1]["data"]["session_id"])

    async def test_remember_applies_to_next_turn_and_commands_are_not_model_prompts(self):
        result = await self.chat(["/help", "/remember use unittest", "/memories", "/versions", "analyze", "/exit"])
        self.assertEqual(result["turns"], 1)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 1)
        context = self.store.events("alice", "repo", "context_assembled")[0]
        self.assertEqual(len(context["data"]["memory_ids"]), 1)

    async def test_forget_removes_memory_and_resets_current_conversation(self):
        commands = iter(["/remember secret convention", "fixture:set apple", "/forget", "fixture:recall", "/exit"])

        def reader(_):
            command = next(commands)
            if command == "/forget":
                return "/forget " + Memory(self.store).list("alice", "repo")[0]["id"]
            return command

        with patch("evolving_harness.chat.pi_command", return_value=self.command.copy()):
            await chat_loop(self.store, "alice", "repo", pi_root="unused", workspace=str(self.path),
                            read_line=reader, write_line=self.lines.append)
        self.assertEqual(Memory(self.store).list("alice", "repo"), [])
        self.assertIn("Agent> no previous context", self.lines)

    async def test_eof_and_unknown_command(self):
        def reader(_):
            raise EOFError()

        with patch("evolving_harness.chat.pi_command", return_value=self.command.copy()):
            result = await chat_loop(self.store, "alice", "repo", pi_root="unused", workspace=str(self.path),
                                     read_line=reader, write_line=self.lines.append)
        self.assertEqual(result["turns"], 0)

    async def test_keyboard_interrupt_while_waiting_exits_cleanly(self):
        def reader(_):
            raise KeyboardInterrupt()

        with patch("evolving_harness.chat.pi_command", return_value=self.command.copy()):
            result = await chat_loop(self.store, "alice", "repo", pi_root="unused", workspace=str(self.path),
                                     read_line=reader, write_line=self.lines.append)
        self.assertEqual(result["turns"], 0)
        result = await self.chat(["/not-a-command", "/exit"])
        self.assertEqual(result["turns"], 0)

    async def test_per_command_errors_do_not_exit_the_chat(self):
        result = await self.chat(["/forget missing", "/feedback unknown note", "/evolve bad", "/exit"])
        self.assertEqual(result["turns"], 0)
        self.assertEqual(sum(line.startswith("错误：") for line in self.lines), 3)

    async def test_resume_uses_scoped_session_directory(self):
        args = parser().parse_args(["--db", str(self.path / "db.sqlite3"), "--user", "alice",
                                    "--project", "repo", "chat", "--workspace", str(self.path), "--resume"])
        fake_chat = AsyncMock(return_value={"turns": 0})
        with patch("evolving_harness.cli.chat_loop", fake_chat):
            await execute(args)
            first = fake_chat.call_args.kwargs["session_directory"]
            args.user = "bob"
            await execute(args)
            second = fake_chat.call_args.kwargs["session_directory"]
        self.assertNotEqual(first, second)
        self.assertTrue(fake_chat.call_args.kwargs["resume"])


class ConsoleTests(unittest.TestCase):
    def test_input_restores_signal_handler_on_interrupt(self):
        original = signal.getsignal(signal.SIGINT)
        with patch("builtins.input", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                interactive_input("test> ")
        self.assertIs(signal.getsignal(signal.SIGINT), original)
