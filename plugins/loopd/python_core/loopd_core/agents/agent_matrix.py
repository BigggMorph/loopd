"""
Declarative level x agent subagent matrix.

Single source of truth for which subagents are enabled at each task level.
Consumed by agent_router._get_dynamic_skips() and handoff._get_expected_artifacts().
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Matrix: agent_name -> level (0-4) -> enabled subagents (order preserved)
# ---------------------------------------------------------------------------
# Invariants:
#   - Every agent has entries for levels 0-4
#   - Higher level is a superset of lower level (monotonically increasing)
#   - impl always includes "pr" at every level
#   - Subagent names must match AGENT_SUBAGENTS in agent_router.py
# ---------------------------------------------------------------------------

SUBAGENT_MATRIX: dict[str, dict[int, list[str]]] = {
    "analysis": {
        0: [],
        1: [],
        2: ["product_brief"],
        3: ["research", "product_brief"],
        4: ["research", "brainstorm", "product_brief"],
    },
    "planning": {
        0: [],
        1: [],
        2: [],
        3: ["prd", "ux_design"],
        4: ["prd", "ux_design"],
    },
    "solutioning": {
        0: [],
        1: ["tech_spec"],
        2: ["architecture", "tech_spec"],
        3: ["architecture", "epic_story", "tech_spec"],
        4: ["architecture", "epic_story", "tech_spec"],
    },
    "impl": {
        0: ["dev", "pr"],
        1: ["dev", "test", "pr"],
        2: ["dev", "test", "review", "pr"],
        3: ["dev", "test", "review", "self_refine", "pr"],
        4: ["dev", "test", "review", "self_refine", "pr"],
    },
}

# Subagent -> artifact type(s) mapping.
# Default: subagent_name == artifact_type.
# Entries listed here are exceptions: multi-artifact mappings or no-artifact subagents.
_SUBAGENT_TO_ARTIFACT: dict[str, list[str]] = {
    "epic_story": ["epic", "epic_story"],
    "dev": [],
    "pr": [],
    "test": [],
    "review": [],
    "self_refine": [],
    "plan_critic": [],
    "solution_critic": [],
    "research_critic": [],
}

# ---------------------------------------------------------------------------
# Critic Matrix: agent -> level (0-4) -> enabled critics
# ---------------------------------------------------------------------------
# Level 0-1: no critic (skip)
# Level 2: self-critique (handled in subagent prompt, no separate critic run)
# Level 3: single-pass (critic runs once after each target subagent)
# Level 4: full loop (critic iterates until PASS or max_iterations)
# ---------------------------------------------------------------------------

CRITIC_MATRIX: dict[str, dict[int, list[str]]] = {
    "analysis": {
        0: [],
        1: [],
        2: [],
        3: ["research_critic"],
        4: ["research_critic"],
    },
    "planning": {
        0: [],
        1: [],
        2: [],
        3: ["plan_critic"],
        4: ["plan_critic"],
    },
    "solutioning": {
        0: [],
        1: [],
        2: [],
        3: ["solution_critic"],
        4: ["solution_critic"],
    },
}

_MAX_LEVEL = 4
_MIN_LEVEL = 0


def get_enabled_subagents(agent: str, level: int) -> list[str]:
    """Return subagents enabled for *agent* at *level*.

    Args:
        agent: Main agent name ("analysis", "solutioning", etc.)
        level: Task complexity level (0-4, clamped if out of range)

    Returns:
        New list of enabled subagent names (order preserved).
    """
    agent_levels = SUBAGENT_MATRIX.get(agent)
    if not agent_levels:
        return []
    clamped = max(_MIN_LEVEL, min(_MAX_LEVEL, level))
    return list(agent_levels[clamped])


def get_level_skips(agent: str, level: int) -> list[str]:
    """Return subagents to skip for *agent* at *level*.

    Computes: AGENT_SUBAGENTS[agent] - enabled(matrix) = skips.
    Uses lazy import to avoid circular dependency with agent_router.

    Args:
        agent: Main agent name
        level: Task complexity level (0-4)

    Returns:
        List of subagent names that should be skipped.
    """
    # Lazy import to break circular dependency (agent_router <-> agent_matrix)
    from loopd_core.agents.agent_router import AGENT_SUBAGENTS
    from loopd_core.types import AgentType

    try:
        agent_type = AgentType(agent)
    except ValueError:
        return []

    all_subagents = AGENT_SUBAGENTS.get(agent_type, [])
    enabled = set(get_enabled_subagents(agent, level))
    return [s for s in all_subagents if s not in enabled]


def get_enabled_critics(agent: str, level: int) -> list[str]:
    """Return critics enabled for *agent* at *level*.

    Args:
        agent: Main agent name ("planning", "solutioning")
        level: Task complexity level (0-4, clamped if out of range)

    Returns:
        New list of enabled critic names.
    """
    agent_levels = CRITIC_MATRIX.get(agent)
    if not agent_levels:
        return []
    clamped = max(_MIN_LEVEL, min(_MAX_LEVEL, level))
    return list(agent_levels[clamped])


def get_expected_artifacts(agent: str, level: int) -> list[str]:
    """Return expected artifact types for *agent* at *level*.

    Converts enabled subagents to artifact types via _SUBAGENT_TO_ARTIFACT.
    Used by handoff._get_expected_artifacts().

    Args:
        agent: Main agent name
        level: Task complexity level (0-4)

    Returns:
        List of expected artifact type names.
    """
    enabled = get_enabled_subagents(agent, level)
    artifacts: list[str] = []
    for sub in enabled:
        mapped = _SUBAGENT_TO_ARTIFACT.get(sub)
        if mapped is not None:
            artifacts.extend(mapped)
        else:
            # Default: subagent name == artifact type
            artifacts.append(sub)
    return artifacts
