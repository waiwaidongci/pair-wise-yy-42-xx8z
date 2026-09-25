import tempfile, unittest
from pathlib import Path
from src.domain import ConflictError, NotFoundError, PermissionDenied, ValidationError
from src.repository import Repository
from src.service import Service
from src.rules import STATES
class ResourceTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.repo=Repository(str(Path(self.tmp.name)/"test.db")); self.service=Service(self.repo)
        self.item=self.service.create_item({"title":"fire alpha","description":"first incident","severity":"high","quantity":10,"threshold":5,"external_ref":"INC-1"},"cmd","field_commander")
        self.item2=self.service.create_item({"title":"fire bravo","description":"second incident","severity":"moderate","quantity":3,"threshold":5,"external_ref":"INC-2"},"cmd","field_commander")
        self.crew=self.service.register_resource({"code":"FR-101","kind":"personnel","type":"消防员"},"log","logistics")
        self.engine=self.service.register_resource({"code":"ENG-7","kind":"vehicle","type":"水罐车"},"log","logistics")
    def tearDown(self): self.repo.close(); self.tmp.cleanup()
    def _close(self,item):
        current=self.service.get_item(item["id"],"viewer")
        for target in STATES[1:]: current=self.service.transition(current["id"],target,current["version"],"ic","incident_commander")
        return current
    def test_register_validation_and_duplicate_code(self):
        self.assertEqual(self.crew["status"],"available"); self.assertEqual(self.engine["kind"],"vehicle")
        with self.assertRaises(ConflictError): self.service.register_resource({"code":"FR-101","kind":"personnel","type":"通讯员"},"log","logistics")
        with self.assertRaises(ValidationError): self.service.register_resource({"code":"X-1","kind":"drone","type":"无人机"},"log","logistics")
        with self.assertRaises(PermissionDenied): self.service.register_resource({"code":"FR-102","kind":"personnel","type":"消防员"},"v","viewer")
    def test_occupancy_conflict_returns_details(self):
        first=self.service.assign_resource({"request_ref":"REQ-1","resource_id":self.crew["id"],"item_id":self.item["id"]},"cmd","field_commander")
        self.assertFalse(first["replayed"]); self.assertEqual(self.service.get_resource(self.crew["id"],"viewer")["status"],"assigned")
        with self.assertRaises(ConflictError) as ctx:
            self.service.assign_resource({"request_ref":"REQ-2","resource_id":self.crew["id"],"item_id":self.item2["id"]},"cmd","incident_commander")
        details=ctx.exception.details
        self.assertEqual(details["item_id"],self.item["id"]); self.assertEqual(details["request_ref"],"REQ-1"); self.assertEqual(details["resource_code"],"FR-101")
        self.assertEqual(self.service.get_resource(self.crew["id"],"viewer")["active_assignment"]["id"],first["id"])
    def test_duplicate_request_ref_returns_original(self):
        payload={"request_ref":"REQ-9","resource_code":"ENG-7","item_id":self.item["id"]}
        first=self.service.assign_resource(payload,"cmd","incident_commander")
        replay=self.service.assign_resource(payload,"cmd","incident_commander")
        self.assertTrue(replay["replayed"]); self.assertEqual(replay["id"],first["id"])
        self.assertEqual(len(self.service.list_assignments("viewer",resource_id=self.engine["id"])),1)
        with self.assertRaises(ConflictError): self.service.assign_resource({"request_ref":"REQ-9","resource_code":"ENG-7","item_id":self.item2["id"]},"cmd","incident_commander")
    def test_release_on_event_close_and_reassign(self):
        assignment=self.service.assign_resource({"request_ref":"REQ-5","resource_id":self.crew["id"],"item_id":self.item["id"]},"cmd","field_commander")
        closed=self._close(self.item)
        self.assertEqual(closed["status"],STATES[-1])
        resource=self.service.get_resource(self.crew["id"],"viewer")
        self.assertEqual(resource["status"],"available"); self.assertIsNone(resource["active_assignment"])
        released=self.service.list_assignments("viewer",item_id=self.item["id"],status="released")
        self.assertEqual([r["id"] for r in released],[assignment["id"]]); self.assertIsNotNone(released[0]["released_at"])
        followup=self.service.assign_resource({"request_ref":"REQ-6","resource_id":self.crew["id"],"item_id":self.item2["id"]},"cmd","field_commander")
        self.assertFalse(followup["replayed"])
        replay=self.service.assign_resource({"request_ref":"REQ-5","resource_id":self.crew["id"],"item_id":self.item["id"]},"cmd","field_commander")
        self.assertTrue(replay["replayed"]); self.assertEqual(replay["status"],"released")
        self.assertTrue(self.repo.verify_audit_chain())
    def test_manual_release_is_idempotent(self):
        assignment=self.service.assign_resource({"request_ref":"REQ-7","resource_id":self.engine["id"],"item_id":self.item["id"]},"cmd","incident_commander")
        released=self.service.release_assignment(assignment["id"],"log","logistics")
        self.assertTrue(released["released_now"]); self.assertEqual(self.service.get_resource(self.engine["id"],"viewer")["status"],"available")
        again=self.service.release_assignment(assignment["id"],"log","logistics")
        self.assertFalse(again["released_now"])
        with self.assertRaises(PermissionDenied): self.service.release_assignment(assignment["id"],"v","viewer")
    def test_maintenance_and_closed_event_guards(self):
        self.service.update_resource_status(self.engine["id"],{"status":"maintenance"},"log","logistics")
        with self.assertRaises(ConflictError): self.service.assign_resource({"request_ref":"REQ-8","resource_id":self.engine["id"],"item_id":self.item["id"]},"cmd","field_commander")
        with self.assertRaises(ValidationError): self.service.update_resource_status(self.engine["id"],{"status":"assigned"},"log","logistics")
        self.service.update_resource_status(self.engine["id"],{"status":"available"},"log","logistics")
        self.service.assign_resource({"request_ref":"REQ-8","resource_id":self.engine["id"],"item_id":self.item["id"]},"cmd","field_commander")
        with self.assertRaises(ConflictError): self.service.update_resource_status(self.engine["id"],{"status":"maintenance"},"log","logistics")
        self._close(self.item)
        with self.assertRaises(ConflictError): self.service.assign_resource({"request_ref":"REQ-10","resource_id":self.crew["id"],"item_id":self.item["id"]},"cmd","field_commander")
        with self.assertRaises(NotFoundError): self.service.assign_resource({"request_ref":"REQ-11","resource_code":"NOPE-1","item_id":self.item2["id"]},"cmd","field_commander")
if __name__=="__main__": unittest.main()
