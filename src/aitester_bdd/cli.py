"""aitester CLI — entrypoint for discover / author / refine / run.

    aitester author --story "..." --base-url http://localhost:5173
    aitester discover --base-url http://localhost:5173 --source-root /path/to/app
    aitester run path/to/suite.robot
"""
from __future__ import annotations

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
def author(
    story: str = typer.Option(..., "--story", "-s", help="Plain-English test intention"),
    base_url: str = typer.Option(..., "--base-url", "-u", help="URL of the target app"),
    out: str = typer.Option("suite.robot", "--out", "-o", help="Output .robot path"),
    source_root: str | None = typer.Option(None, "--source-root", help="App source for white-box discovery"),
) -> None:
    """Author a .robot test suite from a story and a live app."""
    typer.echo(f"[author] story={story!r} base_url={base_url}")
    typer.echo("Not yet implemented — Phase 1 placeholder.")
    raise typer.Exit(code=1)


@app.command()
def discover(
    base_url: str = typer.Option(..., "--base-url", "-u"),
    source_root: str | None = typer.Option(None, "--source-root"),
    out_dir: str = typer.Option("./discovered", "--out", "-o"),
) -> None:
    """Discover testable journeys (mode A black-box, mode B with source-root)."""
    mode = "white-box" if source_root else "black-box"
    typer.echo(f"[discover/{mode}] base_url={base_url} source_root={source_root}")
    typer.echo("Not yet implemented — Phase 1 placeholder.")
    raise typer.Exit(code=1)


@app.command()
def run(
    suite: str = typer.Argument(..., help="Path to .robot suite"),
) -> None:
    """Run a .robot suite via the robot framework runner."""
    import subprocess
    import sys
    typer.echo(f"[run] suite={suite}")
    rc = subprocess.run([sys.executable, "-m", "robot", suite]).returncode
    raise typer.Exit(code=rc)


if __name__ == "__main__":
    app()
