"""aitester CLI — author / run / doctor.

  aitester author --story "..." --base-url http://localhost:5173
  aitester run path/to/suite.robot
  aitester doctor
  aitester version
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import typer

app = typer.Typer(
    name="aitester",
    help="Agent-loop authoring of Robot Framework BDD test suites.",
    no_args_is_help=True,
)


@app.command()
def version() -> None:
    """Print the package version."""
    from aitester_bdd import __version__
    typer.echo(__version__)


@app.command()
def author(
    story: str = typer.Option(..., "--story", "-s", help="Plain-English intention to verify."),
    base_url: str = typer.Option(..., "--base-url", "-u", help="URL of the live target app."),
    out: str = typer.Option("suite.robot", "--out", "-o", help="Output path for the .robot suite."),
    triage_dir: str = typer.Option("triage", "--triage-dir", help="Directory for bug reports when authoring is blocked."),
    source_root: str | None = typer.Option(None, "--source-root", help="Optional source root for white-box read_file access."),
    max_iters: int = typer.Option(40, "--max-iters", help="Max agent iterations before giving up."),
) -> None:
    """Run the authoring agent loop.

    The agent drives the live target via agent-browser (per SKILL.md),
    then either writes the .robot suite or files a bug report.
    """
    from aitester_bdd.authoring.agent_loop import author_with_agent

    suite_path = Path(out)
    triage_path = Path(triage_dir)
    src_root = Path(source_root) if source_root else None

    typer.echo(f"[author] story={story!r}")
    typer.echo(f"[author] base_url={base_url}")
    typer.echo(f"[author] suite_path={suite_path}, triage_dir={triage_path}")
    if src_root:
        typer.echo(f"[author] source_root={src_root}")

    result = author_with_agent(
        story=story,
        base_url=base_url,
        suite_path=suite_path,
        bug_report_dir=triage_path,
        source_root=src_root,
        max_iters=max_iters,
    )

    if result.suite_path:
        typer.echo(f"[author] ✓ wrote suite: {result.suite_path}")
    elif result.bug_report_path:
        typer.echo(f"[author] ! filed bug report: {result.bug_report_path}")
    else:
        typer.echo(f"[author] ✗ agent did not reach a terminal call in {result.iterations} steps")
        typer.echo(f"  final message: {result.final_message[:300]}")
        raise typer.Exit(code=1)


@app.command(name="init-browser")
def init_browser() -> None:
    """Run `rfbrowser init` — downloads Playwright + Chromium/Firefox/WebKit
    into robotframework-browser's wrapper directory.

    Required once per install before `aitester run <suite>` will work.
    Authoring (Explore phase) does NOT need this — it uses the separate
    `agent-browser` CLI.
    """
    typer.echo("Running `rfbrowser init` — this may take a few minutes (downloads ~300MB of browsers)...")
    rc = subprocess.run([sys.executable, "-m", "Browser.entry", "init"]).returncode
    if rc == 0:
        typer.echo("✓ rfbrowser init complete. `aitester doctor` should now show initialized.")
    else:
        typer.echo(f"✗ rfbrowser init failed with exit code {rc}")
    raise typer.Exit(code=rc)


@app.command()
def run(
    suite: str = typer.Argument(..., help="Path to a .robot suite"),
    base_url: str | None = typer.Option(None, "--base-url", help="Override BASE_URL variable"),
) -> None:
    """Run a .robot suite via Robot Framework (no LLM in the loop)."""
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
    chosen = os.environ.get("AITESTER_BROWSER", "playwright").lower()
    typer.echo(f"  ℹ AITESTER_BROWSER={chosen} (default: playwright)")

    # Playwright backend status
    try:
        import Browser  # type: ignore[import-not-found]
        ver = getattr(Browser, '__version__', '?')
        wrapper_dir = Path(Browser.__file__).parent / "wrapper"
        node_modules = wrapper_dir / "node_modules"
        if node_modules.is_dir():
            typer.echo(f"  ✓ playwright backend: rfbrowser {ver} (initialized)")
        else:
            tag = "⚠" if chosen == "playwright" else "ℹ"
            typer.echo(
                f"  {tag} playwright backend: rfbrowser {ver} installed but NOT initialized"
                f" — run `aitester init-browser` (only needed if AITESTER_BROWSER=playwright)"
            )
    except Exception as e:
        typer.echo(f"  ✗ playwright backend (rfbrowser): {e}")

    # Nodriver backend status
    try:
        import nodriver  # type: ignore[import-not-found]  # noqa: F401
        from aitester_bdd.engine.nodriver_backend import _find_browser_binary
        binary = _find_browser_binary()
        if binary:
            typer.echo(f"  ✓ nodriver backend: installed, Chrome/Edge at {binary}")
        else:
            typer.echo("  ⚠ nodriver backend: installed but no Chrome/Edge found on system")
    except ImportError:
        tag = "⚠" if chosen == "nodriver" else "ℹ"
        typer.echo(
            f"  {tag} nodriver backend: not installed"
            f" (install with `pip install aitester-bdd[stealth]` for bot-detected sites)"
        )
    try:
        import importlib.metadata
        for pkg in ("deepagents", "langgraph", "langchain", "langchain-openai", "litellm"):
            try:
                v = importlib.metadata.version(pkg)
                typer.echo(f"  ✓ {pkg} {v}")
            except importlib.metadata.PackageNotFoundError:
                typer.echo(f"  ✗ {pkg} not installed")
    except Exception as e:
        typer.echo(f"  ✗ {e}")
    model = os.environ.get("AITESTER_LLM_MODEL")
    base = os.environ.get("OPENAI_BASE_URL")
    if model:
        typer.echo(f"  ✓ AITESTER_LLM_MODEL={model}")
    else:
        typer.echo("  ~ AITESTER_LLM_MODEL not set (default: cc/claude-opus-4-7)")
    if base:
        typer.echo(f"  ✓ OPENAI_BASE_URL={base}")
    else:
        typer.echo("  ~ OPENAI_BASE_URL not set (default: http://localhost:20128/v1)")
    try:
        r = subprocess.run(["agent-browser", "--version"], capture_output=True, text=True, timeout=5)
        typer.echo(f"  ✓ agent-browser {r.stdout.strip() or r.stderr.strip()}")
    except FileNotFoundError:
        typer.echo("  ✗ agent-browser CLI not found (required for the agent loop)")


if __name__ == "__main__":
    app()
