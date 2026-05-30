"""HyperXTalk language dictionary: parse the engine's ``.lcdoc`` reference files.

The 1800+ ``.lcdoc`` files (one per language term) live in the HyperXTalk source/app under
``docs/dictionary/<type>/<term>.lcdoc``. We resolve that folder, parse the simple ``Key: value``
format, and expose lookup/search so the agent can write correct xTalk (real syntax, not guessed).

Path resolution (OS-agnostic; Python never constructs an engine path):
  1. ``$HXT_DICTIONARY_PATH`` if set,
  2. an explicit root set via :func:`set_root` (phase 8b wires the bridge-resolved app docs path),
  3. a dev fallback: a sibling ``HyperXTalk/docs/dictionary`` checkout next to this repo.
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path

# Fields we recognise; anything else ending in ":" is body text, not a field header.
_SINGLE_FIELDS = {
    "Name",
    "Type",
    "Summary",
    "Introduced",
    "Deprecated",
    "OS",
    "Platforms",
    "Security",
    "Associations",
    "Returns",
    "Synonyms",
    "Tags",
    "Value",
}
_MULTI_FIELDS = {"Syntax", "Example"}
_BLOCK_FIELDS = {"Summary", "Description", "Parameters", "Value", "Returns", "The result"}
_KNOWN_FIELDS = _SINGLE_FIELDS | _MULTI_FIELDS | {"Description", "Parameters", "The result"}

_FIELD_RE = re.compile(r"^([A-Z][A-Za-z][A-Za-z ]*?):\s?(.*)$")

_root_override: Path | None = None


def set_root(path: str | os.PathLike[str] | None) -> None:
    """Override the dictionary root (e.g. the bridge-resolved app docs path). Clears the cache."""
    global _root_override
    _root_override = Path(path) if path else None
    _index.cache_clear()


def dictionary_root() -> Path | None:
    """Resolve the dictionary folder, or None if it can't be found."""
    if _root_override is not None:
        return _root_override if _root_override.is_dir() else None
    env = os.environ.get("HXT_DICTIONARY_PATH")
    if env:
        p = Path(env)
        return p if p.is_dir() else None
    dev = Path(__file__).resolve().parents[3] / "HyperXTalk" / "docs" / "dictionary"
    return dev if dev.is_dir() else None


def parse_lcdoc(text: str) -> dict:
    """Parse one ``.lcdoc`` file into a structured dict.

    Single-value fields map to strings; ``syntax`` and ``examples`` are lists; ``summary``,
    ``description``, and ``parameters`` keep their multi-line block text.
    """
    collected: dict[str, list[str]] = {}
    current: str | None = None
    buf: list[str] = []

    def flush() -> None:
        if current is not None:
            collected.setdefault(current, []).append("\n".join(buf).strip())

    for line in text.splitlines():
        m = _FIELD_RE.match(line)
        if m and m.group(1) in _KNOWN_FIELDS:
            flush()
            current = m.group(1)
            buf = [m.group(2)] if m.group(2) else []
        else:
            buf.append(line)
    flush()

    out: dict = {}
    if "Name" in collected:
        out["name"] = collected["Name"][0]
    if "Type" in collected:
        out["type"] = collected["Type"][0]
    if "Summary" in collected:
        out["summary"] = collected["Summary"][0]
    if "Syntax" in collected:
        out["syntax"] = [s for s in collected["Syntax"] if s]
    if "Example" in collected:
        out["examples"] = [e for e in collected["Example"] if e]
    if "Parameters" in collected:
        out["parameters"] = collected["Parameters"][0]
    if "Description" in collected:
        out["description"] = collected["Description"][0]
    for key, field in (
        ("returns", "Returns"),
        ("associations", "Associations"),
        ("os", "OS"),
        ("platforms", "Platforms"),
        ("synonyms", "Synonyms"),
    ):
        if field in collected:
            out[key] = collected[field][0]
    return out


@lru_cache(maxsize=1)
def _index() -> dict[str, list[Path]]:
    """Map lower-cased term name -> list of ``.lcdoc`` paths (a name may have >1 type)."""
    root = dictionary_root()
    index: dict[str, list[Path]] = {}
    if root is None:
        return index
    for path in root.rglob("*.lcdoc"):
        name = _read_name(path) or path.stem.replace("-", " ")
        index.setdefault(name.lower(), []).append(path)
    return index


def _read_name(path: Path) -> str | None:
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                m = _FIELD_RE.match(line)
                if m and m.group(1) == "Name":
                    return m.group(2).strip()
                if line.strip():
                    break
    except OSError:
        return None
    return None


def lookup(term: str) -> list[dict]:
    """Return parsed entries whose name matches ``term`` exactly (case-insensitive)."""
    paths = _index().get(term.strip().lower(), [])
    out = []
    for path in paths:
        try:
            out.append(parse_lcdoc(path.read_text(encoding="utf-8", errors="replace")))
        except OSError:
            continue
    return out


def search(query: str, limit: int = 20) -> list[dict]:
    """Find terms whose name (preferred) or summary contains ``query`` (case-insensitive)."""
    q = query.strip().lower()
    if not q:
        return []
    name_hits: list[dict] = []
    summary_hits: list[dict] = []
    seen: set[str] = set()
    for name_lc, paths in sorted(_index().items()):
        path = paths[0]
        if name_lc in seen:
            continue
        if q in name_lc:
            seen.add(name_lc)
            name_hits.append(_summary_entry(path))
        else:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if q in text.lower():
                entry = parse_lcdoc(text)
                if q in (entry.get("summary", "").lower()):
                    seen.add(name_lc)
                    summary_hits.append(_compact(entry))
    return (name_hits + summary_hits)[:limit]


def _summary_entry(path: Path) -> dict:
    try:
        return _compact(parse_lcdoc(path.read_text(encoding="utf-8", errors="replace")))
    except OSError:
        return {"name": path.stem}


def _compact(entry: dict) -> dict:
    return {
        "name": entry.get("name", ""),
        "type": entry.get("type", ""),
        "summary": entry.get("summary", ""),
        "syntax": (entry.get("syntax") or [""])[0],
    }
