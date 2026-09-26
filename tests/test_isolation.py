import tempfile
import unittest
from pathlib import Path

from src.domain import (
    Actor,
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


def _item(consignment_id, bay, recheck_date="2026-10-01"):
    return {
        "consignment_id": consignment_id,
        "bay": bay,
        "disinfection": "fumigation",
        "recheck_date": recheck_date,
    }


class IsolationOrderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin-1", "admin")

    def tearDown(self):
        self.tmp.cleanup()

    def _positive_batch(self, code):
        consignment = self.service.create(
            self.admin,
            "consignment",
            {"code": code, "origin": "Port-A", "destination": "Farm-B"},
        )
        self.service.transition(
            self.admin,
            consignment["id"],
            "inspect",
            {"inspector": "I-1", "inspection_result": "suspected"},
        )
        self.service.transition(
            self.admin,
            consignment["id"],
            "quarantine",
            {"pest_found": True, "sample_id": "S-" + code},
        )
        return consignment["id"]

    def test_register_multiple_batches_in_one_order(self):
        first = self._positive_batch("C-1")
        second = self._positive_batch("C-2")
        order = self.service.create(
            self.admin,
            "isolation_order",
            {"items": [_item(first, "B-1"), _item(second, "B-2", "2026-10-05")]},
        )
        self.assertEqual(order["status"], "active")
        items = order["data"]["items"]
        self.assertEqual([item["state"] for item in items], ["isolating", "isolating"])
        self.assertEqual(items[0]["bay"], "B-1")
        self.assertEqual(items[0]["disinfection"], "fumigation")
        self.assertEqual(items[1]["recheck_date"], "2026-10-05")
        dashboard = self.service.isolation_dashboard()
        self.assertEqual(dashboard["pending_recheck_count"], 2)
        occupied = {slot["bay"] for slot in dashboard["bays"] if slot["occupied"]}
        self.assertEqual(occupied, {"B-1", "B-2"})
        self.assertEqual(len(dashboard["records"]), 1)

    def test_bay_conflict_rejects_whole_order_and_names_batches(self):
        first = self._positive_batch("C-1")
        second = self._positive_batch("C-2")
        third = self._positive_batch("C-3")
        self.service.create(
            self.admin, "isolation_order", {"items": [_item(first, "B-1")]}
        )
        with self.assertRaises(ConflictError) as ctx:
            self.service.create(
                self.admin,
                "isolation_order",
                {"items": [_item(second, "B-2"), _item(third, "B-1")]},
            )
        message = str(ctx.exception)
        self.assertIn(third, message)
        self.assertIn("B-1", message)
        self.assertEqual(len(self.service.list("isolation_order")), 1)
        dashboard = self.service.isolation_dashboard()
        bays = {slot["bay"]: slot["occupied"] for slot in dashboard["bays"]}
        self.assertEqual(bays, {"B-1": True})

    def test_duplicate_bay_within_one_order_conflicts(self):
        first = self._positive_batch("C-1")
        second = self._positive_batch("C-2")
        with self.assertRaises(ConflictError) as ctx:
            self.service.create(
                self.admin,
                "isolation_order",
                {"items": [_item(first, "B-1"), _item(second, "B-1")]},
            )
        message = str(ctx.exception)
        self.assertIn(first, message)
        self.assertIn(second, message)
        self.assertEqual(self.service.list("isolation_order"), [])

    def test_recheck_pass_only_releases_bay(self):
        first = self._positive_batch("C-1")
        second = self._positive_batch("C-2")
        order = self.service.create(
            self.admin, "isolation_order", {"items": [_item(first, "B-1")]}
        )
        updated = self.service.transition(
            self.admin,
            order["id"],
            "recheck",
            {"consignment_id": first, "passed": True},
        )
        self.assertEqual(updated["status"], "closed")
        item = updated["data"]["items"][0]
        self.assertEqual(item["state"], "released")
        self.assertEqual(item["recheck_result"], "passed")
        consignment = self.service.get(first)
        self.assertEqual(consignment["status"], "quarantined")
        dashboard = self.service.isolation_dashboard()
        self.assertEqual(dashboard["pending_recheck_count"], 0)
        self.assertFalse(dashboard["bays"][0]["occupied"])
        pending_ids = [entry["id"] for entry in dashboard["pending_disposal"]]
        self.assertNotIn(first, pending_ids)
        self.assertIn(second, pending_ids)
        follow_up = self.service.create(
            self.admin, "isolation_order", {"items": [_item(second, "B-1")]}
        )
        self.assertEqual(follow_up["status"], "active")

    def test_recheck_fail_returns_batch_to_pending_disposal(self):
        first = self._positive_batch("C-1")
        order = self.service.create(
            self.admin, "isolation_order", {"items": [_item(first, "B-1")]}
        )
        updated = self.service.transition(
            self.admin,
            order["id"],
            "recheck",
            {"consignment_id": first, "passed": False},
        )
        self.assertEqual(updated["data"]["items"][0]["state"], "returned")
        dashboard = self.service.isolation_dashboard()
        self.assertEqual(
            [entry["id"] for entry in dashboard["pending_disposal"]], [first]
        )
        self.assertFalse(dashboard["bays"][0]["occupied"])
        retry = self.service.create(
            self.admin,
            "isolation_order",
            {"items": [_item(first, "B-1", "2026-11-01")]},
        )
        self.assertEqual(retry["status"], "active")

    def test_partial_recheck_keeps_order_active(self):
        first = self._positive_batch("C-1")
        second = self._positive_batch("C-2")
        order = self.service.create(
            self.admin,
            "isolation_order",
            {"items": [_item(first, "B-1"), _item(second, "B-2")]},
        )
        updated = self.service.transition(
            self.admin,
            order["id"],
            "recheck",
            {"consignment_id": first, "passed": True},
        )
        self.assertEqual(updated["status"], "active")
        dashboard = self.service.isolation_dashboard()
        self.assertEqual(dashboard["pending_recheck_count"], 1)
        bays = {slot["bay"]: slot["occupied"] for slot in dashboard["bays"]}
        self.assertEqual(bays, {"B-1": False, "B-2": True})

    def test_old_batch_without_isolation_info_still_listed(self):
        legacy = self._positive_batch("C-0")
        consignments = self.service.list("consignment")
        self.assertEqual([item["id"] for item in consignments], [legacy])
        quarantined = self.service.list("consignment", status="quarantined")
        self.assertEqual([item["id"] for item in quarantined], [legacy])
        dashboard = self.service.isolation_dashboard()
        self.assertEqual(
            [entry["id"] for entry in dashboard["pending_disposal"]], [legacy]
        )
        self.assertEqual(dashboard["pending_disposal"][0]["code"], "C-0")

    def test_idempotent_resend_returns_first_result(self):
        first = self._positive_batch("C-1")
        payload = {"items": [_item(first, "B-1")]}
        order = self.service.create(
            self.admin, "isolation_order", payload, idempotency_key="order-1"
        )
        resent = self.service.create(
            self.admin, "isolation_order", payload, idempotency_key="order-1"
        )
        self.assertEqual(order["id"], resent["id"])
        self.assertEqual(len(self.service.list("isolation_order")), 1)

    def test_non_admin_cannot_register_order(self):
        first = self._positive_batch("C-1")
        with self.assertRaises(PermissionDenied):
            self.service.create(
                Actor("insp-1", "inspector"),
                "isolation_order",
                {"items": [_item(first, "B-1")]},
            )

    def test_recheck_requires_pending_item(self):
        first = self._positive_batch("C-1")
        second = self._positive_batch("C-2")
        order = self.service.create(
            self.admin,
            "isolation_order",
            {"items": [_item(first, "B-1"), _item(second, "B-2")]},
        )
        self.service.transition(
            self.admin,
            order["id"],
            "recheck",
            {"consignment_id": first, "passed": True},
        )
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.admin,
                order["id"],
                "recheck",
                {"consignment_id": first, "passed": True},
            )
        closed = self.service.transition(
            self.admin,
            order["id"],
            "recheck",
            {"consignment_id": second, "passed": True},
        )
        self.assertEqual(closed["status"], "closed")
        with self.assertRaises(InvalidTransition):
            self.service.transition(
                self.admin,
                order["id"],
                "recheck",
                {"consignment_id": second, "passed": True},
            )

    def test_item_must_reference_quarantined_batch(self):
        consignment = self.service.create(
            self.admin,
            "consignment",
            {"code": "C-9", "origin": "A", "destination": "B"},
        )
        with self.assertRaises(ValidationError):
            self.service.create(
                self.admin,
                "isolation_order",
                {"items": [_item(consignment["id"], "B-1")]},
            )
        with self.assertRaises(ValidationError):
            self.service.create(
                self.admin,
                "isolation_order",
                {"items": [_item("missing-id", "B-1")]},
            )

    def test_item_fields_validated(self):
        first = self._positive_batch("C-1")
        with self.assertRaises(ValidationError):
            self.service.create(self.admin, "isolation_order", {"items": []})
        bad_date = _item(first, "B-1", "not-a-date")
        with self.assertRaises(ValidationError):
            self.service.create(self.admin, "isolation_order", {"items": [bad_date]})
        missing = {"consignment_id": first, "bay": "B-1"}
        with self.assertRaises(ValidationError):
            self.service.create(self.admin, "isolation_order", {"items": [missing]})


if __name__ == "__main__":
    unittest.main()
