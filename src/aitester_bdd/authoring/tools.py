"""LangChain tools for the authoring agent — agent-browser CLI explorer.

The agent drives the live target via the `agent-browser` CLI. Why this
backend instead of Playwright sync API:

  - Accessibility-tree snapshots are 1-2 orders of magnitude tighter
    than raw DOM dumps. A login page is 3 lines (`- button "Sign in"
    [ref=e1]`) vs ~800 tokens of JSON.
  - Every interactive element gets an ephemeral `@ref` (`@e1`, `@e2`,
    ...). All subsequent action/inspection tools accept `@refs` OR
    CSS — the agent never has to synthesize a selector mid-exploration.
  - `find role|text|label|placeholder|alt|title|testid <value> <action>`
    is Playwright's locator API exposed verbatim. Author by intent,
    crystallize CSS only at suite-write time.
  - `wait <sel|ms>`, `get count <sel>`, `get attr <ref> <name>` are
    first-class — no need for raw JS evals.

The authored `.robot` still emits PURE CSS so all three runtime backends
(agent-browser, playwright/rfbrowser, nodriver) can replay the suite.
The agent uses `browser_get_attr(ref, name)` to bridge ref → stable CSS
attribute (`data-testid`, `aria-label`, `id`, `name`, `placeholder`).
"""
from __future__ import annotations

import atexit
import json
import logging
import os
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Optional

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

log = logging.getLogger("aitester_bdd.authoring.tools")


# ─── agent-browser session client ─────────────────────────────────────


_SESSION_ID: Optional[str] = None
_AGENT_BROWSER_BIN: Optional[str] = None
_DEFAULT_TIMEOUT_SEC = 30


def _resolve_bin() -> str:
    """Locate the agent-browser CLI. Cached after first lookup."""
    global _AGENT_BROWSER_BIN
    if _AGENT_BROWSER_BIN is not None:
        return _AGENT_BROWSER_BIN
    bin_path = shutil.which("agent-browser")
    if not bin_path:
        raise RuntimeError(
            "agent-browser CLI not found on PATH. Install via `npm i -g "
            "@bayeslearner/agent-browser` or follow the agent-browser "
            "project README."
        )
    _AGENT_BROWSER_BIN = bin_path
    return bin_path


def _session() -> str:
    """Return (and lazily create) the per-run session id."""
    global _SESSION_ID
    if _SESSION_ID is None:
        _SESSION_ID = f"aitester-author-{uuid.uuid4().hex[:8]}"
    return _SESSION_ID


def _ab(*args: str, timeout: int = _DEFAULT_TIMEOUT_SEC) -> dict:
    """Run an agent-browser subcommand, return the parsed JSON envelope.

    Always uses `--json` so callers get a structured `{success, data,
    error}` shape. On non-zero exit OR `success=false`, returns a
    normalized error dict instead of raising — the agent tool wrappers
    surface this as `ERROR: <msg>` so the LLM can recover.
    """
    cmd = [_resolve_bin(), "--session", _session(), *args, "--json"]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"success": False, "error": f"timeout after {timeout}s: {' '.join(args)}"}
    except Exception as exc:
        return {"success": False, "error": f"{type(exc).__name__}: {exc}"}
    stdout = (proc.stdout or "").strip()
    if not stdout:
        # Non-JSON CLI failure (rare). Return stderr.
        return {
            "success": False,
            "error": (proc.stderr or "").strip() or f"exit {proc.returncode}, empty stdout",
        }
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        return {"success": False, "error": f"non-JSON output: {stdout[:200]}"}


def _ok(reply: dict, key: Optional[str] = None):
    """Unwrap the success envelope, or raise a tool-result string."""
    if not reply.get("success"):
        return f"ERROR: {reply.get('error') or 'unknown error'}"
    data = reply.get("data")
    if key is None:
        return data
    if isinstance(data, dict):
        return data.get(key)
    return data


def _close_session() -> None:
    """Best-effort cleanup at interpreter exit."""
    global _SESSION_ID
    if _SESSION_ID is None:
        return
    try:
        subprocess.run(
            [_resolve_bin(), "--session", _SESSION_ID, "close"],
            capture_output=True, timeout=10,
        )
    except Exception:
        pass
    _SESSION_ID = None


atexit.register(_close_session)


# ─── Tool argument schemas ────────────────────────────────────────────


