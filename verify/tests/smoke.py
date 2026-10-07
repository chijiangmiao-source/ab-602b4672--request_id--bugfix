"""HTTP smoke tests: field preservation, conflict adjudication, health.

Run by the verify container against the live app service.  Exits 0 only if
every check passes.
"""

import json
import os
import sys
import time
import urllib.request
import uuid

BASE = os.environ.get("APP_BASE_URL", "http://localhost:8000")

# Unique per run so the smoke suite is repeatable against a reused data volume.
RUN = uuid.uuid4().hex[:8]


def rid(name):
    return f"smoke-{RUN}-{name}"

CHECKS = []


def check(name):
    def deco(fn):
        CHECKS.append((name, fn))
        return fn
    return deco


def req(method, path, body=None, raw=False):
    data = body.encode("utf-8") if isinstance(body, str) else body
    r = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"} if data is not None else {},
    )
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            payload = resp.read()
            headers = {k.lower(): v for k, v in resp.headers.items()}
            return resp.status, (payload if raw else json.loads(payload)), headers
    except urllib.error.HTTPError as e:
        payload = e.read()
        headers = {k.lower(): v for k, v in e.headers.items()}
        if raw:
            return e.code, payload, headers
        return e.code, json.loads(payload), headers


def update_body(rid, base, known, set_ops=None, delete=None):
    return json.dumps({
        "request_id": rid,
        "base_revision": base,
        "terminal": {"id": "smoke-terminal", "known_fields": known},
        "patch": {"set": set_ops or {}, "delete": delete or []},
    })


ALL_CORE = ["name", "satellite_id", "priority", "orbit", "commands",
            "valid_window", "notes"]

# Exact raw extension fragment: odd spacing, 1e3, escaped unicode, key order.
EXT_FRAGMENT = '"x_vendor_meta": { "seq": 1e3,  "note": "caf\\u00e9", "tags": [ "a" , "b" ] }'
CREATE_BODY = (
    '{"request_id": "' + rid('create-1') + '",'
    ' "terminal": {"id": "reviewer", "known_fields": ' + json.dumps(ALL_CORE) + '},'
    ' "document": {"name": "SMOKE-1", "priority": "high",'
    '   "orbit": {"altitude_km": 410}, ' + EXT_FRAGMENT + '}}'
)

STATE = {}


@check("health endpoint responds ok")
def t_health():
    status, body, _ = req("GET", "/healthz")
    assert status == 200 and body.get("status") == "ok", body


@check("create separates core and extension fields")
def t_create():
    status, body, _ = req("POST", "/api/packages", CREATE_BODY)
    assert status == 200, body
    assert body["decision"] == "created" and body["revision"] == 1, body
    assert body["extension_fields"] == ["x_vendor_meta"], body
    STATE["pid"] = body["package_id"]
    STATE["summary1"] = body["summary"]


@check("extension subtree survives byte-for-byte on read")
def t_extension_bytes_preserved():
    status, raw, headers = req("GET", "/api/packages/" + STATE["pid"], raw=True)
    assert status == 200, raw
    text = raw.decode("utf-8")
    assert EXT_FRAGMENT in text, text
    assert headers.get("x-package-revision") == "1", headers
    assert headers.get("x-package-summary") == STATE["summary1"], headers


@check("legacy terminal patch applies and keeps unknown subtree byte-for-byte")
def t_legacy_patch_preserves_extensions():
    pid = STATE["pid"]
    status, body, _ = req("POST", f"/api/packages/{pid}/revisions",
                          update_body(rid('up-1'), 1, ["name"], {"name": "SMOKE-1B"}))
    assert status == 200 and body["decision"] == "applied" and body["revision"] == 2, body
    status, raw, _ = req("GET", "/api/packages/" + pid, raw=True)
    text = raw.decode("utf-8")
    assert EXT_FRAGMENT in text, text
    doc = json.loads(text)
    assert doc["name"] == "SMOKE-1B", doc
    assert doc["x_vendor_meta"]["seq"] == 1000, doc  # 1e3 parsed back


@check("identical request_id replays same revision and summary")
def t_idempotent_replay():
    status, body, _ = req("POST", "/api/packages", CREATE_BODY)
    assert status == 200, body
    assert body["decision"] == "replayed", body
    assert body["revision"] == 1 and body["summary"] == STATE["summary1"], body
    assert body["package_id"] == STATE["pid"], body


