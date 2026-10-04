from __future__ import annotations
import hashlib
from typing import Any, Dict, Iterable, Mapping, Set
from .domain import ConflictError, ValidationError
TITLE='空气污染源许可与合规检查'; ENTITY='排污许可'; ID_PREFIX='AQ'
SEVERITIES=['low', 'medium', 'high', 'critical']; STATES=['draft', 'submitted', 'inspection', 'correction', 'approved']; TRANSITIONS={'draft': ['submitted'], 'submitted': ['inspection'], 'inspection': ['correction'], 'correction': ['approved'], 'approved': []}; TRANSITION_ROLES={'submitted': ['applicant'], 'inspection': ['inspector'], 'correction': ['inspector'], 'approved': ['compliance_manager']}
CREATE_ROLES=set(['applicant']); RECORD_ROLES=set(['applicant', 'inspector']); AUDIT_ROLES=set(['compliance_manager', 'viewer']); VIEW_ROLES=set(['applicant', 'inspector', 'compliance_manager', 'viewer'])
SEVERITY_WEIGHT={'low': 1.0, 'medium': 3.0, 'high': 6.0, 'critical': 9.0}; DEADLINE_HOURS={'low': 72, 'medium': 24, 'high': 8, 'critical': 4}; TERMINAL_STATES=set(['approved'])
# 续期（影响清单）状态与角色
RENEWAL_STATES=['draft', 'submitted', 'verifying', 'approved']
EQUIPMENT_STATUSES=['running', 'stopped']
EQUIPMENT_ROLES=set(['applicant'])
RECORD_CORRECTION_ROLES=set(['inspector'])
RENEWAL_ROLES=set(['applicant'])
APPROVE_ROLES=set(['compliance_manager'])
# 环境变更类型
ENV_EQUIPMENT_START='equipment_start'; ENV_EQUIPMENT_STOP='equipment_stop'
ENV_RECORD_CORRECTION='record_correction'; ENV_MATERIAL_SUPPLEMENT='material_supplement'
def priority_score(severity,quantity=0.0,threshold=1.0,open_records=0):
    if severity not in SEVERITY_WEIGHT: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(0,min(10,int(round(SEVERITY_WEIGHT[severity]+min(4.0,ratio*4.0)+min(3.0,float(open_records))))))
def response_deadline_hours(severity,quantity=0.0,threshold=1.0):
    if severity not in DEADLINE_HOURS: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(1,int(DEADLINE_HOURS[severity]/max(1.0,ratio)))
def escalation_required(severity,quantity=0.0,threshold=1.0):
    return severity==SEVERITIES[-1] or (threshold>0 and quantity>=threshold)
def can_transition(current,target): return target in TRANSITIONS.get(current,[])
def validate_transition(current,target):
    if current not in STATES or target not in STATES: raise ValidationError("未知状态")
    if not can_transition(current,target): raise ConflictError(f"不能从{current}转换到{target}")
def completion_blockers(target,open_records): return ["仍有未关闭事项"] if target in TERMINAL_STATES and open_records>0 else []
def role_for_transition(target): return set(TRANSITION_ROLES.get(target,[]))
# ---- 续期（影响清单）规则 ----
def hash_material(content: str) -> str:
    return hashlib.sha256(content.encode('utf-8')).hexdigest()
def environment_effect(status: str) -> str | None:
    """环境变更对续期清单的影响：approved 保留结论并列影响；在途清单退回核对。"""
    if status == 'approved':
        return 'impact'
    if status in ('submitted', 'verifying'):
        return 'return'
    return None
def can_submit_renewal(status: str) -> bool:
    return status in ('draft', 'verifying')
def can_approve_renewal(status: str) -> bool:
    return status == 'submitted'
def _changed_names(base: Mapping[str, Any], other: Mapping[str, Any]) -> Set[str]:
    return {n for n in set(base) | set(other) if other.get(n) != base.get(n)}
def diff_materials(base_map: Mapping[str, str], current_map: Mapping[str, str]) -> Dict[str, Any]:
    added = [n for n in current_map if n not in base_map]
    removed = [n for n in base_map if n not in current_map]
    changed = [{"name": n, "from_hash": base_map[n], "to_hash": current_map[n]}
               for n in base_map if n in current_map and base_map[n] != current_map[n]]
    return {"added": added, "removed": removed, "changed": changed}
def diff_snapshots(base_list: Iterable[Mapping[str, Any]],
                   current_list: Iterable[Mapping[str, Any]]) -> Dict[str, Any]:
    base_map = {x['id']: x for x in base_list}
    cur_map = {x['id']: x for x in current_list}
    added = [cur_map[i] for i in cur_map if i not in base_map]
    removed = [base_map[i] for i in base_map if i not in cur_map]
    changed = [{"id": i, "before": base_map[i], "after": cur_map[i]}
               for i in base_map if i in cur_map and base_map[i] != cur_map[i]]
    return {"added": added, "removed": removed, "changed": changed}
def conflicting_materials(base_map: Mapping[str, str], current_map: Mapping[str, str],
                          submitted_map: Mapping[str, str]) -> list:
    a_changed = _changed_names(base_map, current_map)
    b_changed = _changed_names(base_map, submitted_map)
    conflicts = a_changed & b_changed
    return [{"name": n, "base_hash": base_map.get(n),
             "current_hash": current_map.get(n),
             "submitted_hash": submitted_map.get(n)}
            for n in sorted(conflicts)]