class OpenParams(BaseModel):
    url: str = Field(description="Absolute URL to navigate to.")


class SnapshotParams(BaseModel):
    scope: str = Field(
        default="",
        description=(
            "Optional CSS selector to scope the snapshot to (e.g., "
            "'.chat-panel'). Empty = whole page. Use scope to keep the "
            "tree tight when you're working inside one card/panel."
        ),
    )
    depth: int = Field(
        default=0,
        description="Optional max depth of the a11y tree. 0 = no limit.",
    )


class TargetParams(BaseModel):
    target: str = Field(
        description=(
            "Either an @ref from a recent snapshot (e.g. '@e3') OR a "
            "CSS selector. @refs are session-scoped and shorter."
        ),
    )


class FillParams(BaseModel):
    target: str = Field(description="@ref or CSS selector for the input.")
    text: str = Field(description="Text to fill (clears the field first).")


class PressParams(BaseModel):
    key: str = Field(
        description=(
            "Key name (e.g. 'Enter', 'Tab', 'Escape', 'Control+a', "
            "'ArrowDown'). Playwright key syntax."
        ),
    )


class FindParams(BaseModel):
    locator: str = Field(
        description=(
            "Locator type: 'role' | 'text' | 'label' | 'placeholder' "
            "| 'alt' | 'title' | 'testid'. The Playwright-style "
            "intent vocabulary. Prefer 'role' (most stable, "
            "accessibility-grounded)."
        ),
    )
    value: str = Field(
        description=(
            "Value to match. For locator='role', this is the role name "
            "(e.g. 'button', 'textbox', 'link'). For locator='testid', "
            "the testid string. For text/label/placeholder/alt/title, "
            "the visible text."
        ),
    )
    action: str = Field(
        description=(
            "What to do once found: 'click' | 'fill' | 'type' | 'hover' "
            "| 'focus' | 'check' | 'uncheck' | 'get_text' | 'get_count'."
        ),
    )
    name: str = Field(
        default="",
        description=(
            "For locator='role': accessible name filter (e.g. for "
            "`role=button --name 'Sign in'`). Empty = no name filter."
        ),
    )
    text: str = Field(
        default="",
        description="For action='fill' or 'type': the text to enter.",
    )


class GetParams(BaseModel):
    what: str = Field(
        description=(
            "What to read: 'text' | 'html' | 'value' | 'count' | "
            "'url' | 'title'. 'count' matches a CSS selector and "
            "returns the integer count. 'url' / 'title' ignore target."
        ),
    )
    target: str = Field(
        default="",
        description=(
            "@ref or CSS selector. Required for text/html/value/count. "
            "Ignored for url/title."
        ),
    )


class GetAttrParams(BaseModel):
    target: str = Field(description="@ref or CSS selector.")
    name: str = Field(
        description="Attribute name (e.g. 'data-testid', 'aria-label', 'id').",
    )


class WaitParams(BaseModel):
    target: str = Field(
        description=(
            "Either a CSS selector to wait for (becomes attached/visible) "
            "OR an integer number of milliseconds to sleep."
        ),
    )


class ValidateSelectorParams(BaseModel):
    css: str = Field(
        description=(
            "CSS selector to validate. Supports pipe-fallback (`a | b "
            "| c`); each candidate is checked independently."
        ),
    )
    scope: str = Field(
        default="",
        description=(
            "Optional parent CSS scope. Each candidate is tested under "
            "`scope >> candidate`. Use to disambiguate selectors that "
            "aren't unique page-wide but are unique within a card / panel."
        ),
    )
    expected: str = Field(
        default="unique",
        description=(
            "Expected match count: 'unique' (count == 1, the default — "
            "for click/type/observe targets) OR 'many' (count >= 1 — "
            "for table rows / list items) OR 'absent' (count == 0 — "
            "for negation/disappearance asserts)."
        ),
    )


class ReadFileParams(BaseModel):
    path: str = Field(description="Absolute path inside the configured source_root.")


class WriteRobotSuiteParams(BaseModel):
    path: str = Field(description="Absolute path to write the .robot file to.")
    content: str = Field(description="The complete .robot suite contents.")


class ReportBugParams(BaseModel):
    path: str = Field(description="Absolute path to write the bug report markdown to.")
    content: str = Field(description="The bug report markdown contents.")


