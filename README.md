# HyperXTalk-mcp

An [MCP](https://modelcontextprotocol.io) server that lets AI agents (Claude Code et al.) work with
**live [HyperXTalk](https://github.com/emily-elizabeth/HyperXTalk) stacks** — enumerate open
stacks / cards / controls / widgets, read & edit xTalk script (compile-checked), create / delete /
modify controls and widgets, snapshot cards, and manage settings.

> **Status:** early. The transport layer (agent ⇄ server ⇄ live IDE) is working and tested on macOS;
> object discovery and editing are in progress.

## How it works

```
MCP client ──stdio──▶ FastMCP server (server/) ──HTTP loopback──▶ bridge plugin (bridge/) ──▶ live HyperXTalk engine
```

- **`server/`** — a Python [FastMCP](https://github.com/jlowin/fastmcp) server. It discovers the
  bridge via a handshake file and calls its `/rpc` endpoint over loopback with a per-session token.
- **`bridge/`** — an xTalk IDE plugin you launch once per session. It owns the live object model and
  serves `/rpc` with a small custom socket reader.

The host is the installed HyperXTalk IDE (`/Applications/HyperXTalk.app` on macOS).

## Requirements

- HyperXTalk (the IDE), installed and running.
- Python 3.11+.

## Install & run

### 1. Install + launch the bridge plugin (once per IDE session)

A HyperXTalk script-only stack can't be opened as a plugin window, so the bridge ships as a
script-only **source** (`bridge/hxt-mcp-bridge.livecodescript`) plus a one-shot **builder** that
wraps it into a binary plugin stack in your user Plugins folder.

In the HyperXTalk **message box** (use the multi-line box; run with Cmd/Ctrl+Enter), replacing
`<repo>` with the path to this checkout:

```
if there is a stack "hxt-mcp-build-plugin" then delete stack "hxt-mcp-build-plugin"
if there is a stack "hxt-mcp-bridge" then delete stack "hxt-mcp-bridge"
start using stack "<repo>/bridge/build-plugin.livecodescript"
hxtBuildPlugin "<repo>/bridge/hxt-mcp-bridge.livecodescript"
answer the result
```

This installs `hxt-mcp-bridge.livecode` into the user Plugins folder
(`revEnvironmentUserPluginsPath()`) **and starts it immediately**. A successful run shows
`installed to …; start result:` with nothing after `start result:`.

For subsequent sessions, just launch it from **Development ▸ Plugins ▸ hxt-mcp-bridge** (there is no
auto-start). On launch it binds a loopback ephemeral port and writes a handshake file to the per-user
data folder (`~/Library/Application Support/HyperXTalk/mcp-bridge.json` on macOS); the server
discovers it there.

Useful message-box checks:

```
put value("hxtBridgeStatus()", stack "hxt-mcp-bridge")
put url ("file:" & specialFolderPath("home") & "/Library/Application Support/HyperXTalk/mcp-bridge.json")
```

To stop the bridge: `send "hxtBridgeStop" to stack "hxt-mcp-bridge"`. It also stops when you close
the plugin or quit the IDE. A diagnostic log is written to `~/hxt-mcp-bridge.log`.

### 2. Run the MCP server

```sh
cd server
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
hyperxtalk-mcp          # runs the MCP server over stdio
```

With the bridge running, the `ping` tool returns the engine version and platform; if the bridge
isn't running it returns a clear "launch the bridge plugin" message.

## Development

```sh
cd server && . .venv/bin/activate
ruff check .
pytest                  # unit + mock tests; live-bridge integration tests auto-skip
pytest -m integration   # live tests — requires the bridge running in HyperXTalk
```

## Project layout

```
bridge/   hxt-mcp-bridge.livecodescript   bridge logic (the plugin's stack script)
          build-plugin.livecodescript     one-shot installer/builder
server/   hyperxtalk_mcp/                  FastMCP server, bridge client, handles, errors
          tests/                           pytest (unit, mock bridge, live integration)
```

## Notes

- **Loopback only.** The bridge binds `127.0.0.1` and requires a per-session token; the handshake
  file is written owner-only. It can run arbitrary code in your live IDE session — treat it like a
  localhost dev tool.
- **Cross-platform by design.** All OS-specific paths are resolved engine-side; the Python side stays
  OS-agnostic. Verified on macOS; Linux/Windows verification in progress.
- **Large payloads are slow** pending two upstream HyperXTalk fixes — O(n²) socket read
  ([#295](https://github.com/emily-elizabeth/HyperXTalk/issues/295)) and `JsonImport`
  ([#296](https://github.com/emily-elizabeth/HyperXTalk/issues/296)). Typical script payloads are
  small and fast.

## License

TBD.
