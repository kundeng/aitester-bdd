"""Authoring — agent loop (deepagents/langgraph) that drives the live target
via agent-browser, then writes either a .robot suite or a bug report."""

from aitester_bdd.authoring.agent_loop import (  # noqa: F401
    AuthoringResult,
    author_with_agent,
    load_skill,
)
