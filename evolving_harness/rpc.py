from __future__ import annotations

import asyncio
import contextlib
import json
import os
from collections import deque
from pathlib import Path
from typing import Callable

from .domain import uid
from .node import resolve_node


class RpcError(RuntimeError):
    pass


def pi_command(root: str | Path, *, tools: bool = True, provider: str | None = None,
               model: str | None = None) -> list[str]:
    root = Path(root).resolve()
    node = resolve_node()
    entry = root / "packages/coding-agent/dist/bundle/cli.js"
    if not entry.is_file():
        raise RpcError(f"Pi build not found: {entry}")
    # Explicit resources only; do not load arbitrary personal/project extension code.
    command = [node, str(entry), "--mode", "rpc", "--offline", "--no-session", "--no-extensions", "--no-mcp",
               "--no-skills", "--no-prompt-templates", "--no-themes", "--no-context-files", "--no-approve"]
    if not tools:
        command.append("--no-tools")
    if provider:
        command.extend(["--provider", provider])
    if model:
        command.extend(["--model", model])
    return command


class PiRpc:
    """One active prompt, concurrent ID-correlated commands, continuously drained pipes."""

    def __init__(self, command: list[str], cwd: str | Path, timeout: float = 120,
                 on_event: Callable[[dict], None] | None = None, env: dict | None = None):
        self.command, self.cwd, self.timeout = command, str(cwd), timeout
        self.on_event, self.env = on_event, env
        self.process: asyncio.subprocess.Process | None = None
        self.pending: dict[str, asyncio.Future] = {}
        self.listeners: list[asyncio.Queue] = []
        self.stderr = deque(maxlen=30)
        self.readers = []
        self.failure: BaseException | None = None
        self.prompt_lock = asyncio.Lock()

    async def __aenter__(self):
        self.process = await asyncio.create_subprocess_exec(
            *self.command, cwd=self.cwd, env=self.env or os.environ.copy(),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, limit=16 * 1024 * 1024)
        self.readers = [asyncio.create_task(self._stdout()), asyncio.create_task(self._stderr())]
        try:
            await self.request("get_state")
        except BaseException:
            await self.__aexit__(None, None, None)
            raise
        return self

    async def _stderr(self):
        assert self.process and self.process.stderr
        while line := await self.process.stderr.readline():
            self.stderr.append(line.decode("utf-8", errors="replace").rstrip())

    def _fail(self, error: BaseException):
        self.failure = error
        for future in self.pending.values():
            if not future.done():
                future.set_exception(error)
        for listener in self.listeners:
            listener.put_nowait(error)

    async def _stdout(self):
        assert self.process and self.process.stdout
        try:
            while line := await self.process.stdout.readline():
                record = json.loads(line)
                if not isinstance(record, dict):
                    raise RpcError("Pi emitted a non-object protocol record")
                if record.get("type") == "response":
                    future = self.pending.get(record.get("id"))
                    if future and not future.done():
                        future.set_result(record)
                else:
                    if self.on_event:
                        self.on_event(record)
                    for listener in self.listeners:
                        listener.put_nowait(record)
            self._fail(RpcError("Pi stdout closed; inspect local diagnostics"))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._fail(RpcError(f"Invalid Pi event stream: {exc}"))

    async def request(self, kind: str, **fields) -> dict:
        if self.failure:
            raise self.failure
        assert self.process and self.process.stdin
        request_id = uid("rpc")
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            command = {"id": request_id, "type": kind, **fields}
            self.process.stdin.write(json.dumps(command, ensure_ascii=False).encode("utf-8") + b"\n")
            await asyncio.wait_for(self.process.stdin.drain(), self.timeout)
            response = await asyncio.wait_for(future, self.timeout)
            if response.get("success") is not True:
                raise RpcError(str(response.get("error", f"{kind} failed")))
            return response.get("data") or {}
        finally:
            self.pending.pop(request_id, None)

    async def prompt(self, message: str) -> dict:
        async with self.prompt_lock:
            queue: asyncio.Queue = asyncio.Queue()
            self.listeners.append(queue)  # subscribe before submitting: settle may precede response
            final_message = None
            deltas: dict[int, str] = {}
            try:
                async with asyncio.timeout(self.timeout):
                    acceptance = await self.request("prompt", message=message)
                    if acceptance.get("disposition") == "handled":
                        return {"text": "", "handled": True, "message": None}
                    while True:
                        event = await queue.get()
                        if isinstance(event, BaseException):
                            raise event
                        if event.get("type") == "message_update":
                            update = event.get("assistantMessageEvent", {})
                            index = update.get("contentIndex", 0)
                            if update.get("type") == "text_delta":
                                deltas[index] = deltas.get(index, "") + update.get("delta", "")
                        elif event.get("type") == "message_end":
                            message_record = event.get("message", {})
                            if message_record.get("role") == "assistant":
                                final_message = message_record
                                deltas = {}  # final message replaces partial text, also across turns
                        elif event.get("type") == "agent_settled":
                            if event.get("aborted"):
                                raise RpcError("Pi run aborted")
                            if final_message is None:
                                raise RpcError("Pi settled without an authoritative assistant message")
                            if final_message.get("stopReason") in {"error", "aborted", "length"}:
                                raise RpcError(final_message.get("errorMessage") or
                                               f"Pi stopped: {final_message['stopReason']}")
                            text = "".join(block.get("text", "") for block in final_message.get("content", [])
                                           if block.get("type") == "text")
                            return {"text": text, "message": final_message, "handled": False}
            except (TimeoutError, asyncio.CancelledError):
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(self.request("abort"), 3)
                raise
            finally:
                self.listeners.remove(queue)

    async def __aexit__(self, *_):
        if self.process:
            if self.process.stdin:
                with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                    self.process.stdin.close()
            try:
                await asyncio.wait_for(self.process.wait(), 3)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    self.process.terminate()
                await self.process.wait()
        for reader in self.readers:
            reader.cancel()
        if self.readers:
            await asyncio.gather(*self.readers, return_exceptions=True)
