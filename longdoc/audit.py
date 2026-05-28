from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal
from functools import wraps
from typing import Any
from uuid import UUID

from django.db import models

from .models import AIBatch, AIBatchChange, AISession, AssistantAuditLog, ProjectRequirement, RequirementSectionRef


AUDIT_EXCLUDED_FIELDS = {"created_at", "updated_at"}


def _serialize_value(value: Any) -> Any:
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, UUID):
        return str(value)
    return value


def serialize_model_instance(instance: models.Model | None) -> dict[str, Any]:
    if instance is None:
        return {}
    data: dict[str, Any] = {}
    for field in instance._meta.concrete_fields:
        if field.primary_key or field.name in AUDIT_EXCLUDED_FIELDS:
            continue
        attr_name = field.attname if field.is_relation else field.name
        data[field.name] = _serialize_value(getattr(instance, attr_name))
    return data


def diff_model_snapshots(before: dict[str, Any] | None, after: dict[str, Any] | None) -> dict[str, list[Any]]:
    before = before or {}
    after = after or {}
    changed: dict[str, list[Any]] = {}
    for field_name in sorted(set(before) | set(after)):
        old_value = before.get(field_name)
        new_value = after.get(field_name)
        if old_value != new_value:
            changed[field_name] = [old_value, new_value]
    return changed


def resolve_audit_project(instance: models.Model):
    if hasattr(instance, "project_id"):
        return instance.project
    if isinstance(instance, RequirementSectionRef):
        return instance.requirement.project
    if isinstance(instance, ProjectRequirement):
        return instance.project
    if isinstance(instance, AIBatch):
        return instance.session.project
    if isinstance(instance, AIBatchChange):
        return instance.batch.session.project
    if isinstance(instance, AISession):
        return instance.project
    raise ValueError(f"Cannot resolve project for {instance.__class__.__name__}")


def create_assistant_audit_log(
    *,
    instance: models.Model,
    operation: str,
    source: str,
    actor=None,
    summary: str = "",
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    changed_fields: dict[str, list[Any]] | None = None,
    object_id: int | None = None,
) -> AssistantAuditLog:
    project = resolve_audit_project(instance)
    payload = changed_fields if changed_fields is not None else diff_model_snapshots(before, after)
    return AssistantAuditLog.objects.create(
        project=project,
        actor=actor,
        source=source,
        model_name=instance.__class__.__name__,
        object_id=object_id if object_id is not None else (instance.pk or 0),
        operation=operation,
        changed_fields=payload,
        summary=summary,
    )


def _first_model_instance(values) -> models.Model | None:
    for value in values:
        if isinstance(value, models.Model):
            return value
    return None


def _resolve_callable_or_value(value, args, kwargs, result):
    return value(args, kwargs, result) if callable(value) else value


def audit_assistant_change(*, operation: str, source, actor=None, summary="", instance_resolver=None):
    def decorator(func):
        @wraps(func)
        def wrapped(*args, **kwargs):
            before_instance = instance_resolver(args, kwargs, None) if instance_resolver else None
            if before_instance is None:
                before_instance = _first_model_instance(list(args) + list(kwargs.values()))
            original_object_id = before_instance.pk if before_instance is not None else None
            before_snapshot = None
            if before_instance is not None and operation != AssistantAuditLog.Operation.CREATE:
                before_snapshot = serialize_model_instance(before_instance)
            result = func(*args, **kwargs)
            audit_instance = instance_resolver(args, kwargs, result) if instance_resolver else None
            if audit_instance is None:
                if isinstance(result, models.Model):
                    audit_instance = result
                elif before_instance is not None:
                    audit_instance = before_instance
            if audit_instance is None:
                raise ValueError("audit_assistant_change could not resolve a model instance")
            after_snapshot = None
            if operation != AssistantAuditLog.Operation.DELETE:
                after_snapshot = serialize_model_instance(audit_instance)
            create_assistant_audit_log(
                instance=audit_instance,
                operation=operation,
                source=_resolve_callable_or_value(source, args, kwargs, result),
                actor=_resolve_callable_or_value(actor, args, kwargs, result),
                summary=_resolve_callable_or_value(summary, args, kwargs, result) or "",
                before=before_snapshot,
                after=after_snapshot,
                object_id=original_object_id if operation == AssistantAuditLog.Operation.DELETE else None,
            )
            return result

        return wrapped

    return decorator
