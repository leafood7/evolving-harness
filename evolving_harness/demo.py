from __future__ import annotations

import json
from pathlib import Path

from .bootstrap import bootstrap
from .evolution import Evolution, ReplayProposer, feedback
from .memory import Memory
from .store import Store


async def run_demo(directory: Path) -> dict:
    directory.mkdir(parents=True, exist_ok=True)
    database = directory / "demo.sqlite3"
    if database.exists():
        raise ValueError("Demo directory already contains a run; choose a new --output directory")
    store = Store(database)
    try:
        owner, project = "demo-user", "demo-project"
        bootstrap(store, owner, project, demo=True)
        memory = Memory(store)
        good = memory.add(owner, project, "Use pytest for project tests", "explicit-user", verified=True)
        guess = memory.add(owner, project, "Assume npm test works", "agent-inference", verified=False)
        old = memory.add(owner, project, "Use retired test runner", "old-readme", verified=True, expires="2020-01-01")
        foreign = memory.add(owner, "other-project", "Use a foreign command", "other-project-user", verified=True)
        before = memory.retrieve(owner, project, "project tests")
        feedback(store, owner, project, "unverified", "An unverified test command was injected", memory_id=guess)
        feedback(store, owner, project, "expired", "A retired test runner was injected", memory_id=old)
        engine = Evolution(store, owner, project, ReplayProposer())
        rounds = []
        # v1 has no defect attribution rules; its unchanged proposal must be rejected.
        rounds.append({"stage": "policy proposal by improve v1", **await engine.run("memory-policy", apply=True)})
        # Self-targeting: improve v1 generates improve v2; this is recorded in generated_by.
        rounds.append({"stage": "improve v1 updates itself", **await engine.run("improve", apply=True)})
        improved = store.current(owner, project, "improve")
        rounds.append({"stage": "improve v2 updates memory policy", **await engine.run("memory-policy", apply=True)})
        after = memory.retrieve(owner, project, "project tests")
        duplicate = memory.add(owner, project, " use PYTEST for project tests ", "explicit-user", verified=True)
        memory.retrieve(owner, project, "project tests")
        feedback(store, owner, project, "duplicate", "Repeated equivalent memory wastes context", memory_id=duplicate)
        # The accepted v2, rather than the original seed, now generates its own successor.
        rounds.append({"stage": "improve v2 updates itself", **await engine.run("improve", apply=True)})
        rounds.append({"stage": "improve v3 updates memory policy", **await engine.run("memory-policy", apply=True)})
        final = memory.retrieve(owner, project, "project tests")
        artifacts = store.history(owner, project)
        chain = [{"id": a["id"], "name": a["name"], "version": a["version"],
                  "parent": a["parent"], "generated_by": a["generated_by"], "backend": a["backend"]}
                 for a in artifacts]
        assert rounds[0]["decision"] == "reject" and not rounds[0]["applied"]
        assert all(r["applied"] for r in rounds[1:])
        assert rounds[2]["generated_by"] == improved["id"]
        assert rounds[3]["generated_by"] == improved["id"]
        assert [m["id"] for m in final] == [good]
        assert foreign not in [m["id"] for m in before]
        report = {
            "mode": "offline-replay", "claim": "Recursive lifecycle and memory behavior demonstration only",
            "limitation": "Scripted proposals and a small fixed regression suite; no real model or coding gains claimed.",
            "memory_selection": {"before": [m["id"] for m in before],
                                 "after_verification_and_expiry": [m["id"] for m in after],
                                 "after_deduplication": [m["id"] for m in final]},
            "rounds": rounds, "lineage": chain,
            "events": len(store.events(owner, project)),
        }
        (directory / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        lines = ["# 递归自进化演示", "", "运行模式：offline-replay（确定性脚本，无模型调用）。", "",
                 "演示证明版本、评测、作用域与递归链路能工作，不证明真实模型获得能力提升。", "",
                 "| 阶段 | 结果 | 基线分数 | 候选分数 | 已启用 |", "|---|---|---:|---:|---|"]
        for item in rounds:
            lines.append(f"| {item['stage']} | {item['decision']} | {item['report']['baseline']['score']:.3f} | "
                         f"{item['report']['candidate']['score']:.3f} | {item['applied']} |")
        lines += ["", "递归链：improve v1 → improve v2 → improve v3；每个已启用版本都实际参与后续提案。",
                  "", f"记忆注入数量：{len(before)} → {len(after)} → {len(final)}。其他项目的记忆从始至终不可见。",
                  "", "完整来源关系、评测明细与事件保存在 report.json 和 demo.sqlite3。"]
        (directory / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        return {"output": str(directory.resolve()), "rounds": len(rounds), "events": report["events"],
                "recursive_versions": 3, "mode": "offline-replay"}
    finally:
        store.close()
