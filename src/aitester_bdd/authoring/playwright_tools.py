"""Playwright-backed LangChain tools for the explore agent.

When running inside Robot Framework (`aitester run`), the RF Browser
library is already active — `BuiltIn().get_library_instance("Browser")`
returns the live Playwright session the walker is using. These tools
wrap that same instance so the explore agent operates on the same page,
cookies, and DOM state as the pinned rules before and after it.

No subprocess, no session handoff, no agent-browser CLI. Just typed
Python function calls on the shared RF Browser instance.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

log = logging.getLogger("aitester_bdd.authoring.playwright_tools")

# ---------------------------------------------------------------------------
# Lazy singleton — shares the walker's RF Browser instance
# ---------------------------------------------------------------------------

_backend = None


def _get_backend():
    """Get the _PlaywrightBackend that shares the live RF Browser instance."""
    global _backend
    if _backend is None:
        from aitester_bdd.engine.browser import _PlaywrightBackend
        _backend = _PlaywrightBackend()
    return _backend


def reset_backend():
    """Reset for fresh test runs."""
    global _backend
    _backend = None


# ---------------------------------------------------------------------------
# Tool parameter models
# ---------------------------------------------------------------------------

class OpenParams(BaseModel):
    url: str = Field(description="URL to navigate to")


class SelectorParams(BaseModel):
    selector: str = Field(description="CSS selector")


class ClickParams(BaseModel):
    selector: str = Field(description="CSS selector or text selector to click")


class TypeParams(BaseModel):
    selector: str = Field(description="CSS selector of the input")
    text: str = Field(description="Text to type")


class FillParams(BaseModel):
    selector: str = Field(description="CSS selector of the input")
    value: str = Field(description="Value to fill (clears first)")


class EvalParams(BaseModel):
    expression: str = Field(description="JavaScript expression to evaluate in the page")


class PressParams(BaseModel):
    key: str = Field(description="Key to press (Enter, Tab, Escape, etc.)")


class WaitParams(BaseModel):
    selector: str = Field(description="CSS selector to wait for")
    timeout_ms: int = Field(default=10000, description="Timeout in milliseconds")


# ---------------------------------------------------------------------------
# Tool implementations — all delegate to the shared RF Browser
# ---------------------------------------------------------------------------

def _open(url: str) -> str:
    b = _get_backend()
    b.open(url)
    return json.dumps({"success": True, "url": url})


def _snapshot() -> str:
    """Compact page snapshot mimicking agent-browser's token-efficient format.

    Returns: URL, title, and a compact list of interactive elements with
    short descriptors. Each element is one line: `@eN tag "text" [attrs]`.
    """
    b = _get_backend()
    url = b.url()
    rfb = b._rf_browser()
    try:
        title = str(rfb.get_title() if hasattr(rfb, "get_title") else "")
    except Exception:
        title = ""
    try:
        js = """() => {
            const out = [];
            let i = 0;
            document.querySelectorAll('a, button, input, select, textarea, [role=button], [role=tab], [role=link], [role=menuitem], h1, h2, h3').forEach(el => {
                if (el.offsetParent === null) return;
                const tag = el.tagName.toLowerCase();
                const text = (el.textContent || '').trim().replace(/\\s+/g, ' ').slice(0, 60);
                const href = el.getAttribute('href') || '';
                const type = el.getAttribute('type') || '';
                const ph = el.getAttribute('placeholder') || '';
                const role = el.getAttribute('role') || '';
                let desc = '@e' + i + ' ' + tag;
                if (role) desc += '[' + role + ']';
                if (type) desc += '[type=' + type + ']';
                if (ph) desc += ' "' + ph + '"';
                else if (text) desc += ' "' + text + '"';
                if (href) desc += ' href=' + href;
                out.push(desc);
                i++;
            });
            return out;
        }"""
        elements = rfb.evaluate_javascript(None, js)
    except Exception:
        elements = []
    lines = [f"URL: {url}", f"Title: {title}"]
    for el in (elements or [])[:40]:
        lines.append(str(el))
    return "\n".join(lines)


def _click(selector: str) -> str:
    b = _get_backend()
    try:
        b.click(selector)
        return json.dumps({"success": True, "clicked": selector})
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


def _get_text(selector: str) -> str:
    b = _get_backend()
    try:
        text = b.get_text(selector)
        return json.dumps({"success": True, "text": text})
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


def _get_count(selector: str) -> str:
    b = _get_backend()
    try:
        count = b.get_count(selector)
        return json.dumps({"success": True, "count": count})
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


def _type_text(selector: str, text: str) -> str:
    b = _get_backend()
    try:
        rfb = b._rf_browser()
        rfb.type_text(selector, text)
        return json.dumps({"success": True, "typed": text})
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


def _fill(selector: str, value: str) -> str:
    b = _get_backend()
    try:
        rfb = b._rf_browser()
        rfb.fill_text(selector, value)
        return json.dumps({"success": True, "filled": value})
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


def _press_key(key: str) -> str:
    b = _get_backend()
    try:
        rfb = b._rf_browser()
        rfb.keyboard_key("press", key)
        return json.dumps({"success": True, "key": key})
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


def _evaluate(expression: str) -> str:
    b = _get_backend()
    try:
        rfb = b._rf_browser()
        result = rfb.evaluate_javascript(None, expression)
        return json.dumps({"success": True, "result": result}, default=str)
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


def _get_url() -> str:
    b = _get_backend()
    return json.dumps({"url": b.url()})


def _wait_for(selector: str, timeout_ms: int = 10000) -> str:
    b = _get_backend()
    ok = b.wait_for_elements_state(selector, "attached", timeout_ms=timeout_ms)
    return json.dumps({"success": ok, "selector": selector})


# ---------------------------------------------------------------------------
# Build tool list for the explore agent
# ---------------------------------------------------------------------------

def build_playwright_browser_tools() -> list[StructuredTool]:
    """LangChain tools that wrap the shared RF Browser/Playwright instance."""
    return [
        StructuredTool.from_function(
            func=_open, name="browser_open",
            args_schema=OpenParams,
            description="Navigate to a URL",
        ),
        StructuredTool.from_function(
            func=_snapshot, name="browser_snapshot",
            description="Get a compact snapshot of the current page: URL, title, and interactive elements (links, buttons, inputs). No parameters needed.",
        ),
        StructuredTool.from_function(
            func=_click, name="browser_click",
            args_schema=ClickParams,
            description="Click an element by CSS selector",
        ),
        StructuredTool.from_function(
            func=_get_text, name="browser_get_text",
            args_schema=SelectorParams,
            description="Get the text content of an element by CSS selector",
        ),
        StructuredTool.from_function(
            func=_get_count, name="browser_get_count",
            args_schema=SelectorParams,
            description="Count elements matching a CSS selector",
        ),
        StructuredTool.from_function(
            func=_type_text, name="browser_type",
            args_schema=TypeParams,
            description="Type text into an input (appends to existing value)",
        ),
        StructuredTool.from_function(
            func=_fill, name="browser_fill",
            args_schema=FillParams,
            description="Fill an input with a value (clears existing value first)",
        ),
        StructuredTool.from_function(
            func=_press_key, name="browser_press",
            args_schema=PressParams,
            description="Press a keyboard key (Enter, Tab, Escape, ArrowDown, etc.)",
        ),
        StructuredTool.from_function(
            func=_evaluate, name="browser_eval",
            args_schema=EvalParams,
            description="Evaluate a JavaScript expression in the page context. Use arrow functions: '() => document.title'",
        ),
        StructuredTool.from_function(
            func=_get_url, name="browser_url",
            description="Get the current page URL. No parameters needed.",
        ),
        StructuredTool.from_function(
            func=_wait_for, name="browser_wait",
            args_schema=WaitParams,
            description="Wait for an element to appear in the DOM (conditional wait, returns immediately if already present)",
        ),
    ]


# ---------------------------------------------------------------------------
# System prompt for Playwright-backed explore agent
# ---------------------------------------------------------------------------

PLAYWRIGHT_EXPLORE_PROMPT = """You are a QA tester exploring a live web application. You have browser tools that operate on the current page.

