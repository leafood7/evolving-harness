from __future__ import annotations

import re
from datetime import datetime, timezone

from .domain import digest, now, uid
from .store import Store


def normalized(text: str) -> str:
    return " ".join(text.casefold().split())


def expired(value: str | None, at: datetime) -> bool:
    if not value:
        return False
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp <= at


def select_memories(memories: list[dict], policy: dict, *, owner: str, project: str,
                    task_id: str | None = None, query: str = "",
                    at: datetime | None = None) -> list[dict]:
    """Scopes are invariant; the optimizer cannot relax ownership or scope checks."""
    at = at or datetime.now(timezone.utc)
    terms = set(re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]", query.casefold()))
    eligible = []
    seen = set()
    for memory in memories:
        if memory["owner"] != owner or memory["status"] != "active":
            continue
        if memory["scope"] != "user" and memory["project"] != project:
            continue
        if memory["scope"] == "task" and (not task_id or memory["task_id"] != task_id):
            continue
        if policy["verified_only"] and not memory["verified"]:
            continue
        if policy["exclude_expired"] and expired(memory.get("expires"), at):
            continue
        key = normalized(memory["content"])
        if policy["deduplicate"] and key in seen:
            continue
        seen.add(key)
        relevance = sum(term in memory["content"].casefold() for term in terms)
        eligible.append((relevance, memory))
    eligible.sort(key=lambda item: -item[0])  # stable: earlier canonical record wins ties
    return [memory for _, memory in eligible[:policy["top_k"]]]


class Memory:
    def __init__(self, store: Store):
        self.store = store

    def add(self, owner: str, project: str, content: str, source: str, *, scope: str = "project",
            verified: bool = False, expires: str | None = None, task_id: str | None = None) -> str:
        if scope not in {"user", "project", "task"} or not content.strip() or not source.strip():
            raise ValueError("Memory requires valid scope, nonempty content and source")
        if scope == "task":
            if not task_id:
                raise ValueError("Task-scoped memory requires task_id")
            self.store.get_task(owner, project, task_id)
        if expires:
            expired(expires, datetime.now(timezone.utc))  # validate before persistence
        memory_id = uid("memory")
        with self.store.db:
            self.store.db.execute("INSERT INTO memories VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                                  (memory_id, owner, None if scope == "user" else project, scope,
                                   task_id if scope == "task" else None, content, source, int(verified),
                                   expires, "active", 1, now()))
        self.store.event(owner, project, "memory_added", {"memory_id": memory_id, "scope": scope})
        return memory_id

    def list(self, owner: str, project: str, *, include_inactive: bool = False) -> list[dict]:
        sql = "SELECT * FROM memories WHERE owner=? AND (project=? OR scope='user')"
        if not include_inactive:
            sql += " AND status='active'"
        return [dict(row) for row in self.store.db.execute(sql + " ORDER BY rowid", (owner, project))]

    def get(self, owner: str, project: str, memory_id: str) -> dict:
        return next((m for m in self.list(owner, project, include_inactive=True) if m["id"] == memory_id),
                    None) or self._missing()

    @staticmethod
    def _missing():
        raise ValueError("Memory not found in this scope")

    def edit(self, owner: str, project: str, memory_id: str, content: str, source: str, verified: bool):
        memory = self.get(owner, project, memory_id)
        if not content.strip() or not source.strip():
            raise ValueError("Content and correction source are required")
        with self.store.db:
            self.store.db.execute(
                "UPDATE memories SET content=?,source=?,verified=?,revision=revision+1,updated=? WHERE id=?",
                (content, source, int(verified), now(), memory_id))
        self.store.event(owner, project, "memory_corrected",
                         {"memory_id": memory_id, "previous_revision": memory["revision"]})

    def forget(self, owner: str, project: str, memory_id: str):
        self.get(owner, project, memory_id)
        with self.store.db:
            self.store.db.execute("DELETE FROM memories WHERE id=?", (memory_id,))
        self.store.event(owner, project, "memory_forgotten", {"memory_id": memory_id})

    def retrieve(self, owner: str, project: str, query: str, task_id: str | None = None) -> list[dict]:
        policy = self.store.current(owner, project, "memory-policy")
        memories = self.list(owner, project)
        selected = select_memories(memories, policy["payload"], owner=owner, project=project,
                                    task_id=task_id, query=query)
        self.store.event(owner, project, "memory_retrieved", {
            "policy_id": policy["id"], "policy_version": policy["version"],
            "considered_ids": [m["id"] for m in memories], "selected_ids": [m["id"] for m in selected],
            "selected_facts": [{"id": m["id"], "revision": m["revision"], "verified": bool(m["verified"]),
                                "expires": m["expires"], "content_hash": digest(normalized(m["content"]))}
                               for m in selected],
        }, task_id)
        return selected

    def audit(self, owner: str, project: str) -> dict:
        """Detect metadata defects using retrieval-time facts, not today's mutable memories.

        This cannot establish semantic truth or infer causal contribution to task success.
        """
        events = self.store.events(owner, project)
        processed = {(e["data"].get("trace_seq"), e["data"].get("defect")) for e in events
                     if e["kind"] == "feedback" and e["data"].get("source") == "memory-audit"}
        findings = []
        inspected = 0
        for event in events:
            if event["kind"] != "memory_retrieved":
                continue
            inspected += 1
            facts = event["data"].get("selected_facts", [])
            at = datetime.fromisoformat(event["created"])
            unverified = [m["id"] for m in facts if not m["verified"]]
            stale = [m["id"] for m in facts if expired(m["expires"], at)]
            hashes, duplicates = set(), []
            for memory in facts:
                if memory["content_hash"] in hashes:
                    duplicates.append(memory["id"])
                hashes.add(memory["content_hash"])
            for defect, ids in [("unverified", unverified), ("expired", stale), ("duplicate", duplicates)]:
                if ids and (event["seq"], defect) not in processed:
                    seq = self.store.event(owner, project, "feedback", {
                        "defect": defect, "note": f"Retrieval-time metadata audit: {defect}",
                        "memory_ids": ids, "trace_seq": event["seq"], "source": "memory-audit",
                    }, event["task_id"])
                    findings.append({"event": seq, "trace_seq": event["seq"], "defect": defect})
        return {"inspected_traces": inspected, "new_findings": findings,
                "limitation": "Metadata checks only; no semantic correctness or causal quality claim."}
