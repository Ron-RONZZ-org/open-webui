"""Model-scoped allowlist for tool-server tools.

A model's ``meta.toolFilter`` keeps only the tool-server tools whose resolved
name or raw tool name matches one of its ``fnmatch`` patterns. An MCP tool is
stored under ``<server_id>_<tool>`` and its spec name is rewritten to that same
prefixed name, so the raw name travels separately in ``raw_name``; without it a
bare pattern such as ``upload-file`` cannot match anything and the whole
connection is filtered out. Builtin tools (the code interpreter, knowledge, web
search, …) are never filtered: they are capabilities rather than tool servers,
and their names are not stable configuration surface.

An empty or missing ``toolFilter`` keeps every tool, so existing models are
unaffected.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Iterable, Mapping

# Tool dictionaries built for MCP connections and workspace/external tool servers.
FILTERED_TOOL_TYPES = frozenset({'mcp', 'external'})


def tool_matches_filter(
    patterns: Iterable[str],
    resolved_name: str,
    spec_name: str | None = None,
    raw_name: str | None = None,
) -> bool:
    """True when a pattern matches the resolved name, the raw tool name, or the spec name."""
    candidates = [resolved_name]
    for candidate in (spec_name, raw_name):
        if candidate and candidate not in candidates:
            candidates.append(candidate)
    return any(fnmatch.fnmatchcase(candidate, pattern) for pattern in patterns for candidate in candidates)


def apply_tool_filter(
    tools_dict: Mapping[str, dict],
    patterns: Iterable[str] | None,
) -> dict[str, dict]:
    """Return the tools a model may use.

    ``patterns`` is an allowlist of fnmatch patterns (e.g. ``upload-file``,
    ``mediawiki-mcp_*``). Tool-server tools that match nothing are dropped;
    every other tool type passes through untouched.
    """
    active = [pattern for pattern in (patterns or []) if pattern]
    if not active:
        return dict(tools_dict)

    kept: dict[str, dict] = {}
    for name, tool in tools_dict.items():
        if tool.get('type') not in FILTERED_TOOL_TYPES:
            kept[name] = tool
            continue
        spec_name = (tool.get('spec') or {}).get('name')
        if tool_matches_filter(active, name, spec_name, tool.get('raw_name')):
            kept[name] = tool
    return kept