## Tools
- browser_open(url) — navigate to URL
- browser_snapshot() — compact view: URL + interactive elements (@e0, @e1, ...)
- browser_click(selector) — click an element
- browser_get_text(selector) — read element text
- browser_get_count(selector) — count matching elements
- browser_fill(selector, value) — clear + fill an input
- browser_type(selector, text) — append text to an input
- browser_press(key) — press a key (Enter, Tab, Escape)
- browser_eval(expression) — run JavaScript in the page
- browser_url() — get current URL
- browser_wait(selector) — wait for element to appear

## Efficiency rules
1. Start with browser_snapshot() to see the current page.
2. Chain multiple actions before taking another snapshot. Example: fill username, fill password, click submit — then snapshot to see the result. Don't snapshot between every action.
3. Use CSS selectors from the snapshot. The @eN refs are for reference — use the actual CSS selector (tag, role, href, placeholder) to click/fill.
4. After navigating to a new page, one snapshot tells you everything. Don't call get_text/get_count if the snapshot already shows the answer.
5. Verify observations by reading the snapshot, not by individual get_text calls.

## Completion
- When done with ALL steps, call journey_complete with detailed notes.
- If blocked (page broken, element missing, action fails), call journey_blocked.
- Do NOT stop early — complete every step in the story.
"""
