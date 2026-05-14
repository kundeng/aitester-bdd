"""LLMClient Protocol — the interface aitester-bdd authoring requires.

Implementations may wrap robotframework-aiagent, direct SDK calls, local
models, or recorded fixtures (for tests).
"""
from __future__ import annotations

from typing import Protocol


class LLMClient(Protocol):
    """Minimal interface aitester-bdd needs from an LLM provider."""

    def author(
        self,
        *,
        story: str,
        snapshot: str,
        skill: str,
        base_url: str,
    ) -> str:
        """Given a story + accessibility snapshot + skill grammar,
        return a complete .robot suite as a string."""
        ...

    def refine(
        self,
        *,
        suite: str,
        dryrun_output: str,
        latest_snapshot: str,
        skill: str,
    ) -> str:
        """Given a failing suite + dryrun output + fresh snapshot,
        return a refined .robot suite that addresses the failure."""
        ...

    def ground_selector(
        self,
        *,
        target_description: str,
        snapshot: str,
    ) -> str | None:
        """Given a description of a desired element and a current snapshot,
        return the best CSS/aria selector visible in the snapshot, or
        None if no suitable element is present."""
        ...
