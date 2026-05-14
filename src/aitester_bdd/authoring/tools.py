"""LangChain tools for the authoring agent — Playwright explorer.

The agent drives a real Playwright browser to take CSS-grounded snapshots
of the live target before writing the `.robot` suite. Selectors come out
of real DOM attributes (`data-testid`, `placeholder`, `aria-label`,
`id`, `name`, `class`, `role`), NOT inferred from common HTML
conventions.

Earlier iterations of this file used the `agent-browser` CLI to explore.
That returns an accessibility tree with ephemeral `@e1`-style refs and
no clean ref→CSS conversion path — the agent had to infer CSS selectors
from common HTML knowledge, which produced silent failures
(e.g. `input[autocomplete=username]` against a form that didn't use
autocomplete). Playwright's sync API surfaces real attributes natively.

Threading note: Playwright sync API is incompatible with `asyncio` loops.
DeepAgents/LangGraph runs the agent in an asyncio loop, so all
Playwright calls are bridged onto a dedicated daemon worker thread.
"""
from __future__ import annotations

import json
import logging
import queue
import threading
from pathlib import Path
from typing import Any, Optional

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

log = logging.getLogger("aitester_bdd.authoring.tools")


# ─── Tool argument schemas ────────────────────────────────────────────


class OpenParams(BaseModel):
    url: str = Field(description="Absolute URL to navigate to.")


class CssParams(BaseModel):
    css: str = Field(
        description="CSS selector (Playwright syntax — supports >>, text=, nth=).",
    )


class TypeParams(BaseModel):
    css: str = Field(description="CSS selector for the input element.")
    text: str = Field(description="Text to fill into the element (clears first).")


class EvalParams(BaseModel):
    js: str = Field(description="JavaScript expression to evaluate in the page context.")


class ScreenshotParams(BaseModel):
    path: str = Field(
        default="/tmp/aitester-bdd-shot.png",
        description="Absolute path to save the PNG screenshot to.",
    )


class GetHtmlParams(BaseModel):
    css: str = Field(
        description=(
            "CSS selector. Returns outerHTML of the first matching element "
            "— use to see every attribute the element actually has when "
            "browser_snapshot's per-element summary isn't enough."
        )
    )


class GetAttrParams(BaseModel):
    css: str = Field(description="CSS selector.")
    name: str = Field(description="Attribute name (e.g. 'data-testid', 'href').")


