import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from src.domain import (
    Actor,
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)
from src.http_api import create_server
from src.repository import SQLiteRepository
from src.rules import RuleEngine, disposal_summary
from src.service import DomainService


def _item(batch, bay, recheck="2026-10-01", disinfection="spray"):
    return {
        "consignment_id": batch,
        "bay_id": bay,
        "disinfection": disinfection,
        "recheck_date": recheck,
    }


class DisposalOrderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin-1", "admin")

    def tearDown(self):
        self.tmp.cleanup()

    def _order(self, items, idempotency_key=None):
        return self.service.create(
            self.admin, "disposal_order", {"items": items}, idempotency_key=idempotency_key
        )

    def _recheck(self, order, results, actor=None, expected_version=None):
        return self.service.transition(
            actor or self.admin,
            order["id"],
            "recheck",
            {"results": results},
            expected_version=expected_version,
        )

    def test_create_multi_batch_order_occupies_bays(self):
        order = self._order([_item("C-1", "BAY-1"), _item("C-2", "BAY-2")])
        self.assertEqual(order["status"], "active")
        self.assertEqual(
            [item["status"] for item in order["data"]["items"]], ["occupied", "occupied"]
        )
        summary = self.service.disposal_summary()
        self.assertEqual(summary["pending_recheck"], 2)
        bays = {bay["bay_id"]: bay for bay in summary["bays"]}
        self.assertTrue(bays["BAY-1"]["occupied"])
        self.assertEqual(bays["BAY-2"]["consignment_id"], "C-2")
        self.assertEqual(bays["BAY-2"]["recheck_date"], "2026-10-01")

    def test_bay_conflict_within_order_rejects_all(self):
        with self.assertRaises(ConflictError) as ctx:
            self._order([_item("C-1", "BAY-1"), _item("C-2", "BAY-1")])
        message = str(ctx.exception)
        self.assertIn("C-1", message)
        self.assertIn("C-2", message)
        self.assertEqual(self.service.list("disposal_order"), [])

    def test_bay_conflict_across_orders_rejects_all(self):
        first = self._order([_item("C-1", "BAY-1")])
        with self.assertRaises(ConflictError) as ctx:
            self._order([_item("C-2", "BAY-2"), _item("C-3", "BAY-1")])
        message = str(ctx.exception)
        self.assertIn("C-3", message)
        self.assertIn("C-1", message)
        orders = self.service.list("disposal_order")
        self.assertEqual([order["id"] for order in orders], [first["id"]])

    def test_occupied_batch_cannot_be_registered_twice(self):
        self._order([_item("C-1", "BAY-1")])
        with self.assertRaises(ConflictError) as ctx:
            self._order([_item("C-1", "BAY-2")])
        self.assertIn("C-1", str(ctx.exception))
        self.assertEqual(len(self.service.list("disposal_order")), 1)

    def test_recheck_pass_releases_bay_fail_returns_pending(self):
        order = self._order([_item("C-1", "BAY-1"), _item("C-2", "BAY-2")])
        updated = self._recheck(
            order,
            [
                {"consignment_id": "C-1", "passed": True},
                {"consignment_id": "C-2", "passed": False},
            ],
            expected_version=order["version"],
        )
        self.assertEqual(updated["status"], "completed")
        items = {item["consignment_id"]: item for item in updated["data"]["items"]}
        self.assertEqual(items["C-1"]["status"], "released")
        self.assertEqual(items["C-1"]["recheck_result"], "passed")
        self.assertEqual(items["C-2"]["status"], "pending")
        self.assertEqual(items["C-2"]["recheck_result"], "failed")
        summary = self.service.disposal_summary()
        self.assertEqual(summary["pending_recheck"], 0)
        bays = {bay["bay_id"]: bay for bay in summary["bays"]}
        self.assertFalse(bays["BAY-1"]["occupied"])
        self.assertFalse(bays["BAY-2"]["occupied"])

    def test_partial_recheck_keeps_order_active(self):
        order = self._order([_item("C-1", "BAY-1"), _item("C-2", "BAY-2")])
        updated = self._recheck(order, [{"consignment_id": "C-1", "passed": True}])
        self.assertEqual(updated["status"], "active")
        items = {item["consignment_id"]: item for item in updated["data"]["items"]}
        self.assertEqual(items["C-1"]["status"], "released")
        self.assertEqual(items["C-2"]["status"], "occupied")
        self.assertEqual(self.service.disposal_summary()["pending_recheck"], 1)

    def test_recheck_unknown_or_settled_batch_rejected(self):
        order = self._order([_item("C-1", "BAY-1")])
        with self.assertRaises(ValidationError):
            self._recheck(order, [{"consignment_id": "C-9", "passed": True}])
        settled = self._recheck(order, [{"consignment_id": "C-1", "passed": True}])
        self.assertEqual(settled["status"], "completed")
        with self.assertRaises(InvalidTransition):
            self._recheck(order, [{"consignment_id": "C-1", "passed": True}])

    def test_recheck_version_conflict(self):
        order = self._order([_item("C-1", "BAY-1")])
        with self.assertRaises(ConflictError):
            self._recheck(
                order, [{"consignment_id": "C-1", "passed": True}], expected_version=99
            )

    def test_bay_reusable_after_release_and_failed_batch_reregistered(self):
        order = self._order([_item("C-1", "BAY-1"), _item("C-2", "BAY-2")])
        self._recheck(
            order,
            [
                {"consignment_id": "C-1", "passed": True},
                {"consignment_id": "C-2", "passed": False},
            ],
        )
        second = self._order([_item("C-2", "BAY-1"), _item("C-3", "BAY-3")])
        self.assertEqual(second["status"], "active")
        summary = self.service.disposal_summary()
        self.assertEqual(summary["pending_recheck"], 2)
        bays = {bay["bay_id"]: bay for bay in summary["bays"]}
        self.assertEqual(bays["BAY-1"]["consignment_id"], "C-2")

    def test_idempotent_resend_returns_first_order(self):
        first = self._order([_item("C-1", "BAY-1")], idempotency_key="order-77")
        second = self._order([_item("C-1", "BAY-1")], idempotency_key="order-77")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(self.service.list("disposal_order")), 1)

    def test_legacy_consignments_still_listed(self):
        consignment = self.service.create(
            self.admin, "consignment", {"code": "C-0", "origin": "A", "destination": "B"}
        )
        self._order([_item("C-1", "BAY-1")])
        items = self.service.list("consignment")
        self.assertEqual([item["id"] for item in items], [consignment["id"]])
        self.assertNotIn("items", items[0]["data"])

    def test_roles(self):
        with self.assertRaises(PermissionDenied):
            self.service.create(
                Actor("insp", "inspector"), "disposal_order", {"items": [_item("C-1", "BAY-1")]}
            )
        order = self._order([_item("C-1", "BAY-1")])
        with self.assertRaises(PermissionDenied):
            self._recheck(
                order,
                [{"consignment_id": "C-1", "passed": True}],
                actor=Actor("insp", "inspector"),
            )
        updated = self._recheck(
            order,
            [{"consignment_id": "C-1", "passed": True}],
            actor=Actor("qz", "quarantine"),
        )
        self.assertEqual(updated["status"], "completed")

    def test_item_validation(self):
        with self.assertRaises(ValidationError):
            self._order([])
        with self.assertRaises(ValidationError):
            self._order([{"consignment_id": "C-1", "bay_id": "BAY-1"}])
        with self.assertRaises(ValidationError):
            self._order([_item("C-1", "BAY-1", recheck="not-a-date")])
        with self.assertRaises(ValidationError):
            self._order([_item("C-1", "BAY-1"), _item("C-1", "BAY-2")])

    def test_summary_function_counts_only_occupied(self):
        orders = [
            {
                "id": "o1",
                "status": "active",
                "data": {
                    "items": [
                        {"consignment_id": "C-1", "bay_id": "BAY-1", "status": "occupied"},
                        {"consignment_id": "C-2", "bay_id": "BAY-2", "status": "released"},
                        {"consignment_id": "C-3", "bay_id": "BAY-3", "status": "pending"},
                    ]
                },
            }
        ]
        summary = disposal_summary(orders)
        self.assertEqual(summary["pending_recheck"], 1)
        bays = {bay["bay_id"]: bay for bay in summary["bays"]}
        self.assertTrue(bays["BAY-1"]["occupied"])
        self.assertFalse(bays["BAY-2"]["occupied"])
        self.assertFalse(bays["BAY-3"]["occupied"])


class DisposalHttpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.server = create_server("127.0.0.1", 0, self.service, RuleEngine(), "")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.tmp.cleanup()

    def _post(self, path, payload, role="admin", idem=None):
        request = urllib.request.Request(
            "http://127.0.0.1:%d%s" % (self.port, path),
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
        )
        request.add_header("Content-Type", "application/json")
        request.add_header("X-User-Id", "admin-1")
        request.add_header("X-Role", role)
        if idem:
            request.add_header("Idempotency-Key", idem)
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def _get(self, path):
        with urllib.request.urlopen("http://127.0.0.1:%d%s" % (self.port, path)) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def test_summary_endpoint_and_idempotent_resend(self):
        payload = {"items": [_item("C-1", "BAY-1")]}
        status, first = self._post("/api/disposal_order", payload, idem="resend-1")
        self.assertEqual(status, 201)
        status, second = self._post("/api/disposal_order", payload, idem="resend-1")
        self.assertEqual(status, 201)
        self.assertEqual(first["id"], second["id"])
        status, summary = self._get("/api/disposal_summary")
        self.assertEqual(status, 200)
        self.assertEqual(summary["pending_recheck"], 1)
        self.assertEqual(summary["bays"][0]["bay_id"], "BAY-1")
        self.assertTrue(summary["bays"][0]["occupied"])
        self.assertEqual(len(summary["orders"]), 1)

    def test_conflict_returns_409_and_saves_nothing(self):
        payload = {"items": [_item("C-1", "BAY-1"), _item("C-2", "BAY-1")]}
        status, body = self._post("/api/disposal_order", payload)
        self.assertEqual(status, 409)
        self.assertIn("C-1", body["error"])
        self.assertIn("C-2", body["error"])
        _, listed = self._get("/api/disposal_order")
        self.assertEqual(listed["items"], [])

    def test_recheck_over_http_releases_bay(self):
        _, order = self._post("/api/disposal_order", {"items": [_item("C-1", "BAY-1")]})
        status, updated = self._post(
            "/api/entities/%s/actions" % order["id"],
            {
                "action": "recheck",
                "data": {"results": [{"consignment_id": "C-1", "passed": True}]},
                "expected_version": order["version"],
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(updated["status"], "completed")
        _, summary = self._get("/api/disposal_summary")
        self.assertEqual(summary["pending_recheck"], 0)
        self.assertFalse(summary["bays"][0]["occupied"])


if __name__ == "__main__":
    unittest.main()
