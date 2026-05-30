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


# --- editing tools (phase 4) ---------------------------------------------------------------------
# All mutations are gated by the bridge's safe-to-edit check; an unsafe state returns
# {"error": "not_safe_to_edit: <reason>"} (e.g. paused, open_in_editor, stack_cant_modify).


def _call(op: str, params: dict) -> dict:
    try:
        return _client.call(op, params)
    except (BridgeUnavailable, BridgeError) as exc:
        return _err(exc)


@mcp.tool
def create_control(
    parent_handle: str, control_type: str, name: str = "", props: dict | None = None
) -> dict:
    """Create a control on a card or inside a group.

    `parent_handle` is a card or group handle (from get_tree). `control_type` is one of
    button, field, graphic, image, scrollbar, player, group. Optionally set `name` and `props`
    (a property→value object). Returns the new control's id, type, and opaque handle.
    """
    params: dict = {"parentHandle": parent_handle, "type": control_type}
    if name:
        params["name"] = name
    if props:
        params["props"] = props
    return _call("control.create", params)


@mcp.tool
def create_widget(parent_handle: str, kind: str, name: str = "", props: dict | None = None) -> dict:
    """Create a widget of a given `kind` (LCB module id, e.g. com.livecode.widget.list) on a card
    or inside a group. Returns the new widget's id, type, kind, and handle."""
    params: dict = {"parentHandle": parent_handle, "kind": kind}
    if name:
        params["name"] = name
    if props:
        params["props"] = props
    return _call("widget.create", params)


@mcp.tool
def delete_object(handle: str) -> dict:
    """Delete a control/widget or a card by `handle`. (Stacks cannot be deleted via this tool.)"""
    return _call("object.delete", {"handle": handle})


@mcp.tool
def clone_object(handle: str, name: str = "") -> dict:
    """Clone a control/widget (optionally renaming the copy). Returns the new object's handle."""
    params: dict = {"handle": handle}
    if name:
        params["name"] = name
    return _call("object.clone", params)


@mcp.tool
def set_properties(handle: str, props: dict) -> dict:
    """Set a subset of an object's properties (partial update; unknown/read-only keys ignored)."""
    return _call("object.setProps", {"handle": handle, "props": props})


@mcp.tool
def set_script(handle: str, script: str) -> dict:
    """Set the xTalk script of an object (transactional + compile-checked).

    On a syntax error the original script is restored and `{"error": "compile: ..."}` is returned.
    """
    return _call("object.setScript", {"handle": handle, "script": script})


@mcp.tool
def save_stack(handle: str) -> dict:
    """Save a stack to its existing file. Use save_stack_as for a never-saved stack."""
    return _call("stack.save", {"handle": handle})


@mcp.tool
def save_stack_as(handle: str, file_name: str) -> dict:
    """Save a stack under a new bare file name (no path) in the user's Documents folder."""
    return _call("stack.saveAs", {"handle": handle, "fileName": file_name})


# --- visual & settings tools (phase 7) -----------------------------------------------------------


@mcp.tool
def create_stack(name: str = "") -> dict:
    """Create a new, empty top-level stack and open it. Returns its `handle` and `name`.

    `name` is optional; if omitted the engine auto-names it (e.g. "Untitled 1"). The returned
    `name` is the engine-assigned one (it may differ from `name` if that name was already taken).
    Use the handle with create_card / create_control to populate it.
    """
    return _call("stack.create", {"name": name} if name else {})


@mcp.tool
def delete_stack(handle: str) -> dict:
    """Discard an UNSAVED scratch stack from memory.

    Refuses any stack that has a file on disk (it never deletes saved work) — use this to clean up
    stacks created with create_stack that you don't want to keep.
    """
    return _call("stack.delete", {"handle": handle})


@mcp.tool
def create_card(stack_handle: str) -> dict:
    """Append a new card to a stack (from list_stacks). Returns the new card's id and handle."""
    return _call("card.create", {"stackHandle": stack_handle})


@mcp.tool
def snapshot(handle: str) -> dict:
    """Render a card, group, or control to a PNG image (stacks are not supported).

    Returns `png` (base64-encoded PNG, no line breaks), `kind`, logical `width`/`height`, and the
    device `scale` (on a HiDPI display the PNG's pixel dimensions are width*scale by height*scale).
    """
    return _call("object.snapshot", {"handle": handle})


@mcp.tool
def get_environment() -> dict:
    """Read the engine/IDE environment: version, platform, systemVersion, processor, screenRect,
    the current `tool` (browse=run / pointer=edit), and the device pixelScale."""
    return _call("env.get", {})


@mcp.tool
def list_extensions() -> dict:
    """List the runtime-loaded extension module ids (widgets, libraries, modules)."""
    result = _call("extensions.list", {})
    if "error" in result:
        return result
    return {"extensions": _as_list(result.get("extensions"))}


@mcp.tool
def set_run_mode(mode: str) -> dict:
    """Switch the IDE between run and edit mode (`mode` is "run" or "edit").

    "run" selects the browse tool (clicking activates controls); "edit" selects the pointer tool
    (clicking selects/moves controls). This is an IDE-global setting, not per-stack.
    """
    return _call("run.setMode", {"mode": mode})


def main() -> None:
    """Run the MCP server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
