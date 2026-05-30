"""Phase 1: handshake discovery + stale-pid handling."""

import json
import os

import pytest

from hyperxtalk_mcp import bridge_client


@pytest.fixture
def handshake_file(tmp_path, monkeypatch):
    path = tmp_path / "mcp-bridge.json"
    monkeypatch.setattr(bridge_client, "handshake_path", lambda: path)
    return path


def test_missing_handshake_returns_none(handshake_file):
    assert bridge_client.find_handshake() is None


def test_corrupt_handshake_returns_none(handshake_file):
    handshake_file.write_text("not json", encoding="utf-8")
    assert bridge_client.find_handshake() is None


def test_live_pid_handshake_is_returned(handshake_file, monkeypatch):
    monkeypatch.setattr(bridge_client, "_port_reachable", lambda port: True)
    handshake_file.write_text(
        json.dumps({"port": 49152, "token": "abc", "pid": os.getpid()}), encoding="utf-8"
    )
    data = bridge_client.find_handshake()
    assert data is not None
    assert data["port"] == 49152


def test_unreachable_port_handshake_is_ignored(handshake_file, monkeypatch):
    monkeypatch.setattr(bridge_client, "_port_reachable", lambda port: False)
    handshake_file.write_text(
        json.dumps({"port": 49152, "token": "abc", "pid": os.getpid()}), encoding="utf-8"
    )
    assert bridge_client.find_handshake() is None


def test_stale_pid_handshake_is_ignored(handshake_file):
    # PID 2^31-1 is effectively never a live process.
    handshake_file.write_text(
        json.dumps({"port": 49152, "token": "abc", "pid": 2147483647}), encoding="utf-8"
    )
    assert bridge_client.find_handshake() is None


def test_non_dict_handshake_returns_none(handshake_file):
    handshake_file.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    assert bridge_client.find_handshake() is None


def test_malformed_handshake_raises_unavailable(monkeypatch):
    # Live-looking handshake but missing port/token -> BridgeUnavailable, not KeyError.
    monkeypatch.setattr(bridge_client, "find_handshake", lambda: {"pid": os.getpid()})
    with pytest.raises(bridge_client.BridgeUnavailable):
        bridge_client.BridgeClient().call("ping")
