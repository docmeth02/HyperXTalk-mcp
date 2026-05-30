"""Phase 1: BridgeClient against a mock bridge that mimics the /rpc protocol.

Covers the client logic (token header, JSON request/response, error mapping, large-body echo)
without needing the live IDE.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from hyperxtalk_mcp import bridge_client
from hyperxtalk_mcp.bridge_client import BridgeClient
from hyperxtalk_mcp.errors import BridgeError
from hyperxtalk_mcp.server import do_ping

TOKEN = "test-token-123"


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # silence test server logging
        pass

    def _send(self, status: int, obj: dict) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path != "/rpc":
            self._send(404, {"ok": False, "error": {"kind": "notfound", "message": "use /rpc"}})
            return
        if self.headers.get("X-HXT-Token") != TOKEN:
            self._send(401, {"ok": False, "error": {"kind": "unauthorized", "message": "bad"}})
            return
        length = int(self.headers.get("Content-Length", 0))
        req = json.loads(self.rfile.read(length).decode("utf-8"))
        rid, op, params = req.get("id"), req.get("op"), req.get("params", {})

        if op == "ping":
            result = {"engine": "0.9.15", "platform": "test"}
            self._send(200, {"id": rid, "ok": True, "result": result})
        elif op == "echo":
            self._send(200, {"id": rid, "ok": True, "result": {"data": params.get("data")}})
        elif op == "busy":
            self._send(409, {"id": rid, "ok": False, "error": {"kind": "busy", "message": "busy"}})
        elif op == "boom":
            self._send(
                200,
                {"id": rid, "ok": False,
                 "error": {"kind": "compile", "message": "syntax error", "line": 3, "col": 1}},
            )
        else:
            self._send(200, {"id": rid, "ok": False, "error": {"kind": "notfound", "message": op}})


@pytest.fixture
def mock_bridge(monkeypatch):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(bridge_client, "find_handshake", lambda: {"port": port, "token": TOKEN})
    try:
        yield port
    finally:
        server.shutdown()
        server.server_close()


def test_ping_returns_engine_info(mock_bridge):
    result = BridgeClient().call("ping")
    assert result == {"engine": "0.9.15", "platform": "test"}


def test_do_ping_tool_logic_connected(mock_bridge):
    assert do_ping(BridgeClient()) == {"connected": True, "engine": "0.9.15", "platform": "test"}


def test_large_body_echo_roundtrip(mock_bridge):
    payload = "on mouseUp\n  put \"x\" && quote into field 1\nend mouseUp\n" * 25_000  # ~1.2 MB
    assert len(payload) > 1_000_000
    result = BridgeClient().call("echo", {"data": payload})
    assert result["data"] == payload


def test_bad_token_maps_to_unauthorized(mock_bridge, monkeypatch):
    monkeypatch.setattr(
        bridge_client, "find_handshake", lambda: {"port": mock_bridge, "token": "wrong"}
    )
    with pytest.raises(BridgeError) as exc:
        BridgeClient().call("ping")
    assert exc.value.kind == "unauthorized"


def test_busy_maps_to_bridge_error(mock_bridge):
    with pytest.raises(BridgeError) as exc:
        BridgeClient().call("busy")
    assert exc.value.kind == "busy"


def test_op_level_error_carries_line_col(mock_bridge):
    with pytest.raises(BridgeError) as exc:
        BridgeClient().call("boom")
    assert exc.value.kind == "compile"
    assert exc.value.line == 3 and exc.value.col == 1


def test_unreachable_bridge_raises_unavailable(monkeypatch):
    # Handshake points at a closed port -> connection refused -> BridgeUnavailable.
    monkeypatch.setattr(bridge_client, "find_handshake", lambda: {"port": 1, "token": TOKEN})
    with pytest.raises(bridge_client.BridgeUnavailable):
        BridgeClient(timeout=1.0).call("ping")