class ScreenshotParams(BaseModel):
    path: str = Field(
        default="/tmp/aitester-bdd-shot.png",
        description="Absolute path to save the PNG screenshot to.",
    )


# ─── Browser tools ────────────────────────────────────────────────────


def browser_open(url: str) -> str:
    """Navigate the live browser to a URL. Persistent session across calls."""
    reply = _ab("open", url)
    if not reply.get("success"):
        return f"ERROR: {reply.get('error')}"
    return f"OK: navigated to {url}"


def browser_snapshot(scope: str = "", depth: int = 0) -> str:
    """Accessibility-tree snapshot of the current page (or scope).

    Returns a YAML-style tree of interactive elements with their roles,
    accessible names, and `@ref` markers you can pass to other tools.
    Example:

        - textbox "Username" [ref=e1]
        - textbox "Password" [ref=e2]
        - button "Sign in" [ref=e3]

    `@refs` are ephemeral — they last only until the next snapshot. The
    accompanying ref map (`{e1: {role, name}, ...}`) tells you what each
    one is. Use `browser_get_attr(@ref, '<attr>')` to materialize a
    stable CSS attribute (`data-testid`, `aria-label`, `id`, `name`) for
    the authored suite.

    Call after every navigation or action that changes the page.
    """
    args = ["snapshot", "-i", "-c"]
    if scope:
        args.extend(["-s", scope])
    if depth and depth > 0:
        args.extend(["-d", str(depth)])
    reply = _ab(*args)
    if not reply.get("success"):
        return f"ERROR: {reply.get('error')}"
    data = reply.get("data") or {}
    snap = data.get("snapshot", "")
    refs = data.get("refs", {})
    if not snap:
        return "(empty snapshot — no interactive elements)"
    # Inline a compact ref map at the end so the LLM can correlate.
    if refs:
        ref_lines = "\n".join(
            f"  {k}: role={v.get('role')} name={v.get('name')!r}"
            for k, v in refs.items()
        )
        return f"{snap}\n\n[refs]\n{ref_lines}"
    return snap


def browser_click(target: str) -> str:
    """Click an element by @ref (from a recent snapshot) or CSS selector."""
    reply = _ab("click", target)
    if not reply.get("success"):
        return f"ERROR: {reply.get('error')}"
    return f"OK: clicked {target}"


def browser_fill(target: str, text: str) -> str:
    """Clear then fill an input. @ref or CSS selector.

    Defensive: agent-browser's `fill` claims to clear but empirically
    appends on some inputs (textareas, controlled React inputs). We
    force-clear via focus + Control+a + Delete before filling.
    """
    _ab("click", target)              # focus the field
    _ab("press", "Control+a")         # select all (Playwright normalizes on Mac)
    _ab("press", "Delete")            # delete selection
    reply = _ab("fill", target, text)
    if not reply.get("success"):
        return f"ERROR: {reply.get('error')}"
    return f"OK: filled {target} with {len(text)} chars (cleared first)"


def browser_press(key: str) -> str:
    """Press a key (Enter, Tab, Escape, Control+a, ArrowDown, ...)."""
    reply = _ab("press", key)
    if not reply.get("success"):
        return f"ERROR: {reply.get('error')}"
    return f"OK: pressed {key}"


def browser_find(
    locator: str, value: str, action: str,
    name: str = "", text: str = "",
) -> str:
    """Playwright-style locator-then-act in one call.

    Examples:
      find role textbox fill --name "Username" --text "admin"
      find role button click --name "Sign in"
      find testid chat-input click
      find text "Forgot password?" click
      find role button get_text --name "Submit"

    Use this when you know the element by its accessible role+name
    rather than by a CSS selector — way more stable than synthesizing
    selectors yourself.
    """
    args = ["find", locator, value, action]
    if name:
        args.extend(["--name", name])
    if action in ("fill", "type") and text:
        args.append(text)
    reply = _ab(*args)
    if not reply.get("success"):
        return f"ERROR: {reply.get('error')}"
    data = reply.get("data") or {}
    if action in ("get_text", "get_count"):
        return json.dumps(data, indent=2)
    return f"OK: {action} via {locator}={value!r}" + (f" name={name!r}" if name else "")


