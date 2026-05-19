"""
Agent routing for oh-my-agents.

Determines which agent should handle a task based on level and state.
"""

from __future__ import annotations

import re
from typing import Optional

from loopd_core.agents.agent_matrix import get_level_skips
from loopd_core.types import AgentType, Task


# Agent pipeline order
AGENT_PIPELINE = [
    AgentType.CORE,
    AgentType.ANALYSIS,
    AgentType.PLANNING,
    AgentType.SOLUTIONING,
    AgentType.IMPL,
]

# Level to starting agent mapping
LEVEL_STARTING_AGENT = {
    0: AgentType.IMPL,       # Simple fix - direct to implementation
    1: AgentType.SOLUTIONING,  # Small feature - start at solutioning
    2: AgentType.SOLUTIONING,  # Medium feature - start at solutioning
    3: AgentType.PLANNING,   # Large feature - start at planning
    4: AgentType.ANALYSIS,   # Epic - full analysis
}

# Subagents for each main agent
AGENT_SUBAGENTS = {
    AgentType.CORE: [],  # Core has no subagents
    AgentType.ANALYSIS: ["research", "brainstorm", "product_brief"],
    AgentType.PLANNING: ["prd", "ux_design"],
    AgentType.SOLUTIONING: ["architecture", "epic_story", "tech_spec"],
    AgentType.IMPL: ["dev", "test", "review", "pr", "self_refine"],
}