class ValidateSelectorParams(BaseModel):
    css: str = Field(
        description=(
            "CSS selector to validate. Can include pipe-fallback "
            "(`a | b | c`) — each candidate is checked independently."
        ),
    )
    scope: str = Field(
        default="",
        description=(
            "Optional parent scope. If provided, each candidate is "
            "tested under `scope >> candidate`. Use this to disambiguate "
            "selectors that aren't unique page-wide but are unique "
            "within a card / panel / row."
        ),
    )
    expected: str = Field(
        default="unique",
        description=(
            "What count you expect: 'unique' (count == 1, the default — "
            "for click/type/observe targets) OR 'many' (count >= 1 — "
            "for table rows, list items, expansion targets) OR 'absent' "
            "(count == 0 — for negation/disappearance asserts)."
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


# ─── Playwright worker thread ─────────────────────────────────────────


# JS executed inside the page during snapshot. Returns a structured
# view: per interactive element, the actual DOM attributes the agent
# needs to construct CSS selectors. The agent reads this output and
# picks a selector from real attributes — never inferred.
_SNAPSHOT_JS = """
() => {
  const ATTRS_OF_INTEREST = [
    'data-testid', 'placeholder', 'aria-label', 'aria-labelledby',
    'name', 'id', 'type', 'role', 'href', 'value',
    'autocomplete', 'aria-describedby', 'title', 'alt'
  ];
  const SELECTORS = [
    'input', 'textarea', 'select', 'button',
    'a[href]',
    '[role="button"]', '[role="link"]', '[role="textbox"]',
    '[role="checkbox"]', '[role="radio"]', '[role="combobox"]',
    '[role="tab"]', '[role="menuitem"]', '[role="option"]',
    '[role="dialog"]', '[role="alert"]',
    '[tabindex]:not([tabindex="-1"])',
    'h1', 'h2', 'h3',
    '[data-testid]'
  ];
  const seen = new Set();
  const out = [];
  for (const sel of SELECTORS) {
    for (const el of document.querySelectorAll(sel)) {
      if (seen.has(el)) continue;
      seen.add(el);
      // Skip hidden elements
      const r = el.getBoundingClientRect();
      if (r.width === 0 && r.height === 0 && !el.offsetParent) continue;
      const cs = window.getComputedStyle(el);
      if (cs.visibility === 'hidden' || cs.display === 'none') continue;
      const attrs = {};
      for (const a of ATTRS_OF_INTEREST) {
        const v = el.getAttribute(a);
        if (v !== null && v !== '') attrs[a] = v;
      }
      const cls = (el.className && typeof el.className === 'string') ? el.className.trim() : '';
      if (cls) attrs['class'] = cls;
      const text = (el.innerText || el.value || '').toString().trim().slice(0, 120);
      out.push({ tag: el.tagName.toLowerCase(), text, attrs });
    }
  }
  return { url: window.location.href, title: document.title, elements: out };
}
"""


def _format_snapshot(data: dict) -> str:
    """Render the JS-evaluated snapshot into a CSS-rich agent-readable view."""
    lines = [
        f"URL: {data.get('url', '?')}",
        f"Title: {data.get('title', '')!r}",
        f"Interactive elements ({len(data.get('elements', []))}):",
    ]
    for el in data.get("elements", []):
        tag = el.get("tag", "?")
        attrs = el.get("attrs", {}) or {}
        text = el.get("text", "") or ""
        attr_parts = []
        # Surface high-signal attrs first
        for key in (
            "data-testid", "id", "name", "type", "role", "aria-label",
            "placeholder", "href", "autocomplete", "title", "value",
        ):
            if key in attrs:
                v = attrs[key]
                short = v if len(v) <= 80 else v[:77] + "..."
                attr_parts.append(f'{key}={json.dumps(short)}')
        # Then class (truncated)
        if "class" in attrs:
            cls = attrs["class"]
            short = cls if len(cls) <= 60 else cls[:57] + "..."
            attr_parts.append(f'class={json.dumps(short)}')
        text_part = ""
        if text:
            t = text if len(text) <= 80 else text[:77] + "..."
            text_part = f"  text={json.dumps(t)}"
        lines.append(f"  <{tag}>  {' '.join(attr_parts)}{text_part}")
    return "\n".join(lines)


class _PlaywrightWorker:
    """Single-threaded Playwright session.

    Playwright sync API can't share a thread with an asyncio event loop.
    DeepAgents runs the agent under asyncio, so we ferry every tool call
    onto this worker thread and wait for the result.
    """

    def __init__(self, *, headless: bool = True) -> None:
        self._headless = headless
        self._inq: queue.Queue = queue.Queue()
        self._ready = threading.Event()
        self._init_error: Optional[BaseException] = None
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            self._init_error = exc
            self._ready.set()
            return
        try:
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(headless=self._headless)
            self._context = self._browser.new_context()
            self._page = self._context.new_page()
        except Exception as exc:
            self._init_error = exc
            self._ready.set()
            return
        self._ready.set()

        while True:
            fn, args, kwargs, result_q = self._inq.get()
            if fn is None:
                try:
                    self._context.close()
                except Exception:
                    pass
                try:
                    self._browser.close()
                except Exception:
                    pass
                try:
                    self._pw.stop()
                except Exception:
                    pass
                return
            try:
                value = fn(self._page, *args, **kwargs)
                result_q.put(("ok", value))
            except Exception as exc:
                result_q.put(("err", exc))

    def call(self, fn, *args, **kwargs):
        self._ready.wait(timeout=60)
        if self._init_error is not None:
            raise RuntimeError(
                f"Playwright explorer failed to start: "
                f"{type(self._init_error).__name__}: {self._init_error}. "
                f"Run `aitester init-browser` to install Playwright browsers."
            ) from self._init_error
        result_q: queue.Queue = queue.Queue()
        self._inq.put((fn, args, kwargs, result_q))
        kind, value = result_q.get()
        if kind == "err":
            raise value
        return value

    def shutdown(self) -> None:
        self._inq.put((None, None, None, None))
        self._thread.join(timeout=10)


_worker: Optional[_PlaywrightWorker] = None


def _w() -> _PlaywrightWorker:
    global _worker
    if _worker is None:
        _worker = _PlaywrightWorker()
    return _worker


def _shutdown_worker() -> None:
    global _worker
    if _worker is not None:
        try:
            _worker.shutdown()
        except Exception:
            pass
        _worker = None


# ─── Tool implementations ─────────────────────────────────────────────


def browser_open(url: str) -> str:
    """Navigate to a URL."""
    def _do(page, u):
        page.goto(u, wait_until="domcontentloaded")
        return f"OK: navigated to {page.url}"
    return _w().call(_do, url)


def browser_snapshot() -> str:
    """Take a CSS-grounded structured snapshot of the page.

    Returns URL + title + a list of interactive/landmark elements with
    their REAL attributes (data-testid, placeholder, aria-label, id,
    name, type, role, href, class). Use this as your source of truth
    for selectors. Never invent attributes that aren't here.
    """
    def _do(page):
        try:
            page.wait_for_load_state("domcontentloaded", timeout=10_000)
        except Exception:
            pass
        data = page.evaluate(_SNAPSHOT_JS)
        return _format_snapshot(data)
    return _w().call(_do)


def browser_click(css: str) -> str:
    """Click the first element matching `css`."""
    def _do(page, c):
        page.locator(c).first.click(timeout=10_000)
        return f"OK: clicked {c}"
    return _w().call(_do, css)


def browser_type(css: str, text: str) -> str:
    """Fill an input element with text (clears first)."""
    def _do(page, c, t):
        page.locator(c).first.fill(t, timeout=10_000)
        return f"OK: typed {len(t)} chars into {c}"
    return _w().call(_do, css, text)


def browser_get_text(css: str) -> str:
    """Get the inner text of the first element matching `css`."""
    def _do(page, c):
        try:
            return page.locator(c).first.inner_text(timeout=5_000) or ""
        except Exception as exc:
            return f"ERROR: {exc}"
    return _w().call(_do, css)


def browser_get_count(css: str) -> str:
    """Count how many elements match `css`. Returns the integer as text."""
    def _do(page, c):
        return str(page.locator(c).count())
    return _w().call(_do, css)


def browser_get_html(css: str) -> str:
    """Return the outerHTML of the first element matching `css`.

    Use when browser_snapshot's summary doesn't surface an attribute
    you need (e.g. nested children, computed value, exact class list).
    """
    def _do(page, c):
        try:
            return page.locator(c).first.evaluate(
                "el => el.outerHTML", timeout=5_000,
            )
        except Exception as exc:
            return f"ERROR: {exc}"
    return _w().call(_do, css)


def browser_get_attr(css: str, name: str) -> str:
    """Get a single attribute value from the first matching element."""
    def _do(page, c, n):
        try:
            v = page.locator(c).first.get_attribute(n, timeout=5_000)
            return "" if v is None else str(v)
        except Exception as exc:
            return f"ERROR: {exc}"
    return _w().call(_do, css, name)


# JS executed in the page to introspect a candidate match.
# Returns count + (for the first match) a "robustness profile":
#   stable_attrs   — attributes a robust selector should key on
#                    (data-testid, aria-label, name, role, id, href)
#   classes        — class list (so we can warn about brittle utility
#                    soup like "rounded-lg px-3 py-2 text-sm")
#   tag, text      — basic identity hints
# The agent uses this to decide whether the selector it picked is
# (a) unique (count == 1), (b) robust (keys on a stable attr or
# semantic role rather than a long compound-class), and (c) what to
# fall back to / scope against if it isn't.
_VALIDATE_JS = """
(css) => {
  const els = Array.from(document.querySelectorAll(css));
  const count = els.length;
  if (count === 0) return {count: 0};
  const e = els[0];
  const STABLE_ATTRS = [
    'data-testid', 'aria-label', 'aria-labelledby',
    'name', 'id', 'role', 'href', 'placeholder', 'title', 'alt',
  ];
  const stable = {};
  for (const a of STABLE_ATTRS) {
    const v = e.getAttribute(a);
    if (v) stable[a] = v;
  }
  const cls = (e.getAttribute('class') || '').trim().split(/\\s+/).filter(Boolean);
  const text = (e.innerText || '').trim().slice(0, 80);
  return {
    count,
    tag: e.tagName.toLowerCase(),
    stable_attrs: stable,
    classes: cls,
    text,
  };
}
"""


# Tailwind utility class prefixes — selectors built ONLY out of these
# are brittle (rebuild on theme tweaks, variant additions, JIT purge).
# A selector with one or two non-utility classes is fine; flag when
# every class in the chain is a utility.
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


def _looks_brittle(classes: list[str], css: str) -> tuple[bool, str]:
    """Quick brittleness check for a selector based on the matched
    element's classes. Returns (is_brittle, why)."""
    # If selector keys on a stable attribute (data-testid, aria-label,
    # id, name, role, href), we don't care about brittle class soup.
    for marker in ("data-testid=", "aria-label=", "[id=", "#",
                   "[name=", "role=", "[href"):
        if marker in css:
            return False, ""
    # If selector compounds 3+ classes AND every class is a utility,
    # flag it. e.g. ".px-3.py-2.text-sm.rounded-lg.bg-blue-500"
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

    Returns a JSON report:
      {
        "candidate": "<css under scope>",
        "count": N,
        "expected": "unique|many|absent",
        "ok": bool,
        "warning": "..." or null,
        "stable_attrs": {...},      # data-testid, aria-label, etc.
        "classes": [...],
        "text": "...",
        "suggestion": "..." or null,
      }

    For pipe-fallback selectors (`a | b | c`), each candidate is
    validated independently and the report is a list per candidate.
    """
    def _do(page, raw: str, sc: str, exp: str):
        candidates = [s.strip() for s in raw.split("|")] if "|" in raw else [raw]
        reports = []
        for cand in candidates:
            full = (
                cand if not sc
                else (sc if cand.strip() == "." else f"{sc} >> {cand}")
            )
            try:
                info = page.evaluate(_VALIDATE_JS, full)
            except Exception as exc:
                reports.append({
                    "candidate": full, "error": str(exc), "ok": False,
                })
                continue
            count = info.get("count", 0)
            if exp == "unique":
                ok = (count == 1)
            elif exp == "many":
                ok = (count >= 1)
            elif exp == "absent":
                ok = (count == 0)
            else:
                ok = False
            report: dict = {
                "candidate": full, "count": count,
                "expected": exp, "ok": ok,
            }
            if count > 0:
                report["tag"] = info.get("tag")
                report["stable_attrs"] = info.get("stable_attrs", {})
                report["classes"] = info.get("classes", [])
                report["text"] = info.get("text", "")
                brittle, why = _looks_brittle(
                    info.get("classes", []), full,
                )
                if brittle:
                    report["warning"] = why
                if exp == "unique" and count > 1:
                    stable = info.get("stable_attrs", {})
                    if stable:
                        # Suggest the most specific stable attr first.
                        for k in ("data-testid", "aria-label", "id",
                                  "name", "role", "href"):
                            if k in stable:
                                report["suggestion"] = (
                                    f'tighten to [{k}="{stable[k]}"]'
                                    + (f" under scope {sc!r}" if sc else "")
                                )
                                break
                    else:
                        report["suggestion"] = (
                            "no stable attrs on the matched element — "
                            "add a parent scope ('And I scope children to') "
                            "or pick a different starting element."
                        )
            else:
                if exp != "absent":
                    report["suggestion"] = (
                        "0 matches — re-check via browser_snapshot, "
                        "the element may not be rendered yet (await), "
                        "or the selector spelling is wrong."
                    )
            reports.append(report)
        if len(reports) == 1:
            return json.dumps(reports[0], indent=2)
        return json.dumps({"candidates": reports}, indent=2)
    return _w().call(_do, css, scope, expected)


def browser_eval(js: str) -> str:
    """Run a JS expression in the page context. Returns the result as text."""
    def _do(page, expr):
        try:
            v = page.evaluate(expr)
            return json.dumps(v, default=str) if not isinstance(v, str) else v
        except Exception as exc:
            return f"ERROR: {exc}"
    return _w().call(_do, js)


def browser_screenshot(path: str = "/tmp/aitester-bdd-shot.png") -> str:
    """Save a PNG screenshot of the current viewport."""
    def _do(page, p):
        page.screenshot(path=p)
        return f"OK: wrote {p}"
    return _w().call(_do, path)


def browser_close() -> str:
    """Tear down the browser session."""
    _shutdown_worker()
    return "OK: browser session closed"


# ─── File tools ───────────────────────────────────────────────────────


_SOURCE_ROOT: Optional[Path] = None


def _read_file(path: str) -> str:
    """Read a source file under the configured source_root.

    Used for white-box authoring: the agent can grep / read route
    definitions, component files, etc. on demand.
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
    authoring is complete. The agent loop stops after this call."""
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"OK: wrote {len(content)} bytes to {p}\nAUTHORING_DONE: suite={p}"
    except Exception as exc:
        return f"ERROR: {type(exc).__name__}: {exc}"


def _report_bug(path: str, content: str) -> str:
    """TERMINAL TOOL. Write a bug report when the system is broken in a way
    that prevents authoring a meaningful test. The agent loop stops after
    this call."""
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

    Playwright browser tools are always included. The read_file tool is
    only included when source_root is provided. Terminal tools
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
                "Take a CSS-grounded structured snapshot of the current page. "
                "Returns URL + title + a list of interactive elements with their "
                "REAL attributes (data-testid, placeholder, aria-label, id, name, "
                "type, role, href, class). This is your source of truth for "
                "selectors — never invent attributes that are not in the snapshot. "
                "Call after every navigation or action that changes the page."
            ),
        ),
        StructuredTool.from_function(
            func=browser_click, name="browser_click", args_schema=CssParams,
            description="Click the first element matching the CSS selector.",
        ),
        StructuredTool.from_function(
            func=browser_type, name="browser_type", args_schema=TypeParams,
            description="Fill an input element with text (clears first).",
        ),
        StructuredTool.from_function(
            func=browser_get_text, name="browser_get_text", args_schema=CssParams,
            description="Get the inner text of the first element matching `css`.",
        ),
        StructuredTool.from_function(
            func=browser_get_count, name="browser_get_count", args_schema=CssParams,
            description="Count elements matching `css`.",
        ),
        StructuredTool.from_function(
            func=browser_get_html, name="browser_get_html", args_schema=GetHtmlParams,
            description=(
                "Get outerHTML of the first matching element. Use when "
                "browser_snapshot's per-element attribute summary is not enough "
                "(nested children, computed values, full class list)."
            ),
        ),
        StructuredTool.from_function(
            func=browser_get_attr, name="browser_get_attr", args_schema=GetAttrParams,
            description="Get a single attribute value from the first matching element.",
        ),
        StructuredTool.from_function(
            func=browser_validate_selector, name="browser_validate_selector",
            args_schema=ValidateSelectorParams,
            description=(
                "REQUIRED BEFORE WRITING ANY SELECTOR INTO .robot. "
                "Validates the candidate against the live page: returns "
                "{count, ok (vs expected), stable_attrs, classes, text, "
                "warning, suggestion}. "
                "expected='unique' (default) for click/type/observe; "
                "'many' for table rows or expansion targets; 'absent' "
                "for disappearance asserts. "
                "If ok=false, READ THE SUGGESTION — tighten to a "
                "data-testid / aria-label / id, OR add scope. "
                "If warning is set, the selector compounds Tailwind "
                "utility classes — replace it with a semantic anchor. "
                "Validate every selector you write, including pipe-fallback "
                "alternatives (`a | b | c` — each is checked independently)."
            ),
        ),
        StructuredTool.from_function(
            func=browser_eval, name="browser_eval", args_schema=EvalParams,
            description="Run a JS expression in the page context. Returns the result as text.",
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
                "path. Call only when you have driven the live target, every "
                "selector traces to a snapshot you took, and you are ready to "
                "hand the suite off. The agent loop stops after this call."
            ),
        ),
        StructuredTool.from_function(
            func=_report_bug, name="report_bug",
            args_schema=ReportBugParams,
            description=(
                "TERMINAL TOOL. Write a bug report when the system is broken "
                "in a way that prevents authoring a meaningful test "
                "(unreachable URL, broken auth, missing feature, untestable "
                "terminal state). The agent loop stops after this call. Do NOT "
                "use for selector difficulty or async timing — those are "
                "authoring problems, not system bugs."
            ),
        ),
    ])

    return tools