def browser_get(what: str, target: str = "") -> str:
    """Read a single piece of page state.

    `what` is one of: text | html | value | count | url | title.
    `target` is an @ref or CSS selector (required for text/html/value/count;
    ignored for url/title).
    """
    args = ["get", what]
    if target and what not in ("url", "title"):
        args.append(target)
    reply = _ab(*args)
    if not reply.get("success"):
        return f"ERROR: {reply.get('error')}"
    data = reply.get("data") or {}
    if isinstance(data, dict):
        for k in ("text", "html", "value", "count", "url", "title"):
            if k in data:
                return str(data[k])
    return json.dumps(data)


def browser_get_attr(target: str, name: str) -> str:
    """Resolve an @ref (or CSS selector) to a single DOM attribute.

    The ref→CSS bridge. After `browser_find` or `browser_snapshot`
    picks an element via its role+name, call this to materialize a
    stable CSS attribute (`data-testid`, `aria-label`, `id`, `name`,
    `placeholder`, `href`) for the authored suite.

    Returns the value as a string, or empty string when the attribute
    is not set on the element.
    """
    reply = _ab("get", "attr", target, name)
    if not reply.get("success"):
        return f"ERROR: {reply.get('error')}"
    data = reply.get("data") or {}
    val = data.get("value") if isinstance(data, dict) else None
    return "" if val is None else str(val)


def browser_wait(target: str) -> str:
    """Wait for a CSS selector (becomes attached/visible) OR sleep N ms.

    `target` is either a CSS selector OR an integer string ("5000")
    interpreted as milliseconds to sleep.
    """
    reply = _ab("wait", target, timeout=60)
    if not reply.get("success"):
        return f"ERROR: {reply.get('error')}"
    return f"OK: waited on {target}"


# Tailwind utility class prefixes — selectors built ONLY out of these
# are brittle. Same heuristic as the Playwright-era validator.
_TAILWIND_PREFIXES = (
    "px-", "py-", "pt-", "pb-", "pl-", "pr-", "p-",
    "mx-", "my-", "mt-", "mb-", "ml-", "mr-", "m-",
    "w-", "h-", "min-w-", "min-h-", "max-w-", "max-h-",
    "bg-", "text-", "border-", "rounded-", "shadow-", "ring-",
    "flex-", "grid-", "gap-", "space-", "divide-",
    "items-", "justify-", "self-", "place-", "content-",
    "leading-", "tracking-", "font-", "uppercase", "lowercase",
    "overflow-", "z-", "opacity-", "cursor-", "select-", "pointer-",
    "transition-", "duration-", "ease-", "animate-",
    "hover:", "focus:", "active:", "disabled:", "group-",
    "sm:", "md:", "lg:", "xl:", "2xl:", "dark:",
)


def _looks_brittle(css: str) -> tuple[bool, str]:
    """Lightweight brittleness check on the selector string."""
    for marker in (
        "data-testid=", "aria-label=", "[id=", "#",
        "[name=", "role=", "[href",
    ):
        if marker in css:
            return False, ""
    dotted = [c.strip(".") for c in css.split(".") if c.strip(".")]
    class_tokens = [c for c in dotted if not any(
        ch in c for ch in ("[", "]", ">", " ", "#")
    )]
    if len(class_tokens) >= 3 and all(
        any(t.startswith(p) for p in _TAILWIND_PREFIXES)
        for t in class_tokens
    ):
        return True, (
            "selector is built only from Tailwind utility classes — "
            "these rebuild on theme/variant changes. Prefer a "
            "data-testid, aria-label, role+name, or one semantic class."
        )
    return False, ""


