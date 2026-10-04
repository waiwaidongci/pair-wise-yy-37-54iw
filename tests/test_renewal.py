import tempfile, unittest
from pathlib import Path
from src.domain import ConflictError, PermissionDenied
from src.repository import Repository
from src.service import Service


class RenewalTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.repo=Repository(str(Path(self.tmp.name)/"test.db")); self.service=Service(self.repo)
        self.item=self.service.create_item({"title":"某设施","description":"许可续期设施","severity":"high","quantity":5,"threshold":10,"external_ref":"FAC-1"},"owner","applicant")
        self.equipment=self.service.register_equipment(self.item["id"],{"name":"脱硫塔","status":"running","external_ref":"EQ-1"},"owner","applicant")
    def tearDown(self): self.repo.close(); self.tmp.cleanup()
    def _submit(self,no="RN-1",materials=None):
        return self.service.submit_renewal(self.item["id"],{"application_no":no,"materials":materials or [{"name":"监测报告","detail":"v1"}]},"owner","applicant")
    def test_submit_binds_ledger_and_open_rectifications(self):
        self.service.add_record(self.item["id"],{"kind":"rectification","detail":"限期整改","status":"open","external_ref":"RC-1"},"inspector","inspector")
        self.service.add_record(self.item["id"],{"kind":"rectification","detail":"已办结","status":"closed","external_ref":"RC-2"},"inspector","inspector")
        renewal=self._submit()
        self.assertEqual(renewal["status"],"pending"); self.assertEqual(renewal["version"],1); self.assertFalse(renewal["resumed"])
        self.assertEqual([e["name"] for e in renewal["equipment_snapshot"]],["脱硫塔"])
        self.assertEqual([r["external_ref"] for r in renewal["rectification_snapshot"]],["RC-1"])
        self.assertEqual(renewal["materials"][0]["name"],"监测报告")
    def test_equipment_status_returns_pending_and_scopes_approved(self):
        pending=self._submit("RN-1"); approved=self._submit("RN-2")
        approved=self.service.approve_renewal(approved["id"],{"conclusion":"同意续期"},"boss","compliance_manager")
        result=self.service.set_equipment_status(self.item["id"],self.equipment["id"],{"status":"stopped"},"owner","applicant")
        self.assertEqual(result["equipment"]["status"],"stopped")
        self.assertEqual(len(result["affected_renewals"]),2)
        after_pending=self.service.get_renewal(pending["id"],"viewer"); after_approved=self.service.get_renewal(approved["id"],"viewer")
        self.assertEqual(after_pending["status"],"returned")
        self.assertEqual(after_approved["status"],"approved"); self.assertEqual(after_approved["conclusion"],"同意续期")
        self.assertTrue(any(i["kind"]=="equipment_status" for i in after_approved["affected_scope"]))
    def test_inspection_correction_triggers_impact(self):
        renewal=self._submit()
        result=self.service.correct_inspection(self.item["id"],{"detail":"更正检查记录","external_ref":"FIX-1"},"inspector","inspector")
        self.assertEqual(result["record"]["kind"],"inspection_correction")
        self.assertEqual(self.service.get_renewal(renewal["id"],"viewer")["status"],"returned")
    def test_returned_renewal_recheck_and_approve_guard(self):
        renewal=self._submit()
        self.service.set_equipment_status(self.item["id"],self.equipment["id"],{"status":"stopped"},"owner","applicant")
        with self.assertRaises(ConflictError): self.service.approve_renewal(renewal["id"],{"conclusion":"同意"},"boss","compliance_manager")
        current=self.service.get_renewal(renewal["id"],"viewer")
        result=self.service.supplement_materials(renewal["id"],{"expected_version":current["version"],"materials":[{"name":"补充说明","detail":"已核对"}]},"owner","applicant")
        self.assertEqual(result["renewal"]["status"],"pending")
        approved=self.service.approve_renewal(renewal["id"],{"conclusion":"同意续期"},"boss","compliance_manager")
        self.assertEqual(approved["status"],"approved")
    def test_supplement_on_approved_keeps_conclusion_and_scopes(self):
        renewal=self._submit()
        renewal=self.service.approve_renewal(renewal["id"],{"conclusion":"同意续期"},"boss","compliance_manager")
        result=self.service.supplement_materials(renewal["id"],{"expected_version":renewal["version"],"materials":[{"name":"补充监测","detail":"v2"}]},"owner","applicant")
        after=result["renewal"]
        self.assertEqual(after["status"],"approved"); self.assertEqual(after["conclusion"],"同意续期")
        self.assertTrue(any(i["kind"]=="supplement" for i in after["affected_scope"]))
        self.assertEqual(len(after["materials"]),2)
    def test_concurrent_supplement_conflict_shows_diff(self):
        renewal=self._submit(materials=[{"name":"A","detail":"1"}])
        self.service.supplement_materials(renewal["id"],{"expected_version":1,"materials":[{"name":"B","detail":"2"},{"name":"A","detail":"1b"}]},"alice","applicant")
        with self.assertRaises(ConflictError) as ctx:
            self.service.supplement_materials(renewal["id"],{"expected_version":1,"materials":[{"name":"C","detail":"3"},{"name":"A","detail":"1c"}]},"bob","applicant")
        payload=ctx.exception.payload
        self.assertEqual(payload["current_version"],2); self.assertEqual(payload["expected_version"],1)
        self.assertEqual(payload["diff"]["added"],["C"]); self.assertEqual(payload["diff"]["removed"],["B"]); self.assertEqual(payload["diff"]["changed"],["A"])
        conflicts={m["name"]:m for m in payload["conflicting_materials"]}
        self.assertEqual(conflicts["A"]["saved_detail"],"1b"); self.assertEqual(conflicts["A"]["your_detail"],"1c")
        self.assertIsNone(conflicts["B"]["your_detail"])
        winner=self.service.get_renewal(renewal["id"],"viewer")
        self.assertEqual(winner["version"],2); self.assertEqual(len(winner["materials"]),2)
    def test_resume_by_application_no_after_failure(self):
        first=self._submit("RN-9")
        again=self.service.submit_renewal(self.item["id"],{"application_no":"RN-9","materials":[{"name":"监测报告","detail":"v1"}]},"owner","applicant")
        self.assertTrue(again["resumed"]); self.assertEqual(again["id"],first["id"]); self.assertEqual(again["version"],1)
        self.assertEqual(len(self.service.list_renewals(self.item["id"],"viewer")),1)
        self.assertEqual(again["materials"][0]["name"],"监测报告")
        self.assertTrue(self.repo.verify_audit_chain())
    def test_permission_denied_for_viewer(self):
        with self.assertRaises(PermissionDenied): self.service.submit_renewal(self.item["id"],{"application_no":"RN-X","materials":[]},"guest","viewer")
        with self.assertRaises(PermissionDenied): self.service.register_equipment(self.item["id"],{"name":"旁路"},"guest","viewer")
        with self.assertRaises(PermissionDenied): self.service.correct_inspection(self.item["id"],{"detail":"x"},"owner","applicant")
    def test_audit_chain_covers_renewal_lifecycle(self):
        renewal=self._submit()
        self.service.set_equipment_status(self.item["id"],self.equipment["id"],{"status":"stopped"},"owner","applicant")
        current=self.service.get_renewal(renewal["id"],"viewer")
        self.service.supplement_materials(renewal["id"],{"expected_version":current["version"],"materials":[{"name":"补充","detail":"d"}]},"owner","applicant")
        self.service.approve_renewal(renewal["id"],{"conclusion":"同意"},"boss","compliance_manager")
        self.assertTrue(self.repo.verify_audit_chain())
        actions=[e["action"] for e in self.repo.list_audit()]
        for expected in ("equipment_register","renewal_submit","equipment_status","renewal_impact","renewal_supplement","renewal_approve"):
            self.assertIn(expected,actions)
if __name__=="__main__": unittest.main()
