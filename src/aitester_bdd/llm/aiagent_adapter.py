"""LiteLLM-backed LLM adapter for aitester-bdd.

One library, one config surface, two distinct uses:

  AUTHORING (CLI / standalone Python):
    - `author(story, snapshot, skill, base_url) -> str` — produce .robot
    - `refine(suite, dryrun_output, latest_snapshot, skill) -> str` — patch
    - `ground_selector(target_description, snapshot) -> str | None` — selector

  IN-WALKER ESCAPE HATCH (called by the walker for `semantic` /
  `visual_semantic` StateChecks — sparingly, only when deterministic
  checks can't express the assertion):
    - `judge(criterion, observation) -> bool` — text
    - `judge_visual(criterion, png_bytes) -> bool` — screenshot

Why LiteLLM: one call shape (`litellm.completion`) dispatches to
OpenAI, Anthropic, Azure, Bedrock, OpenAI-compat proxies, etc. Multimodal
(image) messages use the standard OpenAI shape and LiteLLM translates
to each provider's native form.

Config (env vars):
  AITESTER_LLM_MODEL  — LiteLLM model spec; default points at the
                        claude-code-proxy:
                          "openai/cc/claude-opus-4-7"
  OPENAI_BASE_URL     — proxy URL; default "http://localhost:20128/v1"
  OPENAI_API_KEY      — auth (placeholder works against claude-code-proxy)

Other providers work too — anything LiteLLM supports — but this project
is wired against the local claude-code-proxy by default. Override the
three env vars to swap providers.
"""
from __future__ import annotations

import base64
import logging
import os
from typing import Optional

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

SYSTEM_PROMPT_JUDGE = """You are aitester-bdd's in-walker judge — an escape
hatch for assertions that genuinely need AI-level judgment (semantic meaning
of text, visual recognition). The test author opts in by writing a `semantic`
or `visual_semantic` StateCheck.

You receive a CRITERION (what the test expects to be true) and an OBSERVATION
(page text, or a screenshot). Decide PASS or FAIL based ONLY on the
observation. Be strict — if the observation does not clearly satisfy the
criterion, FAIL it. Do not speculate about state you cannot see.

Reply with EXACTLY one word: PASS or FAIL.
"""

SYSTEM_PROMPT_DIAGNOSE = """You are aitester-bdd's failure-diagnosis aspect.

A rule in a test suite just failed. You receive:
  - the verification + scenario + rule name (what the test intends)
  - the failed step (selector, kind, position)
  - what the test expected
  - what was actually observed
  - a snapshot of the page at the moment of failure (URL + visible text)

Write a SHORT (1-3 sentence) diagnosis that:
  1. Names the most likely cause grounded in the snapshot (e.g.,
     "the page returned a 500 error", "the auth banner is still
     visible suggesting login did not complete", "the locator picks
     up the loading skeleton rather than the loaded element").
  2. If the cause is in the SYSTEM under test, say so. If the cause
     is in the TEST itself (wrong selector, race, stale expectation),
     say that.

Do NOT speculate beyond the snapshot. Do NOT propose code fixes. Just
state the cause and where it lives (SUT vs test).
"""


DEFAULT_MODEL = "openai/cc/claude-opus-4-7"
DEFAULT_BASE_URL = "http://localhost:20128/v1"
DEFAULT_API_KEY = "placeholder"  # claude-code-proxy ignores the key


def _resolve_model(explicit: Optional[str]) -> str:
    return explicit or os.environ.get("AITESTER_LLM_MODEL", DEFAULT_MODEL)


def _ensure_proxy_env() -> None:
    """If pointing at the default claude-code-proxy, set OPENAI_BASE_URL +
    OPENAI_API_KEY so litellm picks them up. No-op if user already set them."""
    os.environ.setdefault("OPENAI_BASE_URL", DEFAULT_BASE_URL)
    os.environ.setdefault("OPENAI_API_KEY", DEFAULT_API_KEY)


