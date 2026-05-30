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
