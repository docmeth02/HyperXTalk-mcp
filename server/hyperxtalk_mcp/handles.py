"""Opaque object handles.

A handle is ``base64(JsonExport(...))`` of the structure below (DESIGN.md §3.1), minted by the
bridge. Agents treat it as opaque; the bridge re-resolves it statelessly and fail-closed every call.

These helpers are for the server/test side to construct and inspect handles; the bridge owns the
authoritative encode/decode. (The bridge stringifies numbers via JsonExport, so a decoded handle's
numeric fields may be strings — callers should not depend on their type.)
"""

from __future__ import annotations

import base64
import json

HANDLE_VERSION = 1

# Handle shape (v1):
#   {"v": 1,
#    "stack": {"shortName": str, "mainStackName": str, "fileName": str|"", "stackId": int|str},
#    "cardId": int|str, "objType": str, "objId": int|str}


def encode(obj: dict) -> str:
    """Encode a handle dict to the opaque string form (standard base64 of UTF-8 JSON)."""
    return base64.b64encode(json.dumps(obj).encode("utf-8")).decode("ascii")


def decode(handle: str) -> dict:
    """Decode an opaque handle string back to its dict form. Raises ValueError if malformed."""
    try:
        return json.loads(base64.b64decode(handle).decode("utf-8"))
    except (ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"malformed handle: {exc}") from exc
