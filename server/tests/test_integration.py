"""Phase 1 integration tests against the REAL bridge running inside HyperXTalk.

Skipped automatically unless a live bridge handshake is present. To run: launch HyperXTalk, open
the hxt-mcp-bridge plugin (see bridge/install.md), then `pytest -m integration`.
"""

import pytest

from hyperxtalk_mcp.bridge_client import BridgeClient, find_handshake

pytestmark = pytest.mark.integration

_LIVE = find_handshake() is not None
_skip = pytest.mark.skipif(not _LIVE, reason="no live HyperXTalk bridge handshake found")


@_skip
def test_ping_real_bridge():
    result = BridgeClient().call("ping")
    assert "engine" in result and "platform" in result


@_skip
def test_large_body_echo_real_bridge():
    # A realistic-but-substantial payload (~256 KB) with multibyte + every escape, proving the
    # Content-Length reader and JSON escaping round-trip with no truncation. NOTE: the bridge's
    # current string handling is O(n^2), so payloads >~0.5 MB get slow (correct, just slow) — a
    # tracked follow-up. MCP script payloads are far smaller than this.
    payload = '线 quote=" newline\n tab\t backslash\\ ' * 6_500  # ~250 KB, multibyte + escapes
    assert len(payload) > 200_000
    result = BridgeClient(timeout=30).call("echo", {"data": payload})
    assert result["data"] == payload


@_skip
def test_discovery_and_read_real_bridge():
    """Adaptive: enumerate whatever user stacks are open and read tree/script/props."""
    from hyperxtalk_mcp.server import _as_list, _normalize_controls

    c = BridgeClient(timeout=20)
    stacks = _as_list(c.call("stacks.list").get("stacks"))
    if not stacks:
        pytest.skip("no user stacks open in HyperXTalk (open one, e.g. allcontrols2.hyperxtalk)")

    s = stacks[0]
    assert s["handle"] and s["name"]

    tree = c.call("tree.get", {"handle": s["handle"]})
    cards = _as_list(tree.get("cards"))
    assert cards, "stack should have at least one card"
    assert all(card.get("handle") for card in cards)

    # script of the stack (may be empty, but the field must be present)
    assert "script" in c.call("object.getScript", {"handle": s["handle"]})

    # props of the first card
    card_props = c.call("object.getProps", {"handle": cards[0]["handle"]})["props"]
    assert card_props.get("type") == "card"

    # if the card has controls, read the first one's props + confirm a stable handle/type
    controls = _normalize_controls(cards[0].get("controls"))
    if controls:
        cp = c.call("object.getProps", {"handle": controls[0]["handle"]})["props"]
        assert cp.get("id") and cp.get("type")


@_skip
def test_safe_to_edit_real_bridge():
    """The pause switch flips safeToEdit; pause is checked first so it's robust to stack state."""
    from hyperxtalk_mcp.server import _as_list, _normalize_controls

    c = BridgeClient(timeout=15)
    stacks = _as_list(c.call("stacks.list").get("stacks"))
    if not stacks:
        pytest.skip("no user stacks open in HyperXTalk")
    tree = c.call("tree.get", {"handle": stacks[0]["handle"]})
    cards = _as_list(tree.get("cards"))
    ctrls = _normalize_controls(cards[0].get("controls"))
    handle = (ctrls[0] if ctrls else cards[0])["handle"]

    try:
        c.call("bridge.setPaused", {"paused": True})
        paused = c.call("bridge.safeToEdit", {"handle": handle})
        assert paused["safe"] is False and paused["reason"] == "paused"
    finally:
        c.call("bridge.setPaused", {"paused": False})

    after = c.call("bridge.safeToEdit", {"handle": handle, "operation": "setProps"})
    assert after.get("reason") != "paused"  # unpaused (safe, or some other concrete reason)


