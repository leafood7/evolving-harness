from __future__ import annotations

import json
import signal
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from .bootstrap import bootstrap
from .evolution import Evolution, PiProposer, feedback
from .memory import Memory
from .rpc import PiRpc, RpcError, pi_command
from .runtime import redact, run_task
from .store import Store


HELP = """直接输入消息并回车，连续对话；当前运行期间共享同一个 Pi 会话。
/help                     显示帮助
/new                      开始新对话，保留长期记忆
/remember 内容            保存为当前项目的明确记忆
/memories                 查看当前范围的记忆
/forget 记忆ID            删除记忆并开始新对话，移除当前上下文中的旧记忆
/feedback 类型 说明       标记 unverified / expired / duplicate 缺陷
/audit                    审计已有记忆注入轨迹
/versions                 查看当前能力版本
/evolve [apply]           真实模型提出并评测改进；apply 启用通过门槛的版本
/rollback 名称            回滚 memory-policy 或 improve
/exit                     保存记录并退出（也支持 /quit）
普通聊天不会自动生成永久记忆；需要保存的约定请用 /remember。
"""


def interactive_input(prompt: str) -> str:
    # asyncio.Runner's SIGINT handler only cancels a task; it cannot interrupt blocking input.
    # Use the ordinary console handler while waiting, and restore async cancellation afterwards.
    previous = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        return input(prompt)
    finally:
        signal.signal(signal.SIGINT, previous)


async def chat_loop(store: Store, owner: str, project: str, *, pi_root: str, workspace: str,
                    timeout: float = 180, allow_write: bool = False, provider: str | None = None,
                    model: str | None = None, session_directory: Path | None = None,
                    resume: bool = False, read_line: Callable[[str], str] = interactive_input,
                    write_line: Callable[[str], None] = print) -> dict:
    workspace_path = Path(workspace).resolve()
    if not workspace_path.is_dir():
        raise ValueError("Workspace must be an existing directory")
    bootstrap(store, owner, project)
    memory = Memory(store)
    command = pi_command(pi_root, tools=True, provider=provider, model=model)
    command.extend(["--tools", "read,bash,edit,write,grep,find,ls" if allow_write else "read,grep,find,ls"])
    if session_directory:
        session_directory.mkdir(parents=True, exist_ok=True)
        command.remove("--no-session")
        command.extend(["--session-dir", str(session_directory.resolve())])
        if resume:
            command.append("--continue")
    elif resume:
        raise ValueError("Resume requires a scoped session directory")

    def progress(event: dict):
        if event.get("type") == "tool_execution_start":
            write_line("  [工具] " + str(redact(event.get("toolName", "unknown"))))

    completed = 0
    last_task = None
    write_line("正在连接 Pi…")
    async with PiRpc(command, workspace_path, timeout, progress) as rpc:
        state = await rpc.request("get_state")
        selected = state.get("model") or {}
        endpoint = urlsplit(selected.get("baseUrl") or "").hostname or "未配置"
        write_line(f"Evolving Harness | 用户 {owner} | 项目 {project}")
        write_line(f"模型：{selected.get('provider', '?')}/{selected.get('id', '?')} ({endpoint})")
        write_line(f"工作区：{workspace_path} | {'允许修改' if allow_write else '只读工具'}")
        write_line("输入消息开始对话；/help 查看命令；/exit 退出。")
        while True:
            try:
                # Synchronous input only while idle; no active generation is blocked by it.
                line = read_line("你> ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not line:
                continue
            if line in {"/exit", "/quit"}:
                break
            try:
                if line.startswith("/"):
                    verb, _, argument = line.partition(" ")
                    argument = argument.strip()
                    if verb == "/help":
                        write_line(HELP)
                    elif verb == "/new":
                        result = await rpc.request("new_session")
                        if result.get("cancelled"):
                            raise RpcError("Pi canceled session replacement")
                        last_task = None
                        write_line("已开始新对话。长期记忆保留。")
                    elif verb == "/remember":
                        memory_id = memory.add(owner, project, argument, "explicit-chat-user", verified=True)
                        write_line(f"已保存项目记忆：{memory_id}")
                    elif verb == "/memories":
                        memories = memory.list(owner, project)
                        write_line("\n".join(f"{m['id']} [{m['scope']}] {m['content']}" for m in memories)
                                   or "当前没有长期记忆。")
                    elif verb == "/forget":
                        memory.forget(owner, project, argument)
                        result = await rpc.request("new_session")
                        if result.get("cancelled"):
                            raise RpcError("Memory deleted but Pi did not reset; exit before continuing")
                        last_task = None
                        write_line("记忆已删除，并已开始新对话以移除当前旧上下文。历史记录未擦除。")
                    elif verb == "/feedback":
                        defect, _, note = argument.partition(" ")
                        seq = feedback(store, owner, project, defect, note, task_id=last_task)
                        write_line(f"反馈已保存：{seq}")
                    elif verb == "/audit":
                        write_line(json.dumps(memory.audit(owner, project), ensure_ascii=False, indent=2))
                    elif verb == "/versions":
                        for name in ("coding", "memory-policy", "improve"):
                            artifact = store.current(owner, project, name)
                            write_line(f"{name} v{artifact['version']} {artifact['id']}")
                    elif verb == "/evolve":
                        if argument not in {"", "apply"}:
                            raise ValueError("用法：/evolve 或 /evolve apply")
                        write_line("正在使用模型复盘并评测候选改进…")
                        engine = Evolution(store, owner, project,
                                           PiProposer(pi_root, timeout, provider, model))
                        report = await engine.cycle(rounds=1, apply=argument == "apply")
                        for round_result in report["rounds"]:
                            for change in round_result["changes"]:
                                write_line(f"{change['target']} v{change['version']}: "
                                           f"{change['decision']}, applied={change['applied']}")
                    elif verb == "/rollback":
                        if argument not in {"memory-policy", "improve"}:
                            raise ValueError("用法：/rollback memory-policy 或 /rollback improve")
                        artifact = store.rollback(owner, project, argument)
                        write_line(f"已回滚：{argument} v{artifact['version']}")
                    else:
                        write_line("未知命令；输入 /help 查看。")
                    continue

                write_line("Agent 正在处理…")
                result = await run_task(store, owner, project, line, pi_root=pi_root,
                                        workspace=str(workspace_path), timeout=timeout, allow_write=allow_write,
                                        provider=provider, model=model, rpc_client=rpc)
                last_task = result["task_id"]
                completed += 1
                write_line("Agent> " + result["text"])
                write_line(f"[任务 {last_task}]")
            except (ValueError, RpcError, OSError, TimeoutError) as exc:
                write_line("错误：" + str(redact(str(exc))))
                if rpc.failure:
                    write_line("Pi 连接已关闭，请重新启动 chat；可使用 --resume。")
                    break
    write_line("对话已结束，任务轨迹和长期记忆已保存。")
    return {"turns": completed, "last_task_id": last_task}
