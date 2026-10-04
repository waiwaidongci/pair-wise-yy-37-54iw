from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError
from .rules import (ENTITY, EQUIPMENT_ENTITY, ID_PREFIX, IMPACT_EQUIPMENT,
                    IMPACT_INSPECTION, IMPACT_SUPPLEMENT, RENEWAL_ENTITY,
                    STATES, diff_materials, impact_disposition)


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
                CREATE TABLE IF NOT EXISTS equipment (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'running'
                        CHECK(status IN ('running','stopped')),
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(item_id, external_ref)
                );
                CREATE TABLE IF NOT EXISTS renewals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    application_no TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending','returned','approved')),
                    conclusion TEXT,
                    equipment_snapshot TEXT NOT NULL DEFAULT '[]',
                    rectification_snapshot TEXT NOT NULL DEFAULT '[]',
                    impacts TEXT NOT NULL DEFAULT '[]',
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(item_id, application_no)
                );
                CREATE INDEX IF NOT EXISTS ix_renewals_item ON renewals(item_id);
                CREATE TABLE IF NOT EXISTS renewal_materials (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    renewal_id INTEGER NOT NULL REFERENCES renewals(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(renewal_id, name)
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

    def _audit_tx(self, action: str, entity_type: str, entity_id: int,
                  actor: str, detail: dict) -> Dict[str, Any]:
        # 调用方必须已持有锁并处于事务中，审计与业务写入同事务提交
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
        event["id"] = int(cur.lastrowid)
        return event

    def append_audit(self, action: str, entity_type: str, entity_id: int,
                     actor: str, detail: dict) -> Dict[str, Any]:
        with self._lock, self.conn:
            return self._audit_tx(action, entity_type, entity_id, actor, detail)

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

    # ---- 设备台账 ----
    def add_equipment(self, item_id: int, name: str, status: str,
                      external_ref: Optional[str], actor: str) -> Dict[str, Any]:
        now = utc_now()
        self.get_item(item_id)
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO equipment(item_id, name, status, external_ref,
                       created_by, created_at, updated_at) VALUES(?,?,?,?,?,?,?)""",
                    (item_id, name, status, external_ref, actor, now, now),
                )
                equipment_id = int(cur.lastrowid)
                self._audit_tx("equipment_register", EQUIPMENT_ENTITY, equipment_id,
                               actor, {"item_id": item_id, "name": name, "status": status})
        except sqlite3.IntegrityError as exc:
            raise ConflictError("设备唯一标识已存在") from exc
        return self.get_equipment(equipment_id)

    def get_equipment(self, equipment_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM equipment WHERE id=?", (equipment_id,)).fetchone()
        if row is None:
            raise NotFoundError("设备不存在")
        return dict(row)

    def list_equipment(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM equipment WHERE item_id=? ORDER BY id", (item_id,)).fetchall()
        return [dict(row) for row in rows]

    def set_equipment_status(self, item_id: int, equipment_id: int, status: str,
                             actor: str):
        now = utc_now()
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT * FROM equipment WHERE id=? AND item_id=?",
                (equipment_id, item_id)).fetchone()
            if row is None:
                raise NotFoundError("设备不存在")
            if row["status"] == status:
                return dict(row), []
            self.conn.execute(
                "UPDATE equipment SET status=?, updated_at=? WHERE id=?",
                (status, now, equipment_id))
            summary = f"设备启停：{row['name']} {row['status']}→{status}"
            self._audit_tx("equipment_status", EQUIPMENT_ENTITY, equipment_id, actor,
                           {"item_id": item_id, "name": row["name"],
                            "from": row["status"], "to": status})
            affected = self._apply_impact_tx(item_id, IMPACT_EQUIPMENT, summary, actor)
        return self.get_equipment(equipment_id), affected

    # ---- 检查记录更正 ----
    def add_inspection_correction(self, item_id: int, detail: str, status: str,
                                  external_ref: Optional[str], actor: str):
        now = utc_now()
        self.get_item(item_id)
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO records(item_id, kind, detail, status, external_ref,
                       created_by, created_at) VALUES(?,?,?,?,?,?,?)""",
                    (item_id, "inspection_correction", detail, status, external_ref,
                     actor, now),
                )
                record_id = int(cur.lastrowid)
                self._audit_tx("inspection_correction", ENTITY, item_id, actor,
                               {"record_id": record_id, "detail": detail})
                affected = self._apply_impact_tx(
                    item_id, IMPACT_INSPECTION, f"检查记录更正#{record_id}", actor)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("记录唯一标识已存在") from exc
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        return dict(row), affected

    # ---- 续期影响清单 ----
    def _equipment_snapshot_tx(self, item_id: int) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            """SELECT id, name, status, external_ref FROM equipment
               WHERE item_id=? ORDER BY id""", (item_id,)).fetchall()
        return [dict(row) for row in rows]

    def _rectification_snapshot_tx(self, item_id: int) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            """SELECT id, kind, detail, external_ref FROM records
               WHERE item_id=? AND status='open' ORDER BY id""", (item_id,)).fetchall()
        return [dict(row) for row in rows]

    def _upsert_materials_tx(self, renewal_id: int, materials: List[Dict[str, str]],
                             actor: str, now: str) -> None:
        for material in materials:
            self.conn.execute(
                """INSERT INTO renewal_materials(renewal_id, name, detail, created_by,
                   created_at) VALUES(?,?,?,?,?)
                   ON CONFLICT(renewal_id, name) DO UPDATE SET detail=excluded.detail,
                   created_by=excluded.created_by, created_at=excluded.created_at""",
                (renewal_id, material["name"], material["detail"], actor, now))

    @staticmethod
    def _renewal(row: sqlite3.Row, materials: List[sqlite3.Row]) -> Dict[str, Any]:
        item = dict(row)
        item["equipment_snapshot"] = json.loads(item["equipment_snapshot"])
        item["rectification_snapshot"] = json.loads(item["rectification_snapshot"])
        item["impacts"] = json.loads(item["impacts"])
        item["materials"] = [dict(material) for material in materials]
        return item

    def get_renewal(self, renewal_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM renewals WHERE id=?", (renewal_id,)).fetchone()
            if row is None:
                raise NotFoundError("续期清单不存在")
            materials = self.conn.execute(
                """SELECT name, detail, created_by, created_at FROM renewal_materials
                   WHERE renewal_id=? ORDER BY id""", (renewal_id,)).fetchall()
        return self._renewal(row, materials)

    def get_renewal_by_application(self, item_id: int,
                                   application_no: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                "SELECT id FROM renewals WHERE item_id=? AND application_no=?",
                (item_id, application_no)).fetchone()
        return self.get_renewal(int(row["id"])) if row else None

    def list_renewals(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT id FROM renewals WHERE item_id=? ORDER BY id DESC",
                (item_id,)).fetchall()
        return [self.get_renewal(int(row["id"])) for row in rows]

    def create_renewal(self, item_id: int, application_no: str,
                       materials: List[Dict[str, str]], actor: str) -> Dict[str, Any]:
        now = utc_now()
        self.get_item(item_id)
        try:
            with self._lock, self.conn:
                equipment_snapshot = self._equipment_snapshot_tx(item_id)
                rectification_snapshot = self._rectification_snapshot_tx(item_id)
                cur = self.conn.execute(
                    """INSERT INTO renewals(item_id, application_no, version, status,
                       conclusion, equipment_snapshot, rectification_snapshot, impacts,
                       created_by, created_at, updated_at)
                       VALUES(?,?,1,'pending',NULL,?,?,?,?,?,?)""",
                    (item_id, application_no,
                     json.dumps(equipment_snapshot, ensure_ascii=False),
                     json.dumps(rectification_snapshot, ensure_ascii=False),
                     "[]", actor, now, now),
                )
                renewal_id = int(cur.lastrowid)
                self._upsert_materials_tx(renewal_id, materials, actor, now)
                self._audit_tx("renewal_submit", RENEWAL_ENTITY, renewal_id, actor, {
                    "item_id": item_id, "application_no": application_no, "version": 1,
                    "materials": [m["name"] for m in materials],
                    "equipment_bound": len(equipment_snapshot),
                    "open_rectifications_bound": len(rectification_snapshot)})
        except sqlite3.IntegrityError as exc:
            raise ConflictError("申请号已存在") from exc
        return self.get_renewal(renewal_id)

    def supplement_materials(self, renewal_id: int, expected_version: int,
                             materials: List[Dict[str, str]], actor: str):
        now = utc_now()
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT * FROM renewals WHERE id=?", (renewal_id,)).fetchone()
            if row is None:
                raise NotFoundError("续期清单不存在")
            if int(row["version"]) != expected_version:
                current = [dict(r) for r in self.conn.execute(
                    """SELECT name, detail FROM renewal_materials
                       WHERE renewal_id=? ORDER BY id""", (renewal_id,)).fetchall()]
                incoming = [{"name": m["name"], "detail": m["detail"]} for m in materials]
                diff, conflicts = diff_materials(current, incoming)
                raise ConflictError(
                    "版本冲突：他人已保存当前版本，请核对差异后基于最新版本重新提交",
                    payload={"current_version": row["version"],
                             "expected_version": expected_version, "diff": diff,
                             "conflicting_materials": conflicts,
                             "current_materials": current})
            new_status = "pending" if row["status"] == "returned" else row["status"]
            impacts = json.loads(row["impacts"])
            if row["status"] == "approved":
                impacts.append({"kind": IMPACT_SUPPLEMENT,
                                "summary": "补交材料：" + ",".join(m["name"] for m in materials),
                                "actor": actor, "at": now})
            cur = self.conn.execute(
                """UPDATE renewals SET version=version+1, status=?, impacts=?,
                   equipment_snapshot=?, rectification_snapshot=?, updated_at=?
                   WHERE id=? AND version=?""",
                (new_status, json.dumps(impacts, ensure_ascii=False),
                 json.dumps(self._equipment_snapshot_tx(row["item_id"]), ensure_ascii=False),
                 json.dumps(self._rectification_snapshot_tx(row["item_id"]), ensure_ascii=False),
                 now, renewal_id, expected_version))
            if cur.rowcount == 0:
                raise ConflictError("版本冲突，请刷新后重试")
            self._upsert_materials_tx(renewal_id, materials, actor, now)
            self._audit_tx("renewal_supplement", RENEWAL_ENTITY, renewal_id, actor, {
                "item_id": row["item_id"], "application_no": row["application_no"],
                "version": expected_version + 1, "status": new_status,
                "materials": [m["name"] for m in materials]})
            affected = self._apply_impact_tx(
                row["item_id"], IMPACT_SUPPLEMENT,
                f"清单{row['application_no']}补交材料", actor,
                exclude_renewal_id=renewal_id)
        return self.get_renewal(renewal_id), affected

    def approve_renewal(self, renewal_id: int, conclusion: str,
                        actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT * FROM renewals WHERE id=?", (renewal_id,)).fetchone()
            if row is None:
                raise NotFoundError("续期清单不存在")
            if row["status"] == "approved":
                raise ConflictError("清单已审批，原结论保留")
            if row["status"] == "returned":
                raise ConflictError("清单已退回核对，需重新提交后再审批")
            self.conn.execute(
                """UPDATE renewals SET status='approved', conclusion=?,
                   version=version+1, updated_at=? WHERE id=? AND status='pending'""",
                (conclusion, now, renewal_id))
            self._audit_tx("renewal_approve", RENEWAL_ENTITY, renewal_id, actor, {
                "item_id": row["item_id"], "application_no": row["application_no"],
                "version": row["version"] + 1, "conclusion": conclusion})
        return self.get_renewal(renewal_id)

    def _apply_impact_tx(self, item_id: int, kind: str, summary: str, actor: str,
                         exclude_renewal_id: Optional[int] = None) -> List[Dict[str, Any]]:
        now = utc_now()
        rows = self.conn.execute(
            "SELECT * FROM renewals WHERE item_id=? ORDER BY id", (item_id,)).fetchall()
        affected = []
        for row in rows:
            if exclude_renewal_id is not None and row["id"] == exclude_renewal_id:
                continue
            impacts = json.loads(row["impacts"])
            impacts.append({"kind": kind, "summary": summary, "actor": actor, "at": now})
            disposition = impact_disposition(row["status"])
            new_status = row["status"] if disposition == "affected_scope" else "returned"
            self.conn.execute(
                """UPDATE renewals SET status=?, impacts=?, version=version+1,
                   updated_at=? WHERE id=?""",
                (new_status, json.dumps(impacts, ensure_ascii=False), now, row["id"]))
            self._audit_tx("renewal_impact", RENEWAL_ENTITY, row["id"], actor, {
                "item_id": item_id, "application_no": row["application_no"],
                "kind": kind, "summary": summary, "disposition": disposition,
                "version": row["version"] + 1})
            affected.append({"renewal_id": row["id"],
                             "application_no": row["application_no"],
                             "status": new_status, "disposition": disposition})
        return affected

    def close(self) -> None:
        with self._lock:
            self.conn.close()