@_skip
def test_edit_roundtrip_real_bridge():
    """Create -> set props/script -> read back -> clone -> delete. Self-cleaning; never saves."""
    from hyperxtalk_mcp.errors import BridgeError
    from hyperxtalk_mcp.server import _as_list

    c = BridgeClient(timeout=20)
    stacks = _as_list(c.call("stacks.list").get("stacks"))
    if not stacks:
        pytest.skip("no user stacks open in HyperXTalk")
    card_handle = _as_list(c.call("tree.get", {"handle": stacks[0]["handle"]}).get("cards"))[0][
        "handle"
    ]

    created = []
    try:
        made = c.call(
            "control.create",
            {
                "parentHandle": card_handle,
                "type": "button",
                "name": "MCP Test Button",
                "props": {"width": 120},
            },
        )
        assert made.get("type") == "button" and made.get("id") and made.get("handle")
        h = made["handle"]
        created.append(h)

        props = c.call("object.getProps", {"handle": h})["props"]
        assert props["name"] == "MCP Test Button"
        # `the properties` reports geometry via `rect` (L,T,R,B), not a `width` key
        left, _top, right, _bottom = (int(n) for n in str(props["rect"]).split(","))
        assert right - left == 120

        script = 'on mouseUp\n   answer "hi from mcp"\nend mouseUp'
        c.call("object.setScript", {"handle": h, "script": script})
        assert c.call("object.getScript", {"handle": h})["script"].strip() == script.strip()

        # a syntax error is rejected and the good script is preserved
        bad = "on mouseUp\n   repeat\nend mouseUp"
        with pytest.raises(BridgeError) as ce:
            c.call("object.setScript", {"handle": h, "script": bad})
        assert ce.value.kind == "compile"
        assert c.call("object.getScript", {"handle": h})["script"].strip() == script.strip()

        cloned = c.call("object.clone", {"handle": h, "name": "MCP Test Clone"})
        assert cloned.get("handle")
        created.append(cloned["handle"])
        clone_props = c.call("object.getProps", {"handle": cloned["handle"]})["props"]
        assert clone_props["name"] == "MCP Test Clone"
    finally:
        for handle in created:
            try:
                c.call("object.delete", {"handle": handle})
            except BridgeError:
                pass

    # deleted -> handles are now stale
    for handle in created:
        with pytest.raises(BridgeError) as de:
            c.call("object.getProps", {"handle": handle})
        assert de.value.kind == "stale_handle"


@_skip
def test_handle_survives_sibling_create_real_bridge():
    """Regression: a handle minted BEFORE a control-create must still resolve AFTER it.

    A stack's `the id` is its per-stack id counter (bumped by every newid() on control creation),
    so the resolver must NOT key on it. Before the fix, creating any control invalidated every
    outstanding handle for that stack (spurious stale_handle).
    """
    from hyperxtalk_mcp.errors import BridgeError
    from hyperxtalk_mcp.server import _as_list

    c = BridgeClient(timeout=20)
    stacks = _as_list(c.call("stacks.list").get("stacks"))
    if not stacks:
        pytest.skip("no user stacks open in HyperXTalk")
    # card_handle is minted now, before any creation below
    card_handle = _as_list(c.call("tree.get", {"handle": stacks[0]["handle"]}).get("cards"))[0][
        "handle"
    ]

    created = []
    try:
        a = c.call(
            "control.create",
            {"parentHandle": card_handle, "type": "button", "name": "MCP Sibling A"},
        )
        created.append(a["handle"])
        # the create above bumped the stack's id counter; the SAME pre-create handle must still work
        b = c.call(
            "control.create",
            {"parentHandle": card_handle, "type": "button", "name": "MCP Sibling B"},
        )
        created.append(b["handle"])
        # and a read through the stale-prone stack handle still resolves
        assert _as_list(c.call("tree.get", {"handle": stacks[0]["handle"]}).get("cards"))
    finally:
        for handle in created:
            try:
                c.call("object.delete", {"handle": handle})
            except BridgeError:
                pass


@_skip
def test_stale_handle_real_bridge():
    """A handle for a stack that isn't open resolves fail-closed to stale_handle."""
    from hyperxtalk_mcp import handles
    from hyperxtalk_mcp.errors import BridgeError

    bogus = handles.encode(
        {
            "v": 1,
            "stack": {
                "shortName": "NoSuchStack_xyz",
                "mainStackName": "NoSuchStack_xyz",
                "fileName": "",
                "stackId": 999999,
            },
            "cardId": 1,
            "objType": "stack",
            "objId": 999999,
        }
    )
    with pytest.raises(BridgeError) as exc:
        BridgeClient(timeout=10).call("object.getScript", {"handle": bogus})
    assert exc.value.kind == "stale_handle"


