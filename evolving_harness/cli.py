from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from .bootstrap import bootstrap
from .chat import chat_loop
from .domain import digest
from .demo import run_demo
from .evolution import Evolution, PiProposer, ReplayProposer, feedback
from .memory import Memory
from .rpc import PiRpc, RpcError, pi_command
from .runtime import run_task
from .store import Store


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Pi-backed user-centered recursive evolution harness")
    p.add_argument("--db", default=".harness/state.sqlite3")
    p.add_argument("--user", default="local")
    p.add_argument("--project", default="default")
    p.add_argument("--pi-root", default=os.environ.get("EH_PI_ROOT", r"D:\VScodeProgects\pi"))
    p.add_argument("--timeout", type=float, default=180)
    p.add_argument("--provider")
    p.add_argument("--model")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="Create scoped store and seed skills")
    sub.add_parser("doctor", help="Check real local Pi RPC, without invoking a model")
    demo = sub.add_parser("demo", help="Deterministic offline recursive lifecycle demo")
    demo.add_argument("--output", required=True, type=Path)
    run = sub.add_parser("run", help="Execute a real task through Pi RPC")
    run.add_argument("prompt")
    run.add_argument("--workspace", required=True)
    run.add_argument("--allow-write", action="store_true", help="Enable shell/edit/write; this is not an OS sandbox")
    chat = sub.add_parser("chat", help="Interactive conversation, scoped memory and evolution commands")
    chat.add_argument("--workspace", default=".")
    chat.add_argument("--allow-write", action="store_true")
    chat.add_argument("--resume", action="store_true", help="Resume latest chat in this user/project/workspace")
    native = sub.add_parser("native", help="Open the native Pi TUI with the Harness extension")
    native.add_argument("--workspace", default=".")
    native.add_argument("--allow-write", action="store_true")
    native.add_argument("--resume", action="store_true")
    native.add_argument("--with-user-extensions", action="store_true",
                        help="Also load discovered/configured Pi extensions (default: Harness extension only)")
    native.add_argument("--thinking", choices=["off", "minimal", "low", "medium", "high", "xhigh"],
                        default="medium", help="Pi reasoning level (default: medium)")
    mem = sub.add_parser("memory")
    ms = mem.add_subparsers(dest="memory_command", required=True)
    add = ms.add_parser("add")
    add.add_argument("content")
    add.add_argument("--source", required=True)
    add.add_argument("--scope", choices=["user", "project", "task"], default="project")
    add.add_argument("--task")
    add.add_argument("--verified", action="store_true")
    add.add_argument("--expires")
    ms.add_parser("list")
    search = ms.add_parser("retrieve")
    search.add_argument("query")
    search.add_argument("--task")
    edit = ms.add_parser("edit")
    edit.add_argument("id")
    edit.add_argument("content")
    edit.add_argument("--source", required=True)
    edit.add_argument("--verified", action="store_true")
    forget = ms.add_parser("forget")
    forget.add_argument("id")
    fb = sub.add_parser("feedback")
    fb.add_argument("--defect", required=True, choices=["unverified", "expired", "duplicate"])
    fb.add_argument("--note", required=True)
    fb.add_argument("--task")
    fb.add_argument("--memory")
    evolve = sub.add_parser("evolve")
    evolve.add_argument("--target", choices=["memory-policy", "improve"], required=True)
    evolve.add_argument("--backend", choices=["pi", "replay"], default="pi")
    evolve.add_argument("--apply", action="store_true", help="Activate only if the independent gate accepts")
    cycle = sub.add_parser("cycle", help="Bounded recursive improvement of policies and the improver itself")
    cycle.add_argument("--backend", choices=["pi", "replay"], default="pi")
    cycle.add_argument("--rounds", type=int, default=2)
    cycle.add_argument("--budget-seconds", type=float, default=300)
    cycle.add_argument("--apply", action="store_true")
    promote = sub.add_parser("promote")
    promote.add_argument("candidate")
    promote.add_argument("evaluation")
    rollback = sub.add_parser("rollback")
    rollback.add_argument("name", choices=["memory-policy", "improve"])
    sub.add_parser("versions")
    sub.add_parser("audit", help="Find unverified, expired and duplicate injections from recorded snapshots")
    trace = sub.add_parser("trace")
    trace.add_argument("--task")
    return p


