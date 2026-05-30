"""Opaque object handles.

A handle is ``base64url(json)`` of the structure below (DESIGN.md §3.1). Agents treat it as
opaque; the bridge re-resolves it statelessly and fail-closed on every call.

Phase 2 implements ``encode``/``decode`` and the bridge-side fail-closed resolver. This module is
the server-side encoder/decoder so tests can construct and inspect handles without the bridge.
"""

from __future__ import annotations

# Handle shape (v1):
#   {
#     "v": 1,
#     "stack": {"shortName": str, "mainStackName": str, "fileName": str | None, "stackId": int},
#     "cardId": int,
#     "objType": str,   # "button" | "field" | "group" | "graphic" | "widget" | ...
#     "objId": int,
#   }

HANDLE_VERSION = 1

# TODO(phase-2): implement encode(obj) -> str and decode(str) -> dict with validation.
