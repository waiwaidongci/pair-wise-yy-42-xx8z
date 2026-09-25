from __future__ import annotations

from typing import Any, Dict, Optional

from .domain import (ASSIGNMENT_STATUSES, RESOURCE_KINDS, RESOURCE_STATUSES,
                     ValidationError, ensure_role, normalize_resource_kind,
                     normalize_severity, require_number, require_text)
from .repository import Repository
from .rules import (ASSIGNMENT_ENTITY, AUDIT_ROLES, CREATE_ROLES, DISPATCH_ROLES,
                    ENTITY, RECORD_ROLES, RELEASE_ROLES, RESOURCE_ENTITY,
                    RESOURCE_ROLES, TITLE, VIEW_ROLES, completion_blockers,
                    escalation_required, priority_score, response_deadline_hours,
                    role_for_transition, validate_transition)


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
        updated, released = self.repository.transition_item(
            item_id, target, expected_version, actor)
        self.repository.append_audit("transition", ENTITY, item_id, actor, {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        })
        for assignment in released:
            self.repository.append_audit("release", ASSIGNMENT_ENTITY,
                                         assignment["id"], actor, {
                "item_id": item_id, "resource_id": assignment["resource_id"],
                "request_ref": assignment["request_ref"], "reason": "event_closed",
            })
        return self.enrich(updated)

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
        ensure_role(role, RESOURCE_ROLES)
        actor = require_text(actor, "actor", 100)
        code = require_text(payload.get("code"), "code", 100)
        kind = normalize_resource_kind(payload.get("kind"))
        type_ = require_text(payload.get("type"), "type", 100)
        status = payload.get("status", "available")
        if status not in ("available", "maintenance"):
            raise ValidationError("status必须是available或maintenance")
        resource = self.repository.create_resource(code, kind, type_, status, actor)
        self.repository.append_audit("register", RESOURCE_ENTITY, resource["id"],
                                     actor, {"code": code, "kind": kind, "type": type_})
        return resource

    def list_resources(self, role: str, kind: Optional[str] = None,
                       status: Optional[str] = None) -> list:
        self._view(role)
        if kind is not None and kind not in RESOURCE_KINDS:
            raise ValidationError("未知资源种类")
        if status is not None and status not in RESOURCE_STATUSES:
            raise ValidationError("未知资源状态")
        return self.repository.list_resources(kind, status)

    def get_resource(self, resource_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        resource = self.repository.get_resource(resource_id)
        resource["active_assignment"] = self.repository.active_assignment_for_resource(
            resource_id)
        return resource

    def update_resource_status(self, resource_id: int, payload: Dict[str, Any],
                               actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, RESOURCE_ROLES)
        actor = require_text(actor, "actor", 100)
        status = payload.get("status")
        if status not in ("available", "maintenance"):
            raise ValidationError("status必须是available或maintenance")
        resource = self.repository.get_resource(resource_id)
        if resource["status"] == "assigned":
            from .domain import ConflictError
            raise ConflictError("资源占用中，请先释放分配")
        updated = self.repository.set_resource_status(resource_id, status)
        self.repository.append_audit("status", RESOURCE_ENTITY, resource_id,
                                     actor, {"status": status})
        return updated

    def assign_resource(self, payload: Dict[str, Any], actor: str,
                        role: str) -> Dict[str, Any]:
        ensure_role(role, DISPATCH_ROLES)
        actor = require_text(actor, "actor", 100)
        request_ref = require_text(payload.get("request_ref"), "request_ref", 100)
        item_id = payload.get("item_id")
        if isinstance(item_id, bool) or not isinstance(item_id, int) or item_id < 1:
            raise ValidationError("item_id必须是正整数")
        resource_id = self._resolve_resource_id(payload)
        assignment, replayed = self.repository.assign_resource(
            request_ref, resource_id, item_id, actor)
        if not replayed:
            self.repository.append_audit("assign", ASSIGNMENT_ENTITY,
                                         assignment["id"], actor, {
                "request_ref": request_ref, "resource_id": resource_id,
                "item_id": item_id,
            })
        result = dict(assignment)
        result["replayed"] = replayed
        return result

    def _resolve_resource_id(self, payload: Dict[str, Any]) -> int:
        resource_id = payload.get("resource_id")
        code = payload.get("resource_code")
        if resource_id is not None:
            if isinstance(resource_id, bool) or not isinstance(resource_id, int) \
                    or resource_id < 1:
                raise ValidationError("resource_id必须是正整数")
            return resource_id
        if code is not None:
            code = require_text(code, "resource_code", 100)
            return self.repository.get_resource_by_code(code)["id"]
        raise ValidationError("必须提供resource_id或resource_code")

    def release_assignment(self, assignment_id: int, actor: str,
                           role: str) -> Dict[str, Any]:
        ensure_role(role, RELEASE_ROLES)
        actor = require_text(actor, "actor", 100)
        assignment, released = self.repository.release_assignment(assignment_id, actor)
        if released:
            self.repository.append_audit("release", ASSIGNMENT_ENTITY,
                                         assignment["id"], actor, {
                "item_id": assignment["item_id"],
                "resource_id": assignment["resource_id"],
                "request_ref": assignment["request_ref"], "reason": "manual",
            })
        result = dict(assignment)
        result["released_now"] = released
        return result

    def list_assignments(self, role: str, item_id: Optional[int] = None,
                         resource_id: Optional[int] = None,
                         status: Optional[str] = None) -> list:
        self._view(role)
        if status is not None and status not in ASSIGNMENT_STATUSES:
            raise ValidationError("未知分配状态")
        return self.repository.list_assignments(item_id, resource_id, status)

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
