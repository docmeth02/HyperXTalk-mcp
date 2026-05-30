"""Unit tests for handler parsing/editing (phase 8a)."""

from hyperxtalk_mcp import script_handlers as sh

SCRIPT = 'on mouseUp\n   answer "hi"\nend mouseUp\n\nfunction double n\n   return n * 2\nend double'


def test_parse_handlers_lists_each():
    handlers = sh.parse_handlers(SCRIPT)
    assert [(h.kind, h.name) for h in handlers] == [("on", "mouseUp"), ("function", "double")]
    assert handlers[0].start == 1 and handlers[0].end == 3
    assert handlers[1].start == 5 and handlers[1].end == 7


def test_find_handler_is_case_insensitive():
    h = sh.find_handler(SCRIPT, "MOUSEUP")
    assert h is not None and h.name == "mouseUp"
    assert sh.find_handler(SCRIPT, "nope") is None


def test_nested_control_end_is_ignored():
    s = "on mouseUp\n   repeat 3\n      add 1 to x\n   end repeat\nend mouseUp"
    handlers = sh.parse_handlers(s)
    assert len(handlers) == 1 and handlers[0].end == 5


def test_replace_handler_leaves_siblings():
    new = sh.replace_or_append_handler(
        SCRIPT, "double", "function double n\n   return n + n\nend double"
    )
    assert "n + n" in new and "n * 2" not in new
    assert 'answer "hi"' in new  # sibling untouched
    assert len(sh.parse_handlers(new)) == 2


def test_append_handler_when_absent():
    new = sh.replace_or_append_handler(
        SCRIPT, "openCard", "on openCard\n   pass openCard\nend openCard"
    )
    names = [h.name for h in sh.parse_handlers(new)]
    assert names == ["mouseUp", "double", "openCard"]


def test_crlf_and_trailing_comment_on_end():
    s = "on mouseUp\r\n   beep\r\nend mouseUp -- click\r\n\r\ncommand foo\r\n   return 1\r\nend foo"
    assert [h.name for h in sh.parse_handlers(s)] == ["mouseUp", "foo"]
    new = sh.replace_or_append_handler(s, "mouseUp", "on mouseUp\n   answer 1\nend mouseUp")
    assert "answer 1" in new and "beep" not in new
    assert "command foo" in new  # sibling preserved despite CRLF + trailing comment
    assert "\r" not in new  # normalised to LF


def test_append_to_empty_script():
    new = sh.replace_or_append_handler("", "openCard", "on openCard\nend openCard")
    assert sh.parse_handlers(new)[0].name == "openCard"
