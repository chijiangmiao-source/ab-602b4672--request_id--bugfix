"""Envelope contract tests (包络契约测试).

These run inside the verify container before any HTTP smoke test and pin
down the request-envelope contract: parsing, validation, byte-exact
extension extraction, canonical paths and merge adjudication rules.
"""

import pytest

from packager.core import (
    RejectError,
    canonical,
    diff,
    apply_delta,
    parse_path,
    paths_overlap,
    plan_update,
    summary_of,
)
from packager.envelope import EnvelopeError, parse_create, parse_update
from packager.rawjson import RawJsonError, split_object_members


# ---------------------------------------------------------------------------
# raw member splitting: byte-exact
# ---------------------------------------------------------------------------


def test_split_members_preserves_exact_bytes():
    raw = b'{ "a" : 1 , "x_ext" : { "seq": 1e3,  "s": "caf\\u00e9" } , "b":[ 1 ,2 ] }'
    members = split_object_members(raw)
    by_key = {k: (m, v) for k, m, v in members}
    assert by_key["x_ext"][1] == b'{ "seq": 1e3,  "s": "caf\\u00e9" }'
    assert by_key["x_ext"][0] == b'"x_ext" : { "seq": 1e3,  "s": "caf\\u00e9" }'
    assert by_key["b"][1] == b"[ 1 ,2 ]"


def test_split_members_rejects_garbage_and_duplicates():
    with pytest.raises(RawJsonError):
        split_object_members(b'{"a": 1,,}')
    with pytest.raises(RawJsonError):
        split_object_members(b'{"a": 1, "a": 2}')
    with pytest.raises(RawJsonError):
        split_object_members(b'[1, 2]')


# ---------------------------------------------------------------------------
# create envelope
# ---------------------------------------------------------------------------


def _create_body(**over):
    import json

    body = {
        "request_id": "r1",
        "terminal": {"id": "t", "known_fields": ["name", "priority"]},
        "document": {"name": "PKG", "x_vendor": {"n": 1}},
    }
    body.update(over)
    return json.dumps(body).encode()


def test_parse_create_separates_core_and_extension():
    env = parse_create(
        b'{"request_id":"r1","terminal":{"known_fields":["name"]},'
        b'"document":{"name":"A","x_raw": { "k" : [ 1 , 2 ] } }}'
    )
    assert env.core == {"name": "A"}
    assert [(k, v) for k, _m, v in env.ext_pairs] == [("x_raw", b'{ "k" : [ 1 , 2 ] }')]


def test_parse_create_requires_request_id_and_document():
    with pytest.raises(EnvelopeError):
        parse_create(_create_body(request_id=""))
    with pytest.raises(EnvelopeError):
        parse_create(b'{"request_id":"r1","terminal":{"known_fields":[]}}')
    with pytest.raises(EnvelopeError):
        parse_create(b"not json")


def test_parse_create_rejects_known_fields_outside_core():
    with pytest.raises(EnvelopeError):
        parse_create(_create_body(terminal={"known_fields": ["name", "x_vendor"]}))


# ---------------------------------------------------------------------------
# update envelope
# ---------------------------------------------------------------------------


def _update_body(patch, known=("name", "priority", "notes"), base=1, rid="u1"):
    import json

    return json.dumps({
        "request_id": rid,
        "base_revision": base,
        "terminal": {"id": "legacy", "known_fields": list(known)},
        "patch": patch,
    }).encode()


def test_parse_update_validates_envelope_shape():
    with pytest.raises(EnvelopeError):
        parse_update(_update_body({"set": {"name": "x"}}, base=0))
    with pytest.raises(EnvelopeError):
        parse_update(_update_body({"set": {"a..b": 1}}))
    with pytest.raises(EnvelopeError):
        parse_update(_update_body({"delete": "name"}))


def test_parse_update_rejects_unknown_and_undeclared_fields():
    # field outside the core schema -> unknown_field (would corrupt raw ext)
    with pytest.raises(RejectError) as ei:
        parse_update(_update_body({"delete": ["x_vendor"]}))
    assert ei.value.reason == "unknown_field"
    # core field the terminal did not declare -> undeclared_field
    with pytest.raises(RejectError) as ei:
        parse_update(_update_body({"delete": ["notes"]}, known=("name",)))
    assert ei.value.reason == "undeclared_field"


