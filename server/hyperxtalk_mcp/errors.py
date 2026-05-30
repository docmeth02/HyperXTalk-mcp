"""Error kinds shared by the bridge protocol and the MCP server.

The bridge returns ``{"ok": false, "error": {"kind": ..., "message": ..., "line"?, "col"?}}``.
``BridgeError`` carries that structure back to the MCP layer, which surfaces it verbatim.
"""

from __future__ import annotations

# Canonical error kinds (mirrors DESIGN.md §3).
ERROR_KINDS = frozenset(
    {
        "compile",  # script failed to parse on set
        "runtime",  # engine error executing a command
        "notfound",  # op or object does not exist
        "badarg",  # malformed/invalid params
        "stale_handle",  # handle no longer resolves; re-discover
        "not_safe_to_edit",  # mutation rejected by the safe-to-edit gate / pause
        "busy",  # another request is in flight (one-in-flight)
        "unauthorized",  # missing/invalid token
    }
)


class BridgeError(Exception):
    """A structured error returned by the bridge."""

    def __init__(
        self,
        kind: str,
        message: str,
        line: int | None = None,
        col: int | None = None,
    ) -> None:
        self.kind = kind
        self.message = message
        self.line = line
        self.col = col
        super().__init__(f"{kind}: {message}")

    @classmethod
    def from_payload(cls, error: dict) -> BridgeError:
        return cls(
            kind=error.get("kind", "runtime"),
            message=error.get("message", ""),
            line=error.get("line"),
            col=error.get("col"),
        )
