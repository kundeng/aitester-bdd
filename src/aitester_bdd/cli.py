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
    engine: str = typer.Option(
        "agent-browser", "--engine", "-e",
        help="Runtime engine to declare in the authored suite (agent-browser | playwright | nodriver). "
             "All three accept CSS selectors so the suite is portable; `aitester run` reads ${ENGINE} "
             "from the suite and sets AITESTER_BROWSER accordingly.",
    ),
    max_iters: int = typer.Option(40, "--max-iters", help="Max agent iterations before giving up."),
    debug: bool = typer.Option(
        False, "--debug", "-d",
        help="Stream each agent step (tool calls + results) to stderr so you can watch the explorer work.",
    ),
) -> None:
    """Run the authoring agent loop.

    Exploration uses Playwright sync API in a worker thread, producing
    CSS-grounded snapshots. The agent grounds every selector in real DOM
    attributes — no inference. Authored suite declares ${ENGINE} so
    `aitester run` picks the matching runtime backend.
    """
    from aitester_bdd.authoring.agent_loop import author_with_agent

    if engine not in ("agent-browser", "playwright", "nodriver"):
        typer.echo(f"[author] ✗ unknown engine {engine!r}; expected agent-browser | playwright | nodriver")
        raise typer.Exit(code=2)

    suite_path = Path(out)
    triage_path = Path(triage_dir)
    src_root = Path(source_root) if source_root else None

    typer.echo(f"[author] story={story!r}")
    typer.echo(f"[author] base_url={base_url}")
    typer.echo(f"[author] engine={engine} (declared in suite as ${{ENGINE}})")
    typer.echo(f"[author] suite_path={suite_path}, triage_dir={triage_path}")
    if src_root:
        typer.echo(f"[author] source_root={src_root}")

    result = author_with_agent(
        story=story,
        base_url=base_url,
        suite_path=suite_path,
        bug_report_dir=triage_path,
        source_root=src_root,
        engine=engine,
        max_iters=max_iters,
        debug=debug,
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
    """Install browsers needed for authoring + runtime.

    Two install steps, both required for the default configuration:

      1. `python -m playwright install chromium` — Playwright Python's
         own browser binary, used by the AUTHORING explorer (the agent
         drives this Playwright browser to take CSS-grounded snapshots).

      2. `rfbrowser init` — robotframework-browser's bundled Playwright
         + browsers, used by the `playwright` runtime backend.

    Total download is large (~600MB combined). You can skip step 2 if
    you only use the `agent-browser` or `nodriver` runtime backends.
    """
    typer.echo("[1/2] Installing Playwright Python browsers (used by the authoring explorer)...")
    rc1 = subprocess.run(
        [sys.executable, "-m", "playwright", "install", "chromium"]
    ).returncode
    if rc1 == 0:
        typer.echo("    ✓ Playwright Python chromium installed.")
    else:
        typer.echo(f"    ✗ Playwright Python install failed (exit {rc1}).")

    typer.echo("[2/2] Running `rfbrowser init` (used by the playwright runtime backend)...")
    rc2 = subprocess.run(
        [sys.executable, "-m", "Browser.entry", "init"]
    ).returncode
    if rc2 == 0:
        typer.echo("    ✓ rfbrowser init complete.")
    else:
        typer.echo(f"    ✗ rfbrowser init failed (exit {rc2}).")

    raise typer.Exit(code=(rc1 or rc2))


def _read_engine_from_suite(suite_path: str) -> str | None:
    """Parse `${ENGINE}    <name>` out of the .robot Variables section.

    Returns the engine name or None if not declared. Authored suites set
    ${ENGINE} to declare which runtime backend they want; this allows
    `aitester run` to set AITESTER_BROWSER automatically so the user
    doesn't have to remember.
    """
    import re

    try:
        text = Path(suite_path).read_text(encoding="utf-8")
    except Exception:
        return None
    in_vars = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("***"):
            in_vars = "Variables" in stripped
            continue
        if not in_vars:
            continue
        m = re.match(r"\$\{ENGINE\}\s+(\S+)", line)
        if m:
            return m.group(1).strip()
    return None


@app.command()
def run(
    suite: str = typer.Argument(..., help="Path to a .robot suite"),
    base_url: str | None = typer.Option(None, "--base-url", help="Override BASE_URL variable"),
    engine: str | None = typer.Option(
        None, "--engine", "-e",
        help="Override the suite's declared ${ENGINE}. Default: read from the suite.",
    ),
) -> None:
    """Run a .robot suite via Robot Framework (no LLM in the loop).

    Reads the suite's `${ENGINE}` declaration and sets AITESTER_BROWSER
    so the walker picks the matching runtime backend. CLI --engine
    overrides the suite. Environment AITESTER_BROWSER overrides both.
    """
    declared = _read_engine_from_suite(suite)
    chosen = engine or declared
    if chosen and not os.environ.get("AITESTER_BROWSER"):
        if chosen not in ("agent-browser", "playwright", "nodriver"):
            typer.echo(f"[run] ⚠ unknown engine {chosen!r} in suite; ignoring")
        else:
            os.environ["AITESTER_BROWSER"] = chosen
            src = "--engine" if engine else "suite ${ENGINE}"
            typer.echo(f"[run] AITESTER_BROWSER={chosen} (from {src})")
    elif os.environ.get("AITESTER_BROWSER"):
        typer.echo(f"[run] AITESTER_BROWSER={os.environ['AITESTER_BROWSER']} (from env)")
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
    # Playwright explorer — used during authoring.
    try:
        import playwright  # noqa: F401
        try:
            import importlib.metadata
            v = importlib.metadata.version("playwright")
        except Exception:
            v = "?"
        # Actually try launching chromium — that's the real check.
        # The cache directory location varies (PLAYWRIGHT_BROWSERS_PATH,
        # rfbrowser's own playwright install, system Chrome, etc.), so
        # probing paths is fragile.
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                browser.close()
            typer.echo(f"  ✓ playwright explorer: {v} (chromium launchable)")
        except Exception as exc:
            typer.echo(
                f"  ⚠ playwright explorer: {v} installed but chromium not launchable "
                f"({type(exc).__name__}) — run `aitester init-browser`"
            )
    except ImportError as e:
        typer.echo(f"  ✗ playwright explorer: {e}")

    chosen = os.environ.get("AITESTER_BROWSER", "agent-browser").lower()
    typer.echo(f"  ℹ AITESTER_BROWSER={chosen} (default: agent-browser)")

    # agent-browser RUNTIME backend.
    try:
        r = subprocess.run(["agent-browser", "--version"], capture_output=True, text=True, timeout=5)
        ver = r.stdout.strip() or r.stderr.strip()
        typer.echo(f"  ✓ agent-browser runtime backend: {ver} (zero install)")
    except FileNotFoundError:
        tag = "⚠" if chosen == "agent-browser" else "ℹ"
        typer.echo(
            f"  {tag} agent-browser runtime backend: CLI not found "
            f"(install: `npm i -g agent-browser`)"
        )

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


if __name__ == "__main__":
    app()
