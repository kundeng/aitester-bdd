"""LLMClient Protocol — the interface aitester-bdd needs from an LLM.

One adapter (AIAgentLLM, LiteLLM-backed) covers both uses:

  AUTHORING — author/refine/ground_selector, called by the CLI to
              generate .robot files from a story + snapshot.

  IN-WALKER — judge/judge_visual, the escape-hatch state check the
              walker calls for `semantic` / `visual_semantic` kinds
              when deterministic checks can't express the assertion.
"""
from __future__ import annotations

from typing import Optional, Protocol


class LLMClient(Protocol):
    """Minimal interface aitester-bdd needs from an LLM provider."""

    # ------------------------------------------------------------------
    # Authoring (CLI / standalone)
    # ------------------------------------------------------------------

    def author(
        self, *, story: str, snapshot: str, skill: str, base_url: str,
    ) -> str:
        """Given a story + accessibility snapshot + skill grammar,
        return a complete .robot suite as a string."""
        ...

    def refine(
        self, *, suite: str, dryrun_output: str, latest_snapshot: str, skill: str,
    ) -> str:
        """Given a failing suite + dryrun output + fresh snapshot,
        return a refined .robot suite that addresses the failure."""
        ...

    def ground_selector(
        self, *, target_description: str, snapshot: str,
    ) -> Optional[str]:
        """Given a description of a desired element and a current snapshot,
        return the best CSS/aria selector visible in the snapshot, or
        None if no suitable element is present."""
        ...

    # ------------------------------------------------------------------
    # In-walker escape hatch
    # ------------------------------------------------------------------

    def judge(self, *, criterion: str, observation: str) -> bool:
        """True if the observation (page text) satisfies the criterion.
        Used sparingly — deterministic checks preferred."""
        ...

    def judge_visual(self, *, criterion: str, png_bytes: bytes) -> bool:
        """True if the screenshot satisfies the criterion. Used by the
        walker's `visual_semantic` StateCheck."""
        ...

    def diagnose(self, *, context: str) -> str:
        """AOP failure aspect: hand the failure context to the LLM, get
        a short natural-language explanation. The walker calls this on
        every rule failure. Empty return = no diagnosis available."""
        ...
