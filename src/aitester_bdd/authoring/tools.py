"""LangChain tools for the authoring agent.

Borrowed pattern from prismi3's aitester/browser.py — wraps the
`agent-browser` CLI as StructuredTools so the deepagents ReAct loop can
drive a live target.

The agent uses these to explore the site BEFORE writing the .robot
suite (or bug report). No imagined selectors — every selector in the
final suite must trace to a snapshot the agent took via these tools.
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Optional

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

log = logging.getLogger("aitester_bdd.authoring.tools")


# ─── Tool argument schemas ────────────────────────────────────────────


class OpenParams(BaseModel):
    url: str = Field(description="Absolute URL to navigate to.")


class CssParams(BaseModel):
    css: str = Field(description="CSS selector (Playwright syntax — supports >>, text=, nth=).")


class TypeParams(BaseModel):
    css: str = Field(description="CSS selector for the input element.")
    text: str = Field(description="Text to type into the element.")


class EvalParams(BaseModel):
    js: str = Field(description="JavaScript expression to evaluate in the page context.")


class ScreenshotParams(BaseModel):
    path: str = Field(
        default="/tmp/aitester-bdd-shot.png",
        description="Absolute path to save the PNG screenshot to.",
    )


class ReadFileParams(BaseModel):
    path: str = Field(description="Absolute path inside the optional source_root.")


class WriteRobotSuiteParams(BaseModel):
    path: str = Field(description="Absolute path to write the .robot file to.")
    content: str = Field(description="The complete .robot suite contents.")


class ReportBugParams(BaseModel):
    path: str = Field(description="Absolute path to write the bug report markdown to.")
    content: str = Field(description="The bug report markdown contents.")


# ─── agent-browser CLI bridge ─────────────────────────────────────────


def _run_ab(*args: str, timeout: int = 20) -> str:
    """Run an `agent-browser` CLI command. Returns stdout (or "ERROR: ...")."""
    cmd = ["agent-browser", *args]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        out = (r.stdout or "").strip()
        err = (r.stderr or "").strip()
        if r.returncode != 0:
            return f"ERROR (exit {r.returncode}): {err or out}"
        return out
    except subprocess.TimeoutExpired:
        return f"ERROR: agent-browser {' '.join(args)} timed out after {timeout}s"
    except FileNotFoundError:
        return "ERROR: agent-browser CLI not found on PATH"


# ─── Tool implementations ─────────────────────────────────────────────


def browser_open(url: str) -> str:
    """Navigate the live browser to a URL. Persistent session between calls."""
    return _run_ab("open", url)


def browser_snapshot() -> str:
    """Take an accessibility-tree snapshot of the current page.

    Returns a structured listing of every visible/interactive element
    with CSS selectors, data-testid, aria-label, role, text, and
    attribute summaries. Use this after every navigation / action that
    changes the page — the snapshot is your source of truth for
    selectors.
    """
    return _run_ab("snapshot", "-c", "-d", "3")


def browser_click(css: str) -> str:
    """Click an element by CSS selector."""
    return _run_ab("click", css)


def browser_type(css: str, text: str) -> str:
    """Fill an input element with text (clears existing value first)."""
    return _run_ab("type", css, text)


def browser_get_text(css: str) -> str:
    """Get the inner text of the first matching element."""
    return _run_ab("get", "text", css)


def browser_get_count(css: str) -> str:
    """Count how many elements match a selector. Returns the integer as text."""
    return _run_ab("get", "count", css)


def browser_eval(js: str) -> str:
    """Run a JS expression in the page context. Returns the evaluated result."""
    return _run_ab("eval", js)


def browser_screenshot(path: str = "/tmp/aitester-bdd-shot.png") -> str:
    """Save a PNG screenshot of the current viewport to `path`."""
    return _run_ab("screenshot", path)


def browser_close() -> str:
    """Tear down the browser session. Call when authoring is complete."""
    return _run_ab("close")


# ─── File tools ───────────────────────────────────────────────────────


_SOURCE_ROOT: Optional[Path] = None  # set by build_tools()


def _read_file(path: str) -> str:
    """Read a source file under the configured source_root.

    Used for white-box authoring: the agent can grep / read route
    definitions, component files, etc. on demand instead of loading
    them eagerly up front.
    """
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
    """TERMINAL TOOL. Write the .robot suite to `path` and signal that
    authoring is complete. The agent loop will stop after this call."""
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"OK: wrote {len(content)} bytes to {p}\nAUTHORING_DONE: suite={p}"
    except Exception as exc:
        return f"ERROR: {type(exc).__name__}: {exc}"


def _report_bug(path: str, content: str) -> str:
    """TERMINAL TOOL. Write a bug report when the system is broken in a way
    that prevents authoring a meaningful test. The agent loop will stop
    after this call."""
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
    """Create the tool list for the authoring agent.

    Browser tools are always included. The read_file tool is included
    if source_root is provided (white-box mode). Terminal tools
    (write_robot_suite, report_bug) are always included.
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
            description=(
                "Take an accessibility-tree snapshot of the current page. Returns CSS "
                "selectors, data-testid, aria-label, role, text, attribute summaries. "
                "Call this after every navigation or action — it is your source of "
                "truth for selectors. Never invent a selector that is not in a snapshot."
            ),
        ),
        StructuredTool.from_function(
            func=browser_click, name="browser_click", args_schema=CssParams,
            description="Click an element by CSS selector.",
        ),
        StructuredTool.from_function(
            func=browser_type, name="browser_type", args_schema=TypeParams,
            description="Fill an input element with text.",
        ),
        StructuredTool.from_function(
            func=browser_get_text, name="browser_get_text", args_schema=CssParams,
            description="Get the inner text of the first matching element.",
        ),
        StructuredTool.from_function(
            func=browser_get_count, name="browser_get_count", args_schema=CssParams,
            description="Count how many elements match a selector.",
        ),
        StructuredTool.from_function(
            func=browser_eval, name="browser_eval", args_schema=EvalParams,
            description="Run a JS expression in the page context. Returns the evaluated result.",
        ),
        StructuredTool.from_function(
            func=browser_screenshot, name="browser_screenshot",
            args_schema=ScreenshotParams,
            description="Save a PNG screenshot of the current viewport.",
        ),
        StructuredTool.from_function(
            func=browser_close, name="browser_close",
            description="Tear down the browser session.",
        ),
    ]

    if source_root is not None:
        tools.append(
            StructuredTool.from_function(
                func=_read_file, name="read_file", args_schema=ReadFileParams,
                description=(
                    "Read a source file under the configured source_root (white-box "
                    "mode). Use to find route definitions, component files, "
                    "data-testid declarations on demand."
                ),
            ),
        )

    tools.extend([
        StructuredTool.from_function(
            func=_write_robot_suite, name="write_robot_suite",
            args_schema=WriteRobotSuiteParams,
            description=(
                "TERMINAL TOOL. Write the complete .robot suite to the given path. "
                "Call this only when you have driven the live target via agent-browser, "
                "every selector in the suite traces to a snapshot you took, and you "
                "are ready to hand the suite off. The agent loop stops after this call."
            ),
        ),
        StructuredTool.from_function(
            func=_report_bug, name="report_bug",
            args_schema=ReportBugParams,
            description=(
                "TERMINAL TOOL. Write a bug report when the system is broken in a way "
                "that prevents authoring a meaningful test (unreachable URL, broken "
                "auth, missing feature, untestable terminal state). The agent loop "
                "stops after this call. Do NOT use for selector difficulty or async "
                "timing — those are authoring problems, not system bugs."
            ),
        ),
    ])

    return tools
