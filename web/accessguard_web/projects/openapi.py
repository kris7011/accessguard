"""Deterministic import of an OpenAPI 3 document (JSON) into configuration suggestions.

The document is untrusted input: it is size-limited, only read (never followed: no
remote ``$ref``), and only GET operations are considered. The result is a catalogue of
operations with their response field paths, plus a draft that a person reviews before
anything is applied. Concrete resource ids cannot come from a spec; templates keep their
``{param}`` placeholders until the user fills in ids of their test data.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any

MAX_DOCUMENT_BYTES = 2_000_000
MAX_OPERATIONS = 300
MAX_FIELDS = 40
_SESSION_PATH = re.compile(r"/(me|whoami|current[-_]?user|session|profile|userinfo)$", re.I)
_PARAM = re.compile(r"\{[^}/]+\}")
_IDLIKE = re.compile(r"(^|\.)(id|uuid|created_at|updated_at|createdAt|updatedAt|.*_id|.*Id)$")
_SUBJECT_FIELDS = ("email", "username", "login", "sub", "id")


class OpenApiError(Exception):
    """The document is not a usable OpenAPI 3 JSON document."""


@dataclass(frozen=True)
class Operation:
    path: str
    summary: str
    kind: str  # "item" (ends with a path parameter), "collection", "session" or "other"
    fields: tuple[str, ...]
    """Dotted paths of string fields in the 200 response (``data.name``)."""
    list_items: bool = False


@dataclass(frozen=True)
class Draft:
    path_prefix: str
    session_probe_path: str
    subject_field: str
    collections: list[dict[str, str]] = field(default_factory=list)
    templates: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def parse_document(raw: bytes) -> dict[str, Any]:
    if len(raw) > MAX_DOCUMENT_BYTES:
        raise OpenApiError(f"The document is larger than {MAX_DOCUMENT_BYTES // 1_000_000} MB")
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise OpenApiError(
            "The document is not valid JSON. Export your OpenAPI document as JSON "
            "(YAML is not supported yet)."
        ) from None
    if not isinstance(document, dict) or not str(document.get("openapi", "")).startswith("3."):
        raise OpenApiError("Only OpenAPI 3.x documents are supported")
    if not isinstance(document.get("paths"), dict):
        raise OpenApiError("The document has no paths")
    return document


def _resolve(document: dict[str, Any], schema: Any, depth: int = 0) -> dict[str, Any]:
    """Resolve a local ``#/components/schemas/...`` reference (never a remote one)."""
    if not isinstance(schema, dict) or depth > 5:
        return {}
    ref = schema.get("$ref")
    if isinstance(ref, str):
        if not ref.startswith("#/components/schemas/"):
            return {}
        name = ref.rsplit("/", 1)[-1]
        target = ((document.get("components") or {}).get("schemas") or {}).get(name)
        return _resolve(document, target, depth + 1)
    for combinator in ("allOf",):
        if isinstance(schema.get(combinator), list):
            merged: dict[str, Any] = {"type": "object", "properties": {}}
            for part in schema[combinator]:
                merged["properties"].update(
                    _resolve(document, part, depth + 1).get("properties", {})
                )
            return merged
    return schema


def _string_fields(
    document: dict[str, Any], schema: Any, prefix: str = "", depth: int = 0
) -> tuple[list[str], bool]:
    """Collect dotted string-field paths; descend into objects and into array items once."""
    schema = _resolve(document, schema)
    found: list[str] = []
    is_list = False
    if schema.get("type") == "array":
        inner, _ = _string_fields(document, schema.get("items"), prefix, depth + 1)
        return inner, True
    for name, value in (schema.get("properties") or {}).items():
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,63}", name):
            continue
        path = f"{prefix}{name}"
        resolved = _resolve(document, value)
        if resolved.get("type") == "string":
            found.append(path)
        elif depth < 2 and (
            resolved.get("type") in ("object", "array") or "properties" in resolved
        ):
            inner, inner_list = _string_fields(document, resolved, f"{path}.", depth + 1)
            is_list = is_list or inner_list
            found.extend(inner)
    return found[:MAX_FIELDS], is_list


def catalogue(document: dict[str, Any]) -> list[Operation]:
    operations: list[Operation] = []
    for path, item in list(document["paths"].items())[:MAX_OPERATIONS]:
        if not isinstance(path, str) or not path.startswith("/") or not isinstance(item, dict):
            continue
        get = item.get("get")
        if not isinstance(get, dict):
            continue
        responses = get.get("responses") or {}
        ok = responses.get("200") if isinstance(responses, dict) else None
        content = ok.get("content") if isinstance(ok, dict) else None
        content = content if isinstance(content, dict) else {}
        schema = ((content.get("application/json") or {}).get("schema")) or {}
        fields, is_list = _string_fields(document, schema)
        if _PARAM.search(path.rsplit("/", 1)[-1]):
            kind = "item"
        elif _SESSION_PATH.search(path):
            kind = "session"
        elif is_list:
            kind = "collection"
        else:
            kind = "other"
        summary = str(get.get("summary") or get.get("operationId") or "")[:200]
        operations.append(Operation(path, summary, kind, tuple(fields), is_list))
    return operations


def _common_prefix(paths: list[str]) -> str:
    if not paths:
        return "/"
    split = [p.strip("/").split("/") for p in paths]
    common: list[str] = []
    for parts in zip(*split, strict=False):
        if len(set(parts)) != 1 or _PARAM.search(parts[0]):
            break
        common.append(parts[0])
    return "/" + "/".join(common) if common else "/"


def protected_candidates(fields: tuple[str, ...]) -> list[str]:
    return [f for f in fields if not _IDLIKE.search(f)][:3]


def category_for(path: str) -> str:
    segments = [s for s in path.strip("/").split("/") if s and not _PARAM.search(s)]
    name = segments[-1] if segments else "resource"
    name = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return (name[:-1] if name.endswith("s") and len(name) > 3 else name) or "resource"


def deterministic_draft(operations: list[Operation]) -> Draft:
    notes: list[str] = []
    session = next((op for op in operations if op.kind == "session"), None)
    subject = ""
    if session is not None:
        subject = next(
            (f for name in _SUBJECT_FIELDS for f in session.fields if f.rsplit(".", 1)[-1] == name),
            session.fields[0] if session.fields else "",
        )
    else:
        notes.append("No 'current user' endpoint (such as /me) was found; add a session check.")
    collections = [
        {"id": category_for(op.path) + "-list", "path": op.path, "summary": op.summary}
        for op in operations
        if op.kind == "collection"
    ]
    templates = [
        {
            "path": op.path,
            "summary": op.summary,
            "category": category_for(op.path),
            "protected_fields": protected_candidates(op.fields),
            "fields": list(op.fields),
        }
        for op in operations
        if op.kind == "item"
    ]
    if not templates:
        notes.append("No GET endpoints that read one record by id were found.")
    return Draft(
        path_prefix=_common_prefix([op.path for op in operations]),
        session_probe_path=session.path if session else "",
        subject_field=subject,
        collections=collections,
        templates=templates,
        notes=notes,
    )
