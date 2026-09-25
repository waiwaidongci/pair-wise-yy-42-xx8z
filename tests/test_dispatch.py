import tempfile
import threading
import unittest
from pathlib import Path

from src.domain import (ConflictError, NotFoundError, PermissionDenied,
                        ValidationError)
from src.repository import Repository
from src.service import Service
from src.rules import STATES, TRANSITION_ROLES


class ResourceDispatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _register(self, code, kind, rtype, role="logistics", actor="log-1"):
        return self.service.register_resource(
            {"code": code, "kind": kind, "type": rtype}, actor, role)

    def _incident(self, ref="WF-RES-1"):
        return self.service.create_item(
            {"title": "dispatch item", "description": "fire", "severity": "high",
             "quantity": 1, "threshold": 1, "external_ref": ref},
            "creator", "field_commander")

    def test_register_resource_validation(self):
        r = self._register("P-001", "person", "firefighter")
        self.assertEqual(r["status"], "available")
        self.assertIsNone(r["current_hold"])
        with self.assertRaises(ConflictError):
            self._register("P-001", "person", "driver")
        with self.assertRaises(ValidationError):
            self._register("P-002", "person", "engine")
        with self.assertRaises(PermissionDenied):
            self._register("V-001", "vehicle", "engine", role="viewer")
        self.assertEqual(
            len(self.service.list_resources("viewer", kind="person")), 1)

    def test_allocate_is_idempotent_by_request_no(self):
        self._register("P-001", "person", "firefighter")
        self._register("V-001", "vehicle", "engine")
        payload = {"request_no": "REQ-1", "resource_codes": ["P-001", "V-001"]}
        first = self.service.allocate(payload, "dispatcher", "logistics")
        self.assertFalse(first["replayed"])
        self.assertEqual(first["status"], "active")
        self.assertEqual(2, len(first["resources"]))
        again = self.service.allocate(payload, "another-channel", "logistics")
        self.assertTrue(again["replayed"])
        self.assertEqual(first["id"], again["id"])
        resource = self.service.get_resource(
            self.repo.get_resource_by_code("P-001")["id"], "viewer")
        self.assertEqual(resource["status"], "occupied")
        self.assertEqual(resource["current_hold"]["request_no"], "REQ-1")

    def test_busy_resource_is_rejected_with_hold_detail(self):
        self._register("P-001", "person", "driver")
        self._register("V-001", "vehicle", "tanker")
        self.service.allocate(
            {"request_no": "REQ-A", "resource_codes": ["P-001"]},
            "a", "logistics")
        try:
            self.service.allocate(
                {"request_no": "REQ-B", "resource_codes": ["P-001", "V-001"]},
                "b", "logistics")
            self.fail("expected ConflictError")
        except ConflictError as exc:
            occupied = exc.details["occupied"]
            self.assertEqual(exc.details["request_no"], "REQ-B")
            self.assertEqual([o["resource_code"] for o in occupied], ["P-001"])
            self.assertEqual(occupied[0]["request_no"], "REQ-A")
        tanker = self.service.get_resource(
            self.repo.get_resource_by_code("V-001")["id"], "viewer")
        self.assertEqual(tanker["status"], "available")

    def test_release_frees_resources_and_is_idempotent(self):
        self._register("P-001", "person", "firefighter")
        allocation = self.service.allocate(
            {"request_no": "REQ-R", "resource_codes": ["P-001"]},
            "a", "logistics")
        first = self.service.release(allocation["id"], None, "a", "logistics")
        self.assertFalse(first["replayed"])
        self.assertEqual(first["status"], "released")
        second = self.service.release(allocation["id"], None, "a", "logistics")
        self.assertTrue(second["replayed"])
        person = self.service.get_resource(
            self.repo.get_resource_by_code("P-001")["id"], "viewer")
        self.assertEqual(person["status"], "available")
        self.assertIsNone(person["current_hold"])
        reused = self.service.allocate(
            {"request_no": "REQ-R2", "resource_codes": ["P-001"]},
            "b", "logistics")
        self.assertFalse(reused["replayed"])

    def test_release_by_request_no(self):
        self._register("V-009", "vehicle", "engine")
        allocation = self.service.allocate(
            {"request_no": "REQ-NO-9", "resource_codes": ["V-009"]},
            "a", "logistics")
        by_no = self.service.release(
            allocation["id"], {"request_no": "REQ-NO-9"}, "a", "logistics")
        self.assertEqual(by_no["id"], allocation["id"])
        self.assertEqual(by_no["status"], "released")
        with self.assertRaises(NotFoundError):
            self.service.release(
                allocation["id"], {"request_no": "MISSING"}, "a", "logistics")

    def test_closing_incident_auto_releases_allocations(self):
        self._register("P-010", "person", "firefighter")
        incident = self._incident("WF-AUTO-1")
        allocation = self.service.allocate(
            {"request_no": "REQ-CLOSE", "item_id": incident["id"],
             "resource_codes": ["P-010"]}, "a", "logistics")
        item = incident
        for target in STATES[1:]:
            item = self.service.transition(
                item["id"], target, item["version"], "chief",
                TRANSITION_ROLES[target][0])
        self.assertEqual(item["status"], "closed")
        self.assertIn(allocation["id"], item["released_allocations"])
        view = self.service.get_allocation(allocation["id"], "viewer")
        self.assertEqual(view["status"], "released")
        person = self.service.get_resource(
            self.repo.get_resource_by_code("P-010")["id"], "viewer")
        self.assertEqual(person["status"], "available")
        self.assertTrue(self.repo.verify_audit_chain())

    def test_unknown_and_duplicate_codes_rejected(self):
        self._register("P-020", "person", "medic")
        with self.assertRaises(NotFoundError):
            self.service.allocate(
                {"request_no": "REQ-X", "resource_codes": ["GHOST"]},
                "a", "logistics")
        with self.assertRaises(ValidationError):
            self.service.allocate(
                {"request_no": "REQ-Y", "resource_codes": ["P-020", "P-020"]},
                "a", "logistics")

    def test_concurrent_requests_only_one_wins(self):
        self._register("P-100", "person", "firefighter")
        outcomes = []

        def worker(req):
            try:
                self.service.allocate(
                    {"request_no": req, "resource_codes": ["P-100"]},
                    req, "logistics")
                outcomes.append((req, "ok"))
            except ConflictError as exc:
                outcomes.append((req, exc.details["occupied"][0]["request_no"]))

        threads = [threading.Thread(target=worker, args=(f"REQ-T{i}",))
                   for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        winners = [o for o in outcomes if o[1] == "ok"]
        losers = [o for o in outcomes if o[1] != "ok"]
        self.assertEqual(len(winners), 1)
        winner_no = winners[0][0]
        self.assertTrue(all(holder == winner_no for _, holder in losers))
        active = self.service.list_allocations("viewer", status="active")
        self.assertEqual(len(active), 1)


if __name__ == "__main__":
    unittest.main()