def browser_validate_selector(
    css: str, scope: str = "", expected: str = "unique",
) -> str:
    """Validate a CSS selector against the live page BEFORE writing it
    into a .robot suite.

    Returns a JSON report per candidate: {candidate, count, expected,
    ok, warning?, suggestion?}. For pipe-fallback (`a | b | c`), each
    candidate is validated independently.

    expected:
      - 'unique' (default) — count == 1 (click/type/observe targets)
      - 'many'              — count >= 1 (table rows, list items)
      - 'absent'            — count == 0 (disappearance asserts)
    """
    candidates = [s.strip() for s in css.split("|")] if "|" in css else [css]
    reports = []
    for cand in candidates:
        full = (
            cand if not scope
            else (scope if cand.strip() == "." else f"{scope} >> {cand}")
        )
        count_reply = _ab("get", "count", full)
        if not count_reply.get("success"):
            reports.append({
                "candidate": full,
                "error": count_reply.get("error"),
                "ok": False,
            })
            continue
        data = count_reply.get("data") or {}
        count = data.get("count", 0) if isinstance(data, dict) else 0
        if expected == "unique":
            ok = (count == 1)
        elif expected == "many":
            ok = (count >= 1)
        elif expected == "absent":
            ok = (count == 0)
        else:
            ok = False
        report: dict = {
            "candidate": full, "count": count,
            "expected": expected, "ok": ok,
        }
        if count == 0 and expected != "absent":
            report["suggestion"] = (
                "0 matches — re-check via browser_snapshot, the "
                "element may not be rendered yet (use browser_wait), "
                "or the selector spelling is wrong."
            )
        elif expected == "unique" and count > 1:
            report["suggestion"] = (
                "not unique — narrow with a parent scope ('And I scope "
                "children to') OR pick a stable attribute "
                "(data-testid > aria-label > id > name > placeholder) "
                "via browser_get_attr(<ref>, '<attr>')."
            )
        brittle, why = _looks_brittle(full)
        if brittle:
            report["warning"] = why
        reports.append(report)
    if len(reports) == 1:
        return json.dumps(reports[0], indent=2)
    return json.dumps({"candidates": reports}, indent=2)


def browser_screenshot(path: str = "/tmp/aitester-bdd-shot.png") -> str:
    """Save a PNG screenshot of the current viewport."""
    reply = _ab("screenshot", path)
    if not reply.get("success"):
        return f"ERROR: {reply.get('error')}"
    return f"OK: wrote {path}"


# ─── File tools ───────────────────────────────────────────────────────


_SOURCE_ROOT: Optional[Path] = None


def _read_file(path: str) -> str:
    """Read a source file under the configured source_root."""
    if _SOURCE_ROOT is None:
        return "ERROR: read_file not enabled (no source_root configured)"
    try:
        p = Path(path).resolve()
        if not str(p).startswith(str(_SOURCE_ROOT.resolve())):
            return f"ERROR: path {p} is outside source_root {_SOURCE_ROOT}"
        return p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return f"ERROR: file not found: {path}"
    except Exception as exc:
        return f"ERROR: {type(exc).__name__}: {exc}"


def _write_robot_suite(path: str, content: str) -> str:
    """TERMINAL TOOL. Write the .robot suite and stop the agent loop."""
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"OK: wrote {len(content)} bytes to {p}\nAUTHORING_DONE: suite={p}"
    except Exception as exc:
        return f"ERROR: {type(exc).__name__}: {exc}"


def _report_bug(path: str, content: str) -> str:
    """TERMINAL TOOL. Write a bug report and stop the agent loop."""
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"OK: wrote bug report to {p}\nAUTHORING_DONE: bug_report={p}"
    except Exception as exc:
        return f"ERROR: {type(exc).__name__}: {exc}"


# ─── Tool factory ─────────────────────────────────────────────────────


