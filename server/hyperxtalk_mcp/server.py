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


def main() -> None:
    """Run the MCP server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