@_skip
def test_create_stack_card_delete_real_bridge():
    """Phase 7: create a scratch stack -> add a card -> verify -> delete card -> delete stack."""
    from hyperxtalk_mcp.errors import BridgeError
    from hyperxtalk_mcp.server import _as_list

    c = BridgeClient(timeout=20)
    made = c.call("stack.create", {"name": "MCP Scratch Stack"})
    assert made.get("handle") and made.get("name")
    stack_handle = made["handle"]
    try:
        cards = _as_list(c.call("tree.get", {"handle": stack_handle}).get("cards"))
        assert len(cards) == 1  # a fresh stack has exactly one card

        new_card = c.call("card.create", {"stackHandle": stack_handle})
        assert new_card.get("id") and new_card.get("handle")
        cards2 = _as_list(c.call("tree.get", {"handle": stack_handle}).get("cards"))
        assert len(cards2) == 2

        # the new card handle resolves and is a card
        assert c.call("object.getProps", {"handle": new_card["handle"]})["props"]["type"] == "card"
    finally:
        # unsaved scratch stack -> deletable; this also removes its cards
        try:
            c.call("stack.delete", {"handle": stack_handle})
        except BridgeError:
            pass
    # gone -> stale
    with pytest.raises(BridgeError) as exc:
        c.call("tree.get", {"handle": stack_handle})
    assert exc.value.kind == "stale_handle"


@_skip
def test_create_stack_rejects_injection_real_bridge():
    """stack.create interpolates the name into a `do`; newlines/quotes/slashes are rejected."""
    from hyperxtalk_mcp.errors import BridgeError

    c = BridgeClient(timeout=15)
    for evil in ["x\ndelete stack", 'x"\ndelete stack', "x/y"]:
        with pytest.raises(BridgeError) as exc:
            c.call("stack.create", {"name": evil})
        assert exc.value.kind == "badarg"


@_skip
def test_stack_delete_refuses_saved_stack_real_bridge():
    """stack.delete must refuse a stack that has a file on disk (never nukes saved work)."""
    from hyperxtalk_mcp.errors import BridgeError
    from hyperxtalk_mcp.server import _as_list

    c = BridgeClient(timeout=20)
    saved = [s for s in _as_list(c.call("stacks.list").get("stacks")) if s.get("file")]
    if not saved:
        pytest.skip("no saved (on-disk) stack open to test the refusal")
    with pytest.raises(BridgeError) as exc:
        c.call("stack.delete", {"handle": saved[0]["handle"]})
    assert exc.value.kind == "badarg"


@_skip
def test_snapshot_real_bridge():
    """Phase 7: snapshot a control to a base64 PNG; stacks are refused."""
    import base64

    from hyperxtalk_mcp.errors import BridgeError
    from hyperxtalk_mcp.server import _as_list, _normalize_controls

    c = BridgeClient(timeout=20)
    stacks = _as_list(c.call("stacks.list").get("stacks"))
    if not stacks:
        pytest.skip("no user stacks open in HyperXTalk")
    cards = _as_list(c.call("tree.get", {"handle": stacks[0]["handle"]}).get("cards"))
    ctrls = _normalize_controls(cards[0].get("controls"))
    if not ctrls:
        pytest.skip("first card has no controls to snapshot")

    snap = c.call("object.snapshot", {"handle": ctrls[0]["handle"]})
    assert snap.get("png") and "\n" not in snap["png"]
    assert base64.b64decode(snap["png"])[:8] == b"\x89PNG\r\n\x1a\n"  # PNG magic
    assert int(snap["width"]) > 0 and int(snap["height"]) > 0

    # a stack handle is refused
    with pytest.raises(BridgeError) as exc:
        c.call("object.snapshot", {"handle": stacks[0]["handle"]})
    assert exc.value.kind == "badarg"


@_skip
def test_environment_and_extensions_real_bridge():
    """Phase 7: env.get returns engine info; extensions.list returns a list."""
    from hyperxtalk_mcp.server import _as_list

    c = BridgeClient(timeout=15)
    env = c.call("env.get", {})
    assert env.get("version") and env.get("platform")
    assert "tool" in env and "pixelScale" in env

    exts = _as_list(c.call("extensions.list", {}).get("extensions"))
    assert isinstance(exts, list)  # may be empty, but must be a list