# ---------------------------------------------------------------------------
# canonical paths / summary
# ---------------------------------------------------------------------------


def test_paths_overlap_prefix_semantics():
    assert paths_overlap(parse_path("orbit"), parse_path("orbit.altitude_km"))
    assert paths_overlap(parse_path("orbit.altitude_km"), parse_path("orbit"))
    assert not paths_overlap(parse_path("name"), parse_path("orbit"))


def test_summary_is_canonical_and_order_independent():
    core_a = {"name": "A", "orbit": {"altitude_km": 410}}
    ext_a = [("x_v", b'"x_v": {"b": 1, "a": 2}', b'{"b": 1, "a": 2}')]
    core_b = {"orbit": {"altitude_km": 410}, "name": "A"}
    ext_b = [("x_v", b'"x_v":{"a":2,"b":1}', b'{"a":2,"b":1}')]
    assert summary_of(core_a, ext_a) == summary_of(core_b, ext_b)
    assert summary_of(core_a, ext_a) != summary_of({"name": "B"}, [])


def test_diff_and_apply_delta_roundtrip():
    a = {"name": "A", "orbit": {"altitude_km": 410, "inc": 51.6}, "commands": [1, 2]}
    b = {"name": "B", "orbit": {"altitude_km": 420, "inc": 51.6}, "commands": [1, 2]}
    delta = diff(a, b)
    assert delta == [(("name",), "set", "B"), (("orbit", "altitude_km"), "set", 420)]
    assert apply_delta(a, delta) == b


# ---------------------------------------------------------------------------
# merge adjudication
# ---------------------------------------------------------------------------

BASE = {"name": "A", "priority": "high", "orbit": {"altitude_km": 410}}


def test_disjoint_stale_patch_merges():
    current = dict(BASE, priority="routine")  # someone else changed priority
    plan = plan_update(
        BASE, current,
        set_ops=[(parse_path("name"), "B")], delete_ops=[],
        intervening_paths=["priority"], base_is_current=False,
    )
    assert plan.decision == "merged"
    assert plan.new_core == {"name": "B", "priority": "routine",
                             "orbit": {"altitude_km": 410}}
    assert plan.changed_paths == ["name"]


def test_same_path_different_value_rejected():
    current = dict(BASE, priority="routine")
    with pytest.raises(RejectError) as ei:
        plan_update(
            BASE, current,
            set_ops=[(parse_path("priority"), "low")], delete_ops=[],
            intervening_paths=["priority"], base_is_current=False,
        )
    assert ei.value.reason == "conflicting_paths"
    assert ei.value.detail == ["priority"]


def test_same_path_same_value_is_benign():
    current = dict(BASE, priority="routine")
    plan = plan_update(
        BASE, current,
        set_ops=[(parse_path("priority"), "routine")], delete_ops=[],
        intervening_paths=["priority"], base_is_current=False,
    )
    assert plan.decision == "merged"
    assert plan.new_core["priority"] == "routine"


def test_nested_prefix_overlap_rejected():
    current = dict(BASE, orbit={"altitude_km": 420})
    with pytest.raises(RejectError):
        plan_update(
            BASE, current,
            set_ops=[(parse_path("orbit.altitude_km"), 500)], delete_ops=[],
            intervening_paths=["orbit"], base_is_current=False,
        )


def test_delete_already_gone_is_benign():
    current = {"name": "A", "orbit": {"altitude_km": 410}}  # priority deleted
    plan = plan_update(
        BASE, current,
        set_ops=[], delete_ops=[parse_path("priority")],
        intervening_paths=["priority"], base_is_current=False,
    )
    assert plan.decision == "merged"
    assert "priority" not in plan.new_core


def test_noop_patch_creates_no_revision():
    plan = plan_update(
        BASE, BASE,
        set_ops=[(parse_path("name"), "A")], delete_ops=[],
        intervening_paths=[], base_is_current=True,
    )
    assert plan.decision == "noop"


def test_fresh_base_applies_directly():
    plan = plan_update(
        BASE, BASE,
        set_ops=[(parse_path("name"), "B")], delete_ops=[],
        intervening_paths=[], base_is_current=True,
    )
    assert plan.decision == "applied"
    assert plan.new_core["name"] == "B"


def test_canonical_is_deterministic():
    assert canonical({"b": 1, "a": [2, 3]}) == '{"a":[2,3],"b":1}'