class AgentRouter:
    """
    Routes tasks to appropriate agents based on level and state.

    Replaces the routing logic in lib/agent.sh.
    """

    @staticmethod
    def is_valid_agent(agent: str) -> bool:
        """Check if agent name is valid."""
        try:
            AgentType(agent)
            return True
        except ValueError:
            return agent in ["waiting_human", "complete"]

    @staticmethod
    def get_starting_agent(level: int) -> AgentType:
        """
        Get the starting agent for a given complexity level.

        Args:
            level: Task complexity level (0-4)

        Returns:
            Starting agent type
        """
        if level < 0:
            level = 0
        if level > 4:
            level = 4
        return LEVEL_STARTING_AGENT[level]

    # Research tasks end after planning — solutioning/impl are not needed.
    _RESEARCH_TERMINAL_AGENT = AgentType.PLANNING

    @staticmethod
    def get_next_agent(current_agent: str, task_type: str = "dev") -> Optional[str]:
        """
        Get the next agent in the pipeline.

        Args:
            current_agent: Current agent name
            task_type: Task type ("dev", "research", "query")

        Returns:
            Next agent name, or None if at end of pipeline
        """
        try:
            current = AgentType(current_agent)
        except ValueError:
            return None

        # Research tasks: stop after the terminal agent (planning)
        if task_type == "research" and current == AgentRouter._RESEARCH_TERMINAL_AGENT:
            return None

        try:
            current_index = AGENT_PIPELINE.index(current)
            if current_index < len(AGENT_PIPELINE) - 1:
                return AGENT_PIPELINE[current_index + 1].value
            return None  # End of pipeline
        except ValueError:
            return None

    @staticmethod
    def get_subagents(agent: str) -> list[str]:
        """
        Get subagents for a main agent.

        Args:
            agent: Main agent name

        Returns:
            List of subagent names
        """
        try:
            agent_type = AgentType(agent)
            return AGENT_SUBAGENTS.get(agent_type, [])
        except ValueError:
            return []

    @staticmethod
    def _get_dynamic_skips(agent: str, task: Task) -> list[str]:
        """
        Determine which subagents to dynamically skip based on task context.

        Three-phase approach:
          Phase 1: Matrix-based level skip (all agents, via agent_matrix)
          Phase 2: Agent-specific context overrides (artifacts, human feedback, etc.)
          Phase 3: Backward/feedback/review overrides (below, unchanged)

        Context signals: level, existing artifacts, core_analysis, human_response,
        backward_transition_reason, backward_context focus_areas, review_results,
        inter_agent_review_results, checkpoint_feedback, handoff context.
        """
        skips: list[str] = []

        # Phase 1: Matrix-based level skip (single source of truth)
        skips.extend(get_level_skips(agent, task.level))

        # Phase 2: Agent-specific context overrides
        if agent == "analysis":
            # Human feedback: focus on specific subagent
            human_resp = getattr(task.context, "human_response", None) or ""
            if human_resp:
                lower = human_resp.lower()
                if any(kw in lower for kw in ["조사", "research", "리서치"]):
                    if "brainstorm" not in skips:
                        skips.append("brainstorm")
                elif any(kw in lower for kw in ["brainstorm", "아이디어", "ideation"]):
                    if "research" not in skips:
                        skips.append("research")

        elif agent == "planning":
            core_analysis = task.context.core_analysis or {}
            initial_ctx = core_analysis.get("initial_context", {})
            has_ui = initial_ctx.get("has_ui", True)  # default True

            # No UI → skip ux_design
            if not has_ui and "ux_design" not in skips:
                skips.append("ux_design")

            # Skip subagents whose artifacts already exist
            artifact_types = {a.type for a in (task.artifacts or [])}
            if "prd" in artifact_types and "prd" not in skips:
                skips.append("prd")
            if "ux_design" in artifact_types and "ux_design" not in skips:
                skips.append("ux_design")

            # Backward transition: skip unrelated subagents
            reason = getattr(task.context, "backward_transition_reason", None)
            if reason:
                lower_reason = reason.lower()
                if "prd" in lower_reason or "requirement" in lower_reason:
                    if "ux_design" not in skips:
                        skips.append("ux_design")
                elif "ux" in lower_reason or "design" in lower_reason:
                    if "prd" not in skips:
                        skips.append("prd")

        elif agent == "solutioning":
            # Skip subagents whose artifacts already exist (unless backward rework)
            artifact_types = {a.type for a in (task.artifacts or [])}
            backward_ctx = getattr(task.context, "backward_context", None)
            is_backward = backward_ctx and isinstance(backward_ctx, dict) and backward_ctx.get("target_agent") == agent

            if not is_backward:
                if "architecture" in artifact_types and "architecture" not in skips:
                    skips.append("architecture")
                if "epic" in artifact_types and "epic_story" not in skips:
                    skips.append("epic_story")

        elif agent == "impl":
            core_analysis = task.context.core_analysis or {}
            initial_ctx = core_analysis.get("initial_context", {})
            complexity = initial_ctx.get("complexity", "")

            # Trivial complexity: skip test (additional to matrix)
            if complexity == "trivial" and "test" not in skips:
                skips.append("test")

        # Context-aware: backward_context focus_areas narrow subagent selection
        backward_ctx = getattr(task.context, "backward_context", None)
        if backward_ctx and isinstance(backward_ctx, dict):
            if backward_ctx.get("target_agent") == agent:
                focus_areas = backward_ctx.get("focus_areas", [])
                if focus_areas:
                    focused = AgentRouter._focus_subagents_for_areas(agent, focus_areas)
                    if focused is not None:
                        all_subs = AgentRouter.get_subagents(agent)
                        for sub in all_subs:
                            if sub not in focused and sub not in skips:
                                skips.append(sub)

        # Checkpoint feedback driven focus: if human gave feedback at checkpoint,
        # analyze it to narrow subagent selection for the target agent
        handoff = getattr(task.context, "handoff", None) or {}
        checkpoint_fb = handoff.get("checkpoint_feedback", {})
        if checkpoint_fb and checkpoint_fb.get("target_agent") == agent:
            feedback_text = checkpoint_fb.get("feedback", "")
            if feedback_text:
                focused = AgentRouter._focus_subagents_from_feedback(
                    agent, feedback_text
                )
                if focused is not None:
                    all_subs = AgentRouter.get_subagents(agent)
                    for sub in all_subs:
                        if sub not in focused and sub not in skips:
                            skips.append(sub)

        # Inter-agent review driven skips: if the receiving-agent review
        # identified specific subagents to focus on, skip the rest
        review_results = getattr(task.context, "inter_agent_review_results", [])
        if review_results:
            latest = review_results[-1]
            if latest.get("target_agent") == agent:
                focus_subs = latest.get("focus_subagents", [])
                if focus_subs:
                    all_subs = AgentRouter.get_subagents(agent)
                    for sub in all_subs:
                        if sub not in focus_subs and sub not in skips:
                            skips.append(sub)

        return skips

    @staticmethod
    def _focus_subagents_from_feedback(
        agent: str, feedback: str
    ) -> Optional[list[str]]:
        """
        Analyze human checkpoint feedback to determine which subagents to focus on.

        Returns a list of subagents to keep (others should be skipped),
        or None if feedback is too generic to narrow focus.
        """
        lower = feedback.lower()

        # Keyword-to-subagent mapping per agent
        _FEEDBACK_MAP: dict[str, dict[str, list[str]]] = {
            "analysis": {
                "research|조사|리서치|market": ["research"],
                "brainstorm|아이디어|ideation|creative": ["brainstorm"],
                "brief|요약|정리|product": ["product_brief"],
            },
            "planning": {
                "prd|requirement|요구사항|spec|기능": ["prd"],
                "ux|ui|design|디자인|wireframe|화면": ["ux_design"],
            },
            "solutioning": {
                "architecture|아키텍처|설계|구조|infra": ["architecture"],
                "epic|story|스토리|에픽|breakdown": ["epic_story"],
                "tech.?spec|기술|스택|implementation": ["tech_spec"],
            },
            "impl": {
                "dev|develop|구현|코딩|code": ["dev"],
                "test|테스트|검증|coverage": ["test"],
                "review|리뷰|검토|code review": ["review"],
                "refine|개선|polish": ["self_refine"],
            },
        }

        agent_map = _FEEDBACK_MAP.get(agent)
        if not agent_map:
            return None

        focused: set[str] = set()
        for pattern, subs in agent_map.items():
            if re.search(pattern, lower):
                focused.update(subs)

        if not focused:
            return None

        return list(focused)

    @staticmethod
    def _focus_subagents_for_areas(
        agent: str, focus_areas: list[str]
    ) -> Optional[list[str]]:
        """
        Map backward transition focus_areas to relevant subagents.

        Returns a list of subagents to keep (others should be skipped),
        or None if no mapping applies (run all).
        """
        # Mapping: focus_area → subagents that address it per agent
        _FOCUS_MAP: dict[str, dict[str, list[str]]] = {
            "analysis": {
                "requirements": ["research", "product_brief"],
                "general_rework": ["research", "brainstorm", "product_brief"],
            },
            "planning": {
                "requirements": ["prd"],
                "ux_design": ["ux_design"],
                "general_rework": ["prd", "ux_design"],
            },
            "solutioning": {
                "architecture": ["architecture"],
                "requirements": ["epic_story", "tech_spec"],
                "security": ["architecture", "tech_spec"],
                "performance": ["architecture", "tech_spec"],
                "testing": ["tech_spec"],
                "general_rework": ["architecture", "epic_story", "tech_spec"],
            },
        }

        agent_map = _FOCUS_MAP.get(agent)
        if not agent_map:
            return None

        focused: set[str] = set()
        matched = False
        for area in focus_areas:
            subs = agent_map.get(area)
            if subs:
                focused.update(subs)
                matched = True

        if not matched:
            return None

        return list(focused)

    @staticmethod
    def get_subagents_for_task(agent: str, task: Task) -> list[str]:
        """
        Get subagents to run for a specific task.

        Four-tier context-aware selection:
        1. Agent-driven recommendations (from subagent_recommendations in context)
        2. Review-driven recommendations (from handoff review_results)
        3. Explicit skip_subagents from task context
        4. Dynamic context-aware skips (level, artifacts, backward focus, etc.)

        If agent-driven recommendations exist for this agent, they take priority
        over dynamic skips (but explicit skips still apply as override).

        Args:
            agent: Main agent name
            task: Task with context for dynamic selection

        Returns:
            Filtered list of subagents to run
        """
        all_subagents = AgentRouter.get_subagents(agent)

        # Explicit skip_subagents from task context (always honored as override)
        explicit_skips = (
            task.context.skip_subagents.get(agent, [])
            if task.context.skip_subagents
            else []
        )

        # Tier 1: Agent-driven recommendations (previous agent suggested subagents)
        recommendations = (
            task.context.subagent_recommendations.get(agent, [])
            if task.context.subagent_recommendations
            else []
        )

        # Tier 2: Review-driven recommendations (handoff review suggested subagents)
        if not recommendations and task.context.review_results:
            for review in reversed(task.context.review_results):
                review_recs = review.get("recommended_subagents", {})
                if agent in review_recs:
                    recommendations = review_recs[agent]
                    break

        if recommendations:
            # Use recommendations: only run recommended subagents (filtered by valid set)
            valid_recommendations = [s for s in recommendations if s in all_subagents]
            if valid_recommendations:
                filtered = [s for s in valid_recommendations if s not in explicit_skips]
            else:
                # Invalid recommendations, fall through to dynamic skips
                dynamic_skips = AgentRouter._get_dynamic_skips(agent, task)
                all_skips = set(explicit_skips) | set(dynamic_skips)
                filtered = [s for s in all_subagents if s not in all_skips]
        else:
            # Tier 3+4: Combine explicit skips with dynamic context-aware skips
            dynamic_skips = AgentRouter._get_dynamic_skips(agent, task)
            all_skips = set(explicit_skips) | set(dynamic_skips)
            filtered = [s for s in all_subagents if s not in all_skips]

        # GUARD: "pr" subagent must NEVER be skipped for impl agent.
        if agent == "impl" and "pr" in all_subagents and "pr" not in filtered:
            filtered.append("pr")

        return filtered

    @staticmethod
    def route_task(task: Task) -> str:
        """
        Determine which agent should handle a task.

        Args:
            task: Task to route

        Returns:
            Agent name to run
        """
        # If task already has a current agent, use it
        if task.state.current_agent and task.state.current_agent != "core":
            return task.state.current_agent

        # Otherwise, route based on level
        return AgentRouter.get_starting_agent(task.level).value

    @staticmethod
    def should_skip_agent(task: Task, agent: str) -> bool:
        """
        Check if an agent should be skipped for this task.

        Based on:
        - Task level (lower levels skip earlier agents)
        - Explicit skip configuration in context
        """
        # Research tasks: skip solutioning and impl entirely
        if task.task_type == "research" and agent in ["solutioning", "impl"]:
            return True

        # Level 0 skips to impl directly
        if task.level == 0 and agent in ["analysis", "planning", "solutioning"]:
            return True

        # Level 1 skips analysis and planning
        if task.level == 1 and agent in ["analysis", "planning"]:
            return True

        # Level 2 skips analysis
        if task.level == 2 and agent == "analysis":
            return True

        return False

    @staticmethod
    def get_agent_order_for_level(level: int) -> list[str]:
        """
        Get the ordered list of agents for a given level.

        Args:
            level: Task complexity level (0-4)

        Returns:
            Ordered list of agent names to execute
        """
        starting_agent = AgentRouter.get_starting_agent(level)

        try:
            start_index = AGENT_PIPELINE.index(starting_agent)
            return [a.value for a in AGENT_PIPELINE[start_index:]]
        except ValueError:
            return [AgentType.IMPL.value]
