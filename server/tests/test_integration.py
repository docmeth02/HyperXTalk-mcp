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
    payload = "线 quote=\" newline\n tab\t backslash\\ " * 6_500  # ~250 KB, multibyte + escapes
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
            {"parentHandle": card_handle, "type": "button", "name": "MCP Test Button",
             "props": {"width": 120}},
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
