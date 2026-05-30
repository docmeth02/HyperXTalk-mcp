"""Phase 0 smoke test: the package imports and the FastMCP app is constructed."""

from hyperxtalk_mcp import __version__
from hyperxtalk_mcp.errors import ERROR_KINDS, BridgeError
from hyperxtalk_mcp.server import mcp


def test_version() -> None:
    assert __version__


def test_app_constructed() -> None:
    assert mcp.name == "hyperxtalk-mcp"


def test_bridge_error_roundtrip() -> None:
    err = BridgeError.from_payload({"kind": "compile", "message": "boom", "line": 3, "col": 1})
    assert err.kind in ERROR_KINDS
    assert err.line == 3


def test_launcher_reuses_entry_point() -> None:
    # the mcp_server.py launcher and `python -m hyperxtalk_mcp` both call the real entry point
    import hyperxtalk_mcp.__main__ as dunder_main
    import mcp_server
    from hyperxtalk_mcp.server import main

    assert mcp_server.main is main
    assert dunder_main.main is main
