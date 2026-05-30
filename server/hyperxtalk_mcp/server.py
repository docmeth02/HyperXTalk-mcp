"""FastMCP application entry point.

Tools are added per implementation phase (ping here in phase 1; discovery/read in phase 2; editing
in phase 4; …).
"""

from __future__ import annotations

from fastmcp import FastMCP

from .bridge_client import BridgeClient, BridgeUnavailable
from .errors import BridgeError

mcp: FastMCP = FastMCP("hyperxtalk-mcp")

_client = BridgeClient()


def do_ping(client: BridgeClient) -> dict:
    """Ping the bridge; pure logic, separated from the tool wrapper for testing."""
    try:
        result = client.call("ping")
    except BridgeUnavailable as exc:
        return {"connected": False, "error": str(exc)}
    except BridgeError as exc:
        return {"connected": False, "error": f"{exc.kind}: {exc.message}"}
    return {"connected": True, **result}


@mcp.tool
def ping() -> dict:
    """Check that the HyperXTalk bridge is reachable.

    Returns the running engine version and platform when connected, or a status/error string
    telling the user to launch the bridge plugin when it is not.
    """
    return do_ping(_client)


# --- helpers -------------------------------------------------------------------------------------
# The bridge serialises collections via JsonExport, which turns index-keyed arrays into JSON
# OBJECTS ({"1": ..., "2": ...}) rather than arrays. These restore ordered lists.


def _as_list(value: object) -> list:
    """A bridge collection (index-keyed object, a real list, or empty) -> an ordered list."""
    if isinstance(value, list):
        return value
    if isinstance(value, dict) and value:
        try:
            return [value[k] for k in sorted(value, key=int)]
        except ValueError:
            return [value[k] for k in sorted(value)]  # non-index keys: stable lexical order
    return []


def _normalize_controls(controls: object) -> list:
    out = []
    for ctrl in _as_list(controls):
        if isinstance(ctrl, dict) and "children" in ctrl:
            ctrl["children"] = _normalize_controls(ctrl["children"])
        out.append(ctrl)
    return out


def _err(exc: Exception) -> dict:
    if isinstance(exc, BridgeError):
        return {"error": f"{exc.kind}: {exc.message}", "kind": exc.kind}
    return {"error": str(exc)}


# --- discovery + read tools (phase 2) ------------------------------------------------------------


@mcp.tool
def list_stacks() -> dict:
    """List the currently open user stacks (the IDE's own stacks are excluded).

    Each entry has: name, an opaque `handle` (pass it to other tools), file path, isMainStack,
    and cardCount.
    """
    try:
        result = _client.call("stacks.list")
    except (BridgeUnavailable, BridgeError) as exc:
        return _err(exc)
    return {"stacks": _as_list(result.get("stacks"))}


@mcp.tool
def get_tree(handle: str) -> dict:
    """Get the card → control tree for a stack `handle` (from list_stacks).

    Returns the stack name/handle and a list of cards; each card has its controls, with grouped
    controls nested under their group's `children`. Every node carries an opaque `handle`.
    """
    try:
        result = _client.call("tree.get", {"handle": handle})
    except (BridgeUnavailable, BridgeError) as exc:
        return _err(exc)
    cards = []
    for card in _as_list(result.get("cards")):
        card["controls"] = _normalize_controls(card.get("controls"))
        cards.append(card)
    return {"name": result.get("name"), "handle": result.get("handle"), "cards": cards}


@mcp.tool
def get_properties(handle: str) -> dict:
    """Get an object's properties by `handle` (stack, card, or control/widget).

    Returns the object's settable properties plus id, name, type (and kind for widgets).
    """
    try:
        result = _client.call("object.getProps", {"handle": handle})
    except (BridgeUnavailable, BridgeError) as exc:
        return _err(exc)
    return {"props": result.get("props", {})}


@mcp.tool
def get_script(handle: str) -> dict:
    """Get the xTalk script of an object by `handle` (stack, card, or control)."""
    try:
        result = _client.call("object.getScript", {"handle": handle})
    except (BridgeUnavailable, BridgeError) as exc:
        return _err(exc)
    return {"script": result.get("script", "")}


def main() -> None:
    """Run the MCP server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
