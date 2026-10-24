from __future__ import annotations

from datetime import datetime

from .domain import digest
from .memory import select_memories


EVALUATOR_VERSION = "memory-behavior-v1"
EVALUATION_TIME = datetime.fromisoformat("2026-10-23T00:00:00+00:00")


def memory_record(memory_id: str, content: str, **overrides) -> dict:
    return {"id": memory_id, "content": content, "owner": "eval-user", "project": "eval-project",
            "scope": "project", "task_id": None, "status": "active", "verified": True,
            "expires": None, **overrides}


def heldout_cases() -> list[dict]:
    """Fixed behavioral cases. Not sent to the proposer; toy suite, not a coding benchmark."""
    m = memory_record
    return [
        {"id": "verified-a", "memories": [m("a1", "run checks"), m("a2", "skip checks", verified=False)], "expected": ["a1"]},
        {"id": "verified-b", "memories": [m("b1", "use a guess", verified=False), m("b2", "consult manifest")], "expected": ["b2"]},
        {"id": "expiry-a", "memories": [m("c1", "old setup", expires="2025-01-01"), m("c2", "current setup")], "expected": ["c2"]},
        {"id": "expiry-b", "memories": [m("d1", "future rule", expires="2099-01-01"), m("d2", "retired rule", expires="2020-01-01")], "expected": ["d1"]},
        {"id": "duplicate-a", "memories": [m("e1", "Use project tests"), m("e2", " use PROJECT tests ")], "expected": ["e1"]},
        {"id": "duplicate-b", "memories": [m("f1", "read config"), m("f2", "read   config"), m("f3", "check schema")], "expected": ["f1", "f3"]},
        {"id": "scope-a", "memories": [m("g1", "local rule"), m("g2", "foreign", owner="someone-else"), m("g3", "other project", project="elsewhere")], "expected": ["g1"]},
        {"id": "scope-b", "memories": [m("h1", "user preference", project=None, scope="user"), m("h2", "unrelated task", scope="task", task_id="another-task"), m("h3", "archived", status="archived")], "expected": ["h1"]},
    ]


def score_policy(policy: dict, cases: list[dict]) -> dict:
    tp = fp = fn = violations = 0
    details = []
    for case in cases:
        chosen = select_memories(case["memories"], policy, owner="eval-user", project="eval-project",
                                 task_id="eval-task", at=EVALUATION_TIME)
        actual, expected = {m["id"] for m in chosen}, set(case["expected"])
        tp += len(actual & expected)
        fp += len(actual - expected)
        fn += len(expected - actual)
        violations += sum(m["owner"] != "eval-user" or
                          (m["scope"] != "user" and m["project"] != "eval-project") for m in chosen)
        details.append({"case": case["id"], "selected": sorted(actual), "expected": sorted(expected)})
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {"precision": precision, "recall": recall, "score": f1, "violations": violations,
            "cases": len(cases), "details": details}


def propose_from_rules(improver: dict, policy: dict, defects: list[str]) -> dict:
    """Executable bounded part of an improvement skill, also used during live proposal."""
    result = dict(policy)
    for rule in improver["rules"]:
        if rule["defect"] in defects:
            result.update(rule["patch"])
    return result


def score_improver(payload: dict, cases: list[dict]) -> dict:
    # Independent contexts, different top_k values; the candidate never sees these labels.
    scenarios = [("unverified", "verified-"), ("expired", "expiry-"), ("duplicate", "duplicate-")]
    results = []
    for index, (defect, prefix) in enumerate(scenarios):
        base = {"verified_only": False, "exclude_expired": False, "deduplicate": False, "top_k": 8 + index}
        generated = propose_from_rules(payload, base, [defect])
        results.append(score_policy(generated, [c for c in cases if c["id"].startswith(prefix)]))
    scope_checks = score_policy({"verified_only": True, "exclude_expired": True,
                                 "deduplicate": True, "top_k": 10}, cases[-2:])
    return {"score": sum(r["score"] for r in results) / len(results),
            "recall": min(r["recall"] for r in results),
            "violations": scope_checks["violations"] + sum(r["violations"] for r in results),
            "cases": sum(r["cases"] for r in results) + scope_checks["cases"], "scenarios": results}


def evaluate(baseline: dict, candidate: dict) -> tuple[str, dict]:
    if baseline["kind"] != candidate["kind"]:
        raise ValueError("Cannot compare different artifact kinds")
    cases = heldout_cases()
    evaluator = {"memory_policy": score_policy, "improver": score_improver}.get(candidate["kind"])
    if not evaluator:
        raise ValueError("Task-skill promotion requires a real coding-task evaluator; not implemented in v0.1")
    before, after = evaluator(baseline["payload"], cases), evaluator(candidate["payload"], cases)
    accepted = (after["cases"] >= 4 and after["score"] > before["score"] + 0.02
                and after["recall"] >= before["recall"] and after["violations"] == 0)
    report = {"evaluator": EVALUATOR_VERSION, "evidence_level": "deterministic-memory-behavior",
              "baseline": before, "candidate": after, "decision": "accept" if accepted else "reject",
              "gate": {"min_gain": 0.02, "no_recall_regression": True, "scope_violations": 0},
              "limitation": "Small fixed regression suite; does not establish real coding or population-level gains."}
    return digest({"version": EVALUATOR_VERSION, "cases": cases, "gate": report["gate"]}), report
