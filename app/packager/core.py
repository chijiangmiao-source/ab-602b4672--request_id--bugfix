"""Domain core: canonical form, path algebra and merge adjudication.

Everything in this module is a pure function so the adjudication rules can
be exercised directly by the envelope contract tests without HTTP or storage.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

# Canonical core fields of a command package.  Anything else found in a
# submitted document is an extension field and is persisted as raw bytes.
CORE_FIELDS: Tuple[str, ...] = (
    "name",
    "satellite_id",
    "priority",
    "orbit",
    "commands",
    "valid_window",
    "notes",
)
CORE_FIELD_SET = frozenset(CORE_FIELDS)


class RejectError(Exception):
    """An adjudicated rejection (HTTP 409): the request is well formed but
    must not be applied and must not rewrite any existing version."""

    def __init__(self, reason: str, detail: Any = None):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


def canonical(obj: Any) -> str:
    """Deterministic canonical JSON text used for equality and digests."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


# An extension entry is (key, member_raw, value_raw): the exact byte slices
# of the original document, so the unknown subtree can be spliced back
# byte-for-byte.  Only value_raw feeds the canonical summary.
ExtEntry = Tuple[str, bytes, bytes]


def summary_of(core: Dict[str, Any], ext_pairs: Sequence[ExtEntry]) -> str:
    """Canonical summary digest of the full document (core + extensions)."""
    full: Dict[str, Any] = dict(core)
    for _key, _member_raw, value_raw in ext_pairs:
        full[_key] = json.loads(value_raw.decode("utf-8"))
    return hashlib.sha256(canonical(full).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Canonical paths
# ---------------------------------------------------------------------------

Path = Tuple[str, ...]


def parse_path(text: str) -> Path:
    if not isinstance(text, str) or not text:
        raise ValueError(f"invalid path: {text!r}")
    segs = tuple(text.split("."))
    if any(s == "" for s in segs):
        raise ValueError(f"invalid path: {text!r}")
    return segs


def format_path(path: Path) -> str:
    return ".".join(path)


def paths_overlap(a: Path, b: Path) -> bool:
    """Two canonical paths overlap when one is a prefix of the other."""
    n = min(len(a), len(b))
    return a[:n] == b[:n]


def _is_index(seg: str) -> bool:
    return seg.isdigit()


def get_path(doc: Any, path: Path) -> Any:
    node = doc
    for seg in path:
        if isinstance(node, list):
            if not _is_index(seg) or int(seg) >= len(node):
                raise KeyError(format_path(path))
            node = node[int(seg)]
        elif isinstance(node, dict):
            if seg not in node:
                raise KeyError(format_path(path))
            node = node[seg]
        else:
            raise KeyError(format_path(path))
    return node


def has_path(doc: Any, path: Path) -> bool:
    try:
        get_path(doc, path)
        return True
    except KeyError:
        return False


def set_path(doc: Any, path: Path, value: Any) -> None:
    node = doc
    for i, seg in enumerate(path[:-1]):
        nxt = path[i + 1]
        if isinstance(node, list):
            if not _is_index(seg) or int(seg) >= len(node):
                raise ValueError(f"cannot traverse {format_path(path)}")
            node = node[int(seg)]
        elif isinstance(node, dict):
            if seg not in node:
                node[seg] = [] if _is_index(nxt) else {}
            if not isinstance(node[seg], (dict, list)):
                raise ValueError(f"cannot traverse {format_path(path)}")
            node = node[seg]
        else:
            raise ValueError(f"cannot traverse {format_path(path)}")
    last = path[-1]
    if isinstance(node, list):
        if not _is_index(last) or int(last) >= len(node):
            raise ValueError(f"cannot set {format_path(path)}")
        node[int(last)] = value
    elif isinstance(node, dict):
        node[last] = value
    else:
        raise ValueError(f"cannot set {format_path(path)}")


def delete_path(doc: Any, path: Path) -> bool:
    """Delete the value at path; return True when something was removed."""
    try:
        parent = get_path(doc, path[:-1]) if len(path) > 1 else doc
    except KeyError:
        return False
    last = path[-1]
    if isinstance(parent, list):
        if not _is_index(last) or int(last) >= len(parent):
            return False
        parent.pop(int(last))
        return True
    if isinstance(parent, dict):
        if last not in parent:
            return False
        del parent[last]
        return True
    return False


# ---------------------------------------------------------------------------
# Diff / delta application
# ---------------------------------------------------------------------------

# A delta entry is (path, op, value) with op in {"set", "delete"}.
DeltaEntry = Tuple[Path, str, Any]


def diff(a: Any, b: Any, path: Path = ()) -> List[DeltaEntry]:
    """Leaf-level delta turning ``a`` into ``b`` (lists are atomic)."""
    if isinstance(a, dict) and isinstance(b, dict):
        out: List[DeltaEntry] = []
        for key in a:
            if key not in b:
                out.append((path + (key,), "delete", None))
        for key in b:
            if key not in a:
                out.append((path + (key,), "set", b[key]))
            else:
                out.extend(diff(a[key], b[key], path + (key,)))
        return out
    if canonical(a) == canonical(b):
        return []
    return [(path, "set", b)]


def apply_delta(doc: Any, delta: Sequence[DeltaEntry]) -> Any:
    out = json.loads(canonical(doc))  # deep copy
    for path, op, value in delta:
        if op == "set":
            set_path(out, path, value)
        else:
            delete_path(out, path)
    return out


def apply_patch_ops(
    core: Dict[str, Any],
    set_ops: Sequence[Tuple[Path, Any]],
    delete_ops: Sequence[Path],
) -> Dict[str, Any]:
    """Apply raw patch operations (as submitted) to a core document."""
    out = json.loads(canonical(core))
    for path, value in set_ops:
        set_path(out, path, value)
    for path in delete_ops:
        delete_path(out, path)
    return out


# ---------------------------------------------------------------------------
# Merge adjudication
# ---------------------------------------------------------------------------


@dataclass
class Plan:
    decision: str  # "applied" | "merged" | "noop"
    new_core: Dict[str, Any]
    changed_paths: List[str] = field(default_factory=list)


def plan_update(
    base_core: Dict[str, Any],
    current_core: Dict[str, Any],
    set_ops: Sequence[Tuple[Path, Any]],
    delete_ops: Sequence[Path],
    intervening_paths: Sequence[str],
    base_is_current: bool,
) -> Plan:
    """Decide how a patch based on ``base_core`` relates to ``current_core``.

    The patch is first evaluated against the base revision so that only the
    paths the terminal *actually changed* take part in the overlap check.
    A stale patch merges iff its changed canonical paths do not overlap the
    paths changed by intervening revisions; an overlap is still accepted
    when it is a no-op against the current document (same value, or deleting
    something already gone).  Any other overlap raises RejectError and no
    existing version may be rewritten.
    """
    base_patched = apply_patch_ops(base_core, set_ops, delete_ops)
    delta = diff(base_core, base_patched)
    if not delta:
        return Plan(decision="noop", new_core=current_core, changed_paths=[])

    mine = [entry[0] for entry in delta]
    theirs = [parse_path(p) for p in intervening_paths]

    conflicts: List[str] = []
    for path, op, value in delta:
        if not any(paths_overlap(path, t) for t in theirs):
            continue
        if op == "set":
            try:
                current_value = get_path(current_core, path)
            except KeyError:
                conflicts.append(format_path(path))
                continue
            if canonical(current_value) != canonical(value):
                conflicts.append(format_path(path))
        else:  # delete
            if has_path(current_core, path):
                conflicts.append(format_path(path))
    if conflicts:
        raise RejectError("conflicting_paths", sorted(set(conflicts)))

    new_core = apply_delta(current_core, delta)
    decision = "applied" if base_is_current else "merged"
    return Plan(
        decision=decision,
        new_core=new_core,
        changed_paths=sorted({format_path(p) for p in mine}),
    )
