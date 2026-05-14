"""Adapter: wrap robotframework-aiagent (which itself wraps OpenAI / Anthropic / ...)
as our LLMClient Protocol.

If `robotframework-aiagent` isn't available, fall back to a simple httpx
adapter that talks to an OpenAI-compatible endpoint (e.g., claude-code-proxy
at http://localhost:20128/v1).
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

import httpx

log = logging.getLogger("aitester_bdd.llm.aiagent")


SYSTEM_PROMPT_AUTHOR = """You are aitester-bdd's authoring agent.

Your single output is a complete, valid Robot Framework .robot file using ONLY the
keywords documented in the provided skill. Snapshot-grounded selectors only —
every selector you write MUST appear in the accessibility snapshot you were given.
No invented natural-language verbs. No fixed sleeps. Use observation gates
(`And selector ... exists` after an action) instead.

Output ONLY the .robot file contents, no commentary, no markdown fences.
"""

SYSTEM_PROMPT_REFINE = """You are aitester-bdd's refinement agent.

A .robot suite failed `robot --dryrun` or execution. Given the failure output and
a fresh accessibility snapshot, return the MINIMAL patched .robot file that
addresses the failure. Stay within the shipped keyword vocabulary. Re-ground any
broken selectors against the new snapshot. Preserve rule structure.

Output ONLY the .robot file contents, no commentary, no markdown fences.
"""

SYSTEM_PROMPT_GROUND = """You are aitester-bdd's selector grounding agent.

Given a description of a UI element and an accessibility snapshot, return the
SINGLE best CSS / aria selector for that element. Prefer in this order:
  data-testid > role+name > aria-label > id > stable class > text content.

If no suitable element is visible in the snapshot, return EMPTY (no output).
Output ONLY the selector string, no quotes, no commentary.
"""


class AIAgentLLM:
    """LLMClient implementation.

    Strategy:
      1. Try `robotframework-aiagent` if importable.
      2. Otherwise call an OpenAI-compatible endpoint via httpx
         (defaults to claude-code-proxy at http://localhost:20128/v1).
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
    ) -> None:
        self.base_url = base_url or os.environ.get("AITESTER_LLM_BASE_URL", "http://localhost:20128/v1")
        self.api_key = api_key or os.environ.get("AITESTER_LLM_API_KEY", "placeholder")
        self.model = model or os.environ.get("AITESTER_LLM_MODEL", "cc/claude-opus-4-7")

    def _chat(self, system: str, user: str) -> str:
        """One-shot chat completion against the OpenAI-compat endpoint."""
        r = httpx.post(
            f"{self.base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": 0.2,
            },
            timeout=120.0,
        )
        r.raise_for_status()
        body = r.json()
        return body["choices"][0]["message"]["content"]

    def author(
        self,
        *,
        story: str,
        snapshot: str,
        skill: str,
        base_url: str,
    ) -> str:
        user = (
            f"# Skill grammar\n\n{skill}\n\n"
            f"---\n\n"
            f"# Target base URL\n{base_url}\n\n"
            f"# Accessibility snapshot of entry page\n```\n{snapshot}\n```\n\n"
            f"# Story\n{story}\n\n"
            f"Produce the .robot suite now."
        )
        out = self._chat(SYSTEM_PROMPT_AUTHOR, user)
        return _strip_fences(out)

    def refine(
        self,
        *,
        suite: str,
        dryrun_output: str,
        latest_snapshot: str,
        skill: str,
    ) -> str:
        user = (
            f"# Skill grammar\n\n{skill}\n\n"
            f"---\n\n"
            f"# Current suite\n```robot\n{suite}\n```\n\n"
            f"# Failure output\n```\n{dryrun_output}\n```\n\n"
            f"# Fresh snapshot\n```\n{latest_snapshot}\n```\n\n"
            f"Produce the patched .robot suite now."
        )
        out = self._chat(SYSTEM_PROMPT_REFINE, user)
        return _strip_fences(out)

    def ground_selector(
        self,
        *,
        target_description: str,
        snapshot: str,
    ) -> str | None:
        user = (
            f"# Target\n{target_description}\n\n"
            f"# Snapshot\n```\n{snapshot}\n```\n\n"
            f"Selector:"
        )
        out = self._chat(SYSTEM_PROMPT_GROUND, user).strip()
        return out or None


def _strip_fences(s: str) -> str:
    """Strip ```robot ... ``` or ``` ... ``` fences if the LLM added them."""
    s = s.strip()
    if s.startswith("```"):
        lines = s.splitlines()
        # drop first fence line, trailing fence
        lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        s = "\n".join(lines).strip()
    return s
