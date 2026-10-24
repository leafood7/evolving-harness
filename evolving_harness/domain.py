from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from importlib.resources import files
from typing import Any
from uuid import uuid4


def uid(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:16]}"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


POLICY_KEYS = {"verified_only", "exclude_expired", "deduplicate", "top_k"}
DEFECT_PATCHES = {
    "unverified": {"verified_only": True},
    "expired": {"exclude_expired": True},
    "duplicate": {"deduplicate": True},
}


def compile_improver(rules: list[dict]) -> dict:
    """Render the executable rule vocabulary into the next generator's prompt.

    Free-form instruction changes cannot bypass a gate that only evaluates rules.
    """
    ordered = sorted(rules, key=lambda rule: rule["defect"])
    base = (files("evolving_harness") / "resources" / "improve.md").read_text(encoding="utf-8")
    return {"instructions": base + "\nActive attribution rules:\n" + canonical(ordered), "rules": ordered}


def validate_payload(kind: str, payload: dict) -> None:
    if not isinstance(payload, dict):
        raise ValueError("Artifact payload must be an object")
    if kind == "memory_policy":
        if set(payload) != POLICY_KEYS:
            raise ValueError(f"Memory policy requires exactly {sorted(POLICY_KEYS)}")
        for key in POLICY_KEYS - {"top_k"}:
            if type(payload[key]) is not bool:
                raise ValueError(f"{key} must be a boolean")
        if type(payload["top_k"]) is not int or not 1 <= payload["top_k"] <= 20:
            raise ValueError("top_k must be an integer in [1, 20]")
    elif kind == "improver":
        if set(payload) != {"instructions", "rules"}:
            raise ValueError("Improver requires instructions and rules")
        if not isinstance(payload["instructions"], str) or not payload["instructions"].strip():
            raise ValueError("Improver instructions cannot be empty")
        if not isinstance(payload["rules"], list):
            raise ValueError("Improver rules must be a list")
        seen = set()
        for rule in payload["rules"]:
            if not isinstance(rule, dict) or set(rule) != {"defect", "patch"}:
                raise ValueError("Each rule needs defect and patch")
            defect = rule["defect"]
            if not isinstance(defect, str) or defect not in DEFECT_PATCHES or defect in seen:
                raise ValueError("Unknown or duplicate defect rule")
            seen.add(defect)
            if canonical(rule["patch"]) != canonical(DEFECT_PATCHES[defect]):
                raise ValueError("Rule patch is outside the supported policy vocabulary")
        if payload != compile_improver(payload["rules"]):
            raise ValueError("v0.1 improvement instructions must be compiled from evaluated rules")
    elif kind == "task_skill":
        if set(payload) != {"instructions"} or not isinstance(payload["instructions"], str):
            raise ValueError("Task skill requires instructions")
        if not payload["instructions"].strip():
            raise ValueError("Task skill instructions cannot be empty")
    else:
        raise ValueError(f"Unsupported artifact kind: {kind}")