@check("same request_id with different payload is rejected, nothing rewritten")
def t_request_id_reuse_rejected():
    bad = CREATE_BODY.replace('"SMOKE-1"', '"SMOKE-1-HIJACK"')
    status, body, _ = req("POST", "/api/packages", bad)
    assert status == 409 and body["reason"] == "request_id_reuse", (status, body)
    status, raw, _ = req("GET", "/api/packages/" + STATE["pid"], raw=True)
    assert b"SMOKE-1-HIJACK" not in raw, raw
    meta = req("GET", "/api/packages/" + STATE["pid"] + "/meta")[1]
    assert meta["current_revision"] == 2, meta  # unchanged


@check("disjoint stale patches merge; overlapping values conflict")
def t_merge_and_conflict():
    body = (
        '{"request_id": "' + rid('create-2') + '",'
        ' "terminal": {"id": "reviewer", "known_fields": ' + json.dumps(ALL_CORE) + '},'
        ' "document": {"name": "SMOKE-2", "priority": "high", "notes": "keep",'
        '   "x_tag": {"v": 1}}}'
    )
    status, created, _ = req("POST", "/api/packages", body)
    assert status == 200, created
    pid = created["package_id"]

    # rev 2: terminal A changes priority, based on rev 1
    status, a, _ = req("POST", f"/api/packages/{pid}/revisions",
                       update_body(rid('up-a'), 1, ["priority"], {"priority": "routine"}))
    assert status == 200 and a["decision"] == "applied" and a["revision"] == 2, a

    # rev 3: terminal B changes name, still based on rev 1 -> disjoint, merges
    status, b, _ = req("POST", f"/api/packages/{pid}/revisions",
                       update_body(rid('up-b'), 1, ["name"], {"name": "SMOKE-2B"}))
    assert status == 200 and b["decision"] == "merged" and b["revision"] == 3, b

    doc = json.loads(req("GET", "/api/packages/" + pid, raw=True)[1])
    assert doc["name"] == "SMOKE-2B" and doc["priority"] == "routine", doc
    assert doc["x_tag"] == {"v": 1}, doc

    # terminal C changes priority to a *different* value, based on rev 1 -> 409
    status, c, _ = req("POST", f"/api/packages/{pid}/revisions",
                       update_body(rid('up-c'), 1, ["priority"], {"priority": "low"}))
    assert status == 409 and c["reason"] == "conflicting_paths", (status, c)
    assert c["detail"] == ["priority"], c

    # same value at the same path is a benign overlap -> merges
    status, d, _ = req("POST", f"/api/packages/{pid}/revisions",
                       update_body(rid('up-d'), 1, ["priority"], {"priority": "routine"}))
    assert status == 200 and d["decision"] == "merged", (status, d)

    meta = req("GET", "/api/packages/" + pid + "/meta")[1]
    assert meta["current_revision"] == 4, meta  # only accepted writes advanced it
    STATE["pid2"] = pid


@check("a rejected request id cannot be reused with another payload")
def t_rejected_request_id_is_bound():
    body = (
        '{"request_id": "' + rid('r-create') + '",'
        ' "terminal": {"id": "reviewer", "known_fields": ' + json.dumps(ALL_CORE) + '},'
        ' "document": {"name": "A", "priority": "high"}}'
    )
    status, created, _ = req("POST", "/api/packages", body)
    assert status == 200 and created["revision"] == 1, created
    pid = created["package_id"]

    # rev 2: advance priority to routine, based on rev 1, fresh request id
    status, r2, _ = req("POST", f"/api/packages/{pid}/revisions",
                        update_body(rid('r-up-2'), 1, ["priority"], {"priority": "routine"}))
    assert status == 200 and r2["decision"] == "applied" and r2["revision"] == 2, r2

    # stale low-priority update still based on rev 1 conflicts and is rejected
    conflict_id = rid('reused-after-rejection')
    conflict_body = update_body(conflict_id, 1, ["priority"], {"priority": "low"})
    status, c, _ = req("POST", f"/api/packages/{pid}/revisions", conflict_body)
    assert status == 409 and c["reason"] == "conflicting_paths", (status, c)
    assert c["detail"] == ["priority"], c

    # same id, different content that would otherwise apply (based on rev 2):
    # the rejection must keep the id bound -> 409 request_id_reuse, rev frozen
    reuse_body = update_body(conflict_id, 2, ["name"], {"name": "B"})
    status, r, _ = req("POST", f"/api/packages/{pid}/revisions", reuse_body)
    assert status == 409 and r["reason"] == "request_id_reuse", (status, r)
    meta = req("GET", f"/api/packages/{pid}/meta")[1]
    assert meta["current_revision"] == 2, meta
    doc = json.loads(req("GET", f"/api/packages/{pid}", raw=True)[1])
    assert doc == {"name": "A", "priority": "routine"}, doc

    # resending the original rejected payload replays the same refusal ...
    status, again, _ = req("POST", f"/api/packages/{pid}/revisions", conflict_body)
    assert status == 409 and again["reason"] == "conflicting_paths", (status, again)
    # ... and a third reuse attempt with yet another payload is still refused
    status, r3, _ = req(
        "POST", f"/api/packages/{pid}/revisions",
        update_body(conflict_id, 2, ["name"], {"name": "C"}),
    )
    assert status == 409 and r3["reason"] == "request_id_reuse", (status, r3)
    meta = req("GET", f"/api/packages/{pid}/meta")[1]
    assert meta["current_revision"] == 2, meta
    STATE["pid3"] = pid


