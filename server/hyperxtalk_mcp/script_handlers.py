"""Parse xTalk scripts into handlers, so a single handler can be read/replaced without disturbing
its siblings. Pure text manipulation; the engine remains the source of truth (the bridge's
object.setScript still compile-checks any edited script before it is stored).

A handler is ``on|command|function|getProp|setProp|before|after <name> ... end <name>``. Names are
case-insensitive in xTalk, so matching is case-insensitive. Lines are 1-based.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_START_RE = re.compile(
    r"^[ \t]*(on|command|function|getprop|setprop|before|after)[ \t]+([A-Za-z_][\w]*)",
    re.IGNORECASE,
)
# no end-anchor: \w* greedily captures the whole identifier, so trailing whitespace/comments
# (e.g. `end mouseUp -- done`) and stray CR don't prevent the match.
_END_RE = re.compile(r"^[ \t]*end[ \t]+([A-Za-z_]\w*)", re.IGNORECASE)


def _normalize(script: str) -> str:
    """Collapse CRLF/CR to LF so line splitting and offsets are consistent (engine is LF-native)."""
    return script.replace("\r\n", "\n").replace("\r", "\n")


@dataclass
class Handler:
    kind: str
    name: str
    start: int  # 1-based line of the `on`/`command`/… line
    end: int  # 1-based line of the matching `end`
    text: str

    def as_dict(self) -> dict:
        return {"kind": self.kind, "name": self.name, "start": self.start, "end": self.end}


def parse_handlers(script: str) -> list[Handler]:
    """Return the top-level handlers in ``script`` (nested control `end`s are ignored by name)."""
    lines = _normalize(script).split("\n")
    handlers: list[Handler] = []
    i = 0
    while i < len(lines):
        m = _START_RE.match(lines[i])
        if m:
            kind, name = m.group(1).lower(), m.group(2)
            end_idx = _find_end(lines, i + 1, name)
            if end_idx is not None:
                handlers.append(
                    Handler(kind, name, i + 1, end_idx + 1, "\n".join(lines[i : end_idx + 1]))
                )
                i = end_idx + 1
                continue
        i += 1
    return handlers


def _find_end(lines: list[str], start: int, name: str) -> int | None:
    """Index of the `end <name>` that closes the handler opened just before ``start``."""
    target = name.lower()
    for j in range(start, len(lines)):
        m = _END_RE.match(lines[j])
        if m and m.group(1).lower() == target:
            return j
    return None


def find_handler(script: str, name: str) -> Handler | None:
    target = name.lower()
    for h in parse_handlers(script):
        if h.name.lower() == target:
            return h
    return None


def replace_or_append_handler(script: str, name: str, new_text: str) -> str:
    """Return ``script`` with handler ``name`` replaced by ``new_text`` (appended if absent)."""
    script = _normalize(script)
    new_text = _normalize(new_text).rstrip("\n")
    existing = find_handler(script, name)
    if existing is None:
        sep = "" if script == "" else ("\n" if script.endswith("\n") else "\n\n")
        return script + sep + new_text + "\n"
    lines = script.split("\n")
    before = lines[: existing.start - 1]
    after = lines[existing.end :]
    return "\n".join(before + new_text.split("\n") + after)
