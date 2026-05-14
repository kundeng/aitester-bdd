"""aitester CLI — entrypoints for discover / author / refine / run.

    aitester author --story "..." --base-url http://localhost:5173
    aitester discover --base-url http://localhost:5173 [--source-root /path]
    aitester run path/to/suite.robot
    aitester version
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import typer

app = typer.Typer(
    name="aitester",
    help="LLM-driven BDD test authoring for Robot Framework.",
    no_args_is_help=True,
)


@app.command()
def version() -> None:
    """Print the package version."""
    from aitester_bdd import __version__
    typer.echo(__version__)


@app.command()
def discover(
    base_url: str = typer.Option(..., "--base-url", "-u", help="URL of the target app"),
    source_root: str | None = typer.Option(None, "--source-root", help="Source root for white-box discovery"),
    out: str | None = typer.Option(None, "--out", help="Write the draft suite to this path"),
    story: str = typer.Option("explore the app", "--story", help="Optional story hint"),
) -> None:
    """Discover testable journeys (black-box snapshot + optional white-box source scan)."""
    from aitester_bdd.discovery.blackbox import discover_blackbox
    from aitester_bdd.discovery.whitebox import discover_whitebox

    typer.echo(f"[discover] base_url={base_url}")
    bb = discover_blackbox(base_url, story)
    typer.echo(
        f"  black-box: snapshot bytes={len(bb.snapshot)} buttons={len(bb.found_buttons)} "
        f"inputs={len(bb.found_inputs)} routes={len(bb.found_routes)} login={bb.has_login_form}"
    )
    if source_root:
        wb = discover_whitebox(source_root)
        typer.echo(
            f"  white-box: backend_routes={len(wb.backend_routes)} testids={len(wb.frontend_testids)}"
        )
        for note in wb.notes:
            typer.echo(f"    note: {note}")
    target = Path(out) if out else Path("draft.robot")
    target.write_text(bb.draft_robot)
    typer.echo(f"  wrote draft: {target}")


@app.command()
def author(
    story: str = typer.Option(..., "--story", "-s", help="Plain-English test intention"),
    base_url: str = typer.Option(..., "--base-url", "-u", help="URL of the target app"),
    out: str = typer.Option("suite.robot", "--out", "-o"),
    max_iters: int = typer.Option(3, "--max-iters", help="Max refine iterations on dryrun fail"),
) -> None:
    """Author a .robot suite from a story + live app, refine-on-dryrun-fail."""
    from aitester_bdd.authoring.author import author_with_loop
    from aitester_bdd.llm.aiagent_adapter import AIAgentLLM

    llm = AIAgentLLM()
    typer.echo(f"[author] story={story!r}")
    suite_path, suite, history = author_with_loop(
        story=story, base_url=base_url, llm=llm, max_iters=max_iters,
    )
    Path(out).write_text(suite)
    typer.echo(f"  wrote {out} ({len(suite.splitlines())} lines, {len(history)} iter(s))")


@app.command()
def run(
    suite: str = typer.Argument(..., help="Path to a .robot suite"),
    base_url: str | None = typer.Option(None, "--base-url", help="Override BASE_URL variable"),
) -> None:
    """Run a .robot suite via Robot Framework."""
    cmd = [sys.executable, "-m", "robot"]
    if base_url:
        cmd.extend(["--variable", f"BASE_URL:{base_url}"])
    cmd.append(suite)
    rc = subprocess.run(cmd).returncode
    raise typer.Exit(code=rc)


@app.command()
def doctor() -> None:
    """Check environment readiness."""
    typer.echo("aitester-bdd doctor:")
    try:
        import robot
        typer.echo(f"  ✓ robotframework {robot.__version__}")
    except Exception as e:
        typer.echo(f"  ✗ robotframework: {e}")
    try:
        import Browser  # type: ignore[import-not-found]
        typer.echo(f"  ✓ robotframework-browser {getattr(Browser, '__version__', '?')}")
    except Exception as e:
        typer.echo(f"  ✗ robotframework-browser: {e} (run `rfbrowser init` after install)")
    try:
        import importlib
        importlib.import_module("AIAgent")
        typer.echo("  ✓ robotframework-aiagent")
    except Exception:
        typer.echo("  ~ robotframework-aiagent not installed (will use httpx OpenAI-compat fallback)")
    try:
        r = subprocess.run(["agent-browser", "--version"], capture_output=True, text=True, timeout=5)
        typer.echo(f"  ✓ agent-browser {r.stdout.strip() or r.stderr.strip()}")
    except FileNotFoundError:
        typer.echo("  ✗ agent-browser CLI not found (required for discovery + snapshots)")


if __name__ == "__main__":
    app()
