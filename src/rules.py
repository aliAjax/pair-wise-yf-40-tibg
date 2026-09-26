from datetime import datetime, timedelta, timezone

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)


def _validate_consignment(actor, data, lookup):
    if data.get("origin") == data.get("destination"):
        raise ValidationError("origin and destination must differ")


def _validate_quarantine(actor, entity, data, lookup):
    if not data.get("pest_found"):
        raise ValidationError("pest_found must be true for quarantine")
    return {"quarantined_by": actor.user_id}


def _validate_release(actor, entity, data, lookup):
    if data.get("pest_found"):
        raise ValidationError("pest-positive consignment cannot be released")
    if data.get("treatment") not in ("none", "completed", "certified"):
        raise ValidationError("release requires a valid treatment state")
    return {"released_by": actor.user_id}


DISPOSAL_ITEM_FIELDS = ("consignment_id", "bay_id", "disinfection", "recheck_date")


def _all_entities(lookup, kind):
    if lookup is None:
        return []
    return lookup(kind, None, None) or []


def _validate_disposal_order(actor, data, lookup):
    items = data.get("items")
    if not isinstance(items, list) or not items:
        raise ValidationError("items must be a non-empty list")
    normalized = []
    batches = set()
    bay_holders = {}
    conflicts = []
    for item in items:
        if not isinstance(item, dict):
            raise ValidationError("each disposal item must be an object")
        for field in DISPOSAL_ITEM_FIELDS:
            value = item.get(field)
            if value is None or value == "":
                raise ValidationError("missing required field: items." + field)
        try:
            _date_ordinal(item.get("recheck_date"))
        except (TypeError, ValueError):
            raise ValidationError("items.recheck_date must be an ISO date (YYYY-MM-DD)")
        batch = str(item.get("consignment_id"))
        bay = str(item.get("bay_id"))
        if batch in batches:
            raise ValidationError("duplicate batch in disposal order: " + batch)
        batches.add(batch)
        if bay in bay_holders:
            conflicts.append(
                "bay %s is assigned to both batch %s and batch %s"
                % (bay, bay_holders[bay], batch)
            )
        else:
            bay_holders[bay] = batch
        entry = dict(item)
        entry.update(
            {
                "consignment_id": batch,
                "bay_id": bay,
                "disinfection": str(item.get("disinfection")),
                "recheck_date": str(item.get("recheck_date"))[:10],
                "status": "occupied",
            }
        )
        normalized.append(entry)
    for other in _all_entities(lookup, "disposal_order"):
        for existing in other.get("data", {}).get("items", []):
            if existing.get("status") != "occupied":
                continue
            bay = str(existing.get("bay_id"))
            holder = str(existing.get("consignment_id"))
            if bay in bay_holders:
                conflicts.append(
                    "bay %s requested by batch %s is already occupied by batch %s (order %s)"
                    % (bay, bay_holders[bay], holder, other.get("id"))
                )
            if holder in batches:
                conflicts.append(
                    "batch %s is already isolated in bay %s (order %s)"
                    % (holder, bay, other.get("id"))
                )
    if conflicts:
        raise ConflictError("bay occupancy conflict: " + "; ".join(conflicts))
    return {"items": normalized}


def _validate_disposal_recheck(actor, entity, data, lookup):
    results = data.get("results")
    if not isinstance(results, list) or not results:
        raise ValidationError("results must be a non-empty list")
    items = [dict(item) for item in entity.get("data", {}).get("items", [])]
    positions = {}
    for index, item in enumerate(items):
        if item.get("status") == "occupied":
            positions[str(item.get("consignment_id"))] = index
    seen = set()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for result in results:
        if not isinstance(result, dict):
            raise ValidationError("each recheck result must be an object")
        batch = result.get("consignment_id")
        passed = result.get("passed")
        if batch is None or batch == "":
            raise ValidationError("missing required field: results.consignment_id")
        if not isinstance(passed, bool):
            raise ValidationError("results.passed must be a boolean")
        batch = str(batch)
        if batch in seen:
            raise ValidationError("duplicate recheck result for batch: " + batch)
        seen.add(batch)
        if batch not in positions:
            raise ValidationError("batch is not awaiting recheck in this order: " + batch)
        item = items[positions[batch]]
        item["status"] = "released" if passed else "pending"
        item["recheck_result"] = "passed" if passed else "failed"
        item["rechecked_at"] = now
    still_occupied = any(item.get("status") == "occupied" for item in items)
    return {"items": items, "_next_status": "active" if still_occupied else "completed"}


