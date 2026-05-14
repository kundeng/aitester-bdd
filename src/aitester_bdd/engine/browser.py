"""Browser adapter — minimal surface the walker drives.

Two backends share the same surface:

  * `_PlaywrightBackend` (default) — wraps `robotframework-browser`
    (Playwright via RF). Needs `rfbrowser init` once.
  * `NodriverBackend` (opt-in) — raw CDP via Edge/Chrome. No Playwright
    fingerprint (defeats DataDome/Cloudflare-style detection), and no
    `rfbrowser init` step. Lives in `nodriver_backend.py`. Pulled in
    by setting `AITESTER_BROWSER=nodriver` and installing the
    `aitester-bdd[stealth]` extra.

Pick via env: `AITESTER_BROWSER=playwright` (default) or `nodriver`.

Backs onto `robotframework-browser` (Playwright via RF) when running inside
Robot Framework. Falls back to a NullBrowser stub when RF-Browser is
unimportable (unit tests).

Gotcha-fixes ported from the WISE RPA BDD engine (battle-tested across many
real sites; do not strip without measuring regressions):

  * `click_text` — Playwright `text="X"` (exact) → `text=X` (substring) →
    JS MouseEvent fallback. Plain "Playwright text= only" misses elements
    rendered as `div[role=button]` with whitespace-padded text content.
  * `evaluate_js` — JS that navigates destroys the page context, raising
    "Execution context was destroyed" / "Page navigated". Catch + classify
    as expected navigation, wait for the new page, return success.
  * `wait_for_elements_state` — delegates to RF-Browser's native waiter,
    which uses Playwright's strict mode and is faster + more reliable than
    a Python polling loop.
  * `set_stepper` — clicking a button that re-renders itself triggers
    Playwright "element is unstable" / "detached from DOM" errors. Use a
    JS-click (skips Playwright's stability check) and re-wait between
    repeated clicks.
  * `resolve_fallback_selector` — `"a | b | c"` pipe-fallback: walker picks
    the first candidate that resolves on the current page. Plain selectors
    pass through unchanged.

The walker also calls `_dismiss_interrupts` (in walk.py) before guards and
before every action; that lives in the walker because it needs the
verification's `interrupt_selectors` config, not the browser state.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

log = logging.getLogger("aitester_bdd.engine.browser")


# Navigation-related exception messages from Playwright/RF-Browser. These
# are expected (not real errors) when a click/JS triggers a page change.
_NAV_ERROR_HINTS = (
    "navigation", "context", "destroyed", "detached",
    "navigated", "execution context",
)


class _NullBrowser:
    """No-op browser used when robotframework-browser isn't available."""

    def __getattr__(self, name: str):
        def noop(*args: Any, **kw: Any):
            log.debug("NullBrowser noop: %s(%s, %s)", name, args, kw)
            return ""

        return noop


def BrowserAdapter():
    """Factory — returns the configured backend.

    `AITESTER_BROWSER` selects which:

      - `agent-browser` (default) → AgentBrowserBackend. Same CLI as
        the Explore phase, so author-time and run-time DOM views are
        identical. Zero install friction (the CLI ships its own browser).
        Slower per action (subprocess), but fine for most tests.

      - `playwright` → _PlaywrightBackend (rfbrowser/Playwright). In-process
        speed for action-heavy tests. Requires `aitester init-browser`.

      - `nodriver` → NodriverBackend (raw CDP via Chrome/Edge). For sites
        with bot-detection that targets Playwright, or to skip rfbrowser
        init when Chrome/Edge is already installed. Needs
        `pip install aitester-bdd[stealth]`.

    All three expose the same public surface; the walker is backend-agnostic.
    """
    import os

    choice = os.environ.get("AITESTER_BROWSER", "agent-browser").strip().lower()
    if choice == "nodriver":
        from aitester_bdd.engine.nodriver_backend import NodriverBackend
        return NodriverBackend()
    if choice == "playwright":
        return _PlaywrightBackend()
    # Default: agent-browser CLI.
    from aitester_bdd.engine.agent_browser_backend import AgentBrowserBackend
    return AgentBrowserBackend()


