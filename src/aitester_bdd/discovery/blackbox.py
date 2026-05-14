"""Black-box discovery: snapshot the live app, propose draft .robot skeletons.

Uses agent-browser (subprocess) to take accessibility snapshots. Returns
both the raw snapshot (for downstream authoring) and a draft suite skeleton
with TODO placeholders for selectors that haven't been grounded yet.

The output is intentionally minimal — discovery proposes a structure;
authoring grounds the selectors.
"""
from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass, field

log = logging.getLogger("aitester_bdd.discovery.blackbox")


@dataclass
class DiscoveryResult:
    base_url: str
    snapshot: str = ""
    found_routes: list[str] = field(default_factory=list)
    found_buttons: list[dict[str, str]] = field(default_factory=list)
    found_inputs: list[dict[str, str]] = field(default_factory=list)
    has_login_form: bool = False
    has_topnav: bool = False
    draft_robot: str = ""


def _agent_browser(*args: str, timeout: float = 30.0) -> str:
    """Run agent-browser; return stdout (best effort)."""
    try:
        r = subprocess.run(
            ["agent-browser", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if r.returncode != 0:
            log.warning("agent-browser failed: %s", r.stderr[:300])
            return ""
        return r.stdout
    except FileNotFoundError:
        log.warning("agent-browser CLI not on PATH")
        return ""
    except Exception as exc:
        log.warning("agent-browser error: %s", exc)
        return ""


def _take_snapshot(base_url: str) -> str:
    """Open the base URL and emit an accessibility snapshot."""
    _agent_browser("open", base_url)
    return _agent_browser("snapshot", "-c", "-d", "3")


def _parse_snapshot(snapshot: str) -> DiscoveryResult:
    """Extract clickable buttons, form inputs, navigation links from snapshot text."""
    out = DiscoveryResult(base_url="", snapshot=snapshot)

    # Buttons: `button "Label" [ref=eXX]` or `button ... data-testid='...'`
    for m in re.finditer(r'button\s+"([^"]+)"\s*\[ref=(\w+)\]', snapshot):
        out.found_buttons.append({"label": m.group(1), "ref": m.group(2)})

    # Inputs: `textbox "placeholder" [ref=eXX]`
    for m in re.finditer(r'(textbox|combobox|searchbox)\s+"([^"]+)"\s*\[ref=(\w+)\]', snapshot):
        out.found_inputs.append({
            "kind": m.group(1),
            "label": m.group(2),
            "ref": m.group(3),
        })

    # Routes from `/url: "<path>"` patterns
    for m in re.finditer(r'/url:\s*"([^"]+)"', snapshot):
        path = m.group(1)
        if path not in out.found_routes:
            out.found_routes.append(path)

    # Heuristics
    snap_lower = snapshot.lower()
    out.has_login_form = "password" in snap_lower and ("username" in snap_lower or "email" in snap_lower)
    out.has_topnav = "navigation" in snap_lower or "navbar" in snap_lower

    return out


def _draft_robot(story: str, base_url: str, found: DiscoveryResult) -> str:
    """Emit a starter .robot suite. TODOs mark places authoring must ground."""
    lines = [
        "*** Settings ***",
        "Documentation     " + f"Draft generated from story: {story[:80]}",
        "Library           Browser",
        "Library           aitester_bdd.AITester",
        'Suite Setup       Given I start verification "${DEPLOYMENT}"',
        "Suite Teardown    Then I finalize verification",
        "",
        "*** Variables ***",
        "${DEPLOYMENT}       drafted",
        f"${{BASE_URL}}         {base_url}",
    ]
    if found.has_login_form:
        lines.extend([
            "${ADMIN_USER}       admin",
            "${ADMIN_PASSWORD}   admin",
        ])
    lines.extend([
        "",
        "*** Test Cases ***",
        "Discovered Flow",
        '    [Setup]    Given I start scenario "draft" at "${BASE_URL}"',
    ])
    if found.has_login_form:
        lines.extend([
            '    I define rule "login"',
            '        When I open "${BASE_URL}/#/login"  # TODO: confirm path',
            '        When I type "${ADMIN_USER}" into locator "TODO: ground username selector"',
            '        When I type secret "${ADMIN_PASSWORD}" into locator "TODO: ground password selector"',
            '        When I click locator "TODO: ground submit selector"',
            '        And url contains "/"',
        ])
    lines.extend([
        '    I define rule "verify_landing"',
    ])
    if found.has_login_form:
        lines.append('        And I declare parents "login"')
    lines.extend([
        '        When I open "${BASE_URL}/"',
        '        Then selector "h1, [role=heading]" exists',
    ])
    if found.found_routes:
        lines.append("")
        lines.append("# Routes discovered (review and pick which to test):")
        for p in found.found_routes[:20]:
            lines.append(f"#   {p}")
    return "\n".join(lines) + "\n"


def discover_blackbox(base_url: str, story: str) -> DiscoveryResult:
    """Open the app at base_url, snapshot it, propose a draft .robot skeleton.

    Returns a DiscoveryResult with the raw snapshot and a draft suite. The
    snapshot is intended to be fed to authoring for selector grounding.
    """
    snapshot = _take_snapshot(base_url)
    result = _parse_snapshot(snapshot)
    result.base_url = base_url
    result.draft_robot = _draft_robot(story, base_url, result)
    return result
