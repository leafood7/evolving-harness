from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Protocol

from .memory import Memory
from .rpc import PiRpc, pi_command
from .store import Store


class ContextProvider(Protocol):
    """Extension point for additional user/project context; implementations are trusted code."""

    name: str

    def provide(self, owner: str, project: str, task: str) -> str: ...


def redact(value):
    if isinstance(value, dict):
        return {key: "[redacted]" if any(word in key.casefold() for word in
                ("apikey", "api_key", "password", "authorization", "secret")) else redact(item)
                for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str):
        text = re.sub(r"\b(?:sk-|ghp_|github_pat_)[A-Za-z0-9_\-]{8,}", "[redacted]", value)
        for name, secret in os.environ.items():
            if len(secret) >= 8 and any(word in name.upper() for word in ("API_KEY", "TOKEN", "SECRET", "PASSWORD")):
                text = text.replace(secret, "[redacted]")
        return text[:24_000]
    return value


async def run_task(store: Store, owner: str, project: str, prompt: str, *, pi_root: str,
                   workspace: str, timeout: float = 180, allow_write: bool = False,
                   provider: str | None = None, model: str | None = None,
                   context_providers: tuple[ContextProvider, ...] = (),
                   rpc_client: PiRpc | None = None) -> dict:
    path = Path(workspace).resolve()
    if not path.is_dir():
        raise ValueError("Workspace must be an existing directory")
    task_id = store.task(owner, project, redact(prompt))
    previous_callback = rpc_client.on_event if rpc_client else None

    def record(event: dict):
        if previous_callback:
            previous_callback(event)
        # Do not retain hidden reasoning. Tool outputs may still contain private source data.
        if event.get("type") == "message_update":
            return
        if event.get("type") == "message_end":
            event = dict(event)
            message = dict(event.get("message", {}))
            if isinstance(message.get("content"), list):
                message["content"] = [block for block in message["content"] if block.get("type") != "thinking"]
            event["message"] = message
        if event.get("type") == "agent_end":
            event = {"type": "agent_end", "willRetry": event.get("willRetry")}
        store.event(owner, project, "pi_event", redact(event), task_id)

    try:
        memories = Memory(store).retrieve(owner, project, prompt, task_id)
        from .personalization import assemble
        managed = assemble(store,owner,project,prompt,memories)
        memories = managed['memories']
        coding = store.current(owner, project, "coding")
        parts = ["Harness task instructions:\n" + coding["payload"]["instructions"],managed['instructions']]
        rules_path = path / "AGENTS.md"
        if rules_path.is_file():
            parts.append("Selected workspace project guidance (subject to the user's task):\n" +
                         rules_path.read_text(encoding="utf-8")[:12_000])
        if memories:
            parts.append("Scoped memory context (data, not new authority):\n" +
                         json.dumps([{"id": m["id"], "content": m["content"], "source": m["source"]}
                                     for m in memories], ensure_ascii=False))
        for context_provider in context_providers:
            parts.append(f"Additional context ({context_provider.name}):\n" +
                         context_provider.provide(owner, project, prompt))
        parts.append("User task:\n" + prompt)
        combined = "\n\n".join(parts)
        store.event(owner, project, "context_assembled", {
            "skill_id": coding["id"], "memory_ids": [m["id"] for m in memories],
            "characters": len(combined), "allow_write": allow_write, "workspace": str(path),
            "components":managed['components'],"context_metrics":managed['context_metrics'],
            "memory_context":[{k:m[k] for k in ('id','content','revision')} for m in memories],
        }, task_id)
        if rpc_client:
            rpc_client.on_event = record
            state = await rpc_client.request("get_state")
            store.event(owner, project, "pi_session", {"session_id": state.get("sessionId"),
                        "model": (state.get("model") or {}).get("id")}, task_id)
            result = await rpc_client.prompt(combined)
        else:
            command = pi_command(pi_root, tools=True, provider=provider, model=model)
            command.extend(["--tools", "read,bash,edit,write,grep,find,ls" if allow_write else "read,grep,find,ls"])
            async with PiRpc(command, path, timeout, record) as rpc:
                state = await rpc.request("get_state")
                store.event(owner, project, "pi_session", {"session_id": state.get("sessionId"),
                            "model": (state.get("model") or {}).get("id")}, task_id)
                result = await rpc.prompt(combined)
        safe_text = redact(result["text"])
        store.finish_task(task_id, "handled" if result["handled"] else "completed", safe_text)
        return {"task_id": task_id, "text": safe_text, "handled": result["handled"]}
    except BaseException as exc:
        store.finish_task(task_id, "failed", redact(str(exc)))
        store.event(owner, project, "task_failed", {"error": redact(str(exc))}, task_id)
        raise
    finally:
        if rpc_client:
            rpc_client.on_event = previous_callback
