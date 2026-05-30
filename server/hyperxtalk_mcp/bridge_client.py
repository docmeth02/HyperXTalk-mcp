"""Client to the in-IDE bridge.

Discovers the bridge via the handshake file the bridge writes to the per-user HyperXTalk
Application-Support folder, then POSTs ``/rpc`` requests over loopback with the shared-secret token.

The handshake lives in the user data dir (``platformdirs.user_data_dir("HyperXTalk")`` on the
Python side == the bridge's per-OS data folder: macOS ``$HOME/Library/Application Support``,
Windows ``%APPDATA%``, Linux ``$HOME/.local/share`` — all suffixed ``/HyperXTalk``). NOT the temp
dir, whose ``TMPDIR`` differs between the launchd-spawned IDE and a terminal-spawned server. This
is the one location the OS-agnostic Python side is permitted to resolve itself.

NOTE (portability): the macOS mapping is verified. The Windows/Linux mapping of the bridge folder
vs ``platformdirs`` is reconciled in phase 6.
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path

import httpx
from platformdirs import user_data_dir

from .errors import BridgeError

APP_NAME = "HyperXTalk"
HANDSHAKE_FILENAME = "mcp-bridge.json"


class BridgeUnavailable(Exception):
    """The bridge is not running / not reachable (a client-side condition, not a protocol error)."""


def handshake_path() -> Path:
    """Location of the bridge handshake file (per-user, stable across GUI/CLI).

    roaming=True so Windows resolves to %APPDATA% (Roaming), matching the bridge's
    specialFolderPath("support"); no effect on macOS/Linux. (Windows verified separately.)
    """
    return Path(user_data_dir(APP_NAME, appauthor=False, roaming=True)) / HANDSHAKE_FILENAME


def _pid_alive(pid: int) -> bool:
    """Best-effort liveness check for the bridge's owning process, cross-platform."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        # Query the process via the Win32 API; a valid handle (or access-denied) => alive.
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if handle:
            exit_code = ctypes.c_ulong()
            still_active = kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
            kernel32.CloseHandle(handle)
            return bool(still_active) and exit_code.value == 259  # STILL_ACTIVE
        # ERROR_ACCESS_DENIED (5) => the pid exists but we can't open it => treat as alive.
        return kernel32.GetLastError() == 5
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def find_handshake() -> dict | None:
    """Return the parsed handshake if a live bridge advertised one, else ``None``.

    Ignores a stale handshake left behind by a dead process.
    """
    path = handshake_path()
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if not _pid_alive(int(data.get("pid", 0) or 0)):
        return None
    return data


class BridgeClient:
    """Talks to the bridge over loopback HTTP."""

    def __init__(self, *, timeout: float = 30.0) -> None:
        self._timeout = timeout

    def _endpoint(self) -> tuple[str, str]:
        handshake = find_handshake()
        if handshake is None:
            raise BridgeUnavailable(
                "HyperXTalk MCP bridge is not running. Launch HyperXTalk and open it from "
                "Development > Plugins > hxt-mcp-bridge."
            )
        port, token = handshake.get("port"), handshake.get("token")
        if port is None or token is None:
            raise BridgeUnavailable("Bridge handshake is malformed (missing port or token).")
        return f"http://127.0.0.1:{int(port)}/rpc", str(token)

    def call(self, op: str, params: dict | None = None) -> dict:
        """Invoke a bridge op. Returns the ``result`` dict, or raises ``BridgeError``."""
        url, token = self._endpoint()
        payload = {"id": str(uuid.uuid4()), "op": op, "params": params or {}}
        try:
            resp = httpx.post(
                url, json=payload, headers={"X-HXT-Token": token}, timeout=self._timeout
            )
        except httpx.HTTPError as exc:
            raise BridgeUnavailable(f"Could not reach the bridge at {url}: {exc}") from exc

        try:
            body = resp.json()
        except ValueError as exc:
            raise BridgeError(
                "runtime", f"non-JSON response from bridge (HTTP {resp.status_code})"
            ) from exc

        if not body.get("ok", False):
            raise BridgeError.from_payload(body.get("error", {}))
        return body.get("result", {})