@check("field-boundary rejections bind the request id as well")
def t_field_boundary_rejection_binds_id():
    pid = STATE["pid3"]
    bound_id = rid('bound-after-undeclared')
    # touching an extension field is refused ...
    status, b1, _ = req("POST", f"/api/packages/{pid}/revisions",
                       update_body(bound_id, 2, ["name"], {"x_tag.v": 1}))
    assert status == 409 and b1["reason"] == "unknown_field", (status, b1)
    # ... and the same id cannot afterwards carry an applicable update
    status, b2, _ = req("POST", f"/api/packages/{pid}/revisions",
                       update_body(bound_id, 2, ["name"], {"name": "C"}))
    assert status == 409 and b2["reason"] == "request_id_reuse", (status, b2)

    bound_id2 = rid('bound-after-undeclared-2')
    status, b3, _ = req("POST", f"/api/packages/{pid}/revisions",
                       update_body(bound_id2, 2, ["name"], delete=["notes"]))
    assert status == 409 and b3["reason"] == "undeclared_field", (status, b3)
    status, b4, _ = req("POST", f"/api/packages/{pid}/revisions",
                       update_body(bound_id2, 2, ["name"], {"name": "D"}))
    assert status == 409 and b4["reason"] == "request_id_reuse", (status, b4)

    meta = req("GET", f"/api/packages/{pid}/meta")[1]
    assert meta["current_revision"] == 2, meta
    doc = json.loads(req("GET", f"/api/packages/{pid}", raw=True)[1])
    assert doc == {"name": "A", "priority": "routine"}, doc


@check("deleting unknown or undeclared fields is rejected")
def t_delete_unknown_rejected():
    pid = STATE["pid2"]
    # extension field is unknown to the service schema
    status, body, _ = req("POST", f"/api/packages/{pid}/revisions",
                          update_body(rid('up-e'), 4, ["name"], delete=["x_tag"]))
    assert status == 409 and body["reason"] == "unknown_field", (status, body)
    # core field the terminal did not declare
    status, body, _ = req("POST", f"/api/packages/{pid}/revisions",
                          update_body(rid('up-f'), 4, ["name"], delete=["notes"]))
    assert status == 409 and body["reason"] == "undeclared_field", (status, body)
    # setting an extension path is equally refused
    status, body, _ = req("POST", f"/api/packages/{pid}/revisions",
                          update_body(rid('up-g'), 4, ["name"], {"x_tag.v": 2}))
    assert status == 409 and body["reason"] == "unknown_field", (status, body)
    # nothing was rewritten
    meta = req("GET", "/api/packages/" + pid + "/meta")[1]
    assert meta["current_revision"] == 4, meta
    doc = json.loads(req("GET", "/api/packages/" + pid, raw=True)[1])
    assert doc["x_tag"] == {"v": 1} and doc["notes"] == "keep", doc


@check("adjudication log records every ruling")
def t_adjudications_visible():
    pid = STATE["pid2"]
    status, body, _ = req("GET", f"/api/packages/{pid}/adjudications")
    assert status == 200, body
    decisions = [a["decision"] for a in body["adjudications"]]
    for expected in ("created", "applied", "merged", "rejected"):
        assert expected in decisions, decisions


@check("health endpoint still ok after workload")
def t_health_again():
    t_health()


def main():
    deadline = time.time() + 60
    while True:
        try:
            with urllib.request.urlopen(BASE + "/healthz", timeout=2) as r:
                if r.status == 200:
                    break
        except Exception:
            pass
        if time.time() > deadline:
            print(f"FAIL: {BASE} never became healthy")
            return 1
        time.sleep(1)

    failures = 0
    for name, fn in CHECKS:
        try:
            fn()
            print(f"PASS  {name}")
        except Exception as exc:  # noqa: BLE001 - report and continue
            failures += 1
            print(f"FAIL  {name}: {exc!r}")
    print(f"\n{len(CHECKS) - failures}/{len(CHECKS)} smoke checks passed against {BASE}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