def build_tools(
    *, source_root: Optional[Path] = None,
) -> list[StructuredTool]:
    """Create the lean agent-browser-backed tool set.

    10 browser tools + optional read_file + 2 terminal tools.
    """
    global _SOURCE_ROOT
    _SOURCE_ROOT = source_root

    tools: list[StructuredTool] = [
        StructuredTool.from_function(
            func=browser_open, name="browser_open", args_schema=OpenParams,
            description="Navigate the live browser to a URL. Persistent session across calls.",
        ),
        StructuredTool.from_function(
            func=browser_snapshot, name="browser_snapshot",
            args_schema=SnapshotParams,
            description=(
                "Take an accessibility-tree snapshot — YAML-style tree of "
                "interactive elements with role+name+@ref markers. Way "
                "tighter than a DOM dump (~30 tokens for a login form). "
                "Returns elements like `- button \"Sign in\" [ref=e3]` "
                "that you can pass directly to browser_click/fill/etc. "
                "@refs are ephemeral — they last until the next snapshot. "
                "Optionally scope to a CSS selector (e.g. scope='.chat-panel') "
                "to keep the tree tight when you're working inside one "
                "card / panel. Call after every navigation or state change."
            ),
        ),
        StructuredTool.from_function(
            func=browser_click, name="browser_click", args_schema=TargetParams,
            description="Click an element by @ref (from a snapshot) or CSS selector.",
        ),
        StructuredTool.from_function(
            func=browser_fill, name="browser_fill", args_schema=FillParams,
            description="Clear then fill an input. Target is @ref or CSS selector.",
        ),
        StructuredTool.from_function(
            func=browser_press, name="browser_press", args_schema=PressParams,
            description=(
                "Press a key (Enter, Tab, Escape, Control+a, ArrowDown, ...). "
                "Acts on the focused element."
            ),
        ),
        StructuredTool.from_function(
            func=browser_find, name="browser_find", args_schema=FindParams,
            description=(
                "Playwright-style locator-then-act in one call: find an "
                "element by role/text/label/placeholder/testid/etc. and "
                "perform an action. Examples: "
                "`find role textbox fill --name 'Username' --text 'admin'`, "
                "`find role button click --name 'Sign in'`, "
                "`find testid chat-input click`, "
                "`find text 'Forgot password?' click`. "
                "Use this instead of synthesizing CSS selectors — way "
                "more stable than guessing class soup."
            ),
        ),
        StructuredTool.from_function(
            func=browser_get, name="browser_get", args_schema=GetParams,
            description=(
                "Read page state: what ∈ {text, html, value, count, url, "
                "title}. target is @ref or CSS (required for "
                "text/html/value/count; ignored for url/title). "
                "count returns the integer match count."
            ),
        ),
        StructuredTool.from_function(
            func=browser_get_attr, name="browser_get_attr", args_schema=GetAttrParams,
            description=(
                "Resolve an @ref (or CSS) to a single DOM attribute "
                "value. THIS IS YOUR REF→CSS BRIDGE: after browser_find "
                "or browser_snapshot picks an element by role+name, "
                "call this for 'data-testid' / 'aria-label' / 'id' / "
                "'name' / 'placeholder' to materialize a stable CSS "
                "attribute for the authored .robot suite. Returns '' "
                "when the attribute is not set."
            ),
        ),
        StructuredTool.from_function(
            func=browser_wait, name="browser_wait", args_schema=WaitParams,
            description=(
                "Wait for a CSS selector to attach/become visible OR sleep "
                "N milliseconds. Use this instead of raw timing tricks — "
                "covers SSE streams, late-rendered tool cards, transitions."
            ),
        ),
        StructuredTool.from_function(
            func=browser_validate_selector, name="browser_validate_selector",
            args_schema=ValidateSelectorParams,
            description=(
                "REQUIRED BEFORE WRITING ANY SELECTOR INTO .robot. "
                "Validates the candidate against the live page: returns "
                "{count, ok (vs expected), warning, suggestion}. "
                "expected='unique' (default) for click/type/observe; "
                "'many' for table rows; 'absent' for disappearance asserts. "
                "If ok=false, READ THE SUGGESTION — tighten via "
                "browser_get_attr OR add scope. If warning is set, the "
                "selector compounds Tailwind utility classes — replace "
                "with a semantic anchor. Validate every selector, "
                "including pipe-fallback alternatives (each checked "
                "independently)."
            ),
        ),
        StructuredTool.from_function(
            func=browser_screenshot, name="browser_screenshot",
            args_schema=ScreenshotParams,
            description="Save a PNG screenshot of the current viewport.",
        ),
    ]

    if source_root is not None:
        tools.append(
            StructuredTool.from_function(
                func=_read_file, name="read_file", args_schema=ReadFileParams,
                description=(
                    "Read a source file under the configured source_root "
                    "(white-box mode). Use to find route definitions, "
                    "data-testid declarations, component selectors."
                ),
            ),
        )

    tools.extend([
        StructuredTool.from_function(
            func=_write_robot_suite, name="write_robot_suite",
            args_schema=WriteRobotSuiteParams,
            description=(
                "TERMINAL TOOL. Write the complete .robot suite to the given "
                "path. Call only when every selector traces to a stable "
                "attribute on the live page and you are ready to hand the "
                "suite off. The agent loop stops after this call."
            ),
        ),
        StructuredTool.from_function(
            func=_report_bug, name="report_bug",
            args_schema=ReportBugParams,
            description=(
                "TERMINAL TOOL. Write a bug report when the system is broken "
                "in a way that prevents authoring (unreachable URL, broken "
                "auth, missing feature, untestable terminal state). The "
                "agent loop stops. Do NOT use for selector difficulty or "
                "async timing — those are authoring problems."
            ),
        ),
    ])

    return tools
