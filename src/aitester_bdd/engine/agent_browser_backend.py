"""agent-browser backend — drives the same CLI used during the Explore phase.

Why this is a useful third backend:

  - **Same driver across Explore and Run.** Every selector the
    authoring agent grounded during Explore was taken via
    `agent-browser snapshot`. Running the suite via the same CLI
    guarantees the DOM-view contract is identical; cross-driver
    selector quirks (Playwright's `>>` syntax, nodriver's CDP
    object shapes, etc.) cannot drift in between.

  - **Zero install friction.** No `rfbrowser init` (Playwright/Chromium
    download). No Edge/Chrome binary requirement (nodriver). The CLI
    ships its own browser; if you can author with aitester-bdd you can
    run with aitester-bdd.

  - **Easiest to debug.** Every walker action is a subprocess call you
    can copy/paste at a shell prompt and reproduce by hand.

Trade-offs:

  - Subprocess-per-call latency. Fine for individual tests; not great
    for tests with hundreds of actions per scenario.
  - State (`is_visible`, `is_enabled`, `is_checked`) is queried via
    `agent-browser eval` JS rather than a first-class element-state
    API, because the CLI surface doesn't expose those directly.

Enabled by `AITESTER_BROWSER=agent-browser` (this is the default since
it has the lowest install friction).
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
import time
from typing import Any

log = logging.getLogger("aitester_bdd.engine.agent_browser_backend")


_NAV_ERROR_HINTS = (
    "navigation", "context", "destroyed", "detached",
    "navigated", "execution context",
)


def _strip_eval_quotes(s: str) -> str:
    """agent-browser eval returns values as Node prints them. Numbers /
    booleans / null come through unquoted; strings are JSON-encoded with
    surrounding quotes. Try JSON-parse first; else return raw."""
    s = s.strip()
    if not s:
        return s
    try:
        return json.loads(s)  # type: ignore[no-any-return]
    except Exception:
        # eval result like `42` or `true` or `null` — leave as text.
        return s


class AgentBrowserBackend:
    """Runtime backend backed by the `agent-browser` CLI.

    The CLI maintains a persistent browser session across invocations.
    We rely on that: `open` once, then issue many `snapshot`/`click`/
    `eval`/etc. against the same session. Closing the session is
    explicit (`close`).

    All public methods match the surface the walker expects (same as
    `_PlaywrightBackend` / `NodriverBackend`).
    """

    def __init__(self, *, default_timeout: int = 20) -> None:
        self._timeout = default_timeout
        self._last_response_status: int | None = None
        self._last_response_body: str = ""
        self._opened = False

    # ------------------------------------------------------------------
    # CLI bridge
    # ------------------------------------------------------------------

    def _run(self, *args: str, timeout: int | None = None) -> str:
        """Run an agent-browser subprocess. Returns stdout (stripped) or
        raises RuntimeError on a CLI failure. Empty stdout returns ""."""
        cmd = ["agent-browser", *args]
        try:
            r = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout or self._timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"agent-browser {' '.join(args)} timed out after {timeout or self._timeout}s"
            ) from exc
        except FileNotFoundError as exc:
            raise RuntimeError(
                "agent-browser CLI not found on PATH. Install it (npm i -g agent-browser) "
                "or switch backends via AITESTER_BROWSER=playwright|nodriver."
            ) from exc

        out = (r.stdout or "").strip()
        err = (r.stderr or "").strip()
        if r.returncode != 0:
            msg = err or out or f"exit {r.returncode}"
            # Don't raise for navigation-destruction errors during JS eval —
            # caller may want to treat that as expected.
            raise RuntimeError(f"agent-browser {args[0]} failed: {msg}")
        return out

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def new_session(self, *, headless: bool = True) -> None:
        # The CLI auto-launches its browser on first `open`. There's no
        # explicit session-start call. We track `_opened` so `close()`
        # is safe even before any navigation.
        self._opened = False

    def close(self) -> None:
        if self._opened:
            try:
                self._run("close", timeout=10)
            except Exception:
                pass
        self._opened = False

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    def open(self, url: str) -> None:
        self._run("open", url, timeout=60)
        self._opened = True

    def reload(self) -> None:
        # agent-browser CLI doesn't expose a dedicated reload command;
        # fall back to re-navigating to the current URL.
        cur = self.url()
        if cur:
            self._run("open", cur, timeout=60)

    def go_back(self) -> None:
        self.evaluate_js("window.history.back()")

    def url(self) -> str:
        try:
            v = self.evaluate_js("window.location.href")
        except RuntimeError:
            return ""
        return str(v or "")

    def wait_for_load_state(self, state: str = "domcontentloaded", *, timeout: str = "10s") -> None:
        # The CLI awaits load on navigation. Nothing to do here.
        return None

    # ------------------------------------------------------------------
    # Selector waiting — poll via get_count + JS visibility
    # ------------------------------------------------------------------

    def wait_for_elements_state(
        self, selector: str, state: str = "attached", *, timeout_ms: int = 5000,
    ) -> bool:
        want_present = state in ("attached", "visible")
        deadline = time.time() + (timeout_ms / 1000.0)
        while time.time() < deadline:
            count = self.get_count(selector)
            if want_present and count > 0:
                return True
            if (not want_present) and count == 0:
                return True
            time.sleep(0.1)
        return self.get_count(selector) > 0 if want_present else self.get_count(selector) == 0

    def wait_for_selector(self, selector: str, *, present: bool = True, timeout_ms: int = 5000) -> bool:
        return self.wait_for_elements_state(
            selector,
            "attached" if present else "detached",
            timeout_ms=timeout_ms,
        )

    # ------------------------------------------------------------------
    # Interaction
    # ------------------------------------------------------------------

    def click(self, selector: str) -> None:
        self._run("click", selector)

    def click_text(self, text: str) -> None:
        # Try the CLI's text-selector first; fall back to a JS click
        # for text wrapped in non-button elements.
        for sel in (f'text="{text}"', f"text={text}"):
            try:
                if self.get_count(sel) > 0:
                    self._run("click", sel)
                    return
            except Exception:
                pass
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
        # CLI doesn't have dblclick; emulate via JS event dispatch.
        script = (
            f"() => {{ const el = document.querySelector({json.dumps(selector)}); "
            f"if (!el) return false; "
            f"el.dispatchEvent(new MouseEvent('dblclick', {{bubbles:true}})); "
            f"return true; }}"
        )
        self.evaluate_js(script)

    def type(self, selector: str, value: str, *, secret: bool = False) -> None:
        # agent-browser `type` overwrites by default.
        self._run("type", selector, value)

    def select(self, selector: str, value: str) -> None:
        script = (
            f"() => {{ const el = document.querySelector({json.dumps(selector)}); "
            f"if (!el) return false; "
            f"el.value = {json.dumps(value)}; "
            f"el.dispatchEvent(new Event('change', {{bubbles:true}})); "
            f"return true; }}"
        )
        self.evaluate_js(script)

    def check(self, selector: str) -> None:
        # Click is sufficient for unchecked → checked on most inputs.
        self.click(selector)

    def uncheck(self, selector: str) -> None:
        self.click(selector)

    def hover(self, selector: str) -> None:
        script = (
            f"() => {{ const el = document.querySelector({json.dumps(selector)}); "
            f"if (!el) return false; "
            f"const r = el.getBoundingClientRect(); "
            f"el.dispatchEvent(new MouseEvent('mouseover', "
            f"{{bubbles:true,clientX:r.left+r.width/2,clientY:r.top+r.height/2}})); "
            f"return true; }}"
        )
        self.evaluate_js(script)

    def focus(self, selector: str) -> None:
        script = (
            f"() => {{ const el = document.querySelector({json.dumps(selector)}); "
            f"if (el && el.focus) el.focus(); return !!el; }}"
        )
        self.evaluate_js(script)

    def press(self, selector: str, keys: list[str]) -> None:
        # agent-browser exposes a top-level `press`. Apply each key after
        # focusing the target element.
        self.focus(selector)
        for k in keys:
            self._run("press", k)

    def upload(self, selector: str, path: str) -> None:
        # Not exposed by the CLI; warn rather than fail silently.
        log.warning("upload: not supported in agent-browser backend; selector=%r path=%r", selector, path)

    def scroll(self) -> None:
        self.evaluate_js("window.scrollBy(0, window.innerHeight)")

    def wait_for_idle(self) -> None:
        return None

    def screenshot(self, filename: str | None = None) -> str:
        path = filename or "/tmp/aitester-bdd-ab-shot.png"
        try:
            self._run("screenshot", path)
        except Exception as exc:
            log.debug("screenshot failed: %s", exc)
        return path

    # ------------------------------------------------------------------
    # JS evaluation
    # ------------------------------------------------------------------

    def evaluate_js(self, script: str) -> Any:
        s = script.strip()
        if s.startswith("() =>") or s.startswith("function"):
            s = f"({s})()"
        try:
            out = self._run("eval", s, timeout=30)
        except RuntimeError as exc:
            msg = str(exc).lower()
            if any(h in msg for h in _NAV_ERROR_HINTS):
                log.info("evaluate_js: triggered navigation")
                return "__NAVIGATED__"
            raise
        return _strip_eval_quotes(out)

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

    def select_date(
        self, date_iso: str, *, forward_sel: str = 'button[aria-label*="Move forward"]',
        heading_sel: str = "h2", max_clicks: int = 15,
    ) -> None:
        from datetime import date as dt_date
        try:
            year, month, day = (int(x) for x in date_iso.split("-"))
            d = dt_date(year, month, day)
        except (ValueError, TypeError):
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
        log.warning(
            "call_keyword %r ignored — agent-browser backend does not run inside Robot Framework.",
            kw_name,
        )

    def browser_step(self, method_name: str, args: list) -> Any:
        log.warning("browser_step %r not supported in agent-browser backend", method_name)
        return None

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get_text(self, selector: str) -> str:
        try:
            return self._run("get", "text", selector)
        except Exception:
            return ""

    def get_attribute(self, selector: str, attr: str) -> str:
        v = self.evaluate_js(
            f"(document.querySelector({json.dumps(selector)})||{{}}).getAttribute({json.dumps(attr)}) || ''"
        )
        return str(v or "")

    def get_value(self, selector: str) -> str:
        v = self.evaluate_js(
            f"(document.querySelector({json.dumps(selector)})||{{}}).value"
        )
        return str(v or "")

    def get_class(self, selector: str) -> str:
        return self.get_attribute(selector, "class")

    def get_count(self, selector: str) -> int:
        try:
            out = self._run("get", "count", selector)
        except Exception:
            return 0
        try:
            return int(out.strip())
        except (ValueError, TypeError):
            # Some agent-browser builds print "count: 3" instead of "3".
            m = re.search(r"(\d+)", out)
            return int(m.group(1)) if m else 0

    def is_visible(self, selector: str) -> bool:
        v = self.evaluate_js(
            f"(() => {{ const el = document.querySelector({json.dumps(selector)}); "
            f"if (!el) return false; "
            f"const r = el.getBoundingClientRect(); "
            f"return r.width > 0 && r.height > 0 && el.offsetParent !== null; }})()"
        )
        return bool(v)

    def is_enabled(self, selector: str) -> bool:
        v = self.evaluate_js(
            f"!((document.querySelector({json.dumps(selector)})||{{}}).disabled)"
        )
        return bool(v)

    def is_checked(self, selector: str) -> bool:
        v = self.evaluate_js(
            f"!!((document.querySelector({json.dumps(selector)})||{{}}).checked)"
        )
        return bool(v)

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
    # Network stubs (the walker uses these for last_status checks).
    # ------------------------------------------------------------------

    def record_response(self, status: int, body: str) -> None:
        self._last_response_status = status
        self._last_response_body = body

    def last_response_status(self) -> int | None:
        return self._last_response_status

    def last_response_body(self) -> str:
        return self._last_response_body
