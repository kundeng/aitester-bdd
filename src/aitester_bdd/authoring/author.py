"""Authoring orchestration.

author_suite(story, base_url, llm) — one-shot author:
    1. Discover the entry page (snapshot via agent-browser).
    2. Load skill grammar.
    3. Hand (story + snapshot + skill) to LLM.
    4. Return .robot text.

refine_suite(suite, dryrun_output, base_url, llm) — patch failing suite:
    1. Re-snapshot the live app.
    2. Hand (suite + dryrun_output + fresh snapshot + skill) to LLM.
    3. Return patched .robot text.

author_with_loop(story, base_url, llm, max_iters) — author + dryrun + refine:
    Stops on first clean dryrun or when max_iters exhausted.
"""
from __future__ import annotations

import logging
import subprocess
import sys
import tempfile
from importlib import resources
from pathlib import Path

from aitester_bdd.discovery.blackbox import discover_blackbox
from aitester_bdd.llm.base import LLMClient

log = logging.getLogger("aitester_bdd.authoring")


def load_skill() -> str:
    """Read the shipped SKILL.md from the package."""
    pkg = resources.files("aitester_bdd").joinpath("skill", "SKILL.md")
    return pkg.read_text(encoding="utf-8")


def author_suite(story: str, base_url: str, llm: LLMClient) -> str:
    """One-shot author: returns a .robot suite as a string."""
    skill = load_skill()
    disc = discover_blackbox(base_url, story)
    return llm.author(
        story=story,
        snapshot=disc.snapshot or "(no snapshot — agent-browser unavailable)",
        skill=skill,
        base_url=base_url,
    )


def refine_suite(
    suite: str,
    dryrun_output: str,
    base_url: str,
    llm: LLMClient,
) -> str:
    """Patch a failing suite based on dryrun output + a fresh snapshot."""
    skill = load_skill()
    disc = discover_blackbox(base_url, story="(refine — re-snapshot)")
    return llm.refine(
        suite=suite,
        dryrun_output=dryrun_output,
        latest_snapshot=disc.snapshot or "(no snapshot)",
        skill=skill,
    )


def run_dryrun(suite_path: Path) -> tuple[bool, str]:
    """Run `robot --dryrun` against a suite. Returns (passed, output)."""
    cmd = [
        sys.executable, "-m", "robot",
        "--dryrun",
        "--output", "NONE",
        "--log", "NONE",
        "--report", "NONE",
        str(suite_path),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    output = (r.stdout or "") + ("\n" + r.stderr if r.stderr else "")
    return (r.returncode == 0, output)


def author_with_loop(
    story: str,
    base_url: str,
    llm: LLMClient,
    *,
    max_iters: int = 3,
    workdir: Path | None = None,
) -> tuple[Path, str, list[str]]:
    """Author + dryrun-refine loop.

    Returns (suite_path, final_suite_text, history_of_outputs).
    Stops when dryrun passes or max_iters exhausted.
    """
    workdir = workdir or Path(tempfile.mkdtemp(prefix="aitester-author-"))
    workdir.mkdir(parents=True, exist_ok=True)

    suite_path = workdir / "suite.robot"
    history: list[str] = []

    suite = author_suite(story, base_url, llm)
    suite_path.write_text(suite, encoding="utf-8")

    for i in range(max_iters):
        passed, output = run_dryrun(suite_path)
        history.append(f"# iter {i + 1} dryrun:\n{output[:2000]}")
        if passed:
            log.info("dryrun passed on iter %d", i + 1)
            return (suite_path, suite, history)
        log.info("dryrun failed on iter %d, refining…", i + 1)
        suite = refine_suite(suite, output, base_url, llm)
        suite_path.write_text(suite, encoding="utf-8")

    return (suite_path, suite, history)
