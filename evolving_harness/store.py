from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .domain import canonical, digest, now, uid, validate_payload


class Store:
    """Local single-owner store. Scope predicates are not a multi-user auth system."""

    def __init__(self, path: str | Path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS tasks (
          id TEXT PRIMARY KEY, owner TEXT NOT NULL, project TEXT NOT NULL,
          prompt TEXT NOT NULL, state TEXT NOT NULL, result TEXT, created TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS events (
          seq INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT NOT NULL, project TEXT NOT NULL,
          task_id TEXT, kind TEXT NOT NULL, data TEXT NOT NULL, created TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS memories (
          id TEXT PRIMARY KEY, owner TEXT NOT NULL, project TEXT, scope TEXT NOT NULL,
          task_id TEXT, content TEXT NOT NULL, source TEXT NOT NULL, verified INTEGER NOT NULL,
          expires TEXT, status TEXT NOT NULL, revision INTEGER NOT NULL, updated TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS artifacts (
          id TEXT PRIMARY KEY, owner TEXT NOT NULL, project TEXT NOT NULL, name TEXT NOT NULL,
          kind TEXT NOT NULL, version INTEGER NOT NULL, parent TEXT REFERENCES artifacts(id),
          generated_by TEXT REFERENCES artifacts(id), payload TEXT NOT NULL, hash TEXT NOT NULL,
          evidence TEXT NOT NULL, backend TEXT NOT NULL, created TEXT NOT NULL,
          UNIQUE(owner, project, name, version)
        );
        CREATE TABLE IF NOT EXISTS active (
          owner TEXT NOT NULL, project TEXT NOT NULL, name TEXT NOT NULL,
          artifact_id TEXT NOT NULL REFERENCES artifacts(id), PRIMARY KEY(owner, project, name)
        );
        CREATE TABLE IF NOT EXISTS evaluations (
          id TEXT PRIMARY KEY, owner TEXT NOT NULL, project TEXT NOT NULL,
          candidate TEXT NOT NULL REFERENCES artifacts(id), baseline TEXT NOT NULL,
          candidate_hash TEXT NOT NULL, suite_hash TEXT NOT NULL, report TEXT NOT NULL,
          created TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS event_scope ON events(owner, project, seq);
        CREATE INDEX IF NOT EXISTS memory_scope ON memories(owner, project, status);
        """)

    def close(self):
        self.db.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def event(self, owner: str, project: str, kind: str, data: dict, task_id: str | None = None):
        with self.db:
            cursor = self.db.execute(
                "INSERT INTO events(owner,project,task_id,kind,data,created) VALUES(?,?,?,?,?,?)",
                (owner, project, task_id, kind, canonical(data), now()),
            )
        return cursor.lastrowid

    def events(self, owner: str, project: str, kind: str | None = None) -> list[dict]:
        sql = "SELECT * FROM events WHERE owner=? AND project=?"
        args = [owner, project]
        if kind:
            sql += " AND kind=?"
            args.append(kind)
        rows = self.db.execute(sql + " ORDER BY seq", args).fetchall()
        return [dict(row) | {"data": json.loads(row["data"])} for row in rows]

    def task(self, owner: str, project: str, prompt: str) -> str:
        task_id = uid("task")
        with self.db:
            self.db.execute("INSERT INTO tasks VALUES(?,?,?,?,?,?,?)",
                            (task_id, owner, project, prompt, "running", None, now()))
        return task_id

    def finish_task(self, task_id: str, state: str, result: str):
        with self.db:
            self.db.execute("UPDATE tasks SET state=?,result=? WHERE id=?", (state, result, task_id))

    def get_task(self, owner: str, project: str, task_id: str) -> dict:
        row = self.db.execute("SELECT * FROM tasks WHERE id=? AND owner=? AND project=?",
                              (task_id, owner, project)).fetchone()
        if row is None:
            raise ValueError("Task not found in this scope")
        return dict(row)

    def artifact(self, owner: str, project: str, artifact_id: str) -> dict:
        row = self.db.execute("SELECT * FROM artifacts WHERE id=? AND owner=? AND project=?",
                              (artifact_id, owner, project)).fetchone()
        if row is None:
            raise ValueError("Artifact not found in this scope")
        return dict(row) | {"payload": json.loads(row["payload"]), "evidence": json.loads(row["evidence"])}

    def current(self, owner: str, project: str, name: str) -> dict:
        row = self.db.execute("SELECT artifact_id FROM active WHERE owner=? AND project=? AND name=?",
                              (owner, project, name)).fetchone()
        if row is None:
            raise ValueError(f"No active {name}; run init first")
        return self.artifact(owner, project, row[0])

    def create_artifact(self, owner: str, project: str, name: str, kind: str, payload: dict,
                        *, parent: str | None = None, generated_by: str | None = None,
                        evidence: list[int] | None = None, backend: str = "seed") -> dict:
        validate_payload(kind, payload)
        if parent:
            base = self.artifact(owner, project, parent)
            if (base["name"], base["kind"]) != (name, kind):
                raise ValueError("Candidate must belong to the same artifact lineage")
        if generated_by:
            generator = self.artifact(owner, project, generated_by)
            if generator["kind"] != "improver":
                raise ValueError("Generator must be an improvement skill")
        for seq in evidence or []:
            if not self.db.execute("SELECT 1 FROM events WHERE seq=? AND owner=? AND project=?",
                                   (seq, owner, project)).fetchone():
                raise ValueError("Evidence belongs to another scope or does not exist")
        artifact_id = uid("artifact")
        with self.transaction():
            version = self.db.execute(
                "SELECT COALESCE(MAX(version),0)+1 FROM artifacts WHERE owner=? AND project=? AND name=?",
                (owner, project, name)).fetchone()[0]
            self.db.execute("INSERT INTO artifacts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (artifact_id, owner, project, name, kind, version, parent, generated_by,
                             canonical(payload), digest(payload), canonical(evidence or []), backend, now()))
            if parent is None:
                self.db.execute("INSERT OR IGNORE INTO active VALUES(?,?,?,?)",
                                (owner, project, name, artifact_id))
        return self.artifact(owner, project, artifact_id)

    def save_evaluation(self, owner: str, project: str, candidate: dict, suite_hash: str, report: dict) -> str:
        evaluation_id = uid("eval")
        with self.db:
            self.db.execute("INSERT INTO evaluations VALUES(?,?,?,?,?,?,?,?,?)",
                            (evaluation_id, owner, project, candidate["id"], candidate["parent"],
                             candidate["hash"], suite_hash, canonical(report), now()))
        return evaluation_id

    def promote(self, owner: str, project: str, candidate_id: str, evaluation_id: str):
        candidate = self.artifact(owner, project, candidate_id)
        with self.transaction():
            row = self.db.execute("SELECT * FROM evaluations WHERE id=? AND owner=? AND project=?",
                                  (evaluation_id, owner, project)).fetchone()
            if row is None or row["candidate"] != candidate_id or row["candidate_hash"] != candidate["hash"]:
                raise ValueError("Evaluation does not match candidate")
            report = json.loads(row["report"])
            if report.get("decision") != "accept":
                raise ValueError("Candidate did not pass the independent evaluation gate")
            changed = self.db.execute(
                "UPDATE active SET artifact_id=? WHERE owner=? AND project=? AND name=? AND artifact_id=?",
                (candidate_id, owner, project, candidate["name"], candidate["parent"])).rowcount
            if changed != 1:
                raise ValueError("Active baseline changed; reevaluate against the current version")
        self.event(owner, project, "artifact_promoted", {"candidate": candidate_id, "evaluation": evaluation_id})

    def rollback(self, owner: str, project: str, name: str):
        with self.transaction():
            active = self.current(owner, project, name)
            if not active["parent"]:
                raise ValueError("Seed version has no parent to roll back to")
            self.db.execute("UPDATE active SET artifact_id=? WHERE owner=? AND project=? AND name=?",
                            (active["parent"], owner, project, name))
        self.event(owner, project, "artifact_rolled_back", {"from": active["id"], "to": active["parent"]})
        return self.current(owner, project, name)

    def history(self, owner: str, project: str) -> list[dict]:
        rows = self.db.execute("SELECT id FROM artifacts WHERE owner=? AND project=? ORDER BY created",
                               (owner, project)).fetchall()
        return [self.artifact(owner, project, row[0]) for row in rows]