class _PlaywrightBackend:
    """robotframework-browser / Playwright backend.

    Wraps the RF-Browser library if available (the RF-native path), falls
    back to a NullBrowser stub for unit testing. All gotcha-fix logic lives
    in this class so the walker can stay focused on rule semantics.
    """

    def __init__(self) -> None:
        self._rfb: Any | None = None
        self._last_response_status: int | None = None
        self._last_response_body: str = ""

    def _rf_browser(self) -> Any:
        """Lazy-acquire RF-Browser instance.

        When running inside RF: returns the live `Browser` library
        instance from RF's keyword store (so all our calls operate on the
        same page the user's `.robot` file is driving).

        When RF context not available: instantiates a fresh Browser()
        (CLI / standalone runner path).

        When RF-Browser not importable: returns _NullBrowser (unit tests).
        """
        if self._rfb is None:
            try:
                # Path A: inside RF — grab the live instance.
                from robot.libraries.BuiltIn import BuiltIn
                try:
                    self._rfb = BuiltIn().get_library_instance("Browser")
                except Exception:
                    self._rfb = None
                # Path B: not in RF context — fresh instance.
                if self._rfb is None:
                    from Browser import Browser  # type: ignore[import-not-found]
                    self._rfb = Browser()
            except Exception as exc:
                log.warning(
                    "robotframework-browser not available (%s) — using NullBrowser",
                    exc,
                )
                self._rfb = _NullBrowser()
        return self._rfb

    # ------------------------------------------------------------------
    # Lifecycle — usually managed by RF Suite Setup; safe no-ops if RF
    # already opened the browser for us.
    # ------------------------------------------------------------------

    def new_session(self, *, headless: bool = True) -> None:
        b = self._rf_browser()
        try:
            b.new_browser(headless=headless)
            b.new_context()
            b.new_page()
        except Exception as exc:
            # Likely: RF already opened a browser for us. That's fine.
            log.debug("new_session no-op (%s)", exc)

    def close(self) -> None:
        try:
            self._rf_browser().close_browser("ALL")
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Navigation + page-readiness
    # ------------------------------------------------------------------

    def open(self, url: str) -> None:
        self._rf_browser().go_to(url)

    def reload(self) -> None:
        b = self._rf_browser()
        if hasattr(b, "reload"):
            b.reload()

    def go_back(self) -> None:
        b = self._rf_browser()
        if hasattr(b, "go_back"):
            b.go_back()

    def url(self) -> str:
        try:
            return str(self._rf_browser().get_url() or "")
        except Exception:
            return ""

    def wait_for_load_state(self, state: str = "domcontentloaded", *, timeout: str = "10s") -> None:
        """Wait for page to reach a load state. Swallows timeouts — the
        caller usually has a follow-up state check that surfaces real
        failures with richer evidence."""
        b = self._rf_browser()
        if hasattr(b, "wait_for_load_state"):
            try:
                b.wait_for_load_state(state, timeout)
            except Exception as exc:
                log.debug("wait_for_load_state(%s) timeout/error: %s", state, exc)

    # ------------------------------------------------------------------
    # Selector waiting — delegates to RF-Browser's native Playwright waiter
    # (much faster + more reliable than a Python polling loop).
    # ------------------------------------------------------------------

    def wait_for_elements_state(
        self, selector: str, state: str = "attached", *, timeout_ms: int = 5000
    ) -> bool:
        b = self._rf_browser()
        if hasattr(b, "wait_for_elements_state"):
            try:
                b.wait_for_elements_state(selector, state, f"{timeout_ms}ms")
                return True
            except Exception:
                return False
        # Fallback for NullBrowser/missing API: poll
        end = time.time() + (timeout_ms / 1000.0)
        while time.time() < end:
            try:
                count = self.get_count(selector)
                want_present = state in ("attached", "visible")
                if want_present and count > 0:
                    return True
                if (not want_present) and count == 0:
                    return True
            except Exception:
                pass
            time.sleep(0.05)
        return False

    # Backward-compat alias used by older walker code.
    def wait_for_selector(self, selector: str, *, present: bool = True, timeout_ms: int = 5000) -> bool:
        state = "attached" if present else "detached"
        return self.wait_for_elements_state(selector, state, timeout_ms=timeout_ms)

    # ------------------------------------------------------------------
    # Interaction
    # ------------------------------------------------------------------

    def click(self, selector: str) -> None:
        self._rf_browser().click(selector)

    def click_text(self, text: str) -> None:
        """Click an element by visible text.

        Try Playwright's native text selector (exact then substring) first,
        which dispatches a real mouse event. If neither matches, fall back
        to JS that finds the first visible element containing the text
        among button-like roles and dispatches a synthetic MouseEvent.

        Ported from WISE: real sites mix native buttons, ARIA role buttons,
        and tabindex-divs with padded whitespace. Playwright's text=
        selector alone misses many of them.
        """
        b = self._rf_browser()
        for sel in (f'text="{text}"', f"text={text}"):
            try:
                if self.get_count(sel) > 0:
                    b.click(sel)
                    return
            except Exception:
                pass
        # JS fallback: dispatch synthetic MouseEvent on first visible match.
        script = (
            f"() => {{ const t = {json.dumps(text)}; "
            f"for (const el of document.querySelectorAll("
            f"'button, a, [role=button], div[tabindex]')) {{ "
            f"if (el.offsetParent !== null && el.textContent.includes(t)) "
            f"{{ el.dispatchEvent(new MouseEvent('click', {{bubbles:true}})); "
            f"return true; }} }} return false; }}"
        )
        result = self.evaluate_js(script)
        if not result:
            log.warning("click_text: no element found for %r", text)

    def double_click(self, selector: str) -> None:
        b = self._rf_browser()
        if hasattr(b, "click_with_options"):
            b.click_with_options(selector, clickCount=2)
        else:
            b.click(selector)
            b.click(selector)

    def type(self, selector: str, value: str, *, secret: bool = False) -> None:
        b = self._rf_browser()
        if hasattr(b, "fill_text"):
            b.fill_text(selector, value, secret=secret)
        elif hasattr(b, "type_text"):
            b.type_text(selector, value)
        else:
            log.warning("no fill_text/type_text on browser; skipping")

    def select(self, selector: str, value: str) -> None:
        b = self._rf_browser()
        if hasattr(b, "select_options_by"):
            b.select_options_by(selector, "value", value)

    def check(self, selector: str) -> None:
        b = self._rf_browser()
        if hasattr(b, "check_checkbox"):
            b.check_checkbox(selector)

    def uncheck(self, selector: str) -> None:
        b = self._rf_browser()
        if hasattr(b, "uncheck_checkbox"):
            b.uncheck_checkbox(selector)

    def hover(self, selector: str) -> None:
        b = self._rf_browser()
        if hasattr(b, "hover"):
            b.hover(selector)

    def focus(self, selector: str) -> None:
        b = self._rf_browser()
        if hasattr(b, "focus"):
            b.focus(selector)

    def press(self, selector: str, keys: list[str]) -> None:
        b = self._rf_browser()
        if hasattr(b, "keyboard_key"):
            for k in keys:
                b.keyboard_key("press", k)

    def upload(self, selector: str, path: str) -> None:
        b = self._rf_browser()
        if hasattr(b, "upload_file_by_selector"):
            b.upload_file_by_selector(selector, path)

    def scroll(self) -> None:
        b = self._rf_browser()
        if hasattr(b, "scroll_by"):
            b.scroll_by(None, "0", "1000")

    def wait_for_idle(self) -> None:
        self.wait_for_load_state("networkidle", timeout="5s")

    def screenshot(self, filename: str | None = None) -> str:
        b = self._rf_browser()
        if hasattr(b, "take_screenshot"):
            try:
                return str(b.take_screenshot(filename or "screenshot.png") or "")
            except Exception:
                return ""
        return ""

    # ------------------------------------------------------------------
    # JS evaluation with navigation awareness
    # ------------------------------------------------------------------

    def evaluate_js(self, script: str) -> Any:
        """Evaluate JS in the page context.

        If the script triggers navigation, Playwright destroys the page
        context and raises ("Execution context was destroyed", "Page
        navigated", "Element is detached"). Treat that as expected: wait
        for the new page to load and return a navigation marker rather
        than re-raising.

        Ported from WISE: scrapers + form-submit tests routinely have JS
        that issues `location.href = ...` or click handlers that navigate.
        A bare raise here would fail the rule when it actually succeeded.
        """
        b = self._rf_browser()
        if not hasattr(b, "evaluate_javascript"):
            return None
        url_before = self.url()
        try:
            return b.evaluate_javascript(None, script)
        except Exception as exc:
            msg = str(exc).lower()
            if any(h in msg for h in _NAV_ERROR_HINTS):
                # Expected navigation: wait + report success
                self.wait_for_load_state("domcontentloaded", timeout="5s")
                url_after = self.url()
                log.info(
                    "evaluate_js: triggered navigation %s -> %s",
                    url_before, url_after,
                )
                return "__NAVIGATED__"
            raise

    def add_url_params(self, params: str) -> None:
        """Add query params to current URL and navigate.

        Ported from WISE. Uses URLSearchParams in the page context so
        existing params survive. Suppresses the destroyed-context error
        that the navigation itself raises.
        """
        script = (
            f"() => {{ const u = new URL(window.location.href); "
            f"new URLSearchParams({json.dumps(params)}).forEach((v, k) => "
            f"u.searchParams.set(k, v)); "
            f"window.location.href = u.toString(); }}"
        )
        try:
            self.evaluate_js(script)
        except Exception:
            pass  # navigation destroys context — handled by evaluate_js
        self.wait_for_load_state("domcontentloaded", timeout="5s")

    def select_date(
        self, date_iso: str, *, forward_sel: str = 'button[aria-label*="Move forward"]',
        heading_sel: str = "h2", max_clicks: int = 15,
    ) -> None:
        """Navigate a datepicker to the target month and click the day.

        Ported from WISE. Works with ARIA-compliant datepickers:
        - Month headings visible as h2/h3
        - Forward button advances the calendar
        - Day buttons have aria-labels containing the date info

        date_iso is YYYY-MM-DD. Polls until heading text changes between
        clicks (calendar re-rendered) — fixed waits would race re-mount.
        """
        from datetime import date as dt_date

        try:
            year, month, day = (int(x) for x in date_iso.split("-"))
            d = dt_date(year, month, day)
        except (ValueError, TypeError):
            log.warning("select_date: invalid ISO date %r", date_iso)
            return
        month_year = d.strftime("%B %Y")
        day_str = str(day)  # no leading zero

        if not self.wait_for_elements_state(forward_sel, "attached", timeout_ms=5000):
            log.warning("select_date: forward button not found: %s", forward_sel)
            return

        check_script = (
            f"(() => {{ "
            f"const headings = []; "
            f"for (const h of document.querySelectorAll({json.dumps(heading_sel)})) "
            f"{{ const t = h.textContent.trim(); headings.push(t); "
            f"if (t === {json.dumps(month_year)}) return {{found: true, headings}}; }} "
            f"return {{found: false, headings}}; }})()"
        )
        fwd_script = (
            f"(() => {{ const btn = document.querySelector({json.dumps(forward_sel)}); "
            f"if (btn) {{ btn.click(); return true; }} return false; }})()"
        )
        for _ in range(max_clicks):
            result = self.evaluate_js(check_script)
            if isinstance(result, dict) and result.get("found"):
                break
            old_headings = result.get("headings", []) if isinstance(result, dict) else []
            if not self.evaluate_js(fwd_script):
                break
            # Poll until heading text changes (calendar re-rendered)
            deadline = time.time() + 3.0
            while time.time() < deadline:
                new_result = self.evaluate_js(check_script)
                new_headings = (
                    new_result.get("headings", []) if isinstance(new_result, dict) else []
                )
                if new_headings != old_headings:
                    break

        click_script = (
            f"(() => {{ "
            f"for (const b of document.querySelectorAll('button')) {{ "
            f"const l = b.getAttribute('aria-label') || ''; "
            f"if (l.startsWith({json.dumps(day_str + ', ')}) && "
            f"l.includes({json.dumps(month_year)})) "
            f"{{ b.click(); return true; }} }} return false; }})()"
        )
        if not self.evaluate_js(click_script):
            log.warning("select_date: could not find day button for %s", date_iso)

    def call_keyword(self, kw_name: str, args: list) -> None:
        """Defer to an arbitrary Robot Framework keyword.

        Ported from WISE — the escape hatch for multi-step user flows
        defined in a *** Keywords *** block. Only works when running
        inside RF (BuiltIn() must be available).
        """
        try:
            from robot.libraries.BuiltIn import BuiltIn
            BuiltIn().run_keyword(kw_name, *args)
        except Exception as exc:
            log.warning("call_keyword %r failed: %s", kw_name, exc)
            raise

    def browser_step(self, method_name: str, args: list) -> Any:
        """Call any method directly on the underlying Browser library.

        Ported from WISE — escape hatch for RF-Browser methods not in the
        adapter surface. Use sparingly; missing surface should be added
        as a proper method here instead.
        """
        b = self._rf_browser()
        method = getattr(b, method_name, None)
        if method is None or not callable(method):
            log.warning("browser_step: method %r not found", method_name)
            return None
        return method(*args)

    def set_stepper(self, selector: str, count: int) -> None:
        """Click a self-re-rendering stepper N times via JS.

        Stepper widgets (number-up arrows etc.) destroy and re-mount their
        button after each click, which trips Playwright's stability check
        ('element is unstable' / 'detached from DOM'). Use JS-click to
        bypass the stability check, and wait for re-attach between clicks.

        Ported from WISE: this exact failure mode happens on common date
        and quantity pickers; the native click path simply does not work.
        """
        self.wait_for_elements_state(selector, "attached", timeout_ms=5000)
        click_script = (
            f"(() => {{ const el = document.querySelector({json.dumps(selector)}); "
            f"if (el) {{ el.click(); return true; }} return false; }})()"
        )
        for _ in range(count):
            self.evaluate_js(click_script)
            self.wait_for_elements_state(selector, "attached", timeout_ms=2000)

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get_text(self, selector: str) -> str:
        b = self._rf_browser()
        if hasattr(b, "get_text"):
            try:
                return str(b.get_text(selector) or "")
            except Exception:
                return ""
        return ""

    def get_attribute(self, selector: str, attr: str) -> str:
        b = self._rf_browser()
        if hasattr(b, "get_attribute"):
            try:
                return str(b.get_attribute(selector, attr) or "")
            except Exception:
                return ""
        return ""

    def get_value(self, selector: str) -> str:
        b = self._rf_browser()
        if hasattr(b, "get_property"):
            try:
                return str(b.get_property(selector, "value") or "")
            except Exception:
                return ""
        return ""

    def get_class(self, selector: str) -> str:
        return self.get_attribute(selector, "class")

    def get_count(self, selector: str) -> int:
        b = self._rf_browser()
        if hasattr(b, "get_element_count"):
            try:
                return int(b.get_element_count(selector) or 0)
            except Exception:
                return 0
        return 0

    def is_visible(self, selector: str) -> bool:
        b = self._rf_browser()
        if hasattr(b, "get_element_state"):
            try:
                return bool(b.get_element_state(selector, "visible"))
            except Exception:
                return False
        return self.get_count(selector) > 0

    def is_enabled(self, selector: str) -> bool:
        b = self._rf_browser()
        if hasattr(b, "get_element_state"):
            try:
                return bool(b.get_element_state(selector, "enabled"))
            except Exception:
                return False
        return True

    def is_checked(self, selector: str) -> bool:
        b = self._rf_browser()
        if hasattr(b, "get_element_state"):
            try:
                return bool(b.get_element_state(selector, "checked"))
            except Exception:
                return False
        return self.get_attribute(selector, "checked") in ("true", "checked", "")

    # ------------------------------------------------------------------
    # Selector fallback resolution — ported from WISE.
    # ------------------------------------------------------------------

    def resolve_fallback_selector(self, raw: str, scope: str = "") -> str:
        """Resolve a pipe-fallback selector, optionally scoped.

        Syntax: `"primary | fallback1 | fallback2"`. The walker tries each
        candidate on the live page and returns the first that matches.
        Plain selectors (no ` | `) pass through unchanged (modulo scope).

        When `scope` is set (TIER 2.5 child scope propagation), every
        candidate is prefixed with `<scope> >> ` before testing. A raw
        selector of `.` means "the scope element itself."

        Ported from WISE: real apps re-skin frequently; a test that
        encodes both the old and the new selector survives one redesign
        without re-authoring.
        """
        def _scoped(c: str) -> str:
            if not scope:
                return c
            if c == ".":
                return scope
            return f"{scope} >> {c}"

        if " | " not in raw:
            return _scoped(raw)
        candidates = [c.strip() for c in raw.split(" | ")]
        for idx, c in enumerate(candidates):
            sel = _scoped(c)
            try:
                if self.get_count(sel) > 0:
                    log.info("Fallback selector: using %r (option %d/%d)", sel, idx + 1, len(candidates))
                    return sel
            except Exception:
                continue
        return _scoped(candidates[0])

    # ------------------------------------------------------------------
    # Network / API surface (filled by the walker's network hook)
    # ------------------------------------------------------------------

    def record_response(self, status: int, body: str) -> None:
        self._last_response_status = status
        self._last_response_body = body

    def last_response_status(self) -> int | None:
        return self._last_response_status

    def last_response_body(self) -> str:
        return self._last_response_body
