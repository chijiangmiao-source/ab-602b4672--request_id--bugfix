"""HTTP service: reviewer web page + JSON API for command package revisions."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from typing import Any, Dict, List, Optional, Tuple

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from .core import RejectError, canonical, plan_update, summary_of
from .envelope import EnvelopeError, parse_create, parse_update
from .store import Store

DATA_DIR = os.environ.get("DATA_DIR", "./data")
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

store = Store(DATA_DIR)
app = FastAPI(title="Satellite Command Package Revision Service")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _error(status: int, payload: Dict[str, Any]) -> JSONResponse:
    return JSONResponse(status_code=status, content=payload)


def _request_hash(body: bytes) -> str:
    """Semantic hash of the request payload (formatting-independent)."""
    return hashlib.sha256(canonical(json.loads(body.decode("utf-8"))).encode("utf-8")).hexdigest()


def _request_id_safe(body: bytes) -> str:
    try:
        rid = json.loads(body.decode("utf-8")).get("request_id")
        return rid if isinstance(rid, str) else "<unknown>"
    except Exception:
        return "<unknown>"


def _bound_request(body: bytes) -> Tuple[Optional[Dict[str, Any]], str]:
    """Look up a request id already bound to a business verdict.

    Returns ``(record, request_id)``; record is None for an unbound id or
    when no request id can be extracted from the body.
    """
    request_id = _request_id_safe(body)
    if request_id == "<unknown>":
        return None, request_id
    return store.find_request(request_id), request_id


def _serve_bound_request(record: Dict[str, Any], body: bytes) -> Response:
    """Replay an already-adjudicated request id, or refuse its reuse.

    Same payload -> replay the stored verdict (200 for applied requests,
    the original 409 for rejected ones).  Different payload -> 409
    ``request_id_reuse`` regardless of whether the original verdict was
    applied or rejected: a rejection binds the id just as firmly and may
    never become reusable for another update.  Nothing is rewritten.
    """
    request_id = record["request_id"]
    try:
        same_payload = record["request_hash"] == _request_hash(body)
    except Exception:
        # An unparseable body cannot be identical to a stored valid payload.
        same_payload = False

    if same_payload:
        stored = json.loads(record["response_json"])
        if record.get("outcome") == "rejected":
            store.record_adjudication(
                record["package_id"], request_id, None, "rejected",
                "identical payload replayed", None,
            )
            return _error(409, stored)
        stored["decision"] = "replayed"
        store.record_adjudication(
            record["package_id"], request_id, None, "replayed",
            "identical payload replayed", stored.get("revision"),
        )
        return _error(200, stored)

    store.record_adjudication(
        record["package_id"], request_id, None, "rejected",
        "request_id_reuse", None,
    )
    return _error(409, {
        "decision": "rejected",
        "reason": "request_id_reuse",
        "detail": "request_id was already used with a different payload",
        "package_id": record["package_id"],
    })


def _reject(
    body: bytes,
    request_id: str,
    package_id: Optional[str],
    base_revision: Optional[int],
    reason: str,
    payload: Dict[str, Any],
) -> Response:
    """Return a 409 and bind the request id so it cannot be reused.

    The rejection payload is itself recorded under the request id: a later
    identical submission replays this same refusal, and a different payload
    is rejected as ``request_id_reuse`` without touching any revision.
    """
    if package_id is not None and request_id != "<unknown>":
        store.record_rejected_request(
            request_id, package_id, _request_hash(body), payload
        )
    if package_id is not None:
        store.record_adjudication(
            package_id, request_id, base_revision, "rejected", reason, None
        )
    return _error(409, payload)


def _splice_document(core: Dict[str, Any], ext_pairs: List[Tuple[str, bytes, bytes]]) -> bytes:
    """Rebuild the full document; extension members keep their exact bytes."""
    members: List[bytes] = []
    for key, value in core.items():
        members.append(
            json.dumps(key, ensure_ascii=False).encode("utf-8")
            + b":"
            + json.dumps(value, ensure_ascii=False).encode("utf-8")
        )
    for _key, member_raw, _value_raw in ext_pairs:
        members.append(member_raw)
    return b"{" + b",".join(members) + b"}"


def _meta(package_id: str) -> Optional[Dict[str, Any]]:
    pkg = store.get_package(package_id)
    if pkg is None:
        return None
    core, ext_pairs, changed_paths, summary = store.get_revision(
        package_id, pkg["current_revision"]
    )
    return {
        "id": package_id,
        "current_revision": pkg["current_revision"],
        "created_at": pkg["created_at"],
        "summary": summary,
        "summary_algorithm": "sha256(canonical-json)",
        "core_fields": sorted(core.keys()),
        "extension_fields": [k for k, _m, _v in ext_pairs],
        "extensions_raw": b",".join(m for _k, m, _v in ext_pairs).decode("utf-8"),
        "changed_paths": changed_paths,
    }


# ---------------------------------------------------------------------------
# pages & health
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    with open(os.path.join(STATIC_DIR, "index.html"), encoding="utf-8") as fh:
        return fh.read()


@app.get("/healthz")
def healthz() -> Dict[str, Any]:
    store.list_packages()  # fail loudly if the store is unusable
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


@app.get("/api/packages")
def list_packages() -> Dict[str, Any]:
    return {"packages": store.list_packages()}


@app.post("/api/packages")
async def create_package(request: Request) -> Response:
    body = await request.body()
    try:
        env = parse_create(body)
    except EnvelopeError as exc:
        return _error(422, {"detail": str(exc)})

    record = store.find_request(env.request_id)
    if record is not None:
        return _serve_bound_request(record, body)

    package_id = uuid.uuid4().hex[:12]
    summary = summary_of(env.core, env.ext_pairs)
    response = {
        "package_id": package_id,
        "revision": 1,
        "summary": summary,
        "decision": "created",
        "reason": None,
        "changed_paths": sorted(env.core.keys()),
        "extension_fields": [k for k, _m, _v in env.ext_pairs],
    }
    store.create_package(
        package_id, env.core, env.ext_pairs, response["changed_paths"], summary, env.request_id
    )
    store.record_request(env.request_id, package_id, _request_hash(body), response)
    store.record_adjudication(package_id, env.request_id, None, "created", None, 1)
    return _error(200, response)


@app.get("/api/packages/{package_id}")
def get_package_document(package_id: str) -> Response:
    """Full document for reading terminals: core + byte-preserved extensions."""
    pkg = store.get_package(package_id)
    if pkg is None:
        return _error(404, {"detail": "package not found"})
    core, ext_pairs, _changed, summary = store.get_revision(package_id, pkg["current_revision"])
    return Response(
        content=_splice_document(core, ext_pairs),
        media_type="application/json",
        headers={
            "X-Package-Revision": str(pkg["current_revision"]),
            "X-Package-Summary": summary,
        },
    )


@app.get("/api/packages/{package_id}/meta")
def get_package_meta(package_id: str) -> Response:
    meta = _meta(package_id)
    if meta is None:
        return _error(404, {"detail": "package not found"})
    return _error(200, meta)


@app.get("/api/packages/{package_id}/adjudications")
def get_adjudications(package_id: str) -> Response:
    if store.get_package(package_id) is None:
        return _error(404, {"detail": "package not found"})
    return _error(200, {"adjudications": store.list_adjudications(package_id)})


@app.post("/api/packages/{package_id}/revisions")
async def submit_revision(package_id: str, request: Request) -> Response:
    body = await request.body()
    pkg = store.get_package(package_id)
    try:
        env = parse_update(body)
    except EnvelopeError as exc:
        return _error(422, {"detail": str(exc)})
    except RejectError as exc:
        # Patch touches a field the terminal must not touch (unknown to the
        # service, or not declared by the terminal).  An id already bound to
        # a verdict governs: replay the prior refusal or reject its reuse.
        if pkg is not None:
            bound, _request_id = _bound_request(body)
            if bound is not None:
                return _serve_bound_request(bound, body)
        payload: Dict[str, Any] = {
            "decision": "rejected", "reason": exc.reason, "detail": exc.detail
        }
        if pkg is not None:
            payload["package_id"] = package_id
        return _reject(
            body, _request_id_safe(body),
            package_id if pkg is not None else None,
            None, exc.reason, payload,
        )

    if pkg is None:
        return _error(404, {"detail": "package not found"})

    # The id is already bound to a verdict: identical payload replays it,
    # a different payload is request_id_reuse -- including an id whose prior
    # verdict was a rejection, which must never become reusable again.
    record = store.find_request(env.request_id)
    if record is not None:
        return _serve_bound_request(record, body)

    current_rev: int = pkg["current_revision"]
    if env.base_revision > current_rev:
        return _error(422, {"detail": f"base_revision {env.base_revision} does not exist"})
    base = store.get_revision(package_id, env.base_revision)
    current = store.get_revision(package_id, current_rev)
    assert base is not None and current is not None
    base_core, _base_ext, _base_changed, _base_summary = base
    current_core, current_ext, _cur_changed, _cur_summary = current

    intervening = store.changed_paths_between(package_id, env.base_revision, current_rev)
    try:
        plan = plan_update(
            base_core,
            current_core,
            env.set_ops,
            env.delete_ops,
            intervening,
            base_is_current=(env.base_revision == current_rev),
        )
    except RejectError as exc:
        payload = {
            "decision": "rejected",
            "reason": exc.reason,
            "detail": exc.detail,
            "package_id": package_id,
            "current_revision": current_rev,
        }
        return _reject(
            body, env.request_id, package_id, env.base_revision, exc.reason, payload
        )
    except ValueError as exc:
        return _error(422, {"detail": str(exc)})

    if plan.decision == "noop":
        response = {
            "package_id": package_id,
            "revision": current_rev,
            "summary": summary_of(current_core, current_ext),
            "decision": "noop",
            "reason": "patch does not change the base revision",
            "changed_paths": [],
            "extension_fields": [k for k, _m, _v in current_ext],
        }
        store.record_request(env.request_id, package_id, _request_hash(body), response)
        store.record_adjudication(
            package_id, env.request_id, env.base_revision, "noop",
            "patch does not change the base revision", current_rev,
        )
        return _error(200, response)

    # The raw extension subtree is carried over untouched: a legacy terminal
    # never rewrites bytes it does not know.
    new_summary = summary_of(plan.new_core, current_ext)
    revision = store.append_revision(
        package_id, plan.new_core, current_ext, plan.changed_paths, new_summary, env.request_id
    )
    response = {
        "package_id": package_id,
        "revision": revision,
        "summary": new_summary,
        "decision": plan.decision,
        "reason": None,
        "changed_paths": plan.changed_paths,
        "extension_fields": [k for k, _m, _v in current_ext],
    }
    store.record_request(env.request_id, package_id, _request_hash(body), response)
    store.record_adjudication(
        package_id, env.request_id, env.base_revision, plan.decision, None, revision
    )
    return _error(200, response)
