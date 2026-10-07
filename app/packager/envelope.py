"""Request envelope parsing and validation.

The envelope is the contract between maintenance terminals and the service:

  create:  {"request_id": str, "terminal": {"id": str, "known_fields": [str]},
            "document": {...}}
  update:  {"request_id": str, "base_revision": int,
            "terminal": {"id": str, "known_fields": [str]},
            "patch": {"set": {path: value}, "delete": [path]}}

EnvelopeError maps to HTTP 422 (malformed envelope).  RejectError (from
core) maps to HTTP 409 (adjudicated rejection, nothing is persisted).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from .core import CORE_FIELD_SET, ExtEntry, Path, RejectError, parse_path
from .rawjson import RawJsonError, find_member, split_object_members


class EnvelopeError(ValueError):
    """Malformed request envelope (HTTP 422)."""


def _load(body: bytes) -> Any:
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EnvelopeError(f"body is not valid JSON: {exc}") from exc


def _require(cond: bool, message: str) -> None:
    if not cond:
        raise EnvelopeError(message)


def _parse_terminal(obj: Dict[str, Any]) -> Tuple[Optional[str], List[str]]:
    terminal = obj.get("terminal")
    _require(isinstance(terminal, dict), "terminal must be an object")
    tid = terminal.get("id")
    _require(tid is None or isinstance(tid, str), "terminal.id must be a string")
    known = terminal.get("known_fields")
    _require(
        isinstance(known, list) and all(isinstance(k, str) for k in known),
        "terminal.known_fields must be a list of strings",
    )
    unknown = sorted(set(known) - CORE_FIELD_SET)
    _require(
        not unknown,
        f"terminal.known_fields declares fields outside the core schema: {unknown}",
    )
    return tid, list(known)


def _parse_request_id(obj: Dict[str, Any]) -> str:
    rid = obj.get("request_id")
    _require(isinstance(rid, str) and bool(rid.strip()), "request_id must be a non-empty string")
    return rid


def _validate_patch_path(path_text: str, known_fields: List[str]) -> Path:
    try:
        path = parse_path(path_text)
    except ValueError as exc:
        raise EnvelopeError(str(exc)) from exc
    top = path[0]
    if top not in CORE_FIELD_SET:
        # The field is not part of the core schema: it belongs to the raw
        # extension subtree, which a patch may never touch.
        raise RejectError("unknown_field", path_text)
    if top not in known_fields:
        # The terminal did not declare this field: touching it (in
        # particular deleting it) would silently drop unknown data.
        raise RejectError("undeclared_field", path_text)
    return path


@dataclass
class CreateEnvelope:
    request_id: str
    terminal_id: Optional[str]
    known_fields: List[str]
    core: Dict[str, Any]
    ext_pairs: List[ExtEntry]  # (key, member_raw, value_raw) exact byte slices


@dataclass
class UpdateEnvelope:
    request_id: str
    base_revision: int
    terminal_id: Optional[str]
    known_fields: List[str]
    set_ops: List[Tuple[Path, Any]]
    delete_ops: List[Path]


def parse_create(body: bytes) -> CreateEnvelope:
    obj = _load(body)
    _require(isinstance(obj, dict), "envelope must be a JSON object")
    request_id = _parse_request_id(obj)
    terminal_id, known_fields = _parse_terminal(obj)

    try:
        doc_raw = find_member(body, "document")
    except RawJsonError as exc:
        raise EnvelopeError(f"malformed envelope: {exc}") from exc
    _require(doc_raw is not None, "document is required")

    try:
        members = split_object_members(doc_raw)
    except RawJsonError as exc:
        raise EnvelopeError(f"malformed document: {exc}") from exc

    core: Dict[str, Any] = {}
    ext_pairs: List[ExtEntry] = []
    for key, member_raw, value_raw in members:
        if key in CORE_FIELD_SET:
            core[key] = json.loads(value_raw.decode("utf-8"))
        else:
            # Unknown to the service: keep the exact bytes, never re-serialize.
            ext_pairs.append((key, member_raw, value_raw))
    return CreateEnvelope(
        request_id=request_id,
        terminal_id=terminal_id,
        known_fields=known_fields,
        core=core,
        ext_pairs=ext_pairs,
    )


def parse_update(body: bytes) -> UpdateEnvelope:
    obj = _load(body)
    _require(isinstance(obj, dict), "envelope must be a JSON object")
    request_id = _parse_request_id(obj)
    terminal_id, known_fields = _parse_terminal(obj)

    base = obj.get("base_revision")
    _require(isinstance(base, int) and not isinstance(base, bool) and base >= 1,
             "base_revision must be an integer >= 1")

    patch = obj.get("patch")
    _require(isinstance(patch, dict), "patch must be an object")
    raw_set = patch.get("set", {})
    raw_delete = patch.get("delete", [])
    _require(isinstance(raw_set, dict), "patch.set must be an object")
    _require(
        isinstance(raw_delete, list) and all(isinstance(p, str) for p in raw_delete),
        "patch.delete must be a list of path strings",
    )

    set_ops: List[Tuple[Path, Any]] = []
    for path_text, value in raw_set.items():
        _require(isinstance(path_text, str), "patch.set keys must be path strings")
        set_ops.append((_validate_patch_path(path_text, known_fields), value))
    delete_ops = [_validate_patch_path(p, known_fields) for p in raw_delete]

    return UpdateEnvelope(
        request_id=request_id,
        base_revision=base,
        terminal_id=terminal_id,
        known_fields=known_fields,
        set_ops=set_ops,
        delete_ops=delete_ops,
    )
