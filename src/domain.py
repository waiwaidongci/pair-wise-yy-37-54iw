from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
class ErrorKind:
    VALIDATION="validation"; NOT_FOUND="not_found"; FORBIDDEN="forbidden"; CONFLICT="conflict"
class DomainError(Exception):
    kind=ErrorKind.VALIDATION
    def __init__(self,message): super().__init__(message); self.message=message
class ValidationError(DomainError): kind=ErrorKind.VALIDATION
class NotFoundError(DomainError): kind=ErrorKind.NOT_FOUND
class PermissionDenied(DomainError): kind=ErrorKind.FORBIDDEN
class ConflictError(DomainError): kind=ErrorKind.CONFLICT
class VersionConflict(ConflictError):
    """版本冲突：后到者携带差异清单和冲突材料。"""
    def __init__(self,message,diff=None,conflicting_materials=None):
        super().__init__(message)
        self.diff=diff
        self.conflicting_materials=conflicting_materials or []
SEVERITIES=['low', 'medium', 'high', 'critical']; STATES=['draft', 'submitted', 'inspection', 'correction', 'approved']; ROLES=['applicant', 'inspector', 'compliance_manager', 'viewer']
RENEWAL_STATES=['draft', 'submitted', 'verifying', 'approved']
EQUIPMENT_STATUSES=['running', 'stopped']
@dataclass(frozen=True)
class Item:
    id:int; title:str; description:str; severity:str; quantity:float; threshold:float; status:str; version:int; external_ref:Optional[str]; created_by:str; created_at:str; updated_at:str
@dataclass(frozen=True)
class Record:
    id:int; item_id:int; kind:str; detail:str; status:str; version:int; external_ref:Optional[str]; created_by:str; created_at:str
@dataclass(frozen=True)
class AuditEntry:
    id:int; action:str; entity_type:str; entity_id:int; actor:str; detail:Dict[str,Any]; previous_hash:str; entry_hash:str; created_at:str
@dataclass(frozen=True)
class Equipment:
    id:int; item_id:int; name:str; status:str; version:int; created_by:str; created_at:str; updated_at:str
@dataclass(frozen=True)
class Renewal:
    id:int; application_no:str; item_id:int; status:str; version:int
    ledger_snapshot:List[Dict[str,Any]]; rectification_snapshot:List[Dict[str,Any]]
    impact_scope:List[Dict[str,Any]]; conclusion:Optional[str]
    created_by:str; created_at:str; updated_at:str
@dataclass(frozen=True)
class RenewalMaterial:
    id:int; renewal_id:int; version:int; name:str; content:str; content_hash:str
    created_by:str; created_at:str
@dataclass(frozen=True)
class EnvironmentEvent:
    id:int; item_id:int; type:str; ref_id:Optional[int]; name:Optional[str]
    detail:str; created_at:str
def require_text(value,field,max_length=2000):
    if not isinstance(value,str) or not value.strip(): raise ValidationError(f"{field}不能为空")
    value=value.strip()
    if len(value)>max_length: raise ValidationError(f"{field}不能超过{max_length}个字符")
    return value
def normalize_severity(value):
    if value not in SEVERITIES: raise ValidationError("severity不在允许范围内")
    return value
def require_number(value,field,minimum=0.0):
    if isinstance(value,bool): raise ValidationError(f"{field}必须是数字")
    try: number=float(value)
    except (TypeError,ValueError): raise ValidationError(f"{field}必须是数字")
    if number<minimum: raise ValidationError(f"{field}不能小于{minimum}")
    return number
def ensure_role(role,allowed):
    if role not in allowed: raise PermissionDenied("当前角色无权执行该操作")
