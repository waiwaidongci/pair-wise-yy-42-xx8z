from __future__ import annotations

from typing import Any, Dict, Optional

from .domain import (ConflictError, ensure_role, normalize_resource_kind,
                     normalize_resource_type, normalize_severity, require_number,
                     require_text)
from .repository import Repository
from .rules import (ALLOCATE_ROLES, AUDIT_ROLES, CREATE_ROLES, ENTITY,
                    RECORD_ROLES, RELEASE_ROLES, RESOURCE_REGISTER_ROLES, TITLE,
                    VIEW_ROLES, completion_blockers, escalation_required,
                    priority_score, response_deadline_hours, role_for_transition,
                    validate_transition)


class Service:
    def __init__(self, repository: Repository):
        self.repository = repository

    def _view(self, role: str) -> None:
        ensure_role(role, VIEW_ROLES)

    def create_item(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        title = require_text(payload.get("title"), "title", 200)
        description = require_text(payload.get("description"), "description")
        severity = normalize_severity(payload.get("severity"))
        quantity = require_number(payload.get("quantity", 0), "quantity")
        threshold = require_number(payload.get("threshold", 1), "threshold", 0.000001)
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        item = self.repository.create_item(title, description, severity, quantity,
                                           threshold, external_ref, actor)
        self.repository.append_audit("create", ENTITY, item["id"], actor, {
            "title": title, "severity": severity, "quantity": quantity,
            "priority": priority_score(severity, quantity, threshold),
        })
        return self.enrich(item)

    def add_record(self, item_id: int, payload: Dict[str, Any], actor: str,
                   role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        kind = require_text(payload.get("kind"), "kind", 100)
        detail = require_text(payload.get("detail"), "detail")
        status = payload.get("status", "open")
        if status not in ("open", "closed"):
            raise ValueError("status必须是open或closed")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        record = self.repository.add_record(item_id, kind, detail, status,
                                            external_ref, actor)
        self.repository.append_audit("record", ENTITY, item_id, actor, {
            "record_id": record["id"], "kind": kind, "status": status,
        })
        return record

    def transition(self, item_id: int, target: str, expected_version: int,
                   actor: str, role: str) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        validate_transition(item["status"], target)
        ensure_role(role, role_for_transition(target))
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        blockers = completion_blockers(target, self.repository.open_record_count(item_id))
        if blockers:
            from .domain import ConflictError
            raise ConflictError("；".join(blockers))
        updated = self.repository.transition_item(item_id, target, expected_version, actor)
        self.repository.append_audit("transition", ENTITY, item_id, actor, {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        })
        result = self.enrich(updated)
        if target == "closed":
            released = self.repository.release_for_item(item_id, actor)
            if released:
                self.repository.append_audit("release_on_close", "allocation",
                                             item_id, actor, {"allocation_ids": released})
            result["released_allocations"] = released
        return result

    def get_item(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.enrich(self.repository.get_item(item_id))

    def list_items(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        return [self.enrich(item) for item in self.repository.list_items(status)]

    def list_records(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_records(item_id)

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    def register_resource(self, payload: Dict[str, Any], actor: str,
                          role: str) -> Dict[str, Any]:
        ensure_role(role, RESOURCE_REGISTER_ROLES)
        actor = require_text(actor, "actor", 100)
        code = require_text(payload.get("code"), "code", 60)
        kind = normalize_resource_kind(payload.get("kind"))
        rtype = require_text(payload.get("type"), "type", 60)
        rtype = normalize_resource_type(kind, rtype)
        resource = self.repository.create_resource(code, kind, rtype, actor)
        self.repository.append_audit("resource_register", "resource",
                                     resource["id"], actor,
                                     {"code": code, "kind": kind, "type": rtype})
        return self.enrich_resource(resource)

    def list_resources(self, role: str, kind: Optional[str] = None,
                       status: Optional[str] = None) -> list:
        self._view(role)
        if kind is not None:
            normalize_resource_kind(kind)
        if status is not None and status not in ("available", "occupied"):
            from .domain import ValidationError
            raise ValidationError("status必须是available或occupied")
        result = []
        for resource in self.repository.list_resources(kind, status):
            result.append(self.enrich_resource(resource))
        return result

    def get_resource(self, resource_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.enrich_resource(self.repository.get_resource(resource_id))

    def get_resource_by_code(self, code: str, role: str) -> Dict[str, Any]:
        self._view(role)
        code = require_text(code, "code", 60)
        resource = self.repository.get_resource_by_code(code)
        if resource is None:
            from .domain import NotFoundError
            raise NotFoundError("资源不存在")
        return self.enrich_resource(resource)

    def allocate(self, payload: Dict[str, Any], actor: str,
                 role: str) -> Dict[str, Any]:
        ensure_role(role, ALLOCATE_ROLES)
        actor = require_text(actor, "actor", 100)
        request_no = require_text(payload.get("request_no"), "request_no", 100)
        note = payload.get("note", "")
        if not isinstance(note, str) or len(note) > 2000:
            from .domain import ValidationError
            raise ValidationError("note必须是字符串且不超过2000字符")
        item_id = payload.get("item_id")
        if item_id is not None:
            if isinstance(item_id, bool) or not isinstance(item_id, int) or item_id < 1:
                from .domain import ValidationError
                raise ValidationError("item_id必须是正整数")
            linked = self.repository.get_item(item_id)
            if linked["status"] == "closed":
                raise ConflictError("事件已办结，不能再派单")
        codes = payload.get("resource_codes")
        if not isinstance(codes, list) or not codes:
            from .domain import ValidationError
            raise ValidationError("resource_codes必须是非空数组")
        normalized, seen = [], set()
        for raw in codes:
            code = require_text(raw, "resource_code", 60)
            if code in seen:
                from .domain import ValidationError
                raise ValidationError(f"资源{code}在同一请求中重复")
            seen.add(code)
            resource = self.repository.get_resource_by_code(code)
            if resource is None:
                from .domain import NotFoundError
                raise NotFoundError(f"资源不存在: {code}")
            normalized.append(resource)
        try:
            outcome = self.repository.allocate_resources(
                request_no, item_id, note.strip(),
                [{"id": r["id"], "code": r["code"], "kind": r["kind"],
                  "type": r["type"]} for r in normalized], actor)
        except ConflictError as exc:
            if exc.details and "occupied" in exc.details:
                exc.details["request_no"] = request_no
            raise
        allocation = outcome["allocation"]
        if not outcome["replayed"]:
            self.repository.append_audit("allocate", "allocation",
                                         allocation["id"], actor, {
                "request_no": request_no, "item_id": item_id,
                "resource_codes": list(seen)})
        return self.enrich_allocation(allocation, outcome["replayed"])

    def release(self, allocation_id: int, payload: Optional[Dict[str, Any]],
                actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, RELEASE_ROLES)
        actor = require_text(actor, "actor", 100)
        request_no = None
        if payload:
            raw = payload.get("request_no")
            if raw is not None:
                request_no = require_text(raw, "request_no", 100)
        if request_no is not None:
            found = self.repository.find_allocation_by_request(request_no)
            if found is None:
                from .domain import NotFoundError
                raise NotFoundError("request_no对应的分配单不存在")
            allocation_id = found["id"]
        outcome = self.repository.release_allocation(allocation_id, actor)
        allocation = outcome["allocation"]
        if not outcome["replayed"]:
            self.repository.append_audit("release", "allocation",
                                         allocation["id"], actor,
                                         {"request_no": allocation["request_no"]})
        return self.enrich_allocation(allocation, outcome["replayed"])

    def get_allocation(self, allocation_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.enrich_allocation(self.repository.get_allocation(allocation_id))

    def list_allocations(self, role: str, status: Optional[str] = None,
                         item_id: Optional[int] = None) -> list:
        self._view(role)
        if status is not None and status not in ("active", "released"):
            from .domain import ValidationError
            raise ValidationError("status必须是active或released")
        return [self.enrich_allocation(a)
                for a in self.repository.list_allocations(status, item_id)]

    def enrich_resource(self, resource: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(resource)
        hold_map = self.repository.hold_map([resource["id"]])
        hold = hold_map.get(resource["id"])
        result["current_hold"] = hold if hold else None
        return result

    def enrich_allocation(self, allocation: Dict[str, Any],
                          replayed: bool = False) -> Dict[str, Any]:
        result = dict(allocation)
        result["resources"] = self.repository.allocation_resources(
            allocation["id"])
        result["replayed"] = replayed
        return result

    @staticmethod
    def enrich(item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"])
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        return result
