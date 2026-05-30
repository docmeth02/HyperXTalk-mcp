"""Unit tests for the xTalk dictionary parser + lookup/search (phase 8a)."""

import pytest

from hyperxtalk_mcp import dictionary

SAMPLE = """Name: put

Type: command

Syntax: put <value> into <container>

Syntax: put <value> after <container>

Summary:
Places a value into a container.

Example:
put "ABC"

Example:
put 3 into x

Parameters:
value (string):
The value to place.
"""

_has_tree = dictionary.dictionary_root() is not None
_skip = pytest.mark.skipif(not _has_tree, reason="HyperXTalk docs/dictionary tree not present")


def test_parse_lcdoc_structure():
    d = dictionary.parse_lcdoc(SAMPLE)
    assert d["name"] == "put"
    assert d["type"] == "command"
    assert d["syntax"] == ["put <value> into <container>", "put <value> after <container>"]
    assert "Places a value" in d["summary"]
    assert d["examples"] == ['put "ABC"', "put 3 into x"]
    assert d["parameters"].startswith("value (string):")


def test_body_line_ending_in_colon_is_not_a_field():
    # a body line like "value (string):" must stay in the Parameters block, not start a field
    d = dictionary.parse_lcdoc(SAMPLE)
    assert "value (string):" in d["parameters"]


@_skip
def test_lookup_real_term():
    entries = dictionary.lookup("put")
    assert entries and entries[0]["name"].lower() == "put"
    assert any("into" in form for form in entries[0].get("syntax", []))


@_skip
def test_search_by_keyword():
    results = dictionary.search("snapshot", 10)
    assert any("snapshot" in r["name"].lower() for r in results)
    assert all({"name", "type", "summary", "syntax"} <= set(r) for r in results)
