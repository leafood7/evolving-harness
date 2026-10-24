from __future__ import annotations

import json
import tempfile
from typing import Protocol

from .domain import DEFECT_PATCHES, compile_improver, validate_payload
from .evaluation import evaluate, propose_from_rules
from .memory import Memory
from .rpc import PiRpc, pi_command
from .store import Store


class Proposer(Protocol):
    name: str

    async def propose(self, target: dict, improver: dict, evidence: list[dict]) -> dict: ...


class ReplayProposer:
    """Deterministic demonstration adapter, NOT an autonomous LLM learner."""

    name = "offline-replay"

    async def propose(self, target: dict, improver: dict, evidence: list[dict]) -> dict:
        defects = sorted({event["data"]["defect"] for event in evidence if event["kind"] == "feedback"})
        if target["kind"] == "memory_policy":
            return propose_from_rules(improver["payload"], target["payload"], defects)
        if target["kind"] == "improver":
            existing = {rule["defect"]: rule for rule in target["payload"]["rules"]}
            for defect in defects:
                existing[defect] = {"defect": defect, "patch": DEFECT_PATCHES[defect]}
            return compile_improver(list(existing.values()))
        raise ValueError("Replay proposer supports memory policies and the improvement skill")


class PiProposer:
    name = "pi-rpc"

    def __init__(self, pi_root: str, timeout: float = 120, provider: str | None = None,
                 model: str | None = None):
        self.pi_root, self.timeout, self.provider, self.model = pi_root, timeout, provider, model

    async def propose(self, target: dict, improver: dict, evidence: list[dict]) -> dict:
        context = {
            "active_improvement_skill": improver["payload"],
            "target": {"name": target["name"], "kind": target["kind"], "payload": target["payload"]},
            "training_evidence": [{"id": e["seq"], "kind": e["kind"], "data": e["data"]} for e in evidence],
            "allowed_defect_patches": DEFECT_PATCHES,
            "schema": "Return only JSON: {\"payload\": <complete updated target payload>}. "
                      "Preserve payload keys/types. Never change evaluator or ownership constraints. "
                      "An improver has instructions:string and rules:[{defect,patch}]. "
                      "Only supported rules in allowed_defect_patches are valid. "
                      "A memory policy has verified_only:boolean, exclude_expired:boolean, "
                      "deduplicate:boolean, top_k:integer 1..20.",
        }
        # Learner receives training evidence only. No tools, project files, evaluation cases or labels.
        prompt = improver["payload"]["instructions"] + "\n\n" + json.dumps(context, ensure_ascii=False)
        with tempfile.TemporaryDirectory(prefix="harness-proposer-") as temporary:
            command = pi_command(self.pi_root, tools=False, provider=self.provider, model=self.model)
            async with PiRpc(command, temporary, self.timeout) as rpc:
                result = await rpc.prompt(prompt)
        try:
            response = json.loads(result["text"])
        except json.JSONDecodeError as exc:
            raise ValueError("Pi proposal must be a JSON object without Markdown fences") from exc
        if not isinstance(response, dict) or set(response) != {"payload"}:
            raise ValueError("Pi proposal requires exactly one payload field")
        payload = response["payload"]
        if target["kind"] == "improver":
            if not isinstance(payload, dict) or set(payload) != {"instructions", "rules"}:
                raise ValueError("Improvement proposal requires instructions and rules")
            if not isinstance(payload["rules"], list):
                raise ValueError("Improvement rules must be an array")
            # Only evaluated rules become operative instructions. Model-authored prose is not promoted.
            try:
                payload = compile_improver(payload["rules"])
            except (KeyError, TypeError) as exc:
                raise ValueError("Invalid improvement rule structure") from exc
        return payload


def feedback(store: Store, owner: str, project: str, defect: str, note: str,
             *, task_id: str | None = None, memory_id: str | None = None) -> int:
    if defect not in DEFECT_PATCHES or not note.strip():
        raise ValueError("Feedback requires a supported defect and a nonempty explanation")
    if task_id:
        store.get_task(owner, project, task_id)
    if memory_id:
        Memory(store).get(owner, project, memory_id)
    return store.event(owner, project, "feedback",
                       {"defect": defect, "note": note, "memory_id": memory_id,
                        "source": "explicit-user-feedback"}, task_id)


class Evolution:
    def __init__(self, store: Store, owner: str, project: str, proposer: Proposer):
        self.store, self.owner, self.project, self.proposer = store, owner, project, proposer

    async def run(self, target_name: str, *, apply: bool = False) -> dict:
        target = self.store.current(self.owner, self.project, target_name)
        improver = self.store.current(self.owner, self.project, "improve")
        if target["kind"] not in {"memory_policy", "improver"}:
            raise ValueError("v0.1 can evaluate memory policies and the improvement skill only")
        # Do not expose independent evaluation labels, details, or scores to the proposer.
        evidence = [event for event in self.store.events(self.owner, self.project)
                    if event["kind"] in {"feedback", "improvement_attempt"}][-100:]
        if not any(event["kind"] == "feedback" for event in evidence):
            raise ValueError("No explicit training feedback; refusing unsupported improvement")
        payload = await self.proposer.propose(target, improver, evidence)
        validate_payload(target["kind"], payload)
        candidate = self.store.create_artifact(
            self.owner, self.project, target["name"], target["kind"], payload,
            parent=target["id"], generated_by=improver["id"], evidence=[e["seq"] for e in evidence],
            backend=self.proposer.name)
        suite_hash, report = evaluate(target, candidate)
        evaluation_id = self.store.save_evaluation(self.owner, self.project, candidate, suite_hash, report)
        self.store.event(self.owner, self.project, "improvement_attempt", {
            "target": target_name, "candidate": candidate["id"], "generated_by": improver["id"],
            "feedback_defects": sorted({e["data"]["defect"] for e in evidence if e["kind"] == "feedback"}),
            "outcome": report["decision"], "backend": self.proposer.name,
        })
        applied = False
        if apply and report["decision"] == "accept":
            self.store.promote(self.owner, self.project, candidate["id"], evaluation_id)
            applied = True
        return {"candidate_id": candidate["id"], "version": candidate["version"],
                "parent": target["id"], "generated_by": improver["id"], "evaluation_id": evaluation_id,
                "decision": report["decision"], "applied": applied, "report": report}

    async def cycle(self, *, rounds: int = 2, apply: bool = False) -> dict:
        """Bounded outer loop; accepted improver versions govern subsequent proposals."""
        if not 1 <= rounds <= 5:
            raise ValueError("Recursive cycle rounds must be in [1, 5]")
        audit = Memory(self.store).audit(self.owner, self.project)
        history = []
        for index in range(rounds):
            changes = []
            for target in ("memory-policy", "improve"):
                changes.append({"target": target, **await self.run(target, apply=apply)})
            if changes[-1]["applied"]:
                changes.append({"target": "memory-policy", **await self.run("memory-policy", apply=apply)})
            history.append({"round": index + 1, "changes": changes})
            if not any(change["applied"] for change in changes):
                break
        return {"rounds": history, "stop_reason": "round_limit" if len(history) == rounds
                and any(c["applied"] for c in history[-1]["changes"]) else "no_accepted_changes",
                "backend": self.proposer.name, "memory_audit": audit}
