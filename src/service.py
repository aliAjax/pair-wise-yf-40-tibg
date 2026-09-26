from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError
from .rules import RuleEngine


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        self.rules.validate_create(actor, kind, payload, self._lookup)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        return updated

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)

    def isolation_dashboard(self):
        orders = self.repository.list_entities(kind="isolation_order")
        consignments = self.repository.list_entities(kind="consignment")
        bays = {}
        pending_recheck = 0
        latest_item_state = {}
        for order in orders:
            for item in order["data"].get("items", []):
                slot = bays.setdefault(
                    item.get("bay"),
                    {
                        "bay": item.get("bay"),
                        "occupied": False,
                        "consignment_id": None,
                        "order_id": None,
                        "recheck_date": None,
                    },
                )
                latest_item_state[item.get("consignment_id")] = item.get("state")
                if item.get("state") == "isolating":
                    pending_recheck += 1
                    slot.update(
                        {
                            "occupied": True,
                            "consignment_id": item.get("consignment_id"),
                            "order_id": order["id"],
                            "recheck_date": item.get("recheck_date"),
                        }
                    )
        pending_disposal = [
            {"id": consignment["id"], "code": consignment["data"].get("code")}
            for consignment in consignments
            if consignment["status"] == "quarantined"
            and latest_item_state.get(consignment["id"]) in (None, "returned")
        ]
        records = [
            {
                "id": order["id"],
                "status": order["status"],
                "created_by": order["created_by"],
                "created_at": order["created_at"],
                "items": order["data"].get("items", []),
            }
            for order in orders
        ]
        return {
            "bays": sorted(bays.values(), key=lambda slot: str(slot["bay"])),
            "pending_recheck_count": pending_recheck,
            "pending_disposal": pending_disposal,
            "pending_disposal_count": len(pending_disposal),
            "records": records,
        }
