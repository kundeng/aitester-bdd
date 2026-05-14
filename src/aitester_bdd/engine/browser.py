"""Browser adapter — thin wrapper over robotframework-browser.

Exposes the minimal surface the walker needs:
- navigate, click, type, fill, select, check, hover, focus, press, upload
- get_url, get_text, get_attribute, get_count, get_value
- wait_for_selector, wait_for_idle
- screenshot, evaluate_js, reload, go_back

If `robotframework-browser` isn't importable (unit test environment),
fall back to a no-op stub so the walker's plan-traversal logic can still
be tested.
"""
from __future__ import annotations

import logging
import time
from typing import Any

log = logging.getLogger("aitester_bdd.engine.browser")


class _NullBrowser:
    """No-op browser used when robotframework-browser isn't available."""

    def __getattr__(self, name: str):
        def noop(*args: Any, **kw: Any):
            log.debug("NullBrowser noop: %s(%s, %s)", name, args, kw)
            return ""

        return noop


class BrowserAdapter:
    """Minimal browser-driving surface for the walker.

    Wraps the robotframework-browser `Browser` library if available.
    Falls back to a NullBrowser stub for unit testing.
    """

    def __init__(self) -> None:
        self._rfb: Any | None = None
        self._last_response_status: int | None = None
        self._last_response_body: str = ""

    def _rf_browser(self) -> Any:
        """Lazy-import robotframework-browser. Returns a NullBrowser if unavailable."""
        if self._rfb is None:
            try:
                from Browser import Browser  # type: ignore[import-not-found]
                self._rfb = Browser()
            except Exception as exc:
                log.warning("robotframework-browser not available (%s) — using NullBrowser", exc)
                self._rfb = _NullBrowser()
        return self._rfb

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def new_session(self, *, headless: bool = True) -> None:
        b = self._rf_browser()
        try:
            b.new_browser(headless=headless)
            b.new_context()
            b.new_page()
        except Exception as exc:
            log.warning("new_session failed: %s", exc)

    def close(self) -> None:
        try:
            self._rf_browser().close_browser("ALL")
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    def open(self, url: str) -> None:
        self._rf_browser().go_to(url)

    def reload(self) -> None:
        b = self._rf_browser()
        if hasattr(b, "reload"):
            b.reload()
        else:
            # rf-browser uses Reload via keyword
            try:
                b.get_url()  # fallback no-op
            except Exception:
                pass

    def go_back(self) -> None:
        b = self._rf_browser()
        if hasattr(b, "go_back"):
            b.go_back()

    def url(self) -> str:
        try:
            return self._rf_browser().get_url() or ""
        except Exception:
            return ""

    # ------------------------------------------------------------------
    # Interaction
    # ------------------------------------------------------------------

    def click(self, selector: str) -> None:
        self._rf_browser().click(selector)

    def click_text(self, text: str) -> None:
        self._rf_browser().click(f"text={text}")

    def double_click(self, selector: str) -> None:
        b = self._rf_browser()
        if hasattr(b, "click_with_options"):
            b.click_with_options(selector, clickCount=2)
        else:
            b.click(selector)
            b.click(selector)

    def type(self, selector: str, value: str, *, secret: bool = False) -> None:
        b = self._rf_browser()
        # rf-browser uses Fill Text / Type Text
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
        b = self._rf_browser()
        if hasattr(b, "wait_for_load_state"):
            b.wait_for_load_state("networkidle")

    def screenshot(self, filename: str | None = None) -> str:
        b = self._rf_browser()
        if hasattr(b, "take_screenshot"):
            return b.take_screenshot(filename or "screenshot.png") or ""
        return ""

    def evaluate_js(self, script: str) -> Any:
        b = self._rf_browser()
        if hasattr(b, "evaluate_javascript"):
            return b.evaluate_javascript(None, script)
        return None

    # ------------------------------------------------------------------
    # Observation gates / assertions
    # ------------------------------------------------------------------

    def wait_for_selector(self, selector: str, *, present: bool = True, timeout_ms: int = 5000) -> bool:
        """Wait for a selector to be (present|absent). Returns True if condition met."""
        b = self._rf_browser()
        end = time.time() + (timeout_ms / 1000.0)
        while time.time() < end:
            try:
                count = self._element_count(selector)
                if present and count > 0:
                    return True
                if (not present) and count == 0:
                    return True
            except Exception:
                pass
            time.sleep(0.1)
        return False

    def _element_count(self, selector: str) -> int:
        b = self._rf_browser()
        if hasattr(b, "get_element_count"):
            try:
                return int(b.get_element_count(selector) or 0)
            except Exception:
                return 0
        return 0

    def selector_exists(self, selector: str, *, timeout_ms: int = 2000) -> bool:
        return self.wait_for_selector(selector, present=True, timeout_ms=timeout_ms)

    def selector_missing(self, selector: str, *, timeout_ms: int = 2000) -> bool:
        return self.wait_for_selector(selector, present=False, timeout_ms=timeout_ms)

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
        return self._element_count(selector)

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
        return self.get_attribute(selector, "checked") in ("true", "checked", "")

    # ------------------------------------------------------------------
    # Network / API
    # ------------------------------------------------------------------

    def record_response(self, status: int, body: str) -> None:
        self._last_response_status = status
        self._last_response_body = body

    def last_response_status(self) -> int | None:
        return self._last_response_status

    def last_response_body(self) -> str:
        return self._last_response_body
