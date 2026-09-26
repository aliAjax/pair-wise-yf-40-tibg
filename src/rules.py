from datetime import datetime, timedelta

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


ISOLATION_ITEM_FIELDS = ("consignment_id", "bay", "disinfection", "recheck_date")


def _validate_isolation_order(actor, data, lookup):
    items = data.get("items")
    if not isinstance(items, list) or not items:
        raise ValidationError("items must be a non-empty list")
    normalized = []
    bay_holders = {}
    consignment_bays = {}
    for item in items:
        if not isinstance(item, dict):
            raise ValidationError("each item must be an object")
        for field in ISOLATION_ITEM_FIELDS:
            if item.get(field) is None or item.get(field) == "":
                raise ValidationError("missing required field: items." + field)
        try:
            _date_ordinal(item["recheck_date"])
        except (TypeError, ValueError):
            raise ValidationError("items.recheck_date must be an ISO date")
        consignment = _find_one(lookup, "consignment", "id", item["consignment_id"])
        if not consignment:
            raise ValidationError("unknown consignment: " + str(item["consignment_id"]))
        if consignment["status"] != "quarantined":
            raise ValidationError(
                "consignment %s is not a quarantined positive batch"
                % item["consignment_id"]
            )
        entry = dict(item)
        entry["state"] = "isolating"
        normalized.append(entry)
        bay_holders.setdefault(item["bay"], []).append(item["consignment_id"])
        consignment_bays.setdefault(item["consignment_id"], []).append(item["bay"])
    conflicts = []
    for bay, holders in bay_holders.items():
        if len(holders) > 1:
            conflicts.append(
                "bay %s assigned to multiple batches in this order: %s"
                % (bay, ", ".join(str(holder) for holder in holders))
            )
    for consignment_id, bays in consignment_bays.items():
        if len(bays) > 1:
            conflicts.append(
                "consignment %s assigned to multiple bays in this order: %s"
                % (consignment_id, ", ".join(str(bay) for bay in bays))
            )
    active_orders = lookup("isolation_order", "status", "active") if lookup else []
    for order in active_orders or []:
        for existing in order.get("data", {}).get("items", []):
            if existing.get("state") != "isolating":
                continue
            for item in normalized:
                if existing.get("bay") == item["bay"]:
                    conflicts.append(
                        "bay %s already occupied by consignment %s (order %s); "
                        "conflicting batch: %s"
                        % (
                            item["bay"],
                            existing.get("consignment_id"),
                            order.get("id"),
                            item["consignment_id"],
                        )
                    )
                if existing.get("consignment_id") == item["consignment_id"]:
                    conflicts.append(
                        "consignment %s already isolating in order %s"
                        % (item["consignment_id"], order.get("id"))
                    )
    if conflicts:
        raise ConflictError("; ".join(conflicts))
    return {"items": normalized}


def _recheck_isolation_order(actor, entity, data, lookup):
    consignment_id = data.get("consignment_id")
    if data.get("passed") is None:
        raise ValidationError("missing required field: passed")
    passed = bool(data.get("passed"))
    items = [dict(item) for item in entity["data"].get("items", [])]
    target = None
    for item in items:
        if item.get("consignment_id") == consignment_id and item.get("state") == "isolating":
            target = item
            break
    if target is None:
        raise ValidationError(
            "no pending recheck item for consignment: " + str(consignment_id)
        )
    target["state"] = "released" if passed else "returned"
    target["recheck_result"] = "passed" if passed else "failed"
    target["rechecked_by"] = actor.user_id
    next_status = "closed" if all(
        item.get("state") != "isolating" for item in items
    ) else "active"
    patch = {
        "items": items,
        "last_recheck": {
            "consignment_id": consignment_id,
            "passed": passed,
            "rechecked_by": actor.user_id,
        },
    }
    return next_status, patch


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


CUSTOM_CREATE = {'consignment': _validate_consignment, 'isolation_order': _validate_isolation_order}
CUSTOM_TRANSITIONS = {('consignment', 'quarantine'): _validate_quarantine, ('consignment', 'release'): _validate_release, ('isolation_order', 'recheck'): _recheck_isolation_order}


class RuleEngine:
    ALIASES = {'consignments': 'consignment', 'facilities': 'facility', 'isolation_orders': 'isolation_order'}
    INITIAL_STATUS = {'consignment': 'declared', 'facility': 'registered', 'isolation_order': 'active'}
    TRANSITIONS = {'consignment': {'inspect': (('declared',), 'inspected'), 'quarantine': (('inspected',), 'quarantined'), 'release': (('inspected',), 'released'), 'destroy': (('quarantined',), 'destroyed'), 'recheck': (('quarantined',), 'inspected')}, 'facility': {'trace': (('registered',), 'traced')}, 'isolation_order': {'recheck': (('active',), 'active')}}
    CREATE_REQUIRED = {'consignment': ('code', 'origin', 'destination'), 'facility': ('name', 'address'), 'isolation_order': ('items',)}
    ACTION_REQUIRED = {('consignment', 'inspect'): ('inspector', 'inspection_result'), ('consignment', 'quarantine'): ('pest_found', 'sample_id'), ('consignment', 'release'): ('pest_found', 'treatment'), ('consignment', 'destroy'): ('method', 'witnessed_by'), ('consignment', 'recheck'): ('sample_id',), ('facility', 'trace'): ('consignment_ids',), ('isolation_order', 'recheck'): ('consignment_id', 'passed')}
    CREATE_ROLES = {'consignment': ('admin', 'inspector'), 'facility': ('admin', 'quarantine'), 'isolation_order': ('admin',)}
    ROLE_ACTIONS = {'inspect': ('admin', 'inspector'), 'quarantine': ('admin', 'quarantine'), 'release': ('admin', 'quarantine'), 'destroy': ('admin', 'quarantine'), 'recheck': ('admin', 'inspector'), 'trace': ('admin', 'quarantine')}

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
            extra = custom(actor, data, lookup)
            if extra:
                data.update(extra)
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
        result = custom(actor, entity, data, lookup) if custom else None
        if isinstance(result, tuple):
            return result
        patch = dict(data)
        if result:
            patch.update(result)
        return next_status, patch


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()
