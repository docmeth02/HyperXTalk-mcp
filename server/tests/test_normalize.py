"""Phase 2: normalizing JsonExport's index-keyed objects back into ordered lists."""

from hyperxtalk_mcp.server import _as_list, _normalize_controls


def test_as_list_index_keyed_object_sorts_numerically():
    # keys are strings; must sort by integer value, not lexically ("10" after "2")
    assert _as_list({"2": "b", "1": "a", "10": "c"}) == ["a", "b", "c"]


def test_as_list_passthrough_and_empty():
    assert _as_list([1, 2, 3]) == [1, 2, 3]
    assert _as_list("") == []
    assert _as_list({}) == []
    assert _as_list(None) == []


def test_normalize_controls_nested_groups():
    controls = {
        "1": {"id": 1, "type": "group", "children": {"1": {"id": 2, "type": "button"}}},
        "2": {"id": 3, "type": "field"},
    }
    out = _normalize_controls(controls)
    assert [c["id"] for c in out] == [1, 3]
    assert out[0]["children"][0]["id"] == 2
    # an empty/absent children collection normalizes to []
    empty_group = _normalize_controls({"1": {"id": 9, "type": "group", "children": ""}})
    assert empty_group[0]["children"] == []
