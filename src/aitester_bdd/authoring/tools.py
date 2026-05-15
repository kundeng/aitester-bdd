"""Terminal tools for the authoring agent — WISE-style.

The agent drives the live target by shelling out to the `agent-browser`
CLI via DeepAgents' `execute` tool (from LocalShellBackend). The Python
side keeps only the two **terminal** tools:

  - `write_robot_suite` — marks AUTHORING_DONE: suite=...
  - `report_bug` — marks AUTHORING_DONE: bug_report=...

This mirrors wise-rpa-bdd: no wrappers around individual browser
operations — the agent uses bash directly, batches calls with `&&`, and
reads JSON from `agent-browser --json` output natively. ~10× fewer LLM
round-trips than the previous Python-wrapper surface.
"""
from __future__ import annotations

import atexit
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


# ─── agent-browser session ───────────────────────────────────────────────


_SESSION_ID: Optional[str] = None
_AGENT_BROWSER_BIN: Optional[str] = None


def _resolve_bin() -> str:
    global _AGENT_BROWSER_BIN
    if _AGENT_BROWSER_BIN is not None:
        return _AGENT_BROWSER_BIN
    bin_path = shutil.which("agent-browser")
    if not bin_path:
        raise RuntimeError(
            "agent-browser CLI not found on PATH. Install via "
            "`npm i -g @bayeslearner/agent-browser`."
        )
    _AGENT_BROWSER_BIN = bin_path
    return bin_path


def session_id() -> str:
    """Per-process agent-browser session — fresh per authoring run."""
    global _SESSION_ID
    if _SESSION_ID is None:
        _SESSION_ID = f"aitester-author-{uuid.uuid4().hex[:8]}"
        atexit.register(_cleanup_session)
    return _SESSION_ID


def _cleanup_session() -> None:
    if _SESSION_ID is None:
        return
    try:
        subprocess.run(
            [_resolve_bin(), "--session", _SESSION_ID, "close", "--json"],
            timeout=10, capture_output=True, check=False,
        )
    except Exception:
        pass


# ─── Terminal tool params ────────────────────────────────────────────────


class WriteRobotSuiteParams(BaseModel):
    path: str = Field(description="Absolute path to write the .robot suite to.")
    content: str = Field(description="Complete .robot file body. Must be valid Robot Framework.")


class ReportBugParams(BaseModel):
    path: str = Field(description="Absolute path to write the bug report markdown.")
    content: str = Field(description="Bug report body — what you tried, what you observed, why testing this story is blocked.")


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


# ─── Tool factory ────────────────────────────────────────────────────────


def build_tools(*, source_root: Optional[Path] = None) -> list[StructuredTool]:
    """Return the two terminal tools. The agent uses LocalShellBackend's
    `execute` for everything else (agent-browser CLI, ls, cat, etc.).

    `source_root` is no longer wired into a custom read_file — the agent
    can use the built-in `read_file` from FilesystemBackend, scoped via
    LocalShellBackend's `root_dir`. Kept as a parameter for API
    compatibility.
    """
    return [
        StructuredTool.from_function(
            func=_write_robot_suite, name="write_robot_suite",
            args_schema=WriteRobotSuiteParams,
            description=(
                "TERMINAL TOOL. Write the complete .robot suite to the "
                "given path. Call only when every selector traces to a "
                "stable attribute observed on the live page. The agent "
                "loop stops after this call."
            ),
        ),
        StructuredTool.from_function(
            func=_report_bug, name="report_bug",
            args_schema=ReportBugParams,
            description=(
                "TERMINAL TOOL. Write a bug report when the system is "
                "broken in a way that prevents authoring (unreachable "
                "URL, missing feature, untestable terminal state). The "
                "agent loop stops. Do NOT use for selector difficulty "
                "or async timing — those are authoring problems."
            ),
        ),
    ]
