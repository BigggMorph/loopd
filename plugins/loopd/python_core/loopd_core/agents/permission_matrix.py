"""
Permission matrix for Claude CLI subagent tool access.

Single source of truth for which tools each subagent may use and which
permission_mode to apply when running claude -p in headless mode.

ADR-001: Python dict constants (no YAML/JSON)
ADR-002: bypassPermissions for all headless runs
ADR-003: Role-based profiles (readonly / research / author / developer)
"""

from __future__ import annotations

from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Base tool sets
# ---------------------------------------------------------------------------

_BASE_TOOLS: tuple[str, ...] = ("Read", "Glob", "Grep")

# ---------------------------------------------------------------------------
# Permission profiles
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PermissionProfile:
    """Immutable permission profile for a subagent."""

    name: str                    # "readonly" | "research" | "author" | "developer"
    permission_mode: str         # always "bypassPermissions"
    allowed_tools: tuple[str, ...]


PERMISSION_PROFILES: dict[str, PermissionProfile] = {
    "readonly": PermissionProfile(
        name="readonly",
        permission_mode="bypassPermissions",
        allowed_tools=_BASE_TOOLS,
    ),
    "research": PermissionProfile(
        name="research",
        permission_mode="bypassPermissions",
        allowed_tools=_BASE_TOOLS + (
            "WebSearch", "WebFetch",
            "mcp__github__search_code", "mcp__github__search_repositories",
            "mcp__github__search_issues", "mcp__github__get_file_contents",
            "mcp__playwright__navigate", "mcp__playwright__get_visible_text",
            "mcp__playwright__screenshot", "mcp__playwright__get_snapshot",
            "Write",  # research agents write artifacts (e.g. research_notes.md)
        ),
    ),
    "author": PermissionProfile(
        name="author",
        permission_mode="bypassPermissions",
        allowed_tools=_BASE_TOOLS + ("Write", "Edit"),
    ),
    "developer": PermissionProfile(
        name="developer",
        permission_mode="bypassPermissions",
        allowed_tools=_BASE_TOOLS + ("Write", "Edit", "Bash"),
    ),
}

# ---------------------------------------------------------------------------
# Subagent → profile mapping
# ---------------------------------------------------------------------------
# Format: agent_name -> subagent_name -> profile_name
# Unknown subagent falls back to "developer" (broadest, matches legacy default).
# ---------------------------------------------------------------------------

SUBAGENT_PERMISSION_MAP: dict[str, dict[str, str]] = {
    "analysis": {
        "research": "research",
        "brainstorm": "author",
        "product_brief": "author",
        "research_critic": "readonly",
    },
    "planning": {
        "prd": "author",
        "ux_design": "author",
        "plan_critic": "readonly",
    },
    "solutioning": {
        "architecture": "author",
        "epic_story": "author",
        "tech_spec": "author",
        "solution_critic": "readonly",
    },
    "impl": {
        "dev": "developer",
        "test": "developer",
        "review": "readonly",
        "self_refine": "developer",
        "pr": "developer",
    },
    # core_agent runs claude directly (subagent=None) — permission matrix not applied.
    # Permissions are controlled by core_agent's own ClaudeCLIConfig.
    "core": {},
}

_DEFAULT_PROFILE_NAME = "developer"


def get_permission_profile(
    agent_name: str,
    subagent_name: str,
    extra_tools: list[str] | None = None,
) -> PermissionProfile:
    """Return a PermissionProfile for the given agent/subagent pair.

    Args:
        agent_name:   Main agent name ("analysis", "impl", etc.)
        subagent_name: Subagent name ("research", "dev", etc.)
        extra_tools:  Additional tools from subagent config.json (merged in,
                      deduplicated, order preserved).

    Returns:
        PermissionProfile with bypassPermissions and the merged tool list.
    """
    profile_name = (
        SUBAGENT_PERMISSION_MAP
        .get(agent_name, {})
        .get(subagent_name, _DEFAULT_PROFILE_NAME)
    )
    base_profile = PERMISSION_PROFILES[profile_name]

    if not extra_tools:
        return base_profile

    # Merge extra_tools, preserving order and deduplicating
    existing = set(base_profile.allowed_tools)
    additions = tuple(t for t in extra_tools if t not in existing)
    if not additions:
        return base_profile

    return PermissionProfile(
        name=base_profile.name,
        permission_mode=base_profile.permission_mode,
        allowed_tools=base_profile.allowed_tools + additions,
    )
