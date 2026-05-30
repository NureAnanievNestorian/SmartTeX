"""Preparation-result cache for ``prepare_document_work``.

Reuses Django's default cache. Keys are scoped per project and per
``user_request`` hash, anchored to the navigation index's schema version
and the last full-build version number so cache entries auto-invalidate
when those advance.
"""
from __future__ import annotations

import hashlib
import uuid
from copy import deepcopy
from typing import Any, Optional

from django.conf import settings
from django.core.cache import cache


def _ttl() -> int:
    return int(getattr(settings, "NAV_PREPARATION_TTL_SECONDS", 600))


def _max_reuse() -> int:
    return int(getattr(settings, "NAV_PREPARATION_MAX_REUSE", 5))


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:32]


def request_signature(user_request: str) -> str:
    return _sha(user_request.strip().lower())


def _request_key(
    project_id: int, user_request_sha: str, schema_version: int, version_number: int
) -> str:
    return (
        f"navprep:req:{project_id}:{user_request_sha}:"
        f"{schema_version}:{version_number}"
    )


def _id_key(preparation_id: str) -> str:
    return f"navprep:id:{preparation_id}"


def new_preparation_id() -> str:
    return uuid.uuid4().hex


def lookup_by_request(
    *, project_id: int, user_request: str, schema_version: int, version_number: int
) -> Optional[dict[str, Any]]:
    sig = request_signature(user_request)
    raw_id = cache.get(
        _request_key(project_id, sig, schema_version, version_number)
    )
    if not raw_id:
        return None
    return lookup_by_id(raw_id)


def lookup_by_id(preparation_id: str) -> Optional[dict[str, Any]]:
    if not preparation_id:
        return None
    payload = cache.get(_id_key(preparation_id))
    if not payload:
        return None
    return deepcopy(payload)


def store(
    *,
    payload: dict[str, Any],
    project_id: int,
    user_request: str,
    schema_version: int,
    version_number: int,
) -> None:
    preparation_id = payload.get("preparation_id")
    if not preparation_id:
        return
    ttl = _ttl()
    payload = deepcopy(payload)
    payload["fresh_until_seconds"] = ttl
    cache.set(_id_key(preparation_id), payload, timeout=ttl)
    sig = request_signature(user_request)
    cache.set(
        _request_key(project_id, sig, schema_version, version_number),
        preparation_id,
        timeout=ttl,
    )


def store_id_only(payload: dict[str, Any]) -> None:
    """Store under the id-key only (no request-key). Used for non-reusable modes
    so that enforcement lookup_by_id still succeeds even without an index."""
    preparation_id = payload.get("preparation_id")
    if not preparation_id:
        return
    p = deepcopy(payload)
    p["fresh_until_seconds"] = _ttl()
    cache.set(_id_key(preparation_id), p, timeout=_ttl())


def bump_reuse(payload: dict[str, Any]) -> dict[str, Any]:
    payload = deepcopy(payload)
    payload["reuse_count"] = int(payload.get("reuse_count", 0)) + 1
    preparation_id = payload.get("preparation_id")
    if preparation_id:
        cache.set(_id_key(preparation_id), payload, timeout=_ttl())
    return payload


def is_reusable(payload: dict[str, Any]) -> bool:
    if not payload:
        return False
    return int(payload.get("reuse_count", 0)) < _max_reuse()


def invalidate_project(project_id: int) -> None:
    # Backend-agnostic: per-request keys carry the version number, so as
    # soon as the index's last_built_version_number changes the request
    # keys naturally miss. We don't enumerate keys here to stay portable
    # across cache backends (LocMem, Redis, DB).
    return None
