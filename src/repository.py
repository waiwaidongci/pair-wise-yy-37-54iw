from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError, VersionConflict
from .rules import (EQUIPMENT_STATUSES, RENEWAL_STATES, STATES,
                    conflicting_materials, diff_materials, diff_snapshots,
                    environment_effect)


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
        renewal_statuses = ",".join("'" + s.replace("'", "''") + "'" for s in RENEWAL_STATES)
        equipment_statuses = ",".join("'" + s.replace("'", "''") + "'" for s in EQUIPMENT_STATUSES)
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
                    version INTEGER NOT NULL DEFAULT 1,
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
                        CHECK(status IN ({equipment_statuses})),
                    version INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(item_id, name)
                );
                CREATE TABLE IF NOT EXISTS renewals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    application_no TEXT NOT NULL UNIQUE,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    status TEXT NOT NULL DEFAULT 'draft'
                        CHECK(status IN ({renewal_statuses})),
                    version INTEGER NOT NULL DEFAULT 1,
                    ledger_snapshot TEXT NOT NULL DEFAULT '[]',
                    rectification_snapshot TEXT NOT NULL DEFAULT '[]',
                    impact_scope TEXT NOT NULL DEFAULT '[]',
                    conclusion TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS renewal_materials (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    renewal_id INTEGER NOT NULL REFERENCES renewals(id) ON DELETE CASCADE,
                    version INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    content TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(renewal_id, version, name)
                );
                CREATE TABLE IF NOT EXISTS renewal_versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    renewal_id INTEGER NOT NULL REFERENCES renewals(id) ON DELETE CASCADE,
                    version INTEGER NOT NULL,
                    ledger_snapshot TEXT NOT NULL DEFAULT '[]',
                    rectification_snapshot TEXT NOT NULL DEFAULT '[]',
                    impact_scope TEXT NOT NULL DEFAULT '[]',
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(renewal_id, version)
                );
                CREATE TABLE IF NOT EXISTS environment_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    type TEXT NOT NULL,
                    ref_id INTEGER,
                    name TEXT,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
            """)
        # 迁移：为旧库的 records 补充 version 列
        try:
            with self.conn:
                self.conn.execute("ALTER TABLE records ADD COLUMN version INTEGER NOT NULL DEFAULT 1")
        except sqlite3.OperationalError:
            pass

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

    def append_audit(self, action: str, entity_type: str, entity_id: int,
                     actor: str, detail: dict) -> Dict[str, Any]:
        with self._lock, self.conn:
            return self._insert_audit(action, entity_type, entity_id, actor, detail)

    def _insert_audit(self, action: str, entity_type: str, entity_id: int,
                      actor: str, detail: dict) -> Dict[str, Any]:
        # 调用方必须已持有 self._lock 且处于事务中
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

    # ---- 设备台账 ----
    def create_equipment(self, item_id: int, name: str, actor: str) -> Dict[str, Any]:
        now = utc_now()
        self.get_item(item_id)
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO equipment(item_id, name, status, version,
                       created_by, created_at, updated_at) VALUES(?,?,?,?,?,?,?)""",
                    (item_id, name, 'running', 1, actor, now, now),
                )
                equipment_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("设备名称已存在") from exc
        return self.get_equipment(equipment_id)

    def get_equipment(self, equipment_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM equipment WHERE id=?", (equipment_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError("设备不存在")
        return dict(row)

    def list_equipment(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT id, name, status, version, created_by, created_at, updated_at "
                "FROM equipment WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def set_equipment_status(self, item_id: int, equipment_id: int, status: str,
                             actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE equipment SET status=?, version=version+1, updated_at=?
                   WHERE id=? AND item_id=?""",
                (status, now, equipment_id, item_id),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute(
                    "SELECT 1 FROM equipment WHERE id=? AND item_id=?",
                    (equipment_id, item_id)).fetchone()
                if exists is None:
                    raise NotFoundError("设备不存在")
                raise ConflictError("设备状态冲突")
            row = self.conn.execute(
                "SELECT * FROM equipment WHERE id=?", (equipment_id,)
            ).fetchone()
            equipment = dict(row)
            action = 'equipment_start' if status == 'running' else 'equipment_stop'
            detail = f"设备{equipment['name']}{'启动' if status == 'running' else '停止'}"
            self._process_environment_change(
                item_id, action, equipment_id, equipment['name'], detail, actor)
        return equipment

    # ---- 检查记录更正 ----
    def correct_record(self, item_id: int, record_id: int, detail: str,
                       actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                "UPDATE records SET detail=?, version=version+1 WHERE id=? AND item_id=?",
                (detail, record_id, item_id),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute(
                    "SELECT 1 FROM records WHERE id=? AND item_id=?",
                    (record_id, item_id)).fetchone()
                if exists is None:
                    raise NotFoundError("记录不存在")
                raise ConflictError("记录更正冲突")
            row = self.conn.execute(
                "SELECT * FROM records WHERE id=?", (record_id,)
            ).fetchone()
            record = dict(row)
            self._process_environment_change(
                item_id, 'record_correction', record_id, None,
                f"检查记录更正: {detail}", actor)
        return record

    # ---- 续期（影响清单）----
    def create_renewal(self, item_id: int, application_no: Optional[str],
                       actor: str) -> Dict[str, Any]:
        now = utc_now()
        self.get_item(item_id)
        with self._lock, self.conn:
            if application_no is None:
                row = self.conn.execute("SELECT COUNT(*) AS n FROM renewals").fetchone()
                application_no = f"XQ-{now[:10].replace('-', '')}-{int(row['n']) + 1:04d}"
            try:
                cur = self.conn.execute(
                    """INSERT INTO renewals(application_no, item_id, status, version,
                       ledger_snapshot, rectification_snapshot, impact_scope, conclusion,
                       created_by, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (application_no, item_id, 'draft', 1, '[]', '[]', '[]', None,
                     actor, now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("申请号已存在") from exc
            renewal_id = int(cur.lastrowid)
            self.conn.execute(
                """INSERT INTO renewal_versions(renewal_id, version, ledger_snapshot,
                   rectification_snapshot, impact_scope, created_by, created_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (renewal_id, 1, '[]', '[]', '[]', actor, now),
            )
            self._insert_audit('renewal_create', 'renewal', renewal_id, actor, {
                "application_no": application_no, "item_id": item_id,
            })
        return self.get_renewal(renewal_id)

    def get_renewal(self, renewal_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM renewals WHERE id=?", (renewal_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError("续期申请不存在")
        return self._renewal(row)

    def get_renewal_by_application(self, application_no: str) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM renewals WHERE application_no=?", (application_no,)
            ).fetchone()
        if row is None:
            raise NotFoundError("续期申请不存在")
        return self._renewal(row)

    def list_renewals(self, item_id: int, status: Optional[str] = None) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        sql = "SELECT * FROM renewals WHERE item_id=?"
        params: list = [item_id]
        if status:
            sql += " AND status=?"
            params.append(status)
        sql += " ORDER BY id DESC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._renewal(row) for row in rows]

    @staticmethod
    def _renewal(row: sqlite3.Row) -> Dict[str, Any]:
        d = dict(row)
        for key in ('ledger_snapshot', 'rectification_snapshot', 'impact_scope'):
            d[key] = json.loads(d[key])
        return d

    def save_renewal_materials(self, application_no: str, expected_version: int,
                               materials: List[Dict[str, str]], actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT * FROM renewals WHERE application_no=?", (application_no,)
            ).fetchone()
            if row is None:
                raise NotFoundError("续期申请不存在")
            renewal = self._renewal(row)
            current_map = self._materials_at(renewal['id'], renewal['version'])
            submitted_map = {m['name']: m['content_hash'] for m in materials}
            if expected_version != renewal['version']:
                if submitted_map == current_map:
                    # 幂等重试：写入实际已成功但响应丢失
                    return {"renewal": renewal, "materials": materials, "idempotent": True}
                diff = self._material_diff(
                    renewal['id'], expected_version, renewal['version'],
                    current_map, submitted_map)
                raise VersionConflict(
                    "版本冲突，请根据差异清单核对后重新提交",
                    diff=diff, conflicting_materials=diff['conflicting_materials'])
            if submitted_map == current_map:
                # 无变化的保存
                return {"renewal": renewal, "materials": materials, "idempotent": True}
            new_version = renewal['version'] + 1
            for m in materials:
                self.conn.execute(
                    """INSERT INTO renewal_materials(renewal_id, version, name, content,
                       content_hash, created_by, created_at) VALUES(?,?,?,?,?,?,?)""",
                    (renewal['id'], new_version, m['name'], m['content'],
                     m['content_hash'], actor, now),
                )
            new_status = 'verifying' if renewal['status'] in ('submitted', 'verifying') else renewal['status']
            if renewal['status'] == 'approved':
                names = ','.join(m['name'] for m in materials)
                self._process_environment_change(
                    renewal['item_id'], 'material_supplement', renewal['id'],
                    names, f"补交材料: {names}", actor)
                # 重新读取，拿到追加后的 impact_scope
                row = self.conn.execute(
                    "SELECT * FROM renewals WHERE id=?", (renewal['id'],)
                ).fetchone()
                renewal = self._renewal(row)
            self.conn.execute(
                "UPDATE renewals SET version=?, status=?, updated_at=? WHERE id=?",
                (new_version, new_status, now, renewal['id']),
            )
            self.conn.execute(
                """INSERT INTO renewal_versions(renewal_id, version, ledger_snapshot,
                   rectification_snapshot, impact_scope, created_by, created_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (renewal['id'], new_version,
                 json.dumps(renewal['ledger_snapshot'], ensure_ascii=False),
                 json.dumps(renewal['rectification_snapshot'], ensure_ascii=False),
                 json.dumps(renewal['impact_scope'], ensure_ascii=False), actor, now),
            )
            self._insert_audit('renewal_material', 'renewal', renewal['id'], actor, {
                "application_no": application_no, "version": new_version,
                "materials": [m['name'] for m in materials], "status": new_status,
            })
        return {"renewal": self.get_renewal(renewal['id']),
                "materials": materials, "idempotent": False}

    def submit_renewal(self, application_no: str, expected_version: int,
                       actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT * FROM renewals WHERE application_no=?", (application_no,)
            ).fetchone()
            if row is None:
                raise NotFoundError("续期申请不存在")
            renewal = self._renewal(row)
            if renewal['status'] not in ('draft', 'verifying'):
                raise ConflictError("当前状态不可提交")
            if expected_version != renewal['version']:
                raise VersionConflict("版本冲突，请刷新后重试",
                                     diff=None, conflicting_materials=[])
            ledger_rows = self.conn.execute(
                "SELECT id, name, status, version FROM equipment "
                "WHERE item_id=? ORDER BY id", (renewal['item_id'],),
            ).fetchall()
            ledger = [dict(r) for r in ledger_rows]
            rect_rows = self.conn.execute(
                "SELECT id, kind, detail, status FROM records "
                "WHERE item_id=? AND status='open' ORDER BY id", (renewal['item_id'],),
            ).fetchall()
            rects = [dict(r) for r in rect_rows]
            new_version = renewal['version'] + 1
            ledger_json = json.dumps(ledger, ensure_ascii=False)
            rect_json = json.dumps(rects, ensure_ascii=False)
            self.conn.execute(
                """UPDATE renewals SET status='submitted', version=?, ledger_snapshot=?,
                   rectification_snapshot=?, updated_at=? WHERE id=?""",
                (new_version, ledger_json, rect_json, now, renewal['id']),
            )
            self.conn.execute(
                """INSERT INTO renewal_versions(renewal_id, version, ledger_snapshot,
                   rectification_snapshot, impact_scope, created_by, created_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (renewal['id'], new_version, ledger_json, rect_json,
                 json.dumps(renewal['impact_scope'], ensure_ascii=False), actor, now),
            )
            self._insert_audit('renewal_submit', 'renewal', renewal['id'], actor, {
                "application_no": application_no, "version": new_version,
                "equipment_count": len(ledger), "open_rectification_count": len(rects),
            })
        return self.get_renewal(renewal['id'])

    def approve_renewal(self, application_no: str, expected_version: int,
                        conclusion: str, actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT * FROM renewals WHERE application_no=?", (application_no,)
            ).fetchone()
            if row is None:
                raise NotFoundError("续期申请不存在")
            renewal = self._renewal(row)
            if renewal['status'] != 'submitted':
                raise ConflictError("仅已提交的续期申请可审批")
            if expected_version != renewal['version']:
                raise VersionConflict("版本冲突，请刷新后重试",
                                     diff=None, conflicting_materials=[])
            open_count = self.conn.execute(
                "SELECT COUNT(*) AS n FROM records WHERE item_id=? AND status='open'",
                (renewal['item_id'],),
            ).fetchone()['n']
            if open_count > 0:
                raise ConflictError("存在未关闭整改，不能批准")
            new_version = renewal['version'] + 1
            self.conn.execute(
                "UPDATE renewals SET status='approved', conclusion=?, version=?, updated_at=? WHERE id=?",
                (conclusion, new_version, now, renewal['id']),
            )
            self.conn.execute(
                """INSERT INTO renewal_versions(renewal_id, version, ledger_snapshot,
                   rectification_snapshot, impact_scope, created_by, created_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (renewal['id'], new_version,
                 json.dumps(renewal['ledger_snapshot'], ensure_ascii=False),
                 json.dumps(renewal['rectification_snapshot'], ensure_ascii=False),
                 json.dumps(renewal['impact_scope'], ensure_ascii=False), actor, now),
            )
            self._insert_audit('renewal_approve', 'renewal', renewal['id'], actor, {
                "application_no": application_no, "conclusion": conclusion,
            })
        return self.get_renewal(renewal['id'])

    def list_materials(self, renewal_id: int, version: Optional[int] = None) -> List[Dict[str, Any]]:
        with self._lock:
            if version is None:
                row = self.conn.execute(
                    "SELECT version FROM renewals WHERE id=?", (renewal_id,)
                ).fetchone()
                version = row['version'] if row else 1
            mat_row = self.conn.execute(
                "SELECT MAX(version) AS v FROM renewal_materials "
                "WHERE renewal_id=? AND version<=?",
                (renewal_id, version),
            ).fetchone()
            mat_version = mat_row['v']
            if mat_version is None:
                return []
            rows = self.conn.execute(
                """SELECT name, content, content_hash, version, created_by, created_at
                   FROM renewal_materials WHERE renewal_id=? AND version=? ORDER BY name""",
                (renewal_id, mat_version),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_renewal_versions(self, renewal_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                """SELECT version, ledger_snapshot, rectification_snapshot, impact_scope,
                   created_by, created_at FROM renewal_versions
                   WHERE renewal_id=? ORDER BY version""",
                (renewal_id,),
            ).fetchall()
        result = []
        for row in rows:
            d = dict(row)
            for key in ('ledger_snapshot', 'rectification_snapshot', 'impact_scope'):
                d[key] = json.loads(d[key])
            result.append(d)
        return result

    def list_environment_events(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM environment_events WHERE item_id=? ORDER BY id",
                (item_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    # ---- 环境变更联动 ----
    def _process_environment_change(self, item_id: int, change_type: str,
                                    ref_id: Optional[int], name: Optional[str],
                                    detail: str, actor: str) -> Dict[str, Any]:
        # 调用方必须已持有 self._lock 且处于事务中
        now = utc_now()
        self.conn.execute(
            """INSERT INTO environment_events(item_id, type, ref_id, name, detail, created_at)
               VALUES(?,?,?,?,?,?)""",
            (item_id, change_type, ref_id, name, detail, now),
        )
        renewals = [dict(r) for r in self.conn.execute(
            "SELECT * FROM renewals WHERE item_id=? ORDER BY id", (item_id,)
        ).fetchall()]
        returned: list = []
        impacted: list = []
        for row in renewals:
            status = row['status']
            effect = environment_effect(status)
            if effect == 'impact':
                renewal = self._renewal(row)
                affected = self._compute_affected(renewal, change_type, ref_id, name, detail)
                if affected:
                    scope = renewal['impact_scope']
                    scope.append({
                        "type": change_type, "ref_id": ref_id, "name": name,
                        "detail": detail, "affected": affected, "at": now,
                    })
                    self.conn.execute(
                        "UPDATE renewals SET impact_scope=?, updated_at=? WHERE id=?",
                        (json.dumps(scope, ensure_ascii=False), now, row['id']),
                    )
                    self._insert_audit('renewal_impact', 'renewal', row['id'], actor, {
                        "application_no": renewal['application_no'],
                        "type": change_type, "affected": affected,
                    })
                    impacted.append(row['id'])
            elif effect == 'return':
                self.conn.execute(
                    "UPDATE renewals SET status='verifying', updated_at=? WHERE id=?",
                    (now, row['id']),
                )
                renewal = self._renewal(row)
                self._insert_audit('renewal_returned', 'renewal', row['id'], actor, {
                    "application_no": renewal['application_no'], "reason": detail,
                })
                returned.append(row['id'])
        return {"returned": returned, "impacted": impacted}

    @staticmethod
    def _compute_affected(renewal: Dict[str, Any], change_type: str,
                          ref_id: Optional[int], name: Optional[str],
                          detail: str) -> List[Dict[str, Any]]:
        if change_type in ('equipment_start', 'equipment_stop'):
            if any(e['id'] == ref_id for e in renewal['ledger_snapshot']):
                return [{"kind": "equipment", "id": ref_id, "name": name}]
        elif change_type == 'record_correction':
            if any(r['id'] == ref_id for r in renewal['rectification_snapshot']):
                return [{"kind": "rectification", "id": ref_id}]
        elif change_type == 'material_supplement':
            return [{"kind": "material", "names": name, "detail": detail}]
        return []

    def _materials_at(self, renewal_id: int, version: int) -> Dict[str, str]:
        row = self.conn.execute(
            "SELECT MAX(version) AS v FROM renewal_materials "
            "WHERE renewal_id=? AND version<=?",
            (renewal_id, version),
        ).fetchone()
        mat_version = row['v']
        if mat_version is None:
            return {}
        rows = self.conn.execute(
            "SELECT name, content_hash FROM renewal_materials "
            "WHERE renewal_id=? AND version=?",
            (renewal_id, mat_version),
        ).fetchall()
        return {r['name']: r['content_hash'] for r in rows}

    def _material_diff(self, renewal_id: int, base_version: int, current_version: int,
                       current_map: Dict[str, str],
                       submitted_map: Dict[str, str]) -> Dict[str, Any]:
        base_map = self._materials_at(renewal_id, base_version)
        base_ver = self.conn.execute(
            "SELECT ledger_snapshot, rectification_snapshot FROM renewal_versions "
            "WHERE renewal_id=? AND version=?",
            (renewal_id, base_version),
        ).fetchone()
        cur_ver = self.conn.execute(
            "SELECT ledger_snapshot, rectification_snapshot FROM renewal_versions "
            "WHERE renewal_id=? AND version=?",
            (renewal_id, current_version),
        ).fetchone()
        base_ledger = json.loads(base_ver['ledger_snapshot']) if base_ver else []
        cur_ledger = json.loads(cur_ver['ledger_snapshot']) if cur_ver else []
        base_rects = json.loads(base_ver['rectification_snapshot']) if base_ver else []
        cur_rects = json.loads(cur_ver['rectification_snapshot']) if cur_ver else []
        diff = {
            "from_version": base_version,
            "to_version": current_version,
            "materials": diff_materials(base_map, current_map),
            "ledger": diff_snapshots(base_ledger, cur_ledger),
            "rectifications": diff_snapshots(base_rects, cur_rects),
        }
        diff["conflicting_materials"] = conflicting_materials(
            base_map, current_map, submitted_map)
        return diff
