# HyperXTalk-mcp

An [MCP](https://modelcontextprotocol.io) server that lets AI agents (Claude Code et al.) work with
**live [HyperXTalk](https://github.com/emily-elizabeth/HyperXTalk) stacks** — enumerate open
stacks / cards / controls / widgets, read & edit xTalk script (compile-checked), create / delete /
modify controls and widgets, snapshot cards, and manage settings.

> **Status:** working and tested on **macOS and Linux** — discovery, editing, creation, snapshots,
> the language dictionary, and handler-level script ops are all implemented (28 tools, integration-tested
> against a live IDE). On Linux (x86_64, GTK3) the bridge plugin loads, binds its loopback socket, writes
> the handshake at the XDG path, and serves RPC end-to-end. Windows verification is still pending (the
> code is OS-agnostic by design).

## Capabilities

28 MCP tools, grouped by area:

- **Discover & inspect** — `ping`, `list_stacks`, `get_tree`, `get_properties`, `get_script`,
  `get_environment`, `list_extensions`
- **Create & capture** — `create_stack`, `delete_stack` (unsaved scratch stacks only), `create_card`,
  `snapshot` (card/control/group → PNG)
- **Edit live stacks** — `create_control`, `create_widget`, `delete_object`, `clone_object`,
  `set_properties`, `set_script` (compile-checked), `save_stack`, `save_stack_as`, `set_run_mode`
- **Language dictionary** — `search_dictionary`, `lookup_dictionary` (real xTalk syntax from the
  engine's own `.lcdoc` reference, so the agent writes correct code)
- **Handler-level script work** — `list_handlers`, `get_handler`, `set_handler` (edit one handler
  without disturbing siblings), `grep_scripts`
- **Invoke & escape hatch** — `send_message` (invoke a handler defined on an object), `eval_xtalk`
  (arbitrary `do`/`value`, **off by default** — must be enabled in the bridge palette Settings)

All mutations are gated by a safe-to-edit check and a master pause switch; edits round-trip through
the real engine, so scripts are validated by the actual parser. The bridge must be launched once per
IDE session (see install step 1).

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
(`revEnvironmentUserPluginsPath()`) and shows `installed to …; bridge started as a palette`.

The build/install commands are **identical on every OS** — all paths are resolved engine-side, so the
same block installs the plugin on macOS, Linux, and Windows. The user Plugins folder it installs into
is `revEnvironmentUserPluginsPath()` (e.g. `~/hyperxtalk_customization/Plugins/` on Linux).

> **Linux caveat — start the bridge from the Plugins menu after building.** On macOS the build also
> starts the bridge immediately. On Linux the build installs the plugin correctly but the palette opens
> **stopped** (`palette stack` doesn't fire `openStack → hxtBridgeStart` for a just-built in-memory
> stack on the GTK engine). Just open it once from **Development ▸ Plugins ▸ hxt-mcp-bridge** — a fresh
> open fires `openStack` and the bridge starts serving. Every subsequent session uses that same
> menu-launch, so this only affects the very first build.

For subsequent sessions, just launch it from **Development ▸ Plugins ▸ hxt-mcp-bridge** (there is no
auto-start). On launch it binds a loopback ephemeral port and writes a handshake file to the per-user
data folder, where the server discovers it. The folder matches `platformdirs.user_data_dir("HyperXTalk")`
on the Python side:

| Platform | Handshake file |
|----------|----------------|
| macOS    | `~/Library/Application Support/HyperXTalk/mcp-bridge.json` |
| Linux    | `~/.local/share/HyperXTalk/mcp-bridge.json` (or `$XDG_DATA_HOME/HyperXTalk/…`) |
| Windows  | `%APPDATA%\HyperXTalk\mcp-bridge.json` |

Useful message-box checks (`hxtBridgeStatus` is OS-agnostic; the second line shows the Linux path):

```
put value("hxtBridgeStatus()", stack "hxt-mcp-bridge")
put url ("file:" & specialFolderPath("home") & "/.local/share/HyperXTalk/mcp-bridge.json")
```

To stop the bridge: `send "hxtBridgeStop" to stack "hxt-mcp-bridge"`. It also stops when you close
the plugin or quit the IDE. A diagnostic log is written to `~/hxt-mcp-bridge.log`.

### 2. Set up the Python server

```sh
cd server
python3 -m venv .venv && . .venv/bin/activate
pip install -e .              # add '.[dev]' for ruff + pytest
```

### 3. Register it with your MCP client

From the `server/` directory (so `$(pwd)` resolves to absolute paths):

```sh
claude mcp add hyperxtalk "$(pwd)/.venv/bin/python" "$(pwd)/mcp_server.py" -s user
```

The launcher (`mcp_server.py`) runs the server over stdio. `-s user` makes it available in every
project. Equivalent documented long form:

```sh
claude mcp add hyperxtalk --scope user -- "$(pwd)/.venv/bin/python" "$(pwd)/mcp_server.py"
```

**OpenClaude** uses the same CLI surface — just swap the binary:

```sh
openclaude mcp add hyperxtalk --scope user -- "$(pwd)/.venv/bin/python" "$(pwd)/mcp_server.py"
```

**opencode** is configured by file, not a CLI command. Add the server to
`~/.config/opencode/opencode.jsonc` (replace `<repo>` with your checkout path):

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "hyperxtalk-mcp": {
      "type": "local",
      "command": ["<repo>/server/.venv/bin/python", "<repo>/server/mcp_server.py"],
      "enabled": true
    }
  }
}
```

The `search_dictionary` / `lookup_dictionary` tools auto-find the engine's docs in
`/Applications/HyperXTalk.app` on macOS. For a non-default location (or Linux/Windows), point them at
the docs explicitly by adding `-e HXT_DICTIONARY_PATH=/path/to/HyperXTalk/docs/dictionary` to the
command above.

Other MCP clients: run `"$(pwd)/.venv/bin/python" "$(pwd)/mcp_server.py"` as a stdio server (or
`python -m hyperxtalk_mcp`).

### 4. Verify

In Claude Code, run `/mcp` and confirm `hyperxtalk` is **connected**, then ask Claude to call the
`ping` tool — with the bridge running it returns the engine version and platform. If it reports the
bridge isn't running, launch it: **Development ▸ Plugins ▸ hxt-mcp-bridge**.

**Pitfalls:** run `claude mcp add` from `server/`; if you move the repo or recreate `.venv`, re-run
it (Claude Code stored absolute paths). The bridge is not auto-started — launch it once per IDE
session.

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
  OS-agnostic. Verified on macOS and Linux (x86_64, GTK3); Windows verification in progress. No
  platform-specific code was needed for Linux — the bridge's existing per-OS path handling already
  resolves the XDG data dir.
- **Large payloads are slow** pending two upstream HyperXTalk fixes — O(n²) socket read
  ([#295](https://github.com/emily-elizabeth/HyperXTalk/issues/295)) and `JsonImport`
  ([#296](https://github.com/emily-elizabeth/HyperXTalk/issues/296)). Typical script payloads are
  small and fast.

## License

TBD.
