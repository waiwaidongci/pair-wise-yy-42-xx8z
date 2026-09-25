from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import (ALLOCATION_STATUSES, RESOURCE_STATUSES, ConflictError,
                     NotFoundError)
from .rules import ID_PREFIX, STATES


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
        resource_statuses = ",".join("'" + s + "'" for s in RESOURCE_STATUSES)
        allocation_statuses = ",".join("'" + s + "'" for s in ALLOCATION_STATUSES)
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
                CREATE TABLE IF NOT EXISTS resources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL UNIQUE,
                    kind TEXT NOT NULL CHECK(kind IN ('person','vehicle')),
                    type TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'available'
                        CHECK(status IN ({resource_statuses})),
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS allocations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_no TEXT NOT NULL UNIQUE,
                    item_id INTEGER REFERENCES items(id),
                    note TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'active'
                        CHECK(status IN ({allocation_statuses})),
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    released_by TEXT,
                    released_at TEXT
                );
                CREATE TABLE IF NOT EXISTS allocation_resources (
                    allocation_id INTEGER NOT NULL REFERENCES allocations(id) ON DELETE CASCADE,
                    resource_id INTEGER NOT NULL REFERENCES resources(id),
                    resource_code TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    type TEXT NOT NULL,
                    PRIMARY KEY(allocation_id, resource_id)
                );
                CREATE TABLE IF NOT EXISTS resource_holds (
                    resource_id INTEGER PRIMARY KEY REFERENCES resources(id),
                    allocation_id INTEGER NOT NULL REFERENCES allocations(id),
                    created_at TEXT NOT NULL
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
                        actor: str) -> Dict[str, Any]:
        now = utc_now()
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
        return self.get_item(item_id)

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

    def create_resource(self, code: str, kind: str, rtype: str,
                        actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO resources(code, kind, type, status, created_by,
                       created_at, updated_at) VALUES(?,?,?,?,?,?,?)""",
                    (code, kind, rtype, "available", actor, now, now),
                )
                resource_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("资源编号已存在") from exc
        return self.get_resource(resource_id)

    def get_resource(self, resource_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM resources WHERE id=?", (resource_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError("资源不存在")
        return dict(row)

    def get_resource_by_code(self, code: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM resources WHERE code=?", (code,)
            ).fetchone()
        return dict(row) if row else None

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

    def hold_map(self, resource_ids: List[int]) -> Dict[int, Dict[str, Any]]:
        if not resource_ids:
            return {}
        marks = ",".join("?" for _ in resource_ids)
        with self._lock:
            rows = self.conn.execute(
                f"""SELECT h.resource_id AS resource_id, h.allocation_id AS allocation_id,
                           a.request_no AS request_no, a.item_id AS item_id,
                           a.created_by AS created_by, a.created_at AS created_at
                    FROM resource_holds h JOIN allocations a ON a.id=h.allocation_id
                    WHERE h.resource_id IN ({marks})""",
                tuple(resource_ids),
            ).fetchall()
        return {int(row["resource_id"]): dict(row) for row in rows}

    def find_allocation_by_request(self, request_no: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM allocations WHERE request_no=?", (request_no,)
            ).fetchone()
        return dict(row) if row else None

    def get_allocation(self, allocation_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM allocations WHERE id=?", (allocation_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError("分配单不存在")
        return dict(row)

    def allocation_resources(self, allocation_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                """SELECT resource_id, resource_code, kind, type
                   FROM allocation_resources WHERE allocation_id=? ORDER BY resource_id""",
                (allocation_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def allocate_resources(self, request_no: str, item_id: Optional[int], note: str,
                           resources: List[Dict[str, Any]],
                           actor: str) -> Dict[str, Any]:
        """单事务完成幂等检查、占用检查、写占用；冲突抛ConflictError(带占用详情)。"""
        now = utc_now()
        ids = [int(r["id"]) for r in resources]
        with self._lock, self.conn:
            existing = self.conn.execute(
                "SELECT * FROM allocations WHERE request_no=?", (request_no,)
            ).fetchone()
            if existing is not None:
                return {"replayed": True, "allocation": dict(existing)}
            marks = ",".join("?" for _ in ids)
            held = self.conn.execute(
                f"""SELECT r.id AS resource_id, r.code AS resource_code,
                           h.allocation_id AS allocation_id, a.request_no AS request_no,
                           a.item_id AS item_id, a.created_by AS created_by,
                           a.created_at AS created_at
                    FROM resources r
                    JOIN resource_holds h ON h.resource_id=r.id
                    JOIN allocations a ON a.id=h.allocation_id
                    WHERE r.id IN ({marks})""",
                tuple(ids),
            ).fetchall()
            if held:
                details = {"occupied": [dict(row) for row in held]}
                raise ConflictError("存在已被其他事件占用的资源", details)
            cur = self.conn.execute(
                """INSERT INTO allocations(request_no, item_id, note, status,
                   created_by, created_at) VALUES(?,?,?,?,?,?)""",
                (request_no, item_id, note, "active", actor, now),
            )
            allocation_id = int(cur.lastrowid)
            self.conn.executemany(
                """INSERT INTO allocation_resources(allocation_id, resource_id,
                   resource_code, kind, type) VALUES(?,?,?,?,?)""",
                [(allocation_id, r["id"], r["code"], r["kind"], r["type"])
                 for r in resources],
            )
            self.conn.executemany(
                "INSERT INTO resource_holds(resource_id, allocation_id, created_at) VALUES(?,?,?)",
                [(r["id"], allocation_id, now) for r in resources],
            )
            self.conn.executemany(
                "UPDATE resources SET status='occupied', updated_at=? WHERE id=?",
                [(now, r["id"]) for r in resources],
            )
            allocation = self.get_allocation(allocation_id)
        return {"replayed": False, "allocation": allocation}

    def release_allocation(self, allocation_id: int, actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            allocation = self.conn.execute(
                "SELECT * FROM allocations WHERE id=?", (allocation_id,)
            ).fetchone()
            if allocation is None:
                raise NotFoundError("分配单不存在")
            if allocation["status"] == "released":
                return {"replayed": True, "allocation": dict(allocation)}
            self.conn.execute(
                """UPDATE allocations SET status='released', released_by=?,
                   released_at=? WHERE id=? AND status='active'""",
                (actor, now, allocation_id),
            )
            self.conn.execute(
                "DELETE FROM resource_holds WHERE allocation_id=?",
                (allocation_id,),
            )
            self.conn.execute(
                """UPDATE resources SET status='available', updated_at=?
                   WHERE id IN (SELECT resource_id FROM allocation_resources
                                WHERE allocation_id=?)
                     AND id NOT IN (SELECT resource_id FROM resource_holds)""",
                (now, allocation_id),
            )
            allocation = self.get_allocation(allocation_id)
        return {"replayed": False, "allocation": allocation}

    def release_for_item(self, item_id: int, actor: str) -> List[int]:
        now = utc_now()
        released: List[int] = []
        with self._lock, self.conn:
            rows = self.conn.execute(
                "SELECT id FROM allocations WHERE item_id=? AND status='active'",
                (item_id,),
            ).fetchall()
            for row in rows:
                allocation_id = int(row["id"])
                released.append(allocation_id)
                self.conn.execute(
                    """UPDATE allocations SET status='released', released_by=?,
                       released_at=? WHERE id=?""",
                    (actor, now, allocation_id),
                )
                self.conn.execute(
                    "DELETE FROM resource_holds WHERE allocation_id=?",
                    (allocation_id,),
                )
                self.conn.execute(
                    """UPDATE resources SET status='available', updated_at=?
                       WHERE id IN (SELECT resource_id FROM allocation_resources
                                    WHERE allocation_id=?)
                         AND id NOT IN (SELECT resource_id FROM resource_holds)""",
                    (now, allocation_id),
                )
        return released

    def list_allocations(self, status: Optional[str] = None,
                         item_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM allocations"
        clauses, params = [], []
        if status:
            clauses.append("status=?"); params.append(status)
        if item_id is not None:
            clauses.append("item_id=?"); params.append(item_id)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, tuple(params)).fetchall()
        return [dict(row) for row in rows]

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
