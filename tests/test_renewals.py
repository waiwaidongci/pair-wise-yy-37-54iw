import json
import tempfile
import unittest
from pathlib import Path

from src.domain import ConflictError, PermissionDenied, VersionConflict
from src.repository import Repository
from src.service import Service


class RenewalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)
        self.item = self.service.create_item(
            {"title": "测试设施", "description": "d", "severity": "high",
             "quantity": 5, "threshold": 10},
            "creator", "applicant")

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _create_renewal(self, application_no=None):
        payload = {"application_no": application_no} if application_no else {}
        return self.service.create_renewal(self.item["id"], payload, "creator", "applicant")

    def _save(self, appno, version, materials, actor="creator", role="applicant"):
        return self.service.save_renewal_materials(
            appno, {"expected_version": version, "materials": materials}, actor, role)

    def _material(self, name, content):
        return {"name": name, "content": content}

    def test_submit_binds_ledger_and_rectifications(self):
        # 设备台账和未关闭整改
        self.service.create_equipment(self.item["id"], {"name": "治理设备A"}, "creator", "applicant")
        self.service.add_record(self.item["id"], {
            "kind": "rectification", "detail": "未关闭整改", "status": "open",
            "external_ref": "R1"}, "creator", "applicant")
        renewal = self._create_renewal()
        self._save(renewal["application_no"], 1, [self._material("报告", "v1")])
        submitted = self.service.submit_renewal(
            renewal["application_no"], {"expected_version": 2}, "creator", "applicant")
        self.assertEqual(submitted["status"], "submitted")
        self.assertEqual(len(submitted["ledger_snapshot"]), 1)
        self.assertEqual(submitted["ledger_snapshot"][0]["name"], "治理设备A")
        self.assertEqual(submitted["ledger_snapshot"][0]["status"], "running")
        self.assertEqual(len(submitted["rectification_snapshot"]), 1)
        self.assertEqual(submitted["rectification_snapshot"][0]["detail"], "未关闭整改")

    def test_equipment_stop_returns_unapproved_for_verification(self):
        eq = self.service.create_equipment(self.item["id"], {"name": "设备A"}, "creator", "applicant")
        renewal = self._create_renewal()
        self._save(renewal["application_no"], 1, [self._material("报告", "v1")])
        self.service.submit_renewal(
            renewal["application_no"], {"expected_version": 2}, "creator", "applicant")
        # 设备停止 -> 未审批清单退回核对
        self.service.set_equipment_status(
            self.item["id"], eq["id"], "stopped", "creator", "applicant")
        after = self.service.get_renewal(renewal["application_no"], "viewer")
        self.assertEqual(after["status"], "verifying")

    def test_record_correction_returns_unapproved_for_verification(self):
        renewal = self._create_renewal()
        self._save(renewal["application_no"], 1, [self._material("报告", "v1")])
        self.service.submit_renewal(
            renewal["application_no"], {"expected_version": 2}, "creator", "applicant")
        rec = self.service.add_record(self.item["id"], {
            "kind": "inspection", "detail": "检查记录", "status": "open",
            "external_ref": "I1"}, "creator", "inspector")
        # 检查记录更正 -> 未审批清单退回核对
        self.service.correct_record(
            self.item["id"], rec["id"], {"detail": "更正后的检查记录"}, "creator", "inspector")
        after = self.service.get_renewal(renewal["application_no"], "viewer")
        self.assertEqual(after["status"], "verifying")

    def test_equipment_stop_approved_keeps_conclusion_and_lists_impact(self):
        eq = self.service.create_equipment(self.item["id"], {"name": "设备A"}, "creator", "applicant")
        renewal = self._create_renewal()
        self._save(renewal["application_no"], 1, [self._material("报告", "v1")])
        self.service.submit_renewal(
            renewal["application_no"], {"expected_version": 2}, "creator", "applicant")
        self.service.approve_renewal(
            renewal["application_no"], {"expected_version": 3, "conclusion": "同意"},
            "approver", "compliance_manager")
        # 设备停止 -> 已审批许可保留原结论，列出受影响范围
        self.service.set_equipment_status(
            self.item["id"], eq["id"], "stopped", "creator", "applicant")
        after = self.service.get_renewal(renewal["application_no"], "viewer")
        self.assertEqual(after["status"], "approved")
        self.assertEqual(after["conclusion"], "同意")
        self.assertEqual(len(after["impact_scope"]), 1)
        self.assertEqual(after["impact_scope"][0]["type"], "equipment_stop")
        self.assertEqual(after["impact_scope"][0]["affected"][0]["kind"], "equipment")
        self.assertEqual(after["impact_scope"][0]["affected"][0]["id"], eq["id"])

    def test_material_supplement_on_approved_records_impact(self):
        renewal = self._create_renewal()
        self._save(renewal["application_no"], 1, [self._material("报告", "v1")])
        self.service.submit_renewal(
            renewal["application_no"], {"expected_version": 2}, "creator", "applicant")
        self.service.approve_renewal(
            renewal["application_no"], {"expected_version": 3, "conclusion": "同意"},
            "approver", "compliance_manager")
        # 补交材料 -> 已审批许可保留结论，记录材料影响
        self._save(renewal["application_no"], 4, [
            self._material("报告", "v1"), self._material("补充", "新材料")])
        after = self.service.get_renewal(renewal["application_no"], "viewer")
        self.assertEqual(after["status"], "approved")
        self.assertEqual(after["conclusion"], "同意")
        self.assertEqual(len(after["impact_scope"]), 1)
        self.assertEqual(after["impact_scope"][0]["type"], "material_supplement")

    def test_concurrent_material_save_conflict_with_diff_and_conflicts(self):
        renewal = self._create_renewal()
        self._save(renewal["application_no"], 1, [self._material("报告", "v1")])
        self.service.submit_renewal(
            renewal["application_no"], {"expected_version": 2}, "creator", "applicant")
        self.service.approve_renewal(
            renewal["application_no"], {"expected_version": 3, "conclusion": "同意"},
            "approver", "compliance_manager")
        base = self.service.get_renewal(renewal["application_no"], "viewer")
        base_version = base["version"]
        # A 先保存，占用当前版本
        a = self._save(renewal["application_no"], base_version, [
            self._material("报告", "A版本"), self._material("新增", "A新增")])
        self.assertEqual(a["renewal"]["version"], base_version + 1)
        # B 后到，携带差异清单和冲突材料
        with self.assertRaises(VersionConflict) as ctx:
            self._save(renewal["application_no"], base_version, [
                self._material("报告", "B版本"), self._material("B新增", "B新增")])
        exc = ctx.exception
        self.assertIsNotNone(exc.diff)
        self.assertEqual(exc.diff["from_version"], base_version)
        self.assertEqual(exc.diff["to_version"], base_version + 1)
        self.assertIn("新增", exc.diff["materials"]["added"])
        self.assertEqual(len(exc.diff["materials"]["changed"]), 1)
        self.assertEqual(exc.diff["materials"]["changed"][0]["name"], "报告")
        # 冲突材料：报告被双方修改
        self.assertEqual(len(exc.conflicting_materials), 1)
        self.assertEqual(exc.conflicting_materials[0]["name"], "报告")
        self.assertIsNotNone(exc.conflicting_materials[0]["base_hash"])
        self.assertIsNotNone(exc.conflicting_materials[0]["current_hash"])
        self.assertIsNotNone(exc.conflicting_materials[0]["submitted_hash"])

    def test_conflict_does_not_write_materials_or_audit(self):
        renewal = self._create_renewal()
        self._save(renewal["application_no"], 1, [self._material("报告", "v1")])
        base = self.service.get_renewal(renewal["application_no"], "viewer")
        # A 保存
        self._save(renewal["application_no"], base["version"], [
            self._material("报告", "A版本")])
        events_before = len(self.service.audit("viewer", self.item["id"]))
        # B 冲突保存
        with self.assertRaises(VersionConflict):
            self._save(renewal["application_no"], base["version"], [
                self._material("报告", "B版本")])
        # 冲突未写入新材料，未新增审计
        events_after = len(self.service.audit("viewer", self.item["id"]))
        self.assertEqual(events_before, events_after)
        current = self.service.get_renewal(renewal["application_no"], "viewer")
        self.assertEqual(current["version"], base["version"] + 1)
        self.assertEqual(len(current["materials"]), 1)
        self.assertEqual(current["materials"][0]["content"], "A版本")

    def test_resume_by_application_no(self):
        renewal = self._create_renewal("XQ-RESUME-001")
        self._save(renewal["application_no"], 1, [self._material("报告", "v1")])
        self.service.submit_renewal(
            renewal["application_no"], {"expected_version": 2}, "creator", "applicant")
        # 按原申请号恢复续传
        resume = self.service.resume_renewal(renewal["application_no"], "viewer")
        self.assertEqual(resume["renewal"]["application_no"], "XQ-RESUME-001")
        self.assertEqual(resume["renewal"]["status"], "submitted")
        self.assertEqual(resume["renewal"]["version"], 3)
        self.assertEqual(len(resume["materials"]), 1)
        self.assertEqual(resume["materials"][0]["name"], "报告")
        self.assertGreaterEqual(len(resume["versions"]), 2)
        # 申请号稳定，可重复查询
        again = self.service.get_renewal(renewal["application_no"], "viewer")
        self.assertEqual(again["application_no"], "XQ-RESUME-001")

    def test_idempotent_retry_after_failure(self):
        renewal = self._create_renewal()
        self._save(renewal["application_no"], 1, [self._material("报告", "v1")])
        base = self.service.get_renewal(renewal["application_no"], "viewer")
        # 保存成功
        saved = self._save(renewal["application_no"], base["version"], [
            self._material("报告", "v2")])
        self.assertEqual(saved["renewal"]["version"], base["version"] + 1)
        # 响应丢失后重试：相同 expected_version + 相同材料 -> 幂等，不升版本
        retry = self._save(renewal["application_no"], base["version"], [
            self._material("报告", "v2")])
        self.assertTrue(retry["idempotent"])
        self.assertEqual(retry["renewal"]["version"], base["version"] + 1)

    def test_approve_requires_no_open_rectifications(self):
        self.service.add_record(self.item["id"], {
            "kind": "rectification", "detail": "未关闭整改", "status": "open",
            "external_ref": "R1"}, "creator", "applicant")
        renewal = self._create_renewal()
        self._save(renewal["application_no"], 1, [self._material("报告", "v1")])
        self.service.submit_renewal(
            renewal["application_no"], {"expected_version": 2}, "creator", "applicant")
        with self.assertRaises(ConflictError):
            self.service.approve_renewal(
                renewal["application_no"], {"expected_version": 3, "conclusion": "同意"},
                "approver", "compliance_manager")

    def test_approve_requires_submitted_status(self):
        renewal = self._create_renewal()
        with self.assertRaises(ConflictError):
            self.service.approve_renewal(
                renewal["application_no"], {"expected_version": 1, "conclusion": "同意"},
                "approver", "compliance_manager")

    def test_role_permissions(self):
        renewal = self._create_renewal()
        # 检查员不能提交续期
        with self.assertRaises(PermissionDenied):
            self.service.submit_renewal(
                renewal["application_no"], {"expected_version": 1}, "creator", "inspector")
        # 申请人不能审批
        with self.assertRaises(PermissionDenied):
            self.service.approve_renewal(
                renewal["application_no"], {"expected_version": 1, "conclusion": "同意"},
                "creator", "applicant")
        # 检查员不能更正设备状态
        with self.assertRaises(PermissionDenied):
            self.service.set_equipment_status(
                self.item["id"], 1, "stopped", "creator", "inspector")

    def test_audit_chain_and_atomic_write(self):
        renewal = self._create_renewal()
        self._save(renewal["application_no"], 1, [self._material("报告", "v1")])
        self.service.submit_renewal(
            renewal["application_no"], {"expected_version": 2}, "creator", "applicant")
        self.service.approve_renewal(
            renewal["application_no"], {"expected_version": 3, "conclusion": "同意"},
            "approver", "compliance_manager")
        # 审计链完整
        self.assertTrue(self.repo.verify_audit_chain())
        events = self.service.audit("viewer", self.item["id"])
        actions = [e["action"] for e in events]
        self.assertIn("renewal_create", actions)
        self.assertIn("renewal_material", actions)
        self.assertIn("renewal_submit", actions)
        self.assertIn("renewal_approve", actions)
        # 版本与材料、审计在同一事务中保存
        current = self.service.get_renewal(renewal["application_no"], "viewer")
        self.assertEqual(current["version"], 4)


if __name__ == "__main__":
    unittest.main()