def disposal_summary(orders):
    bays = {}
    pending_recheck = 0
    for order in orders:
        for item in order.get("data", {}).get("items", []):
            bay_id = str(item.get("bay_id"))
            bay = bays.setdefault(
                bay_id,
                {
                    "bay_id": bay_id,
                    "occupied": False,
                    "consignment_id": None,
                    "order_id": None,
                    "recheck_date": None,
                },
            )
            if item.get("status") == "occupied":
                bay.update(
                    {
                        "occupied": True,
                        "consignment_id": item.get("consignment_id"),
                        "order_id": order.get("id"),
                        "recheck_date": item.get("recheck_date"),
                    }
                )
                pending_recheck += 1
    return {
        "bays": [bays[key] for key in sorted(bays)],
        "pending_recheck": pending_recheck,
        "orders": list(orders),
    }


def trace_downstream(consignments, start_id):
    pending = [start_id]
    visited = set()
    result = []
    while pending:
        current = pending.pop(0)
        if current in visited:
            continue
        visited.add(current)
        result.append(current)
        for item in consignments:
            if item.get("parent_id") == current:
                pending.append(item.get("id"))
    return result


CUSTOM_CREATE = {'consignment': _validate_consignment, 'disposal_order': _validate_disposal_order}
CUSTOM_TRANSITIONS = {('consignment', 'quarantine'): _validate_quarantine, ('consignment', 'release'): _validate_release, ('disposal_order', 'recheck'): _validate_disposal_recheck}


class RuleEngine:
    ALIASES = {'consignments': 'consignment', 'facilities': 'facility', 'disposal_orders': 'disposal_order'}
    INITIAL_STATUS = {'consignment': 'declared', 'facility': 'registered', 'disposal_order': 'active'}
    TRANSITIONS = {'consignment': {'inspect': (('declared',), 'inspected'), 'quarantine': (('inspected',), 'quarantined'), 'release': (('inspected',), 'released'), 'destroy': (('quarantined',), 'destroyed'), 'recheck': (('quarantined',), 'inspected')}, 'facility': {'trace': (('registered',), 'traced')}, 'disposal_order': {'recheck': (('active',), 'completed')}}
    CREATE_REQUIRED = {'consignment': ('code', 'origin', 'destination'), 'facility': ('name', 'address'), 'disposal_order': ('items',)}
    ACTION_REQUIRED = {('consignment', 'inspect'): ('inspector', 'inspection_result'), ('consignment', 'quarantine'): ('pest_found', 'sample_id'), ('consignment', 'release'): ('pest_found', 'treatment'), ('consignment', 'destroy'): ('method', 'witnessed_by'), ('consignment', 'recheck'): ('sample_id',), ('facility', 'trace'): ('consignment_ids',), ('disposal_order', 'recheck'): ('results',)}
    CREATE_ROLES = {'consignment': ('admin', 'inspector'), 'facility': ('admin', 'quarantine'), 'disposal_order': ('admin',)}
    ROLE_ACTIONS = {'inspect': ('admin', 'inspector'), 'quarantine': ('admin', 'quarantine'), 'release': ('admin', 'quarantine'), 'destroy': ('admin', 'quarantine'), 'recheck': ('admin', 'inspector'), 'trace': ('admin', 'quarantine'), ('disposal_order', 'recheck'): ('admin', 'quarantine')}

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def initial_status(self, kind):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        return self.INITIAL_STATUS[kind]

    @staticmethod
    def _ensure_role(actor, allowed):
        if "*" not in allowed and actor.role not in allowed:
            raise PermissionDenied("role %s is not allowed here" % actor.role)

    @staticmethod
    def _require(data, fields):
        for field in fields:
            value = data.get(field)
            if value is None or value == "" or value == [] or value == {}:
                raise ValidationError("missing required field: " + field)

    def validate_create(self, actor, kind, data, lookup=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        self._ensure_role(actor, self.CREATE_ROLES.get(kind, ("admin",)))
        self._require(data, self.CREATE_REQUIRED.get(kind, ()))
        custom = CUSTOM_CREATE.get(kind)
        if custom:
            overrides = custom(actor, data, lookup)
            if overrides:
                data.update(overrides)
        return dict(data)

    def validate_transition(self, actor, entity, action, data, lookup=None):
        kind = self.normalize_kind(entity["kind"])
        transition = self.TRANSITIONS.get(kind, {}).get(action)
        if not transition:
            raise InvalidTransition("unknown action %s for %s" % (action, kind))
        allowed_statuses, next_status = transition
        if entity["status"] not in allowed_statuses:
            raise InvalidTransition(
                "cannot %s from status %s" % (action, entity["status"])
            )
        allowed_roles = self.ROLE_ACTIONS.get(
            (kind, action), self.ROLE_ACTIONS.get(action, ("admin",))
        )
        self._ensure_role(actor, allowed_roles)
        self._require(data, self.ACTION_REQUIRED.get((kind, action), ()))
        custom = CUSTOM_TRANSITIONS.get((kind, action))
        extra = custom(actor, entity, data, lookup) if custom else {}
        if extra and "_next_status" in extra:
            next_status = extra.pop("_next_status")
        patch = dict(data)
        if extra:
            patch.update(extra)
        return next_status, patch


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()
