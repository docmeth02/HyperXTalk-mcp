"""Claude Code stdio launcher for hyperxtalk-mcp.

Register with: claude mcp add hyperxtalk "$(pwd)/.venv/bin/python" "$(pwd)/mcp_server.py" -s user
(run from this `server/` directory so $(pwd) resolves to absolute paths). Reuses the package entry
point — no server logic lives here.
"""

from hyperxtalk_mcp.server import main

if __name__ == "__main__":
    main()
