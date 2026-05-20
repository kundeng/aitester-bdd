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

log = logging.getLogger("aitester_bdd.authoring.agent_loop")


# ─── Defaults ────────────────────────────────────────────────────────


DEFAULT_MODEL = "cc/claude-opus-4-7"
DEFAULT_BASE_URL = "http://localhost:20128/v1"
DEFAULT_API_KEY = "placeholder"
DEFAULT_MAX_ITERS = 100
DEFAULT_MAX_ATTEMPTS = 2  # how many times to re-run the whole author loop on failure

_EXPLORE_SYSTEM_PROMPT = """You are a fluid test agent. Your job is to drive a web browser through a user journey described in the story, exactly as a human operator would.

CRITICAL RULES:
1. READ THE FULL STORY CAREFULLY. Count how many distinct actions/verifications are described. You MUST perform ALL of them before calling journey_complete. If the story says "login, navigate, click a case, open Actions, click Defer, type reason, confirm, verify toast" — that is 8 steps and you must do all 8.
2. Do NOT call journey_complete after just the first few steps. If you've only logged in and navigated but the story asks you to also interact with a case — YOU ARE NOT DONE.
3. Use agent-browser CLI commands via the execute tool. Chain commands with && for efficiency.
4. After each action, verify it worked (snapshot, check URL, check element state).
5. If a step fails after reasonable retries (2-3 attempts), call journey_blocked with details.
6. Only call journey_complete when EVERY action and EVERY verification in the story has been performed. Re-read the story before calling journey_complete to make sure you haven't skipped anything.
7. Your journey_complete notes must describe each step you took and what you observed — these notes are the test evidence.

agent-browser quick reference:
  open <url> --json | snapshot -c -i --json | click '<css|@ref>' --json
  fill '<css>' '<text>' --json | type '<css>' '<text>' --json
  press '<key>' --json | wait '<css>'|<ms> --json | eval '<js>' --json
  get count|text|html|attr '<css>' --json
  find role|text|label|placeholder|testid <value> click|fill [text] --json

Snapshot refs (@e1, @e2) are valid for click/fill/type/get commands.
CSS selectors are valid everywhere. Prefer data-testid > @ref > CSS.
"""


# ─── Outputs ─────────────────────────────────────────────────────────


@dataclass
class AuthoringResult:
    """What the agent loop produced.

    Exactly one of `suite_path` / `bug_report_path` is set on success
    (corresponding to the terminal tool the agent called). If both are
    None, the agent exhausted iterations without calling a terminal
    tool — treat as a soft failure.
    """

    suite_path: Path | None = None
    bug_report_path: Path | None = None
    iterations: int = 0
    final_message: str = ""
    transcript: list[dict] = None  # full message list for debugging

    @property
    def ok(self) -> bool:
        return self.suite_path is not None or self.bug_report_path is not None


@dataclass
class ExploreResult:
    """What the explore agent loop produced.

    `passed` = True means the agent completed the journey without reporting
    a bug.  `notes` is the human-readable step-by-step summary.
    `bug_report` is set when the agent couldn't complete the journey.
    """

    passed: bool = False
    notes: str = ""
    bug_report: str = ""
    iterations: int = 0
    final_message: str = ""


# ─── Helpers ─────────────────────────────────────────────────────────


def load_skill() -> str:
    """Read the shipped SKILL.md from the package."""
    pkg = resources.files("aitester_bdd").joinpath("skill", "SKILL.md")
    return pkg.read_text(encoding="utf-8")


