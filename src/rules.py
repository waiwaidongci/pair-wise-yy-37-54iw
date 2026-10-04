from __future__ import annotations
from .domain import ConflictError, ValidationError
TITLE='空气污染源许可与合规检查'; ENTITY='排污许可'; ID_PREFIX='AQ'
SEVERITIES=['low', 'medium', 'high', 'critical']; STATES=['draft', 'submitted', 'inspection', 'correction', 'approved']; TRANSITIONS={'draft': ['submitted'], 'submitted': ['inspection'], 'inspection': ['correction'], 'correction': ['approved'], 'approved': []}; TRANSITION_ROLES={'submitted': ['applicant'], 'inspection': ['inspector'], 'correction': ['inspector'], 'approved': ['compliance_manager']}
CREATE_ROLES=set(['applicant']); RECORD_ROLES=set(['applicant', 'inspector']); AUDIT_ROLES=set(['compliance_manager', 'viewer']); VIEW_ROLES=set(['applicant', 'inspector', 'compliance_manager', 'viewer'])
RENEWAL_ENTITY='续期清单'; EQUIPMENT_ENTITY='治理设备'
RENEWAL_STATES=['pending', 'returned', 'approved']; EQUIPMENT_STATES=['running', 'stopped']
RENEWAL_WORK_ROLES=set(['applicant']); EQUIPMENT_ROLES=set(['applicant']); INSPECTION_CORRECTION_ROLES=set(['inspector']); RENEWAL_APPROVE_ROLES=set(['compliance_manager'])
IMPACT_EQUIPMENT='equipment_status'; IMPACT_INSPECTION='inspection_correction'; IMPACT_SUPPLEMENT='supplement'
SEVERITY_WEIGHT={'low': 1.0, 'medium': 3.0, 'high': 6.0, 'critical': 9.0}; DEADLINE_HOURS={'low': 72, 'medium': 24, 'high': 8, 'critical': 4}; TERMINAL_STATES=set(['approved'])
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
def normalize_equipment_status(value):
    if value not in EQUIPMENT_STATES: raise ValidationError("设备状态必须是running或stopped")
    return value
def impact_disposition(renewal_status):
    # 未审批清单退回核对；已审批许可保留原结论，仅列出受影响范围
    return 'affected_scope' if renewal_status=='approved' else 'returned'
def diff_materials(current,incoming):
    # 后到者与先保存者占用版本的差异清单和冲突材料
    cur={m["name"]:m.get("detail","") for m in current}; inc={m["name"]:m.get("detail","") for m in incoming}
    added=sorted(n for n in inc if n not in cur); removed=sorted(n for n in cur if n not in inc)
    changed=sorted(n for n in inc if n in cur and inc[n]!=cur[n])
    conflicts=[{"name":n,"saved_detail":cur[n],"your_detail":inc.get(n)} for n in changed+removed]
    return {"added":added,"removed":removed,"changed":changed},conflicts