async def execute(args) -> object:
    if args.timeout <= 0:
        raise ValueError("timeout must be positive")
    if args.command == "demo":
        return await run_demo(args.output)
    if args.command == "doctor":
        package_path = Path(args.pi_root) / "packages/coding-agent/package.json"
        package = json.loads(package_path.read_text(encoding="utf-8"))
        command = pi_command(args.pi_root, tools=False)
        with tempfile.TemporaryDirectory(prefix="harness-doctor-") as temporary:
            async with PiRpc(command, temporary, min(args.timeout, 45)) as rpc:
                state = await rpc.request("get_state")
        selected = state.get("model") or {}
        selected_model = {"provider": selected.get("provider"), "id": selected.get("id"),
                          "endpoint_host": urlsplit(selected.get("baseUrl") or "").hostname}
        return {"pi_root": str(Path(args.pi_root).resolve()), "pi_version": package["version"],
                "rpc": "connected", "model_invoked": False, "is_streaming": state.get("isStreaming"),
                "python": sys.version.split()[0], "node_executable": command[0], "selected_model": selected_model}
    store = Store(args.db)
    try:
        if args.command == "chat":
            scope = digest({"user": args.user, "project": args.project,
                            "workspace": str(Path(args.workspace).resolve())})[:24]
            session_directory = Path(args.db).resolve().parent / "pi-sessions" / scope
            return await chat_loop(store, args.user, args.project, pi_root=args.pi_root,
                                   workspace=args.workspace, timeout=args.timeout, allow_write=args.allow_write,
                                   provider=args.provider, model=args.model, session_directory=session_directory,
                                   resume=args.resume)
        if args.command == "init":
            return {name: {"id": a["id"], "version": a["version"]}
                    for name, a in bootstrap(store, args.user, args.project).items()}
        memory = Memory(store)
        if args.command == "audit":
            return memory.audit(args.user, args.project)
        if args.command == "memory":
            match args.memory_command:
                case "add":
                    return {"id": memory.add(args.user, args.project, args.content, args.source,
                            scope=args.scope, verified=args.verified, expires=args.expires, task_id=args.task)}
                case "list":
                    return memory.list(args.user, args.project)
                case "retrieve":
                    return memory.retrieve(args.user, args.project, args.query, args.task)
                case "edit":
                    memory.edit(args.user, args.project, args.id, args.content, args.source, args.verified)
                    return {"updated": args.id}
                case "forget":
                    memory.forget(args.user, args.project, args.id)
                    return {"forgotten": args.id}
        if args.command == "feedback":
            return {"event": feedback(store, args.user, args.project, args.defect, args.note,
                                       task_id=args.task, memory_id=args.memory)}
        if args.command in {"evolve", "cycle"}:
            proposer = ReplayProposer() if args.backend == "replay" else PiProposer(
                args.pi_root, args.timeout, args.provider, args.model)
            engine = Evolution(store, args.user, args.project, proposer)
            if args.command == "cycle":
                if args.budget_seconds <= 0:
                    raise ValueError("budget-seconds must be positive")
                async with asyncio.timeout(args.budget_seconds):
                    return await engine.cycle(rounds=args.rounds, apply=args.apply)
            return await engine.run(args.target, apply=args.apply)
        if args.command == "run":
            return await run_task(store, args.user, args.project, args.prompt, pi_root=args.pi_root,
                                  workspace=args.workspace, timeout=args.timeout, allow_write=args.allow_write,
                                  provider=args.provider, model=args.model)
        if args.command == "versions":
            active = {a["name"]: store.current(args.user, args.project, a["name"])["id"]
                      for a in store.history(args.user, args.project)}
            return [{"name": a["name"], "version": a["version"], "id": a["id"],
                     "parent": a["parent"], "generated_by": a["generated_by"],
                     "active": active[a["name"]] == a["id"], "backend": a["backend"]}
                    for a in store.history(args.user, args.project)]
        if args.command == "promote":
            store.promote(args.user, args.project, args.candidate, args.evaluation)
            return {"promoted": args.candidate}
        if args.command == "rollback":
            return store.rollback(args.user, args.project, args.name)
        if args.command == "trace":
            events = store.events(args.user, args.project)
            return [e for e in events if not args.task or e["task_id"] == args.task]
        raise ValueError("Unknown command")
    finally:
        store.close()


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    arguments = sys.argv[1:] if argv is None else argv
    args = parser().parse_args(arguments or ["chat"])
    try:
        if args.command == "native":
            from .native import launch
            return launch(args)
        result = asyncio.run(execute(args))
    except (ValueError, RpcError, OSError, TimeoutError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Cancelled", file=sys.stderr)
        return 130
    if args.command != "chat":
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0