def build_system_prompt(mode: str, *, pinning: str = "auto") -> str:
    """Assemble the system prompt for the given mode.

    Modes: 'author' (default CLI), 'explore' (fluid test), 'explore_and_author'.
    The base skill (SKILL.md) is shared; each mode gets a preamble that overrides
    the termination conditions and output expectations.
    """
    if mode == "explore":
        return _EXPLORE_SYSTEM_PROMPT

    base_skill = load_skill()

    if mode == "explore_and_author":
        pinning_guidance = {
            "aggressive": "Pin EVERY step to CSS selectors. Emit I define rule blocks. Use I explore only if a stable selector truly cannot be determined.",
            "conservative": "Pin only login, navigation, and page chrome. Keep interactions and assertions as I explore calls.",
            "none": "Emit ONLY I explore calls capturing the journey structure. No CSS selectors, no I define rule blocks.",
            "auto": "Pin what is structural (login, forms, buttons with data-testid). Keep fluid what is data-dependent (which record, dynamic content, verification of state).",
        }.get(pinning, "")

        preamble = (
            f"MODE: EXPLORE AND AUTHOR (pinning={pinning})\n\n"
            f"You are both TESTING (completing the journey) and AUTHORING (writing a .robot file).\n"
            f"Complete the journey first. Then write the suite.\n\n"
            f"PINNING RULE: {pinning_guidance}\n\n"
            f"The authored .robot CAN contain I explore calls — this is new.\n"
            f"Mixed output (some I define rule blocks + some I explore calls) is valid.\n\n"
            f"---\n\n"
        )
        return preamble + base_skill

    # Default: pure author mode (CLI aitester author)
    return base_skill


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


def _parse_terminal_markers(transcript: list[dict]) -> tuple[Path | None, Path | None]:
    """Scan tool messages for "AUTHORING_DONE: suite=..." / "...bug_report=...".

    The terminal tools (write_robot_suite, report_bug) embed these
    markers in their string return values. We trust whichever fires
    last in the transcript.
    """
    suite_path: Path | None = None
    bug_path: Path | None = None
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
    source_root: Path | None = None,
    engine: str = "agent-browser",
    mode: str = "author",
    pinning: str = "auto",
    max_iters: int = DEFAULT_MAX_ITERS,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    debug: bool = False,
) -> AuthoringResult:
    """Public entry point: retries the inner loop on crash/recursion-limit.

    Each retry feeds back a short hint about why the prior attempt failed,
    so the next attempt knows to be more decisive (terminate earlier).
    """
    last_failure: str | None = None
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
            mode=mode,
            pinning=pinning,
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
    source_root: Path | None = None,
    engine: str = "agent-browser",
    mode: str = "author",
    pinning: str = "auto",
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
    system_prompt = build_system_prompt(mode, pinning=pinning)

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
        f"You are authoring an aitester-bdd .robot test suite for this story.\n\n"
        f"## agent-browser cheatsheet (inline — do NOT run --help)\n\n"
        f"```\n"
        f"agent-browser open <url> --json\n"
        f"agent-browser snapshot -c -i --json   # compact, interactive elements only, with @refs\n"
        f"agent-browser get count '<css>' --json\n"
        f"agent-browser get text '<css>' --json\n"
        f"agent-browser get html '<css>' --json     # CSS only — NOT @refs\n"
        f"agent-browser get attr '@e3'|'<css>' '<name>' --json\n"
        f"agent-browser click '<css|@ref>' --json\n"
        f"agent-browser fill '<css>' '<text>' --json     # clear+fill, native React events\n"
        f"agent-browser type '<css>' '<text>' --json     # append+type\n"
        f"agent-browser press '<key>' --json    # Enter, Tab, Escape, Control+a, ArrowDown\n"
        f"agent-browser wait '<css>'|<ms> --json\n"
        f"agent-browser eval '<js>' --json   # arrow fn or expression; e.g. '() => document.title' or '1+1'\n"
        f"agent-browser find <locator> <value> <action> [text] --json\n"
        f"  locators: role | text | label | placeholder | alt | title | testid | first | last | nth\n"
        f"  actions:  click | fill | check | hover\n"
        f"  examples: find placeholder 'Username' fill admin\n"
        f"            find role button click --name 'Sign in'\n"
        f"            find testid login-form click\n"
        f"```\n\n"
        f"Chain commands with `&&` in one execute call — way fewer LLM round-trips.\n\n"
        f"## Rules\n\n"
        f"  - Every selector in the .robot must come from a real attribute observed on the live page — never invent.\n"
        f"  - Snapshot `name='X'` is the ACCESSIBLE LABEL, not the HTML `name=` attribute. Use `get attr` to read the real HTML attribute.\n"
        f"  - **Stop exploring as soon as every selector is grounded.** Do not re-verify, do not re-snapshot, do not re-explore on a fresh page load — write the suite and exit.\n"
        f"  - Declare ${{ENGINE}}    {engine} in the *** Variables *** section so `aitester run` picks the matching runtime.\n"
        f"  - If the system is broken in a way that prevents authoring, file a bug report instead.\n\n"
        f"When ready:\n"
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


