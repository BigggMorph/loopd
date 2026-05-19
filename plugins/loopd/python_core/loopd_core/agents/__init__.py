"""Agent framework for loopd_core.

LLM-call agents (base_agent, pipeline, critic, etc.) are intentionally absent —
the loopd plugin replaces them with Claude Code's ``Task`` tool. Only routing
and handoff data plumbing remain.
"""

from loopd_core.agents.agent_router import AgentRouter

__all__ = ["AgentRouter"]