@_skip
def test_run_set_mode_real_bridge():
    """Phase 7: run.setMode toggles the IDE tool; leaves it in edit mode afterward."""
    c = BridgeClient(timeout=15)
    try:
        run = c.call("run.setMode", {"mode": "run"})
        assert "browse" in run["tool"]
    finally:
        edit = c.call("run.setMode", {"mode": "edit"})  # restore authoring mode
        assert "pointer" in edit["tool"]


@_skip
def test_handler_edit_roundtrip_real_bridge():
    """Phase 8a: set a 2-handler script, replace one handler, verify the sibling is preserved."""
    from hyperxtalk_mcp import script_handlers as sh
    from hyperxtalk_mcp.errors import BridgeError
    from hyperxtalk_mcp.server import _as_list

    c = BridgeClient(timeout=20)
    st = c.call("stack.create", {"name": "MCP Handler Test"})
    try:
        card = _as_list(c.call("tree.get", {"handle": st["handle"]}).get("cards"))[0]["handle"]
        btn = c.call("control.create", {"parentHandle": card, "type": "button", "name": "b"})[
            "handle"
        ]
        original = (
            "on mouseUp\n   beep\nend mouseUp\n\non mouseDown\n   pass mouseDown\nend mouseDown"
        )
        c.call("object.setScript", {"handle": btn, "script": original})

        script = c.call("object.getScript", {"handle": btn})["script"]
        assert [h.name for h in sh.parse_handlers(script)] == ["mouseUp", "mouseDown"]

        edited = sh.replace_or_append_handler(
            script, "mouseUp", 'on mouseUp\n   answer "changed"\nend mouseUp'
        )
        c.call("object.setScript", {"handle": btn, "script": edited})
        back = c.call("object.getScript", {"handle": btn})["script"]
        assert "answer" in back and "beep" not in back  # target replaced
        assert "mouseDown" in back  # sibling preserved
    finally:
        try:
            c.call("stack.delete", {"handle": st["handle"]})
        except BridgeError:
            pass


@_skip
def test_send_message_and_escape_hatch_real_bridge():
    """Phase 8b: object.send invokes a handler & returns its value; eval off by default."""
    from hyperxtalk_mcp.errors import BridgeError
    from hyperxtalk_mcp.server import _as_list

    c = BridgeClient(timeout=20)
    st = c.call("stack.create", {"name": "MCP Send Test"})
    try:
        card = _as_list(c.call("tree.get", {"handle": st["handle"]}).get("cards"))[0]["handle"]
        btn = c.call("control.create", {"parentHandle": card, "type": "button", "name": "b"})[
            "handle"
        ]
        script = (
            # message dispatch reaches `on`/`command` handlers (not `function`s)
            'command getValue\n   return "hi-handler"\nend getValue\n\n'
            'command echoArg pX\n   return "got:" & pX\nend echoArg'
        )
        c.call("object.setScript", {"handle": btn, "script": script})

        assert (
            c.call("object.send", {"handle": btn, "message": "getValue"})["result"] == "hi-handler"
        )
        with_arg = c.call("object.send", {"handle": btn, "message": "echoArg", "args": ["world"]})
        assert with_arg["result"] == "got:world"

        # code-execution verbs are blocked in object.send
        with pytest.raises(BridgeError) as exc:
            c.call("object.send", {"handle": btn, "message": "do", "args": ["beep"]})
        assert exc.value.kind == "badarg"

        # escape hatch is OFF by default -> unauthorized (until the user enables the palette toggle)
        with pytest.raises(BridgeError) as ex2:
            c.call("bridge.eval", {"code": "put 1 into tX", "mode": "do"})
        assert ex2.value.kind == "unauthorized"
    finally:
        try:
            c.call("stack.delete", {"handle": st["handle"]})
        except BridgeError:
            pass


@_skip
def test_concurrent_request_gets_busy():
    """While one request holds the bridge busy (via __busytest), a second must get 409 busy."""
    import threading

    from hyperxtalk_mcp.errors import BridgeError

    errors: list[BridgeError] = []

    def slow():
        BridgeClient(timeout=10).call("__busytest", {"ms": 700})

    t = threading.Thread(target=slow, daemon=True)
    t.start()
    import time

    time.sleep(0.2)  # let the slow request take the busy token
    try:
        BridgeClient(timeout=10).call("ping")
    except BridgeError as exc:
        errors.append(exc)
    t.join(timeout=10)

    assert errors and errors[0].kind == "busy"
