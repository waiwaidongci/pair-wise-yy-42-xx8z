from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import (ASSIGNMENT_STATUSES, RESOURCE_KINDS, RESOURCE_STATUSES,
                     ConflictError, NotFoundError)
from .rules import ID_PREFIX, STATES, TERMINAL_STATES


class Repository:
    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._create_schema()

    def _create_schema(self) -> None:
        statuses = ",".join("'" + s.replace("'", "''") + "'" for s in STATES)
        resource_kinds = ",".join("'" + k + "'" for k in RESOURCE_KINDS)
        resource_statuses = ",".join("'" + s + "'" for s in RESOURCE_STATUSES)
        assignment_statuses = ",".join("'" + s + "'" for s in ASSIGNMENT_STATUSES)
        with self.conn:
            self.conn.executescript(f"""
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    quantity REAL NOT NULL DEFAULT 0,
                    threshold REAL NOT NULL DEFAULT 1,
                    status TEXT NOT NULL CHECK(status IN ({statuses})),
                    version INTEGER NOT NULL DEFAULT 1,
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_items_external_ref
                    ON items(external_ref) WHERE external_ref IS NOT NULL;
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ('open','closed')),
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, external_ref)
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER NOT NULL,
                    actor TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    entry_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS resources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL UNIQUE,
                    kind TEXT NOT NULL CHECK(kind IN ({resource_kinds})),
                    type TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'available'
                        CHECK(status IN ({resource_statuses})),
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS assignments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_ref TEXT NOT NULL UNIQUE,
                    resource_id INTEGER NOT NULL REFERENCES resources(id),
                    item_id INTEGER NOT NULL REFERENCES items(id),
                    status TEXT NOT NULL DEFAULT 'active'
                        CHECK(status IN ({assignment_statuses})),
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    released_at TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_assignments_active_resource
                    ON assignments(resource_id) WHERE status='active';
            """)

    @staticmethod
    def _item(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    def create_item(self, title: str, description: str, severity: str,
                    quantity: float, threshold: float, external_ref: Optional[str],
                    actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO items(title, description, severity, quantity, threshold,
                       status, version, external_ref, created_by, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (title, description, severity, quantity, threshold, STATES[0], 1,
                     external_ref, actor, now, now),
                )
                item_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("external_ref已存在") from exc
        return self.get_item(item_id)

    def get_item(self, item_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise NotFoundError("项目不存在")
        return self._item(row)

    def list_items(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM items"
        params: tuple = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY id DESC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._item(row) for row in rows]

    def transition_item(self, item_id: int, target: str, expected_version: int,
                        actor: str) -> tuple:
        now = utc_now()
        released: List[Dict[str, Any]] = []
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE items SET status=?, version=version+1, updated_at=?
                   WHERE id=? AND version=?""",
                (target, now, item_id, expected_version),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("项目不存在")
                raise ConflictError("版本冲突，请刷新后重试")
            if target in TERMINAL_STATES:
                rows = self.conn.execute(
                    "SELECT * FROM assignments WHERE item_id=? AND status='active'",
                    (item_id,),
                ).fetchall()
                for row in rows:
                    self.conn.execute(
                        "UPDATE assignments SET status='released', released_at=? WHERE id=?",
                        (now, row["id"]),
                    )
                    self.conn.execute(
                        """UPDATE resources SET status='available', updated_at=?
                           WHERE id=? AND status='assigned'""",
                        (now, row["resource_id"]),
                    )
                    released.append(dict(row))
        return self.get_item(item_id), released

    def add_record(self, item_id: int, kind: str, detail: str, status: str,
                   external_ref: Optional[str], actor: str) -> Dict[str, Any]:
        now = utc_now()
        self.get_item(item_id)
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO records(item_id, kind, detail, status, external_ref,
                       created_by, created_at) VALUES(?,?,?,?,?,?,?)""",
                    (item_id, kind, detail, status, external_ref, actor, now),
                )
                record_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("记录唯一标识已存在") from exc
        with self._lock:
            row = self.conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        return dict(row)

    def list_records(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM records WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def open_record_count(self, item_id: int) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM records WHERE item_id=? AND status='open'",
                (item_id,),
            ).fetchone()
        return int(row["n"])

    def create_resource(self, code: str, kind: str, type_: str, status: str,
                        actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO resources(code, kind, type, status, created_by,
                       created_at, updated_at) VALUES(?,?,?,?,?,?,?)""",
                    (code, kind, type_, status, actor, now, now),
                )
                resource_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("资源编号已存在") from exc
        return self.get_resource(resource_id)

    def get_resource(self, resource_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM resources WHERE id=?", (resource_id,)).fetchone()
        if row is None:
            raise NotFoundError("资源不存在")
        return dict(row)

    def get_resource_by_code(self, code: str) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM resources WHERE code=?", (code,)).fetchone()
        if row is None:
            raise NotFoundError("资源不存在")
        return dict(row)

    def list_resources(self, kind: Optional[str] = None,
                       status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM resources"
        clauses, params = [], []
        if kind:
            clauses.append("kind=?"); params.append(kind)
        if status:
            clauses.append("status=?"); params.append(status)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, tuple(params)).fetchall()
        return [dict(row) for row in rows]

    def set_resource_status(self, resource_id: int, status: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                "UPDATE resources SET status=?, updated_at=? WHERE id=?",
                (status, now, resource_id),
            )
            if cur.rowcount == 0:
                raise NotFoundError("资源不存在")
        return self.get_resource(resource_id)

    def get_assignment(self, assignment_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM assignments WHERE id=?", (assignment_id,)).fetchone()
        if row is None:
            raise NotFoundError("分配不存在")
        return dict(row)

    def list_assignments(self, item_id: Optional[int] = None,
                         resource_id: Optional[int] = None,
                         status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM assignments"
        clauses, params = [], []
        if item_id is not None:
            clauses.append("item_id=?"); params.append(item_id)
        if resource_id is not None:
            clauses.append("resource_id=?"); params.append(resource_id)
        if status is not None:
            clauses.append("status=?"); params.append(status)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id DESC"
        with self._lock:
            rows = self.conn.execute(sql, tuple(params)).fetchall()
        return [dict(row) for row in rows]

    def active_assignment_for_resource(self, resource_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM assignments WHERE resource_id=? AND status='active'",
                (resource_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def assign_resource(self, request_ref: str, resource_id: int, item_id: int,
                        actor: str) -> tuple:
        """原子占用资源：请求编号幂等，同一资源只允许一个活跃分配。"""
        now = utc_now()
        with self._lock, self.conn:
            existing = self.conn.execute(
                "SELECT * FROM assignments WHERE request_ref=?", (request_ref,),
            ).fetchone()
            if existing is not None:
                existing = dict(existing)
                if existing["resource_id"] != resource_id or existing["item_id"] != item_id:
                    raise ConflictError("请求编号已用于其他分配")
                return existing, True
            item = self.conn.execute(
                "SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if item is None:
                raise NotFoundError("项目不存在")
            if item["status"] in TERMINAL_STATES:
                raise ConflictError("事件已办结，不能分配资源")
            resource = self.conn.execute(
                "SELECT * FROM resources WHERE id=?", (resource_id,)).fetchone()
            if resource is None:
                raise NotFoundError("资源不存在")
            if resource["status"] == "maintenance":
                raise ConflictError("资源停用中，无法分配")
            busy = self.conn.execute(
                "SELECT * FROM assignments WHERE resource_id=? AND status='active'",
                (resource_id,),
            ).fetchone()
            if busy is not None:
                busy = dict(busy)
                raise ConflictError("资源已被其他事件占用", details={
                    "resource_id": resource_id,
                    "resource_code": resource["code"],
                    "assignment_id": busy["id"],
                    "item_id": busy["item_id"],
                    "request_ref": busy["request_ref"],
                    "occupied_since": busy["created_at"],
                })
            try:
                cur = self.conn.execute(
                    """INSERT INTO assignments(request_ref, resource_id, item_id, status,
                       created_by, created_at) VALUES(?,?,?,?,?,?)""",
                    (request_ref, resource_id, item_id, "active", actor, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("资源已被占用或请求编号重复") from exc
            assignment_id = int(cur.lastrowid)
            self.conn.execute(
                "UPDATE resources SET status='assigned', updated_at=? WHERE id=?",
                (now, resource_id),
            )
            row = self.conn.execute(
                "SELECT * FROM assignments WHERE id=?", (assignment_id,)).fetchone()
            return dict(row), False

    def release_assignment(self, assignment_id: int, actor: str) -> tuple:
        now = utc_now()
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT * FROM assignments WHERE id=?", (assignment_id,)).fetchone()
            if row is None:
                raise NotFoundError("分配不存在")
            assignment = dict(row)
            if assignment["status"] == "released":
                return assignment, False
            self.conn.execute(
                "UPDATE assignments SET status='released', released_at=? WHERE id=?",
                (now, assignment_id),
            )
            self.conn.execute(
                """UPDATE resources SET status='available', updated_at=?
                   WHERE id=? AND status='assigned'""",
                (now, assignment["resource_id"]),
            )
            assignment["status"] = "released"
            assignment["released_at"] = now
            return assignment, True

    def append_audit(self, action: str, entity_type: str, entity_id: int,
                     actor: str, detail: dict) -> Dict[str, Any]:
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT entry_hash FROM audit_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
            previous = row["entry_hash"] if row else "GENESIS"
            event = make_entry(action, entity_type, entity_id, actor, detail, previous)
            cur = self.conn.execute(
                """INSERT INTO audit_events(action, entity_type, entity_id, actor, detail,
                   previous_hash, entry_hash, created_at) VALUES(?,?,?,?,?,?,?,?)""",
                (event["action"], event["entity_type"], event["entity_id"], event["actor"],
                 json.dumps(event["detail"], ensure_ascii=False, sort_keys=True),
                 event["previous_hash"], event["entry_hash"], event["created_at"]),
            )
            event_id = int(cur.lastrowid)
        event["id"] = event_id
        return event

    def list_audit(self, entity_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM audit_events"
        params: tuple = ()
        if entity_id is not None:
            sql += " WHERE entity_id=?"
            params = (entity_id,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item["detail"])
            result.append(item)
        return result

    def verify_audit_chain(self) -> bool:
        from .audit import calculate_hash
        with self._lock:
            rows = self.conn.execute("SELECT * FROM audit_events ORDER BY id").fetchall()
        previous = "GENESIS"
        for row in rows:
            if row["previous_hash"] != previous:
                return False
            payload = {
                "action": row["action"], "entity_type": row["entity_type"],
                "entity_id": row["entity_id"], "actor": row["actor"],
                "detail": json.loads(row["detail"]), "created_at": row["created_at"],
            }
            if calculate_hash(previous, payload) != row["entry_hash"]:
                return False
            previous = row["entry_hash"]
        return True

    def close(self) -> None:
        with self._lock:
            self.conn.close()