class AIAgentLLM:
    """LiteLLM-backed LLM adapter. Same instance is reused for authoring
    and in-walker judge calls — one credential set, one config."""

    def __init__(self, *, model: Optional[str] = None) -> None:
        self.model = _resolve_model(model)
        _ensure_proxy_env()

    def _completion(self, messages: list[dict], *, max_tokens: int = 2048) -> str:
        """Single litellm completion call. Returns the assistant text.

        opus-4.7 rejects `temperature` as deprecated. Setting it to None
        (not omitting it!) is what litellm respects — its default would
        otherwise inject a value. `drop_params=True` is belt-and-suspenders
        for other unsupported params per provider."""
        import litellm

        resp = litellm.completion(
            model=self.model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=None,
            drop_params=True,
        )
        return resp.choices[0].message.content or ""

    def _chat(self, system: str, user: str, *, max_tokens: int = 4096) -> str:
        return self._completion(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_tokens=max_tokens,
        )

    # ------------------------------------------------------------------
    # Authoring
    # ------------------------------------------------------------------

    def author(self, *, story: str, snapshot: str, skill: str, base_url: str) -> str:
        user = (
            f"# Skill grammar\n\n{skill}\n\n"
            f"---\n\n"
            f"# Target base URL\n{base_url}\n\n"
            f"# Accessibility snapshot of entry page\n```\n{snapshot}\n```\n\n"
            f"# Story\n{story}\n\n"
            f"Produce the .robot suite now."
        )
        return _strip_fences(self._chat(SYSTEM_PROMPT_AUTHOR, user))

    def refine(
        self, *, suite: str, dryrun_output: str, latest_snapshot: str, skill: str,
    ) -> str:
        user = (
            f"# Skill grammar\n\n{skill}\n\n"
            f"---\n\n"
            f"# Current suite\n```robot\n{suite}\n```\n\n"
            f"# Failure output\n```\n{dryrun_output}\n```\n\n"
            f"# Fresh snapshot\n```\n{latest_snapshot}\n```\n\n"
            f"Produce the patched .robot suite now."
        )
        return _strip_fences(self._chat(SYSTEM_PROMPT_REFINE, user))

    def ground_selector(
        self, *, target_description: str, snapshot: str,
    ) -> Optional[str]:
        user = (
            f"# Target\n{target_description}\n\n"
            f"# Snapshot\n```\n{snapshot}\n```\n\n"
            f"Selector:"
        )
        out = self._chat(SYSTEM_PROMPT_GROUND, user, max_tokens=256).strip()
        return out or None

    # ------------------------------------------------------------------
    # In-walker escape hatch — keep it small + cheap
    # ------------------------------------------------------------------

    def judge(self, *, criterion: str, observation: str) -> bool:
        """Text-based semantic judge. Returns True if the observation
        satisfies the criterion. Used by the `semantic` StateCheck."""
        user = (
            f"# Criterion\n{criterion}\n\n"
            f"# Observation\n```\n{observation[:6000]}\n```\n\n"
            f"Reply PASS or FAIL."
        )
        out = self._chat(SYSTEM_PROMPT_JUDGE, user, max_tokens=8).strip().upper()
        return out.startswith("PASS")

    def diagnose(self, *, context: str) -> str:
        """AOP aspect: explain why a rule failed.

        `context` is a structured plain-text block carrying the verification
        name, scenario, rule, failed step, expected/observed, and a page
        snapshot. Returns 1-3 sentences naming the likely cause and where
        it lives (SUT vs test). Empty string on any error so the walker
        can still report the deterministic failure cleanly."""
        try:
            out = self._chat(SYSTEM_PROMPT_DIAGNOSE, context, max_tokens=300)
            return out.strip()
        except Exception as exc:
            log.warning("diagnose call failed: %s", exc)
            return ""

    def judge_visual(self, *, criterion: str, png_bytes: bytes) -> bool:
        """Multimodal judge over a screenshot. Returns True if the screenshot
        satisfies the criterion. Used by `visual_semantic` StateCheck.

        Uses the standard OpenAI multimodal message shape; LiteLLM
        translates for non-OpenAI providers automatically.
        """
        data_url = f"data:image/png;base64,{base64.b64encode(png_bytes).decode()}"
        out = self._completion(
            [
                {"role": "system", "content": SYSTEM_PROMPT_JUDGE},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": f"# Criterion\n{criterion}\n\nReply PASS or FAIL."},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                },
            ],
            max_tokens=8,
        ).strip().upper()
        return out.startswith("PASS")


def _strip_fences(s: str) -> str:
    """Strip ```robot ... ``` or ``` ... ``` fences if the LLM added them."""
    s = s.strip()
    if s.startswith("```"):
        lines = s.splitlines()
        lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        s = "\n".join(lines).strip()
    return s
