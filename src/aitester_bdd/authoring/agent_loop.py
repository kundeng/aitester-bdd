"""Authoring agent loop — same pattern as prismi3's aitester.

We do NOT roll our own tool-calling loop. We use DeepAgents (which wraps
langgraph) so the ReAct loop, tool dispatch, message history, and
checkpointing are battle-tested.

Flow:
  - System prompt = SKILL.md verbatim (so the agent follows the same
    instructions a human-driving Claude Code agent would follow).
  - User prompt = story + base_url + paths for the two terminal outputs.
  - Tools = agent-browser bridges + read_file + write_robot_suite +
    report_bug. The terminal tools (write_robot_suite / report_bug)
    return an "AUTHORING_DONE: ..." marker which the caller scans for
    after the run completes.
  - LLM = langchain_openai.ChatOpenAI configured for the local
    claude-code-proxy by default (model "cc/claude-opus-4-7", base_url
    "http://localhost:20128/v1"); override via env.

The agent decides what to call when and in what order. We don't direct
the exploration step-by-step — that's the whole point.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Optional

log = logging.getLogger("aitester_bdd.authoring.agent_loop")


# ─── Defaults ────────────────────────────────────────────────────────


DEFAULT_MODEL = "cc/claude-opus-4-7"
DEFAULT_BASE_URL = "http://localhost:20128/v1"
DEFAULT_API_KEY = "placeholder"
DEFAULT_MAX_ITERS = 100
DEFAULT_MAX_ATTEMPTS = 2  # how many times to re-run the whole author loop on failure


# ─── Outputs ─────────────────────────────────────────────────────────


@dataclass
class AuthoringResult:
    """What the agent loop produced.

    Exactly one of `suite_path` / `bug_report_path` is set on success
    (corresponding to the terminal tool the agent called). If both are
    None, the agent exhausted iterations without calling a terminal
    tool — treat as a soft failure.
    """

    suite_path: Optional[Path] = None
    bug_report_path: Optional[Path] = None
    iterations: int = 0
    final_message: str = ""
    transcript: list[dict] = None  # full message list for debugging

    @property
    def ok(self) -> bool:
        return self.suite_path is not None or self.bug_report_path is not None


# ─── Helpers ─────────────────────────────────────────────────────────


def load_skill() -> str:
    """Read the shipped SKILL.md from the package."""
    pkg = resources.files("aitester_bdd").joinpath("skill", "SKILL.md")
    return pkg.read_text(encoding="utf-8")


def _build_llm():
    """Construct the ChatOpenAI client.

    Reads env vars, defaults to the local claude-code-proxy:
      AITESTER_LLM_MODEL   -> model name        (default: cc/claude-opus-4-7)
      OPENAI_BASE_URL      -> proxy/base URL    (default: http://localhost:20128/v1)
      OPENAI_API_KEY       -> auth              (default: placeholder)

    For non-OpenAI-compatible backends, the user can swap in any
    langchain chat model and pass it in via `model_override`.
    """
    from langchain_openai import ChatOpenAI

    model = os.environ.get("AITESTER_LLM_MODEL", DEFAULT_MODEL)
    base_url = os.environ.get("OPENAI_BASE_URL", DEFAULT_BASE_URL)
    api_key = os.environ.get("OPENAI_API_KEY", DEFAULT_API_KEY)

    # NB: opus-4.7 rejects `temperature` as deprecated; omit it.
    # max_retries=8: tolerate transient Anthropic 529 overloads + 502/503/504.
    # LangChain backs off exponentially; 8 retries covers a ~60s outage window.
    max_retries = int(os.environ.get("AITESTER_LLM_MAX_RETRIES", "8"))
    return ChatOpenAI(
        model=model,
        base_url=base_url,
        api_key=api_key,
        timeout=120.0,
        max_retries=max_retries,
    )


def _parse_terminal_markers(transcript: list[dict]) -> tuple[Optional[Path], Optional[Path]]:
    """Scan tool messages for "AUTHORING_DONE: suite=..." / "...bug_report=...".

    The terminal tools (write_robot_suite, report_bug) embed these
    markers in their string return values. We trust whichever fires
    last in the transcript.
    """
    suite_path: Optional[Path] = None
    bug_path: Optional[Path] = None
    for msg in transcript:
        content = msg.get("content") if isinstance(msg, dict) else getattr(msg, "content", None)
        if not isinstance(content, str):
            continue
        if "AUTHORING_DONE: suite=" in content:
            suite_path = Path(content.split("AUTHORING_DONE: suite=", 1)[1].splitlines()[0].strip())
        if "AUTHORING_DONE: bug_report=" in content:
            bug_path = Path(content.split("AUTHORING_DONE: bug_report=", 1)[1].splitlines()[0].strip())
    return suite_path, bug_path


# ─── The loop ────────────────────────────────────────────────────────


def author_with_agent(
    *,
    story: str,
    base_url: str,
    suite_path: Path,
    bug_report_dir: Path,
    source_root: Optional[Path] = None,
    engine: str = "agent-browser",
    max_iters: int = DEFAULT_MAX_ITERS,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    debug: bool = False,
) -> AuthoringResult:
    """Public entry point: retries the inner loop on crash/recursion-limit.

    Each retry feeds back a short hint about why the prior attempt failed,
    so the next attempt knows to be more decisive (terminate earlier).
    """
    last_failure: Optional[str] = None
    for attempt in range(1, max_attempts + 1):
        suffix = ""
        if last_failure:
            suffix = (
                f"\n\n[RETRY {attempt}/{max_attempts}] Prior attempt failed: "
                f"{last_failure}. Be more decisive — write the suite as soon "
                f"as your selectors are grounded; do not re-validate alternatives "
                f"or re-explore on a fresh page load."
            )
        if debug:
            import sys
            print(f"[author/attempt {attempt}/{max_attempts}]", file=sys.stderr)
        result = _author_once(
            story=story + suffix,
            base_url=base_url,
            suite_path=suite_path,
            bug_report_dir=bug_report_dir,
            source_root=source_root,
            engine=engine,
            max_iters=max_iters,
            debug=debug,
        )
        if result.suite_path or result.bug_report_path:
            return result
        last_failure = result.final_message or "no terminal call reached"
    return result  # last attempt's result


def _author_once(
    *,
    story: str,
    base_url: str,
    suite_path: Path,
    bug_report_dir: Path,
    source_root: Optional[Path] = None,
    engine: str = "agent-browser",
    max_iters: int = DEFAULT_MAX_ITERS,
    debug: bool = False,
) -> AuthoringResult:
    """Run the authoring agent loop.

    The agent decides what to do at each step. We give it:
      - story + base_url + the paths where terminal outputs should go
      - the engine name to declare in the authored suite (${ENGINE})
      - the SKILL.md as system prompt
      - tools: browser_* (Playwright sync API), optional read_file,
        write_robot_suite, report_bug

    The agent either writes the suite (terminal) or files a bug report
    (terminal). If it exhausts max_iters without a terminal call, we
    return a result with ok=False and the transcript for inspection.
    """
    from deepagents import create_deep_agent
    from deepagents.backends import LocalShellBackend
    from langgraph.checkpoint.memory import InMemorySaver

    from aitester_bdd.authoring.tools import build_tools, session_id

    llm = _build_llm()
    tools = build_tools(source_root=source_root)
    system_prompt = load_skill()

    # LocalShellBackend gives the agent an `execute` tool for shell
    # commands (mirrors wise-rpa-bdd). The agent calls `agent-browser
    # <subcommand> --json` directly; session is pinned via env so all
    # invocations share the same persistent browser session for this
    # authoring run.
    backend_env = dict(os.environ)  # inherit PATH etc.
    backend_env["AGENT_BROWSER_SESSION"] = session_id()

    backend = LocalShellBackend(
        root_dir=str(source_root) if source_root else None,
        env=backend_env,
        inherit_env=True,
        timeout=120,
        max_output_bytes=200_000,
    )

    agent = create_deep_agent(
        model=llm,
        tools=tools,
        system_prompt=system_prompt,
        backend=backend,
        checkpointer=InMemorySaver(),
    )

    # Slug for the bug-report filename in case the agent reaches for one.
    slug = _slugify(story)[:60] or "untitled"
    bug_report_path = bug_report_dir / f"{slug}.md"

    user_message = (
        f"Story: {story}\n\n"
        f"Base URL: {base_url}\n\n"
        f"You are authoring an aitester-bdd .robot test suite for this story. "
        f"Follow the SKILL instructions:\n"
        f"  - Drive the live target via `execute \"agent-browser ... --json\"` "
        f"(shell). Chain multiple agent-browser commands with `&&` to batch "
        f"related steps in one shell call. Every selector in the suite must "
        f"come from a real attribute observed on the live page — never invent.\n"
        f"  - Declare ${{ENGINE}}    {engine} in the *** Variables *** section "
        f"of the suite so `aitester run` picks the matching runtime.\n"
        f"  - If the system is broken in a way that prevents authoring, file a bug report.\n\n"
        f"When you are ready:\n"
        f"  - For a .robot suite, call write_robot_suite with path={suite_path}\n"
        f"  - For a bug report, call report_bug with path={bug_report_path}\n\n"
        f"Begin."
    )

    thread_id = f"author-{slug}"
    config = {"configurable": {"thread_id": thread_id}, "recursion_limit": max_iters * 4}

    try:
        if debug:
            result = _invoke_with_debug_stream(agent, user_message, config)
        else:
            result = agent.invoke(
                {"messages": [{"role": "user", "content": user_message}]},
                config=config,
            )
    except Exception as exc:
        log.exception("agent loop failed")
        return AuthoringResult(
            iterations=0,
            final_message=f"agent loop crashed: {type(exc).__name__}: {exc}",
            transcript=[],
        )

    messages = result.get("messages", []) if isinstance(result, dict) else []
    transcript = [
        {
            "type": type(m).__name__,
            "content": getattr(m, "content", None),
            "tool_calls": getattr(m, "tool_calls", None),
        }
        for m in messages
    ]

    found_suite, found_bug = _parse_terminal_markers(transcript)
    last = messages[-1] if messages else None
    final_message = str(getattr(last, "content", "")) if last is not None else ""

    return AuthoringResult(
        suite_path=found_suite,
        bug_report_path=found_bug,
        iterations=len(messages),
        final_message=final_message,
        transcript=transcript,
    )


# ─── Debug streaming ─────────────────────────────────────────────────


def _short(s, n: int = 120) -> str:
    if s is None:
        return ""
    s = str(s).replace("\n", " ⏎ ")
    return s if len(s) <= n else s[:n] + "…"


def _invoke_with_debug_stream(agent, user_message: str, config: dict) -> dict:
    """Stream the agent loop to stderr — one line per agent turn.

    Format:
      [step N] AI: <preview of model text>
      [step N]    ↪ tool browser_validate_selector(css='...', scope='', ...)
      [step N]    ← tool_result (ok=True, count=1, ...)

    Lets the operator watch the explorer:
      - what the model says it's about to do
      - which tool it picks + key args
      - the tool's response (truncated)
    Result shape matches `agent.invoke` so the caller can keep using
    `result["messages"]`.
    """
    import sys

    print("[author/debug] streaming agent loop to stderr", file=sys.stderr)
    print("[author/debug] " + "─" * 60, file=sys.stderr)

    final_state: dict = {}
    step = 0
    seen_ids: set[str] = set()

    for chunk in agent.stream(
        {"messages": [{"role": "user", "content": user_message}]},
        config=config,
        stream_mode="values",
    ):
        final_state = chunk
        messages = chunk.get("messages", []) if isinstance(chunk, dict) else []
        # Print only messages we haven't logged yet (id-based dedup).
        for m in messages:
            mid = getattr(m, "id", None) or id(m)
            if mid in seen_ids:
                continue
            seen_ids.add(mid)
            kind = type(m).__name__
            if kind in ("HumanMessage", "SystemMessage"):
                # User/system prompts — log once, briefly.
                txt = _short(getattr(m, "content", ""), 200)
                print(f"[author/debug] {kind}: {txt}", file=sys.stderr)
                continue
            step += 1
            if kind == "AIMessage":
                content = getattr(m, "content", "") or ""
                if content:
                    print(f"[author/debug] step {step} AI: {_short(content, 200)}", file=sys.stderr)
                for tc in (getattr(m, "tool_calls", None) or []):
                    name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", "?")
                    args = tc.get("args") if isinstance(tc, dict) else getattr(tc, "args", {})
                    # Show 2-3 most useful args; full args dict can be huge.
                    arg_preview = ", ".join(
                        f"{k}={_short(v, 60)!r}" for k, v in list((args or {}).items())[:3]
                    )
                    print(
                        f"[author/debug] step {step}   ↪ {name}({arg_preview})",
                        file=sys.stderr,
                    )
            elif kind == "ToolMessage":
                tname = getattr(m, "name", "?")
                content = _short(getattr(m, "content", ""), 200)
                print(
                    f"[author/debug] step {step}   ← {tname}: {content}",
                    file=sys.stderr,
                )
            else:
                content = _short(getattr(m, "content", ""), 120)
                print(f"[author/debug] step {step} {kind}: {content}", file=sys.stderr)

    print("[author/debug] " + "─" * 60, file=sys.stderr)
    print(f"[author/debug] loop done in {step} steps", file=sys.stderr)
    return final_state


# ─── Utilities ───────────────────────────────────────────────────────


def _slugify(text: str) -> str:
    import re

    text = re.sub(r"[^a-zA-Z0-9\s-]", "", text.lower())
    return re.sub(r"\s+", "_", text.strip())