# ─── Explore (fluid test) ────────────────────────────────────────────


def _is_inside_rf() -> bool:
    """Check if we're running inside Robot Framework."""
    try:
        from robot.libraries.BuiltIn import BuiltIn
        BuiltIn().get_library_instance("Browser")
        return True
    except Exception:
        return False


def explore_with_agent(
    *,
    story: str,
    base_url: str,
    session: str | None = None,
    source_root: Path | None = None,
    max_iters: int = DEFAULT_MAX_ITERS,
    debug: bool = False,
) -> ExploreResult:
    """Run the agent loop in explore mode — fluid test, no suite output.

    When running inside RF (aitester run): uses Playwright-backed tools
    that share the walker's RF Browser instance. Same page, same cookies,
    same DOM state. No subprocess, no session handoff.

    When running standalone (no RF context): falls back to agent-browser
    CLI via LocalShellBackend.
    """
    from deepagents import create_deep_agent
    from langgraph.checkpoint.memory import InMemorySaver

    from aitester_bdd.authoring.tools import (
        _reset_explore_state,
        build_explore_tools,
        get_explore_result,
    )

    _reset_explore_state()
    llm = _build_llm()

    # Terminal tools (journey_complete, journey_blocked) — always needed
    terminal_tools = build_explore_tools()

    if _is_inside_rf():
        # Playwright path: typed tools wrapping the shared RF Browser
        from aitester_bdd.authoring.playwright_tools import (
            build_playwright_browser_tools,
            PLAYWRIGHT_EXPLORE_PROMPT,
        )
        browser_tools = build_playwright_browser_tools()
        tools = terminal_tools + browser_tools
        system_prompt = PLAYWRIGHT_EXPLORE_PROMPT
        backend = None
    else:
        # Fallback: agent-browser CLI via shell (standalone / aitester author)
        from deepagents.backends import LocalShellBackend
        from aitester_bdd.authoring.tools import session_id

        tools = terminal_tools
        system_prompt = _EXPLORE_SYSTEM_PROMPT
        browser_session = session or session_id()
        backend_env = dict(os.environ)
        backend_env["AGENT_BROWSER_SESSION"] = browser_session
        backend = LocalShellBackend(
            root_dir=str(source_root) if source_root else None,
            env=backend_env,
            inherit_env=True,
            timeout=120,
            max_output_bytes=200_000,
        )

    agent_kwargs = {
        "model": llm,
        "tools": tools,
        "system_prompt": system_prompt,
        "checkpointer": InMemorySaver(),
    }
    if backend is not None:
        agent_kwargs["backend"] = backend

    agent = create_deep_agent(**agent_kwargs)

    import re
    numbered = re.findall(r'\d+\)', story)
    step_count = len(numbered) if numbered else len([s for s in story.split('.') if s.strip()])

    if _is_inside_rf():
        user_message = (
            f"Story ({step_count} steps): {story}\n\n"
            f"Base URL: {base_url}\n\n"
            f"You are EXPLORING — performing a fluid test of this story against the live app.\n\n"
            f"This story has {step_count} steps. You MUST perform ALL {step_count} before "
            f"calling journey_complete. Do NOT stop after login or navigation — "
            f"continue through every interaction and verification described. "
            f"Re-read the story before calling journey_complete to confirm you "
            f"haven't skipped anything.\n\n"
            f"Start with browser_snapshot() to see the current page state, then proceed.\n\n"
            f"Begin."
        )
    else:
        user_message = (
            f"Story ({step_count} steps): {story}\n\n"
            f"Base URL: {base_url}\n\n"
            f"You are EXPLORING — performing a fluid test of this story against the live app.\n\n"
            f"This story has {step_count} steps. You MUST perform ALL {step_count} before "
            f"calling journey_complete. Do NOT stop after login or navigation — "
            f"continue through every interaction and verification described. "
            f"Re-read the story before calling journey_complete to confirm you "
            f"haven't skipped anything.\n\n"
            f"## agent-browser cheatsheet (inline — do NOT run --help)\n\n"
            f"```\n"
            f"agent-browser open <url> --json\n"
            f"agent-browser snapshot -c -i --json\n"
            f"agent-browser get count '<css>' --json\n"
            f"agent-browser get text '<css>' --json\n"
            f"agent-browser click '<css|@ref>' --json\n"
            f"agent-browser fill '<css>' '<text>' --json\n"
            f"agent-browser type '<css>' '<text>' --json\n"
            f"agent-browser press '<key>' --json\n"
            f"agent-browser wait '<css>'|<ms> --json\n"
            f"agent-browser eval '<js>' --json\n"
            f"agent-browser find <locator> <value> <action> [text] --json\n"
            f"```\n\n"
            f"Chain commands with `&&` in one execute call.\n\n"
            f"## Rules\n\n"
            f"  - Complete the journey end-to-end. Do not stop early.\n"
            f"  - If the system is broken (page won't render, element missing, "
            f"action fails after retries), call journey_blocked.\n"
            f"  - If you complete the full journey successfully, call journey_complete "
            f"with a step-by-step summary of what you did and observed.\n\n"
            f"Begin."
        )

    slug = _slugify(story)[:60] or "explore"
    thread_id = f"explore-{slug}"
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
        log.exception("explore agent loop failed")
        return ExploreResult(
            passed=False,
            bug_report=f"agent loop crashed: {type(exc).__name__}: {exc}",
            iterations=0,
            final_message=str(exc),
        )

    messages = result.get("messages", []) if isinstance(result, dict) else []
    notes, bug = get_explore_result()

    if notes:
        return ExploreResult(
            passed=True,
            notes=notes,
            iterations=len(messages),
            final_message=notes,
        )
    elif bug:
        return ExploreResult(
            passed=False,
            bug_report=bug,
            iterations=len(messages),
            final_message=bug,
        )
    else:
        last = messages[-1] if messages else None
        final_message = str(getattr(last, "content", "")) if last is not None else ""
        return ExploreResult(
            passed=False,
            bug_report=f"Agent exhausted {len(messages)} iterations without completing the journey.",
            iterations=len(messages),
            final_message=final_message,
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
    import time

    t0 = time.monotonic()

    def _ts() -> str:
        elapsed = time.monotonic() - t0
        wall = time.strftime("%H:%M:%S", time.localtime())
        return f"{wall} +{elapsed:6.1f}s"

    def _log(line: str) -> None:
        print(f"[{_ts()}] {line}", file=sys.stderr, flush=True)

    _log("[author/debug] streaming agent loop to stderr")
    _log("[author/debug] " + "─" * 60)

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
                _log(f"[author/debug] {kind}: {txt}")
                continue
            step += 1
            if kind == "AIMessage":
                content = getattr(m, "content", "") or ""
                if content:
                    _log(f"[author/debug] step {step} AI: {_short(content, 200)}")
                for tc in (getattr(m, "tool_calls", None) or []):
                    name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", "?")
                    args = tc.get("args") if isinstance(tc, dict) else getattr(tc, "args", {})
                    # Show 2-3 most useful args; full args dict can be huge.
                    arg_preview = ", ".join(
                        f"{k}={_short(v, 60)!r}" for k, v in list((args or {}).items())[:3]
                    )
                    _log(f"[author/debug] step {step}   ↪ {name}({arg_preview})")
            elif kind == "ToolMessage":
                tname = getattr(m, "name", "?")
                content = _short(getattr(m, "content", ""), 200)
                _log(f"[author/debug] step {step}   ← {tname}: {content}")
            else:
                content = _short(getattr(m, "content", ""), 120)
                _log(f"[author/debug] step {step} {kind}: {content}")

    _log("[author/debug] " + "─" * 60)
    _log(f"[author/debug] loop done in {step} steps")
    return final_state


# ─── Utilities ───────────────────────────────────────────────────────


def _slugify(text: str) -> str:
    import re

    text = re.sub(r"[^a-zA-Z0-9\s-]", "", text.lower())
    return re.sub(r"\s+", "_", text.strip())
