from __future__ import annotations

from typing import Any, Dict, List, Optional

from .domain import ensure_role, normalize_severity, require_number, require_text
from .repository import Repository
from .rules import (APPROVE_ROLES, AUDIT_ROLES, CREATE_ROLES, ENTITY,
                    EQUIPMENT_ROLES, RECORD_CORRECTION_ROLES, RECORD_ROLES,
                    RENEWAL_ROLES, TITLE, VIEW_ROLES, completion_blockers,
                    escalation_required, hash_material, priority_score,
                    response_deadline_hours, role_for_transition,
                    validate_transition)


class Service:
    def __init__(self, repository: Repository):
        self.repository = repository

    def _view(self, role: str) -> None:
        ensure_role(role, VIEW_ROLES)

    def create_item(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        title = require_text(payload.get("title"), "title", 200)
        description = require_text(payload.get("description"), "description")
        severity = normalize_severity(payload.get("severity"))
        quantity = require_number(payload.get("quantity", 0), "quantity")
        threshold = require_number(payload.get("threshold", 1), "threshold", 0.000001)
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        item = self.repository.create_item(title, description, severity, quantity,
                                           threshold, external_ref, actor)
        self.repository.append_audit("create", ENTITY, item["id"], actor, {
            "title": title, "severity": severity, "quantity": quantity,
            "priority": priority_score(severity, quantity, threshold),
        })
        return self.enrich(item)

    def add_record(self, item_id: int, payload: Dict[str, Any], actor: str,
                   role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        kind = require_text(payload.get("kind"), "kind", 100)
        detail = require_text(payload.get("detail"), "detail")
        status = payload.get("status", "open")
        if status not in ("open", "closed"):
            raise ValueError("status必须是open或closed")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        record = self.repository.add_record(item_id, kind, detail, status,
                                            external_ref, actor)
        self.repository.append_audit("record", ENTITY, item_id, actor, {
            "record_id": record["id"], "kind": kind, "status": status,
        })
        return record

    def transition(self, item_id: int, target: str, expected_version: int,
                   actor: str, role: str) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        validate_transition(item["status"], target)
        ensure_role(role, role_for_transition(target))
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        blockers = completion_blockers(target, self.repository.open_record_count(item_id))
        if blockers:
            from .domain import ConflictError
            raise ConflictError("；".join(blockers))
        updated = self.repository.transition_item(item_id, target, expected_version, actor)
        self.repository.append_audit("transition", ENTITY, item_id, actor, {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        })
        return self.enrich(updated)

    def get_item(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.enrich(self.repository.get_item(item_id))

    def list_items(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        return [self.enrich(item) for item in self.repository.list_items(status)]

    def list_records(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_records(item_id)

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    # ---- 设备台账 ----
    def create_equipment(self, item_id: int, payload: Dict[str, Any],
                         actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, EQUIPMENT_ROLES)
        actor = require_text(actor, "actor", 100)
        name = require_text(payload.get("name"), "name", 100)
        return self.repository.create_equipment(item_id, name, actor)

    def list_equipment(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_equipment(item_id)

    def set_equipment_status(self, item_id: int, equipment_id: int,
                             target_status: str, actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, EQUIPMENT_ROLES)
        actor = require_text(actor, "actor", 100)
        if target_status not in ('running', 'stopped'):
            raise ValueError("设备状态必须是running或stopped")
        return self.repository.set_equipment_status(
            item_id, equipment_id, target_status, actor)

    # ---- 检查记录更正 ----
    def correct_record(self, item_id: int, record_id: int, payload: Dict[str, Any],
                       actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_CORRECTION_ROLES)
        actor = require_text(actor, "actor", 100)
        detail = require_text(payload.get("detail"), "detail")
        return self.repository.correct_record(item_id, record_id, detail, actor)

    # ---- 续期（影响清单）----
    def create_renewal(self, item_id: int, payload: Dict[str, Any],
                       actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, RENEWAL_ROLES)
        actor = require_text(actor, "actor", 100)
        application_no = payload.get("application_no")
        if application_no is not None:
            application_no = require_text(application_no, "application_no", 100)
        return self.repository.create_renewal(item_id, application_no, actor)

    def get_renewal(self, application_no: str, role: str) -> Dict[str, Any]:
        self._view(role)
        renewal = self.repository.get_renewal_by_application(application_no)
        return self._renewal_detail(renewal)

    def list_renewals(self, item_id: int, role: str,
                      status: Optional[str] = None) -> list:
        self._view(role)
        return [self._renewal_detail(r)
                for r in self.repository.list_renewals(item_id, status)]

    def _renewal_detail(self, renewal: Dict[str, Any]) -> Dict[str, Any]:
        materials = self.repository.list_materials(renewal['id'])
        return dict(renewal, materials=materials)

    def save_renewal_materials(self, application_no: str, payload: Dict[str, Any],
                               actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, RENEWAL_ROLES)
        actor = require_text(actor, "actor", 100)
        expected_version = payload.get("expected_version")
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        raw_materials = payload.get("materials")
        if not isinstance(raw_materials, list) or not raw_materials:
            raise ValueError("materials必须是非空数组")
        materials: List[Dict[str, str]] = []
        names = set()
        for m in raw_materials:
            if not isinstance(m, dict):
                raise ValueError("材料必须是对象")
            name = require_text(m.get("name"), "name", 100)
            content = require_text(m.get("content"), "content")
            if name in names:
                raise ValueError(f"材料{name}重复")
            names.add(name)
            materials.append({"name": name, "content": content,
                              "content_hash": hash_material(content)})
        return self.repository.save_renewal_materials(
            application_no, expected_version, materials, actor)

    def submit_renewal(self, application_no: str, payload: Dict[str, Any],
                        actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, RENEWAL_ROLES)
        actor = require_text(actor, "actor", 100)
        expected_version = payload.get("expected_version")
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        return self.repository.submit_renewal(
            application_no, expected_version, actor)

    def approve_renewal(self, application_no: str, payload: Dict[str, Any],
                        actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, APPROVE_ROLES)
        actor = require_text(actor, "actor", 100)
        expected_version = payload.get("expected_version")
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        conclusion = require_text(payload.get("conclusion"), "conclusion", 2000)
        return self.repository.approve_renewal(
            application_no, expected_version, conclusion, actor)

    def resume_renewal(self, application_no: str, role: str) -> Dict[str, Any]:
        self._view(role)
        renewal = self.repository.get_renewal_by_application(application_no)
        materials = self.repository.list_materials(renewal['id'])
        versions = self.repository.list_renewal_versions(renewal['id'])
        events = self.repository.list_environment_events(renewal['item_id'])
        return {"renewal": renewal, "materials": materials,
                "versions": versions, "environment_events": events}

    @staticmethod
    def enrich(item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"])
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        return result
