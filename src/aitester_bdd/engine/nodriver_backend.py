"""Nodriver backend — raw CDP browser driver, no Playwright, no rfbrowser init.

Ported from the WISE RPA BDD engine (_NodriverAdapter). Two reasons to
choose this over the default rfbrowser/Playwright backend:

  1. Bot-detected sites — nodriver leaves no Playwright fingerprint, so
     detection heuristics that target Playwright (DataDome, Cloudflare
     Bot Management, PerimeterX, etc.) miss it.
  2. Skip `rfbrowser init` — uses any Edge or Chrome binary already on
     the system instead of downloading Playwright's bundled browsers.

Enabled by:
  - installing the `stealth` extra: `pip install aitester-bdd[stealth]`
  - setting `AITESTER_BROWSER=nodriver`

Nodriver is async internally; this adapter runs a dedicated event loop
in a daemon thread and bridges sync calls onto it.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import os
import re
import shutil
import threading
from typing import Any
from urllib.parse import urlparse

log = logging.getLogger("aitester_bdd.engine.nodriver_backend")


# Locations where Edge/Chrome live on macOS + linux. Add to taste.
_BROWSER_BINARIES = [
    # macOS
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    # linux
    "/usr/bin/microsoft-edge",
    "/usr/bin/microsoft-edge-stable",
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
]


# Navigation-related exceptions are not errors, just signals.
_NAV_ERROR_HINTS = (
    "navigation", "context", "destroyed", "detached",
    "navigated", "execution context",
)


def _find_browser_binary() -> str | None:
    """Return the path to an Edge/Chrome binary if any is installed."""
    for c in _BROWSER_BINARIES:
        if shutil.which(c) or os.path.isfile(c):
            return c
    # Fall back to anything PATH provides
    for name in ("microsoft-edge", "google-chrome", "chromium"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _unwrap_cdp(val: Any) -> Any:
    """Unwrap CDP RemoteObject typed wrappers to plain Python values."""
    if isinstance(val, dict):
        if "type" in val and "value" in val and len(val) <= 3:
            return _unwrap_cdp(val["value"])
        return {k: _unwrap_cdp(v) for k, v in val.items()}
    if isinstance(val, list):
        return [_unwrap_cdp(v) for v in val]
    return val


class NodriverBackend:
    """Raw-CDP browser backend. Implements the same public surface as
    the default (rfbrowser-backed) `BrowserAdapter`, so the walker is
    agnostic to which backend is in use.
    """

    def __init__(self) -> None:
        self._browser: Any = None
        self._page: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: threading.Thread | None = None
        self._warmed_origins: set[str] = set()
        self._timeout_ms = 30_000
        self._last_response_status: int | None = None
        self._last_response_body: str = ""

    # ------------------------------------------------------------------
    # Async bridge — nodriver is async; we run our own loop.
    # ------------------------------------------------------------------

    def _ensure_loop(self) -> None:
        if self._loop is not None:
            return
        loop = asyncio.new_event_loop()

        def run() -> None:
            asyncio.set_event_loop(loop)
            loop.run_forever()

        t = threading.Thread(target=run, daemon=True)
        t.start()
        self._loop = loop
        self._loop_thread = t

    def _run(self, coro: Any) -> Any:
        """Run an async coroutine synchronously via the dedicated loop."""
        if self._loop is None:
            raise RuntimeError("event loop not initialized — call new_session first")
        fut: concurrent.futures.Future = concurrent.futures.Future()

        async def wrap() -> None:
            try:
                fut.set_result(await coro)
            except Exception as exc:
                fut.set_exception(exc)

        self._loop.call_soon_threadsafe(
            lambda: self._loop.create_task(wrap())  # type: ignore[union-attr]
        )
        return fut.result(timeout=self._timeout_ms / 1000 + 10)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def new_session(self, *, headless: bool = True) -> None:
        try:
            import nodriver
        except ImportError as exc:
            raise RuntimeError(
                "nodriver is not installed. Install the stealth extra: "
                "`pip install aitester-bdd[stealth]` (or `uv sync --extra stealth`)."
            ) from exc

        binary = _find_browser_binary()
        if not binary:
            raise RuntimeError(
                "nodriver backend requires Microsoft Edge or Google Chrome on the system. "
                "Install one or switch to the default backend (unset AITESTER_BROWSER)."
            )
        self._ensure_loop()

        async def launch() -> Any:
            return await nodriver.start(
                headless=headless,
                browser_executable_path=binary,
            )

        self._browser = self._run(launch())
        # Grab an initial tab.
        tabs = getattr(self._browser, "tabs", []) or []
        self._page = tabs[0] if tabs else self._run(self._browser.get("about:blank"))
        log.info("nodriver started (binary=%s, headless=%s)", os.path.basename(binary), headless)

    def close(self) -> None:
        if self._browser is not None:
            try:
                self._browser.stop()
            except Exception:
                pass
            self._browser = None
        self._page = None
        if self._loop is not None:
            try:
                self._loop.call_soon_threadsafe(self._loop.stop)
            except Exception:
                pass
            self._loop = None
            self._loop_thread = None

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    def open(self, url: str) -> None:
        # Warm-up: visit origin first when seeing a new domain. Some
        # bot-detection responses set first-party cookies on origin GET
        # that the target page expects.
        origin = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
        if origin not in self._warmed_origins and origin and origin != url.rstrip("/"):
            self._warmed_origins.add(origin)
            try:
                self._page = self._run(self._browser.get(origin))
            except Exception as exc:
                log.debug("warmup failed for %s: %s", origin, exc)
        self._page = self._run(self._browser.get(url))

    def reload(self) -> None:
        if self._page and hasattr(self._page, "reload"):
            self._run(self._page.reload())

    def go_back(self) -> None:
        # nodriver's API exposes history navigation via JS.
        self.evaluate_js("window.history.back()")

    def url(self) -> str:
        try:
            v = self.evaluate_js("window.location.href")
            return str(v or "")
        except Exception:
            return ""

    def wait_for_load_state(self, state: str = "domcontentloaded", *, timeout: str = "10s") -> None:
        # nodriver auto-waits on navigation; the explicit load-state wait
        # is a Playwright-ism. Best-effort no-op.
        return None

    # ------------------------------------------------------------------
    # Selector waiting — uses nodriver's wait_for + a JS fallback.
    # ------------------------------------------------------------------

    def wait_for_elements_state(
        self, selector: str, state: str = "attached", *, timeout_ms: int = 5000,
    ) -> bool:
        want_present = state in ("attached", "visible")
        css = selector.split(" >> ")[0].strip()
        end = asyncio.get_event_loop_policy().get_event_loop().time() if False else 0
        # Poll get_count() since nodriver's wait_for raises on timeout.
        import time
        deadline = time.time() + (timeout_ms / 1000.0)
        while time.time() < deadline:
            count = self.get_count(css)
            if want_present and count > 0:
                return True
            if (not want_present) and count == 0:
                return True
            time.sleep(0.1)
        return want_present and self.get_count(css) > 0 or (
            (not want_present) and self.get_count(css) == 0
        )

    def wait_for_selector(self, selector: str, *, present: bool = True, timeout_ms: int = 5000) -> bool:
        state = "attached" if present else "detached"
        return self.wait_for_elements_state(selector, state, timeout_ms=timeout_ms)

    # ------------------------------------------------------------------
    # Element selection via Playwright-like `>>` syntax
    # ------------------------------------------------------------------

    def _select(self, selector: str) -> Any:
        """Resolve a selector to a single element. Supports `a >> b`,
        `text=...`, and `nth=N` parts."""
        parts = [p.strip() for p in selector.split(" >> ")] if " >> " in selector else [selector]
        current: Any = self._page
        for i, part in enumerate(parts):
            m = re.match(r'^text=["\']?(.+?)["\']?$', part)
            if m:
                current = self._run(
                    (current if hasattr(current, "find") else self._page).find(
                        m.group(1), best_match=True,
                    )
                )
                continue
            m = re.match(r"^nth=(\d+)$", part)
            if m:
                idx = int(m.group(1))
                current = current[idx] if isinstance(current, list) and idx < len(current) else None
                continue
            if isinstance(current, list):
                current = current[0] if current else None
            if current is None:
                return None
            # If next part is nth=, collect all matches at this step.
            if i + 1 < len(parts) and parts[i + 1].startswith("nth="):
                parent = self._page if i == 0 else current
                fn = getattr(parent, "select_all", None) or getattr(parent, "query_selector_all", None)
                current = self._run(fn(part)) if fn else []
                current = current or []
            else:
                fn = getattr(current, "query_selector", None) or getattr(current, "select", None)
                current = self._run(fn(part)) if fn else None
            if current is None:
                return None
        return current if not isinstance(current, list) else (current[0] if current else None)

    # ------------------------------------------------------------------
    # Interaction
    # ------------------------------------------------------------------

    def click(self, selector: str) -> None:
        el = self._select(selector)
        if el is not None:
            self._run(el.click())

    def click_text(self, text: str) -> None:
        """Find an element by visible text and click it."""
        try:
            el = self._run(self._page.find(text, best_match=True))
        except Exception:
            el = None
        if el is not None:
            self._run(el.click())
            return
        # Fallback: synthetic JS click on first matching visible element.
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
        el = self._select(selector)
        if el is not None:
            self._run(el.click())
            self._run(el.click())

    def type(self, selector: str, value: str, *, secret: bool = False) -> None:
        el = self._select(selector)
        if el is not None:
            try:
                self._run(el.clear_input())
            except Exception:
                pass
            self._run(el.send_keys(value))

    def select(self, selector: str, value: str) -> None:
        el = self._select(selector)
        if el is not None and hasattr(el, "select_option"):
            self._run(el.select_option(value))

    def check(self, selector: str) -> None:
        self.click(selector)

    def uncheck(self, selector: str) -> None:
        self.click(selector)

    def hover(self, selector: str) -> None:
        el = self._select(selector)
        if el is not None and hasattr(el, "mouse_move"):
            self._run(el.mouse_move())

    def focus(self, selector: str) -> None:
        el = self._select(selector)
        if el is not None:
            self._run(el.click())

    def press(self, selector: str, keys: list[str]) -> None:
        el = self._select(selector)
        if el is None:
            return
        for k in keys:
            self._run(el.send_keys(k))

    def upload(self, selector: str, path: str) -> None:
        el = self._select(selector)
        if el is not None and hasattr(el, "send_file"):
            self._run(el.send_file(path))

    def scroll(self) -> None:
        self.evaluate_js("window.scrollBy(0, window.innerHeight)")

    def wait_for_idle(self) -> None:
        # nodriver auto-waits; nothing to do.
        return None

    def screenshot(self, filename: str | None = None) -> str:
        path = filename or "/tmp/aitester-bdd-nodriver-shot.png"
        if self._page is not None and hasattr(self._page, "save_screenshot"):
            self._run(self._page.save_screenshot(path))
        return path

    # ------------------------------------------------------------------
    # JS evaluation
    # ------------------------------------------------------------------

    def evaluate_js(self, script: str) -> Any:
        """Evaluate JS in page context. Wraps function expressions as IIFE.
        Treats navigation-destroyed-context as expected (returns a marker)."""
        s = script.strip()
        if s.startswith("() =>") or s.startswith("function"):
            s = f"({s})()"
        try:
            result = self._run(self._page.evaluate(s))
        except Exception as exc:
            msg = str(exc).lower()
            if any(h in msg for h in _NAV_ERROR_HINTS):
                log.info("evaluate_js: triggered navigation")
                return "__NAVIGATED__"
            raise
        if hasattr(result, "exception_id"):
            log.warning("nodriver eval error: %s", result)
            return None
        return _unwrap_cdp(result)

    def set_stepper(self, selector: str, count: int) -> None:
        self.wait_for_elements_state(selector, "attached", timeout_ms=5000)
        click_script = (
            f"(() => {{ const el = document.querySelector({json.dumps(selector)}); "
            f"if (el) {{ el.click(); return true; }} return false; }})()"
        )
        for _ in range(count):
            self.evaluate_js(click_script)
            self.wait_for_elements_state(selector, "attached", timeout_ms=2000)

    def add_url_params(self, params: str) -> None:
        script = (
            f"() => {{ const u = new URL(window.location.href); "
            f"new URLSearchParams({json.dumps(params)}).forEach((v, k) => "
            f"u.searchParams.set(k, v)); "
            f"window.location.href = u.toString(); }}"
        )
        try:
            self.evaluate_js(script)
        except Exception:
            pass
        self.wait_for_load_state("domcontentloaded", timeout="5s")

    def select_date(
        self, date_iso: str, *, forward_sel: str = 'button[aria-label*="Move forward"]',
        heading_sel: str = "h2", max_clicks: int = 15,
    ) -> None:
        # Same logic as the rfbrowser path; nodriver's evaluate_js is wired.
        from datetime import date as dt_date
        try:
            year, month, day = (int(x) for x in date_iso.split("-"))
            d = dt_date(year, month, day)
        except (ValueError, TypeError):
            log.warning("select_date: invalid ISO date %r", date_iso)
            return
        month_year = d.strftime("%B %Y")
        day_str = str(day)
        if not self.wait_for_elements_state(forward_sel, "attached", timeout_ms=5000):
            return
        check_script = (
            f"(() => {{ const headings = []; "
            f"for (const h of document.querySelectorAll({json.dumps(heading_sel)})) "
            f"{{ const t = h.textContent.trim(); headings.push(t); "
            f"if (t === {json.dumps(month_year)}) return {{found: true, headings}}; }} "
            f"return {{found: false, headings}}; }})()"
        )
        fwd_script = (
            f"(() => {{ const btn = document.querySelector({json.dumps(forward_sel)}); "
            f"if (btn) {{ btn.click(); return true; }} return false; }})()"
        )
        import time
        for _ in range(max_clicks):
            result = self.evaluate_js(check_script)
            if isinstance(result, dict) and result.get("found"):
                break
            old_headings = result.get("headings", []) if isinstance(result, dict) else []
            if not self.evaluate_js(fwd_script):
                break
            deadline = time.time() + 3.0
            while time.time() < deadline:
                new = self.evaluate_js(check_script)
                new_h = new.get("headings", []) if isinstance(new, dict) else []
                if new_h != old_headings:
                    break
        click_script = (
            f"(() => {{ for (const b of document.querySelectorAll('button')) {{ "
            f"const l = b.getAttribute('aria-label') || ''; "
            f"if (l.startsWith({json.dumps(day_str + ', ')}) && "
            f"l.includes({json.dumps(month_year)})) "
            f"{{ b.click(); return true; }} }} return false; }})()"
        )
        self.evaluate_js(click_script)

    def call_keyword(self, kw_name: str, args: list) -> None:
        # No RF context in nodriver mode — call_keyword is a no-op with a
        # clear warning. Suites that need this should use the default backend.
        log.warning(
            "call_keyword %r ignored — nodriver backend does not run inside Robot Framework.",
            kw_name,
        )

    def browser_step(self, method_name: str, args: list) -> Any:
        # Map a small subset of common Browser-library method names onto our methods.
        mapping = {
            "go_to": self.open,
            "click": self.click,
            "fill_text": lambda *a: self.type(*a),
            "type_text": lambda *a: self.type(*a),
            "get_text": self.get_text,
            "get_attribute": self.get_attribute,
            "get_element_count": self.get_count,
        }
        fn = mapping.get(method_name)
        if fn is None:
            log.warning("browser_step %r not supported in nodriver mode", method_name)
            return None
        return fn(*args)

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get_text(self, selector: str) -> str:
        el = self._select(selector)
        if el is None:
            return ""
        return str(getattr(el, "text", "") or "")

    def get_attribute(self, selector: str, attr: str) -> str:
        el = self._select(selector)
        if el is None:
            return ""
        attrs = getattr(el, "attrs", {}) or {}
        return str(attrs.get(attr, "") or "")

    def get_value(self, selector: str) -> str:
        css = selector.split(" >> ")[0]
        v = self.evaluate_js(f"(document.querySelector({json.dumps(css)})||{{}}).value")
        return str(v or "")

    def get_class(self, selector: str) -> str:
        return self.get_attribute(selector, "class")

    def get_count(self, selector: str) -> int:
        if " >> " not in selector:
            m = re.match(r'^text=["\']?(.+?)["\']?$', selector)
            if m:
                els = self._run(self._page.find_all(m.group(1)))
                return len(els) if els else 0
            v = self.evaluate_js(
                f"document.querySelectorAll({json.dumps(selector)}).length"
            )
            try:
                return int(v or 0)
            except (TypeError, ValueError):
                return 0
        # Complex selector — let the basic case handle it via JS too.
        parts = [p.strip() for p in selector.split(" >> ")]
        # Strip nth= parts for counting; fall back to count of the parent.
        css_parts = [p for p in parts if not p.startswith("nth=") and not p.startswith("text=")]
        if not css_parts:
            return 0
        compound = " ".join(css_parts)
        v = self.evaluate_js(
            f"document.querySelectorAll({json.dumps(compound)}).length"
        )
        try:
            return int(v or 0)
        except (TypeError, ValueError):
            return 0

    def is_visible(self, selector: str) -> bool:
        return self.get_count(selector) > 0

    def is_enabled(self, selector: str) -> bool:
        css = selector.split(" >> ")[0]
        v = self.evaluate_js(
            f"!((document.querySelector({json.dumps(css)})||{{}}).disabled)"
        )
        return bool(v)

    def is_checked(self, selector: str) -> bool:
        css = selector.split(" >> ")[0]
        v = self.evaluate_js(
            f"!!((document.querySelector({json.dumps(css)})||{{}}).checked)"
        )
        return bool(v)

    # ------------------------------------------------------------------
    # Pipe-fallback selector — same contract as the rfbrowser backend
    # ------------------------------------------------------------------

    def resolve_fallback_selector(self, raw: str) -> str:
        if " | " not in raw:
            return raw
        candidates = [c.strip() for c in raw.split(" | ")]
        for idx, c in enumerate(candidates):
            try:
                if self.get_count(c) > 0:
                    log.info("Fallback selector: using %r (option %d/%d)", c, idx + 1, len(candidates))
                    return c
            except Exception:
                continue
        return candidates[0]

    # ------------------------------------------------------------------
    # Network capture stubs (the walker uses these for last_status checks).
    # ------------------------------------------------------------------

    def record_response(self, status: int, body: str) -> None:
        self._last_response_status = status
        self._last_response_body = body

    def last_response_status(self) -> int | None:
        return self._last_response_status

    def last_response_body(self) -> str:
        return self._last_response_body
