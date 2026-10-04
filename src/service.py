from __future__ import annotations

from typing import Any, Dict, Optional

from .domain import (ConflictError, ValidationError, ensure_role,
                     normalize_severity, require_number, require_text)
from .repository import Repository
from .rules import (AUDIT_ROLES, CREATE_ROLES, ENTITY, EQUIPMENT_ROLES,
                    INSPECTION_CORRECTION_ROLES, RECORD_ROLES,
                    RENEWAL_APPROVE_ROLES, RENEWAL_WORK_ROLES, TITLE,
                    VIEW_ROLES, completion_blockers, escalation_required,
                    normalize_equipment_status, priority_score,
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
    def register_equipment(self, item_id: int, payload: Dict[str, Any], actor: str,
                           role: str) -> Dict[str, Any]:
        ensure_role(role, EQUIPMENT_ROLES)
        actor = require_text(actor, "actor", 100)
        name = require_text(payload.get("name"), "name", 200)
        status = normalize_equipment_status(payload.get("status", "running"))
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        return self.repository.add_equipment(item_id, name, status, external_ref, actor)

    def list_equipment(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_equipment(item_id)

    def set_equipment_status(self, item_id: int, equipment_id: int,
                             payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, EQUIPMENT_ROLES)
        actor = require_text(actor, "actor", 100)
        status = normalize_equipment_status(payload.get("status"))
        equipment, affected = self.repository.set_equipment_status(
            item_id, equipment_id, status, actor)
        return {"equipment": equipment, "affected_renewals": affected}

    # ---- 检查记录更正 ----
    def correct_inspection(self, item_id: int, payload: Dict[str, Any], actor: str,
                           role: str) -> Dict[str, Any]:
        ensure_role(role, INSPECTION_CORRECTION_ROLES)
        actor = require_text(actor, "actor", 100)
        detail = require_text(payload.get("detail"), "detail")
        status = payload.get("status", "closed")
        if status not in ("open", "closed"):
            raise ValidationError("status必须是open或closed")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        record, affected = self.repository.add_inspection_correction(
            item_id, detail, status, external_ref, actor)
        return {"record": record, "affected_renewals": affected}

    # ---- 续期影响清单 ----
    @staticmethod
    def _parse_materials(payload: Dict[str, Any], required: bool) -> list:
        materials = payload.get("materials", [])
        if not isinstance(materials, list):
            raise ValidationError("materials必须是数组")
        parsed = []
        for entry in materials:
            if not isinstance(entry, dict):
                raise ValidationError("材料必须是对象")
            parsed.append({"name": require_text(entry.get("name"), "材料名称", 200),
                           "detail": require_text(entry.get("detail"), "材料内容")})
        if required and not parsed:
            raise ValidationError("补交材料不能为空")
        names = [m["name"] for m in parsed]
        if len(names) != len(set(names)):
            raise ValidationError("材料名称重复")
        return parsed

    def submit_renewal(self, item_id: int, payload: Dict[str, Any], actor: str,
                       role: str) -> Dict[str, Any]:
        ensure_role(role, RENEWAL_WORK_ROLES)
        actor = require_text(actor, "actor", 100)
        application_no = require_text(payload.get("application_no"), "application_no", 100)
        materials = self._parse_materials(payload, required=False)
        existing = self.repository.get_renewal_by_application(item_id, application_no)
        if existing is not None:
            # 写入失败后按原申请号恢复续传，返回已保存的材料和版本
            return self.enrich_renewal(dict(existing, resumed=True))
        try:
            renewal = self.repository.create_renewal(item_id, application_no,
                                                     materials, actor)
        except ConflictError:
            resumed = self.repository.get_renewal_by_application(item_id, application_no)
            return self.enrich_renewal(dict(resumed, resumed=True))
        return self.enrich_renewal(dict(renewal, resumed=False))

    def supplement_materials(self, renewal_id: int, payload: Dict[str, Any], actor: str,
                             role: str) -> Dict[str, Any]:
        ensure_role(role, RENEWAL_WORK_ROLES)
        actor = require_text(actor, "actor", 100)
        expected_version = payload.get("expected_version")
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValidationError("expected_version必须是正整数")
        materials = self._parse_materials(payload, required=True)
        renewal, affected = self.repository.supplement_materials(
            renewal_id, expected_version, materials, actor)
        return {"renewal": self.enrich_renewal(renewal), "affected_renewals": affected}

    def approve_renewal(self, renewal_id: int, payload: Dict[str, Any], actor: str,
                        role: str) -> Dict[str, Any]:
        ensure_role(role, RENEWAL_APPROVE_ROLES)
        actor = require_text(actor, "actor", 100)
        conclusion = require_text(payload.get("conclusion"), "conclusion")
        return self.enrich_renewal(
            self.repository.approve_renewal(renewal_id, conclusion, actor))

    def get_renewal(self, renewal_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.enrich_renewal(self.repository.get_renewal(renewal_id))

    def list_renewals(self, item_id: int, role: str) -> list:
        self._view(role)
        return [self.enrich_renewal(r) for r in self.repository.list_renewals(item_id)]

    @staticmethod
    def enrich_renewal(renewal: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(renewal)
        result["affected_scope"] = (
            result["impacts"] if result["status"] == "approved" else [])
        return result

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
