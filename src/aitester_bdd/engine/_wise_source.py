"""Robot Framework keyword library for BDD RPA suites.

Keywords build a declarative rule tree during test execution. The Suite Teardown
(finalize_deployment) walks the tree using robotframework-browser (RF's Playwright
wrapper) to scrape data.

Architecture: Plan-then-Execute (deferred execution model).
- Phase A: RF keywords called sequentially build an in-memory scraping plan.
- Phase B: finalize_deployment executes the plan via robotframework-browser keywords.

This is necessary because RF keywords are called once each in sequence, but the
scraping logic requires nested loops (pagination x element expansion x extraction).
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from robot.api import logger
from robot.api.deco import keyword, library


# ---------------------------------------------------------------------------
# Data structures for the rule tree
# ---------------------------------------------------------------------------

@dataclass
class FieldSpec:
    name: str
    extractor: str  # text, attr, grouped, html, link, image
    locator: str
    attr: str | None = None


@dataclass
class TableFieldSpec:
    name: str
    header: str


@dataclass
class TableSpec:
    name: str
    locator: str
    header_row: int = 0
    fields: list[TableFieldSpec] = field(default_factory=list)


@dataclass
class StateCheck:
    type: str  # url_matches, url_contains, url_not_contains, selector_exists, table_headers
    pattern: str


@dataclass
class Action:
    type: str  # click, type, select, scroll, wait, wait_ms, open, open_bound,
    #            browser_step, call_keyword
    locator: str | None = None
    value: str | None = None
    options: dict = field(default_factory=dict)
    args: tuple = ()


@dataclass
class CombinationAxis:
    action: str  # type, select, click
    control: str  # CSS selector
    values: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)  # patterns to drop from auto-discovered values
    skip: int = 0  # skip first N values (e.g. placeholder "Select...")
    emit: str = ""  # artifact name to emit discovered values to


@dataclass
class Expansion:
    over: str  # elements, pages_next, pages_numeric, combinations
    scope: str | None = None
    locator: str | None = None
    limit: int = 100
    start: int = 1
    order: str = "dfs"
    options: dict = field(default_factory=dict)
    axes: list[CombinationAxis] = field(default_factory=list)


@dataclass
class RuleNode:
    name: str
    parents: list[str] = field(default_factory=list)
    # Type 1 — Guards (preconditions): "Is the world in the expected state?"
    # Defensive checks run before any actions.  In a perfect MDP these are
    # redundant; they exist to catch unexpected state (wrong page, anti-bot).
    guards: list[StateCheck] = field(default_factory=list)
    # Type 2 — Steps: ordered list of Actions and inline StateCheck
    # observations.  Observations between actions are synchronization gates
    # ("has the side effect landed?"), required for correctness.
    steps: list[Action | StateCheck] = field(default_factory=list)
    # Policy when a guard fails: "skip" (default — skip rule + children)
    # or "abort" (stop the entire walk — hard precondition).
    guard_policy: str = "skip"
    expansion: Expansion | None = None
    field_specs: list[FieldSpec] = field(default_factory=list)
    table_spec: TableSpec | None = None
    ai_extraction: dict | None = None
    emit_targets: list[str] = field(default_factory=list)
    emit_flatten_by: dict[str, str] = field(default_factory=dict)
    emit_merge_on: dict[str, str] = field(default_factory=dict)
    children: list[RuleNode] = field(default_factory=list)
    retry_max: int = 0
    retry_delay_ms: int = 1000
    # Per-rule interrupt scoping: override global interrupt selectors
    interrupt_override: list[str] | None = None  # None = inherit global
    interrupt_paused: bool = False                # True = skip dismiss for this rule
    # Declarative rule lifecycle options
    options: dict[str, str] = field(default_factory=dict)
    # Recognized keys: on_enter (screenshot), on_fail (screenshot), timeout_ms (int)


@dataclass
class ArtifactSchema:
    name: str
    fields: list[dict] = field(default_factory=list)
    output: bool = False
    format: str = "json"  # json, jsonl, csv, markdown
    structure: str = "flat"  # nested or flat
    dedupe: str | None = None
    query: str | None = None
    consumes: str | None = None
    description: str = ""


@dataclass
class QualityGate:
    min_records: int | None = None
    filled_pcts: dict[str, float] = field(default_factory=dict)
    max_failed_pct: float | None = None


@dataclass
class ResourceContext:
    name: str
    entry_url: str = ""
    globals_: dict = field(default_factory=dict)
    rules: dict[str, RuleNode] = field(default_factory=dict)
    root_names: list[str] = field(default_factory=list)
    consumes: str | None = None
    entry_template: str | None = None
    iterates_parent: str | None = None


@dataclass
class HookDef:
    name: str
    lifecycle_point: str  # post_discover, pre_extract, post_extract, pre_assemble, post_assemble
    config: dict[str, str] = field(default_factory=dict)


@dataclass
class SetupAction:
    action: str  # open, input, password, click
    css: str = ""
    url: str = ""
    value: str = ""


@dataclass
class DeploymentContext:
    name: str
    artifacts: dict[str, ArtifactSchema] = field(default_factory=dict)
    artifact_store: dict[str, list] = field(default_factory=dict)
    resources: list[ResourceContext] = field(default_factory=list)
    quality_gate: QualityGate = field(default_factory=QualityGate)
    output_dir: str = ""
    write_overrides: dict[str, str] = field(default_factory=dict)
    ai_adapter: str = ""  # aichat, anthropic, openai, cli:<cmd>
    hooks: list[HookDef] = field(default_factory=list)
    setup_actions: list[SetupAction] = field(default_factory=list)
    setup_skip_when: str = ""
    interrupt_selectors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Persistent Artifact Store (staging + committed + checkpoint)
# ---------------------------------------------------------------------------

class PersistentArtifactStore:
    """Drop-in replacement for ``dict[str, list]`` with write-ahead staging.

    Records accumulate in a staging buffer per URL.  Only when the URL
    completes successfully (``commit_url``) do they flush to the committed
    store.  Timeout or crash → staging discarded, URL retried on resume.

    The committed store is persisted to ``_checkpoint.json`` on every commit
    so the engine can resume from where it left off.
    """

    def __init__(self, checkpoint_path: Path | None = None) -> None:
        self._committed: dict[str, list] = {}
        self._staging: dict[str, list] = {}
        self._staging_active = False
        self._current_resource = ""
        self._current_url = ""
        self._completed_resources: set[str] = set()
        self._processed_urls: dict[str, set[str]] = {}
        self._cp_path = checkpoint_path
        if checkpoint_path and checkpoint_path.exists():
            self._load()

    # --- dict-like interface (engine sees this as dict[str, list]) ----------

    def __getitem__(self, key: str) -> list:
        """Return the mutable list for this key.

        During staging: returns the staging list (so .append() mutates it).
        Outside staging: returns the committed list.
        For a merged read-only view, use ``merged(key)`` instead.
        """
        if self._staging_active:
            if key not in self._staging:
                self._staging[key] = []
            return self._staging[key]
        return self._committed.setdefault(key, [])

    def merged(self, key: str) -> list:
        """Read-only merged view of committed + staging for a key."""
        committed = self._committed.get(key, [])
        staged = self._staging.get(key, []) if self._staging_active else []
        if staged:
            return committed + staged
        return committed

    def __setitem__(self, key: str, value: list) -> None:
        if self._staging_active:
            self._staging[key] = value
        else:
            self._committed[key] = value

    def __contains__(self, key: object) -> bool:
        return key in self._committed or (
            self._staging_active and key in self._staging
        )

    def get(self, key: str, default: Any = None) -> Any:
        if key in self:
            return self[key]
        return default

    def items(self) -> Any:
        """Merged read-only view of committed + staging."""
        all_keys = set(self._committed.keys())
        if self._staging_active:
            all_keys |= set(self._staging.keys())
        return [(k, self.merged(k)) for k in all_keys]

    def values(self) -> Any:
        return [self.merged(k) for k in self.keys()]

    def keys(self) -> Any:
        all_keys = set(self._committed.keys())
        if self._staging_active:
            all_keys |= set(self._staging.keys())
        return all_keys

    # --- URL-level staging -------------------------------------------------

    def begin_url(self, resource: str, entry_url: str) -> None:
        """Start staging buffer for this URL."""
        self._staging = {}
        self._staging_active = True
        self._current_resource = resource
        self._current_url = entry_url

    def commit_url(self) -> None:
        """URL completed successfully.  Flush staging → committed + checkpoint."""
        for k, v in self._staging.items():
            self._committed.setdefault(k, []).extend(v)
        self._processed_urls.setdefault(
            self._current_resource, set()
        ).add(self._current_url)
        n_staged = sum(len(v) for v in self._staging.values())
        self._staging = {}
        self._staging_active = False
        self._save()
        logger.info(f"  [CHECKPOINT] committed {n_staged} records for "
                    f"{self._current_url[:60]}")

    def rollback_url(self) -> None:
        """URL failed/timed out.  Discard staging — committed untouched."""
        n_discarded = sum(len(v) for v in self._staging.values())
        if n_discarded:
            logger.info(f"  [CHECKPOINT] rolled back {n_discarded} staged records "
                        f"for {self._current_url[:60]}")
        self._staging = {}
        self._staging_active = False

    # --- Resource-level tracking -------------------------------------------

    def mark_resource_complete(self, resource: str) -> None:
        self._completed_resources.add(resource)
        self._save()

    def is_resource_complete(self, resource: str) -> bool:
        return resource in self._completed_resources

    def is_url_processed(self, resource: str, url: str) -> bool:
        return url in self._processed_urls.get(resource, set())

    # --- Persistence -------------------------------------------------------

    def _save(self) -> None:
        if not self._cp_path:
            return
        checkpoint = {
            "version": 1,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "completed_resources": sorted(self._completed_resources),
            "processed_urls": {k: sorted(v) for k, v in self._processed_urls.items()},
            "artifact_store": self._committed,
        }
        tmp = self._cp_path.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(checkpoint, f, ensure_ascii=False)
        os.replace(tmp, self._cp_path)

    def _load(self) -> None:
        if not self._cp_path or not self._cp_path.exists():
            return
        with open(self._cp_path) as f:
            cp = json.load(f)
        if cp.get("version") != 1:
            logger.warn(f"  Unknown checkpoint version {cp.get('version')}, ignoring")
            return
        self._committed = cp.get("artifact_store", {})
        self._completed_resources = set(cp.get("completed_resources", []))
        self._processed_urls = {
            k: set(v) for k, v in cp.get("processed_urls", {}).items()
        }
        n = sum(len(v) for v in self._committed.values())
        n_urls = sum(len(v) for v in self._processed_urls.values())
        logger.info(f"  [CHECKPOINT] loaded: {len(self._completed_resources)} resources, "
                    f"{n_urls} URLs, {n} records")

    def delete_checkpoint(self) -> None:
        if self._cp_path and self._cp_path.exists():
            self._cp_path.unlink()
            logger.info("  [CHECKPOINT] removed after successful completion")


# ---------------------------------------------------------------------------
# Aspect-Oriented Programming registry
# ---------------------------------------------------------------------------

@dataclass
class Aspect:
    """A cross-cutting concern that hooks into engine lifecycle points.

    Each hook is optional.  ``before_*`` hooks return True to skip the
    phase (e.g., checkpoint aspect skips completed resources/URLs).
    """
    name: str
    before_action: Callable[..., None] | None = None
    after_action: Callable[..., None] | None = None
    before_url: Callable[..., bool] | None = None       # return True = skip
    after_url: Callable[..., None] | None = None         # (res, url, success)
    before_resource: Callable[..., bool] | None = None   # return True = skip
    after_resource: Callable[..., None] | None = None


class AspectRegistry:
    """Manages cross-cutting aspects and fires hooks at lifecycle points."""

    def __init__(self) -> None:
        self._aspects: list[Aspect] = []

    def register(self, aspect: Aspect) -> None:
        self._aspects.append(aspect)

    def fire_before_action(self, action: Any, rule: Any, res: Any) -> None:
        for a in self._aspects:
            if a.before_action:
                a.before_action(action, rule, res)

    def fire_after_action(self, action: Any, rule: Any, res: Any,
                          dt: float) -> None:
        for a in self._aspects:
            if a.after_action:
                a.after_action(action, rule, res, dt)

    def fire_before_url(self, res: Any, url: str) -> bool:
        """Return True if any aspect says to skip this URL."""
        for a in self._aspects:
            if a.before_url and a.before_url(res, url):
                return True
        return False

    def fire_after_url(self, res: Any, url: str, success: bool) -> None:
        for a in self._aspects:
            if a.after_url:
                a.after_url(res, url, success)

    def fire_before_resource(self, res: Any) -> bool:
        """Return True if any aspect says to skip this resource."""
        for a in self._aspects:
            if a.before_resource and a.before_resource(res):
                return True
        return False

    def fire_after_resource(self, res: Any) -> None:
        for a in self._aspects:
            if a.after_resource:
                a.after_resource(res)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_options(options: tuple[str, ...]) -> dict[str, str]:
    """Parse key=value option strings into a dict."""
    result = {}
    for opt in options:
        if "=" in opt:
            k, v = opt.split("=", 1)
            result[k] = _strip_quotes(v)
    return result


def _strip_quotes(s: str) -> str:
    """Strip surrounding double or single quotes from a value."""
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
        return s[1:-1]
    return s


def _parse_field_specs(specs: tuple[str, ...]) -> list[FieldSpec]:
    """Parse field=X extractor=Y locator=Z attr=A sequences."""
    fields = []
    current: dict[str, str] = {}
    for spec in specs:
        if "=" not in spec:
            continue
        k, v = spec.split("=", 1)
        if k == "field" and current.get("field"):
            attr = current.get("attr")
            fields.append(FieldSpec(
                name=current["field"],
                extractor=current.get("extractor", "text"),
                locator=_strip_quotes(current.get("locator", "")),
                attr=_strip_quotes(attr) if attr else None,
            ))
            current = {}
        current[k] = v
    if current.get("field"):
        attr = current.get("attr")
        fields.append(FieldSpec(
            name=current["field"],
            extractor=current.get("extractor", "text"),
            locator=_strip_quotes(current.get("locator", "")),
            attr=_strip_quotes(attr) if attr else None,
        ))
    return fields


def _parse_table_specs(specs: tuple[str, ...]) -> tuple[int, list[TableFieldSpec]]:
    """Parse header_row=N field=X header=Y sequences."""
    header_row = 0
    fields = []
    current: dict[str, str] = {}
    for spec in specs:
        if "=" not in spec:
            continue
        k, v = spec.split("=", 1)
        if k == "header_row":
            header_row = int(v)
            continue
        if k == "field" and current.get("field"):
            fields.append(TableFieldSpec(
                name=current["field"],
                header=_strip_quotes(current.get("header", current["field"])),
            ))
            current = {}
        current[k] = v
    if current.get("field"):
        fields.append(TableFieldSpec(
            name=current["field"],
            header=_strip_quotes(current.get("header", current["field"])),
        ))
    return header_row, fields


# ---------------------------------------------------------------------------
# Browser adapters — unified interface for RF-Browser and raw Playwright
# ---------------------------------------------------------------------------

def _get_rf_browser() -> Any:
    """Return the RF-Browser library instance directly (no wrapper)."""
    from robot.libraries.BuiltIn import BuiltIn
    return BuiltIn().get_library_instance('Browser')


class _BrowserAdapter:
    """Base browser adapter interface for the execution engine.

    Two implementations:
      - ``_PlaywrightAdapter``: fast, uses patchright/playwright (default)
      - ``_NodriverAdapter``: stealth, uses nodriver raw CDP (for anti-bot sites)

    Use ``_make_adapter(stealth=True/False)`` factory to create the right one.
    """

    class load_states:
        """Enum-like for wait_for_load_state — mirrors RF-Browser's PageLoadStates."""
        networkidle = "networkidle"
        domcontentloaded = "domcontentloaded"
        load = "load"

    def __init__(self) -> None:
        self._timeout = 30_000

    # -- lifecycle --
    def new_browser(self, *, headless: bool = True) -> None: ...
    def new_context(self, **kw: Any) -> None: ...
    def new_page(self, url: str = "about:blank") -> None: ...
    def close_context(self) -> None: ...
    def close_browser(self, _which: str = "ALL") -> None: ...

    # -- settings --
    def set_browser_timeout(self, timeout: str) -> None:
        self._timeout = int(timeout.replace("ms", "").strip())

    # -- navigation --
    def go_to(self, url: str, **_kw: Any) -> None: ...
    def get_url(self) -> str: ...

    # -- wait --
    def wait_for_load_state(self, state: Any, **_kw: Any) -> None: ...

    def wait_for_elements_state(self, selector: str, state: str = "attached",
                                timeout: Any = "10s", **_kw: Any) -> None: ...

    # -- element queries --
    def get_element_count(self, selector: str) -> int: ...
    def get_text(self, selector: str, **_kw: Any) -> str: ...
    def get_attribute(self, selector: str, attr: str, **_kw: Any) -> str | None: ...
    def get_property(self, selector: str, prop: str, **_kw: Any) -> Any: ...

    # -- interactions --
    def click(self, selector: str, **_kw: Any) -> None: ...
    def fill_text(self, selector: str, text: str, **_kw: Any) -> None: ...
    def select_options_by(self, selector: str, _attr: str, value: str,
                          **_kw: Any) -> None: ...
    def hover(self, selector: str, **_kw: Any) -> None: ...
    def focus(self, selector: str, **_kw: Any) -> None: ...
    def dblclick(self, selector: str, **_kw: Any) -> None: ...
    def press_keys(self, selector: str, *keys: str, **_kw: Any) -> None: ...
    def take_screenshot(self, *, filename: str = "screenshot.png",
                        **_kw: Any) -> None: ...
    def upload_file(self, selector: str, path: str, **_kw: Any) -> None: ...

    # -- JS evaluation --
    def evaluate_javascript(self, selector: str | None, *function: str,
                            arg: Any = None, all_elements: bool = False,
                            **_kw: Any) -> Any: ...

    @staticmethod
    def _parse_timeout(timeout: Any) -> int:
        """Parse timeout to milliseconds from various formats."""
        from datetime import timedelta
        if isinstance(timeout, timedelta):
            return int(timeout.total_seconds() * 1000)
        if isinstance(timeout, (int, float)):
            return int(timeout)
        s = str(timeout).strip()
        if s.endswith("ms"):
            return int(s[:-2])
        if s.endswith("s"):
            return int(s[:-1]) * 1000
        return int(s)


# ---------------------------------------------------------------------------
# Playwright adapter — fast, default engine
# ---------------------------------------------------------------------------

class _PlaywrightAdapter(_BrowserAdapter):
    """Playwright/patchright browser adapter.  Fast batch CDP, stealth patches
    via playwright-stealth init scripts."""

    _CHANNEL = "msedge"
    _ARGS = ["--disable-blink-features=AutomationControlled"]
    _DEFAULTS = {"locale": "en-US", "timezone_id": "America/New_York"}

    def __init__(self) -> None:
        super().__init__()
        self._pw: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._page: Any = None

    def new_browser(self, *, headless: bool = True) -> None:
        try:
            from patchright.sync_api import sync_playwright
        except ImportError:
            from playwright.sync_api import sync_playwright  # type: ignore[assignment]
        self._pw = sync_playwright().start()
        label = "patchright" if "patchright" in type(self._pw).__module__ else "playwright"
        logger.info(f"  Browser adapter: {label}")
        self._browser = self._pw.chromium.launch(
            headless=headless, channel=self._CHANNEL, args=self._ARGS,
        )

    def new_context(self, **kw: Any) -> None:
        from playwright_stealth import Stealth
        _remap = {"userAgent": "user_agent", "timezoneId": "timezone_id"}
        pw_kw = {_remap.get(k, k): v for k, v in kw.items()}
        merged = {**self._DEFAULTS, **pw_kw}
        self._context = self._browser.new_context(**merged)
        Stealth().apply_stealth_sync(self._context)

    def new_page(self, url: str = "about:blank") -> None:
        self._page = self._context.new_page()
        if url != "about:blank":
            self._page.goto(url, timeout=self._timeout)

    def close_context(self) -> None:
        if self._context:
            self._context.close()
        self._context = None
        self._page = None

    def close_browser(self, _which: str = "ALL") -> None:
        if self._browser:
            self._browser.close()
            self._browser = None
        if self._pw:
            self._pw.stop()
            self._pw = None

    def set_browser_timeout(self, timeout: str) -> None:
        super().set_browser_timeout(timeout)
        if self._page:
            self._page.set_default_timeout(self._timeout)

    def go_to(self, url: str, **_kw: Any) -> None:
        self._page.goto(url, timeout=self._timeout)

    def get_url(self) -> str:
        return self._page.url

    def wait_for_load_state(self, state: Any, **_kw: Any) -> None:
        s = state if isinstance(state, str) else str(state).split(".")[-1]
        self._page.wait_for_load_state(s, timeout=self._timeout)

    def wait_for_elements_state(self, selector: str, state: str = "attached",
                                timeout: Any = "10s", **_kw: Any) -> None:
        ms = self._parse_timeout(timeout)
        self._page.wait_for_selector(selector, state="attached", timeout=ms)

    def get_element_count(self, selector: str) -> int:
        return self._page.locator(selector).count()

    def get_text(self, selector: str, **_kw: Any) -> str:
        return self._page.locator(selector).first.inner_text()

    def get_attribute(self, selector: str, attr: str, **_kw: Any) -> str | None:
        return self._page.locator(selector).first.get_attribute(attr)

    def get_property(self, selector: str, prop: str, **_kw: Any) -> Any:
        return self._page.locator(selector).first.evaluate(f"el => el.{prop}")

    def click(self, selector: str, **_kw: Any) -> None:
        self._page.locator(selector).first.click(timeout=self._timeout)

    def fill_text(self, selector: str, text: str, **_kw: Any) -> None:
        self._page.locator(selector).first.fill(text, timeout=self._timeout)

    def select_options_by(self, selector: str, _attr: str, value: str,
                          **_kw: Any) -> None:
        self._page.locator(selector).first.select_option(value=value)

    def hover(self, selector: str, **_kw: Any) -> None:
        self._page.locator(selector).first.hover(timeout=self._timeout)

    def focus(self, selector: str, **_kw: Any) -> None:
        self._page.locator(selector).first.focus(timeout=self._timeout)

    def dblclick(self, selector: str, **_kw: Any) -> None:
        self._page.locator(selector).first.dblclick(timeout=self._timeout)

    def press_keys(self, selector: str, *keys: str, **_kw: Any) -> None:
        loc = self._page.locator(selector).first
        for key in keys:
            loc.press(key)

    def take_screenshot(self, *, filename: str = "screenshot.png",
                        **_kw: Any) -> None:
        self._page.screenshot(path=filename)

    def upload_file(self, selector: str, path: str, **_kw: Any) -> None:
        self._page.locator(selector).first.set_input_files(path)

    def evaluate_javascript(self, selector: str | None, *function: str,
                            arg: Any = None, all_elements: bool = False,
                            **_kw: Any) -> Any:
        script = " ".join(function)
        if selector and all_elements:
            return self._page.locator(selector).evaluate_all(script)
        elif selector:
            return self._page.locator(selector).first.evaluate(script)
        return self._page.evaluate(script)


# ---------------------------------------------------------------------------
# Nodriver adapter — stealth, for anti-bot sites (DataDome, etc.)
# ---------------------------------------------------------------------------

class _NodriverAdapter(_BrowserAdapter):
    """Nodriver (raw CDP) browser adapter.  Zero Playwright artifacts, invisible
    to DataDome/Cloudflare.  Async internally, sync bridge for RF."""

    _EDGE_BINS = [
        "/usr/bin/microsoft-edge",
        "/usr/bin/microsoft-edge-stable",
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
    ]

    def __init__(self) -> None:
        super().__init__()
        self._browser: Any = None
        self._page: Any = None
        self._loop: Any = None
        self._loop_thread: Any = None
        self._warmed_origins: set[str] = set()

    # -- async bridge -------------------------------------------------------

    def _run(self, coro: Any) -> Any:
        """Run an async coroutine synchronously via the dedicated loop."""
        import concurrent.futures
        future = concurrent.futures.Future()  # type: ignore[type-arg]

        async def _wrap() -> None:
            try:
                future.set_result(await coro)
            except Exception as exc:
                future.set_exception(exc)

        self._loop.call_soon_threadsafe(
            lambda: self._loop.create_task(_wrap()))
        return future.result(timeout=self._timeout / 1000 + 10)

    def _start_loop(self) -> None:
        import asyncio, threading
        self._loop = asyncio.new_event_loop()
        t = threading.Thread(
            target=lambda: (asyncio.set_event_loop(self._loop),
                            self._loop.run_forever()),
            daemon=True)
        t.start()
        self._loop_thread = t

    # -- Playwright >> selector translation ---------------------------------

    def _select(self, selector: str) -> Any:
        """Select first element, handling Playwright ``>>`` syntax."""
        import re
        parts = [p.strip() for p in selector.split(" >> ")] if " >> " in selector else [selector]
        current: Any = self._page
        for i, part in enumerate(parts):
            # text= selector
            m = re.match(r'^text[=]"?(.+?)"?$', part)
            if m:
                current = self._run(
                    (current if hasattr(current, 'find') else self._page)
                    .find(m.group(1), best_match=True))
                continue
            # nth=N
            m = re.match(r'^nth=(\d+)$', part)
            if m:
                idx = int(m.group(1))
                current = current[idx] if isinstance(current, list) and idx < len(current) else None
                continue
            # CSS
            if isinstance(current, list):
                current = current[0] if current else None
            if current is None:
                return None
            # If next part is nth, select all; otherwise select first
            if i + 1 < len(parts) and parts[i + 1].startswith("nth="):
                parent = self._page if i == 0 else current
                current = self._run(parent.select_all(part) if hasattr(parent, 'select_all')
                                    else parent.query_selector_all(part)) or []
            else:
                current = self._run(current.query_selector(part) if hasattr(current, 'query_selector')
                                    else current.select(part))
            if current is None:
                return None
        return current if not isinstance(current, list) else (current[0] if current else None)

    def _eval(self, script: str) -> Any:
        """Evaluate JS, auto-wrapping function expressions as IIFE."""
        s = script.strip()
        if s.startswith("() =>") or s.startswith("function"):
            s = f"({s})()"
        result = self._run(self._page.evaluate(s))
        if hasattr(result, 'exception_id'):
            import sys
            print(f"[nodriver eval ERROR] {result}", file=sys.stderr)
            return 0 if 'length' in script else None
        return self._unwrap_cdp(result)

    @staticmethod
    def _unwrap_cdp(val: Any) -> Any:
        """Unwrap CDP RemoteObject typed wrappers to plain Python values."""
        if isinstance(val, dict):
            if "type" in val and "value" in val and len(val) <= 3:
                return _NodriverAdapter._unwrap_cdp(val["value"])
            return {k: _NodriverAdapter._unwrap_cdp(v) for k, v in val.items()}
        if isinstance(val, list):
            return [_NodriverAdapter._unwrap_cdp(v) for v in val]
        return val

    # -- lifecycle ----------------------------------------------------------

    @classmethod
    def _find_edge_binary(cls) -> str | None:
        import shutil
        for c in cls._EDGE_BINS:
            if shutil.which(c) or os.path.isfile(c):
                return c
        return None

    def new_browser(self, *, headless: bool = True) -> None:
        import nodriver
        edge_bin = self._find_edge_binary()
        if not edge_bin:
            raise RuntimeError("Nodriver stealth mode requires Edge or Chrome")
        self._start_loop()

        async def _launch() -> Any:
            return await nodriver.start(headless=headless,
                                        browser_executable_path=edge_bin)

        self._browser = self._run(_launch())
        logger.info(f"  Stealth adapter: nodriver ({os.path.basename(edge_bin)})")

    def new_context(self, **kw: Any) -> None:
        pass  # nodriver has no separate context

    def new_page(self, url: str = "about:blank") -> None:
        if url != "about:blank":
            self._page = self._run(self._browser.get(url))
        else:
            tabs = self._browser.tabs
            self._page = tabs[0] if tabs else self._run(self._browser.get("about:blank"))

    def close_context(self) -> None:
        self._page = None

    def close_browser(self, _which: str = "ALL") -> None:
        if self._browser:
            self._browser.stop()
            self._browser = None
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._loop = None

    # -- navigation ---------------------------------------------------------

    def go_to(self, url: str, **_kw: Any) -> None:
        from urllib.parse import urlparse
        origin = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
        if origin not in self._warmed_origins:
            self._warmed_origins.add(origin)
            logger.info(f"  Stealth warmup: visiting {origin}")
            self._page = self._run(self._browser.get(origin))
        self._page = self._run(self._browser.get(url))

    def get_url(self) -> str:
        return self._eval("window.location.href")

    # -- wait ---------------------------------------------------------------

    def wait_for_load_state(self, state: Any, **_kw: Any) -> None:
        pass  # nodriver auto-waits on navigation

    def wait_for_elements_state(self, selector: str, state: str = "attached",
                                timeout: Any = "10s", **_kw: Any) -> None:
        ms = self._parse_timeout(timeout)
        base_css = selector.split(" >> ")[0].strip()
        self._run(self._page.wait_for(base_css, timeout=ms / 1000))

    # -- element queries ----------------------------------------------------

    def get_element_count(self, selector: str) -> int:
        import json, re
        if " >> " not in selector:
            m = re.match(r'^text[=]"?(.+?)"?$', selector)
            if m:
                els = self._run(self._page.find_all(m.group(1)))
                return len(els) if els else 0
            return self._eval(
                f"document.querySelectorAll({json.dumps(selector)}).length")
        # Complex selector with >>
        parts = [p.strip() for p in selector.split(" >> ")]
        js_parts = []
        for p in parts:
            m = re.match(r'^nth=(\d+)$', p)
            if m:
                idx = int(m.group(1))
                if js_parts:
                    last = js_parts.pop()
                    js_parts.append(f"[{last}[{idx}]]")
            else:
                js_parts.append(f"document.querySelectorAll({json.dumps(p)})")
        if len(js_parts) == 1:
            return self._eval(f"{js_parts[0]}.length")
        parent_js = js_parts[0]
        for jp in js_parts[1:-1]:
            parent_js = jp.replace("document.querySelectorAll",
                                   f"{parent_js}[0].querySelectorAll")
        child_css = parts[-1]
        m = re.match(r'^nth=(\d+)$', child_css)
        if m:
            total = self._eval(f"{parent_js}.length")
            return 1 if int(m.group(1)) < (total or 0) else 0
        return self._eval(
            f"({parent_js}[0]||document).querySelectorAll("
            f"{json.dumps(child_css)}).length") or 0

    def get_text(self, selector: str, **_kw: Any) -> str:
        el = self._select(selector)
        return el.text if el else ""

    def get_attribute(self, selector: str, attr: str, **_kw: Any) -> str | None:
        el = self._select(selector)
        return el.attrs.get(attr) if el else None

    def get_property(self, selector: str, prop: str, **_kw: Any) -> Any:
        import json
        css = selector.split(" >> ")[0]
        return self._eval(f"(document.querySelector({json.dumps(css)})||{{}}).{prop}")

    # -- interactions -------------------------------------------------------

    def click(self, selector: str, **_kw: Any) -> None:
        el = self._select(selector)
        if el:
            self._run(el.click())

    def fill_text(self, selector: str, text: str, **_kw: Any) -> None:
        el = self._select(selector)
        if el:
            self._run(el.clear_input())
            self._run(el.send_keys(text))

    def select_options_by(self, selector: str, _attr: str, value: str,
                          **_kw: Any) -> None:
        el = self._select(selector)
        if el:
            self._run(el.select_option(value))

    def hover(self, selector: str, **_kw: Any) -> None:
        el = self._select(selector)
        if el:
            self._run(el.mouse_move())

    def focus(self, selector: str, **_kw: Any) -> None:
        el = self._select(selector)
        if el:
            self._run(el.click())

    def dblclick(self, selector: str, **_kw: Any) -> None:
        el = self._select(selector)
        if el:
            self._run(el.click())
            self._run(el.click())

    def press_keys(self, selector: str, *keys: str, **_kw: Any) -> None:
        el = self._select(selector)
        if el:
            for key in keys:
                self._run(el.send_keys(key))

    def take_screenshot(self, *, filename: str = "screenshot.png",
                        **_kw: Any) -> None:
        self._run(self._page.save_screenshot(filename))

    def upload_file(self, selector: str, path: str, **_kw: Any) -> None:
        el = self._select(selector)
        if el:
            self._run(el.send_file(path))

    # -- JS evaluation ------------------------------------------------------

    def evaluate_javascript(self, selector: str | None, *function: str,
                            arg: Any = None, all_elements: bool = False,
                            **_kw: Any) -> Any:
        import json as _json, re as _re
        script = " ".join(function)
        css, nth = selector, None
        if selector and " >> " in selector:
            parts = [p.strip() for p in selector.split(" >> ")]
            css = parts[0]
            m = _re.match(r'^nth=(\d+)$', parts[-1]) if len(parts) > 1 else None
            nth = int(m.group(1)) if m else None
        if selector and all_elements:
            el_js = f"document.querySelectorAll({_json.dumps(css)})"
            return self._eval(f"Array.from({el_js}).map(el => ({script})(el))")
        elif selector:
            target = (f"document.querySelectorAll({_json.dumps(css)})[{nth}]"
                      if nth is not None
                      else f"document.querySelector({_json.dumps(css)})")
            return self._eval(f"({script})({target})")
        return self._eval(script)


# Keep backward-compatible name used by the engine
_StealthAdapter = _NodriverAdapter


def _make_adapter(stealth: bool) -> _BrowserAdapter:
    """Factory: create the right browser adapter based on stealth flag."""
    if stealth:
        return _NodriverAdapter()
    return _PlaywrightAdapter()


class _StealthBrowserBridge:
    """Dynamic RF library that delegates Browser-compatible keywords to the
    stealth adapter.  Registered at runtime so that ``call_keyword`` blocks
    containing raw Browser calls (Click, Fill Text, …) resolve against the
    stealth adapter's live page instead of the RF-Browser library (which has
    no open page in stealth mode).

    Only the most commonly used Browser keywords are bridged.  Add more as
    needed.
    """

    ROBOT_LIBRARY_SCOPE = "GLOBAL"

    def __init__(self, adapter: _StealthAdapter) -> None:
        self._a = adapter

    # -- Navigation --
    def go_to(self, url: str, *_a: Any, **_kw: Any) -> None:
        self._a.go_to(url)

    def get_url(self, *_a: Any, **_kw: Any) -> str:
        return self._a.get_url()

    # -- Interaction --
    def click(self, selector: str, *_a: Any, **_kw: Any) -> None:
        self._a.click(selector)

    def fill_text(self, selector: str, text: str, *_a: Any, **_kw: Any) -> None:
        self._a.fill_text(selector, text)

    def type_text(self, selector: str, text: str, *_a: Any, **_kw: Any) -> None:
        self._a.fill_text(selector, text)

    def hover(self, selector: str, *_a: Any, **_kw: Any) -> None:
        self._a.hover(selector)

    def focus(self, selector: str, *_a: Any, **_kw: Any) -> None:
        self._a.focus(selector)

    def press_keys(self, selector: str, *keys: str, **_kw: Any) -> None:
        self._a.press_keys(selector, *keys)

    def check_checkbox(self, selector: str, *_a: Any, **_kw: Any) -> None:
        self._a.click(selector)

    def select_options_by(self, selector: str, attr: str, *values: str,
                          **_kw: Any) -> None:
        self._a.select_options_by(selector, attr, values[0] if values else "")

    # -- Queries --
    def get_text(self, selector: str, *_a: Any, **_kw: Any) -> str:
        return self._a.get_text(selector)

    def get_attribute(self, selector: str, attr: str, *_a: Any,
                      **_kw: Any) -> str | None:
        return self._a.get_attribute(selector, attr)

    def get_element_count(self, selector: str, *_a: Any, **_kw: Any) -> int:
        return self._a.get_element_count(selector)

    # -- Wait --
    def wait_for_elements_state(self, selector: str, state: str = "attached",
                                timeout: str = "10s", **_kw: Any) -> None:
        self._a.wait_for_elements_state(selector, state, timeout)

    # -- Screenshot --
    def take_screenshot(self, *_a: Any, filename: str = "screenshot.png",
                        **_kw: Any) -> None:
        self._a.take_screenshot(filename=filename)

    # -- JS --
    def evaluate_javascript(self, selector: str | None, *function: str,
                            **_kw: Any) -> Any:
        return self._a.evaluate_javascript(selector, *function, **_kw)

    def get_keyword_names(self) -> list[str]:
        """Dynamic library interface — return all bridged keywords."""
        return [
            "Go To", "Get Url",
            "Click", "Fill Text", "Type Text", "Hover", "Focus",
            "Press Keys", "Check Checkbox", "Select Options By",
            "Get Text", "Get Attribute", "Get Element Count",
            "Wait For Elements State",
            "Take Screenshot", "Evaluate JavaScript",
        ]

    def run_keyword(self, name: str, args: list, kwargs: dict | None = None) -> Any:
        """Dynamic library interface — dispatch keyword by name.

        RF-Browser registers keywords with snake_case names internally
        (e.g. ``fill_text``), but the display names are Title Case
        (``Fill Text``).  The DynamicKeyword handler passes the original
        snake_case ``_orig_name`` to ``run_keyword``, so we must handle both
        forms.
        """
        method_map = {
            # Title Case (display names)
            "Go To": self.go_to,
            "Get Url": self.get_url,
            "Click": self.click,
            "Fill Text": self.fill_text,
            "Type Text": self.type_text,
            "Hover": self.hover,
            "Focus": self.focus,
            "Press Keys": self.press_keys,
            "Check Checkbox": self.check_checkbox,
            "Select Options By": self.select_options_by,
            "Get Text": self.get_text,
            "Get Attribute": self.get_attribute,
            "Get Element Count": self.get_element_count,
            "Wait For Elements State": self.wait_for_elements_state,
            "Take Screenshot": self.take_screenshot,
            "Evaluate JavaScript": self.evaluate_javascript,
            # snake_case (RF-Browser internal _orig_name)
            "go_to": self.go_to,
            "get_url": self.get_url,
            "click": self.click,
            "fill_text": self.fill_text,
            "type_text": self.type_text,
            "hover": self.hover,
            "focus": self.focus,
            "press_keys": self.press_keys,
            "check_checkbox": self.check_checkbox,
            "select_options_by": self.select_options_by,
            "get_text": self.get_text,
            "get_attribute": self.get_attribute,
            "get_element_count": self.get_element_count,
            "wait_for_elements_state": self.wait_for_elements_state,
            "take_screenshot": self.take_screenshot,
            "evaluate_javascript": self.evaluate_javascript,
        }
        method: Any = method_map.get(name)
        if method is not None:
            return method(*args, **(kwargs or {}))
        raise RuntimeError(f"StealthBrowserBridge: keyword '{name}' not bridged")

    def get_keyword_arguments(self, name: str) -> list[str]:
        """Return argument spec for each keyword (required for RF dynamic API)."""
        specs: dict[str, list[str]] = {
            "Go To": ["url", "*args", "**kwargs"],
            "Get Url": [],
            "Click": ["selector", "*args", "**kwargs"],
            "Fill Text": ["selector", "text", "*args", "**kwargs"],
            "Type Text": ["selector", "text", "*args", "**kwargs"],
            "Hover": ["selector", "*args", "**kwargs"],
            "Focus": ["selector", "*args", "**kwargs"],
            "Press Keys": ["selector", "*keys"],
            "Check Checkbox": ["selector", "*args", "**kwargs"],
            "Select Options By": ["selector", "attr", "*values", "**kwargs"],
            "Get Text": ["selector", "*args", "**kwargs"],
            "Get Attribute": ["selector", "attr", "*args", "**kwargs"],
            "Get Element Count": ["selector", "*args", "**kwargs"],
            "Wait For Elements State": ["selector", "state=attached", "timeout=10s", "**kwargs"],
            "Take Screenshot": ["*args", "filename=screenshot.png", "**kwargs"],
            "Evaluate JavaScript": ["selector", "*function", "**kwargs"],
        }
        return specs.get(name, ["*args", "**kwargs"])


# ---------------------------------------------------------------------------
# Execution engine (uses robotframework-browser)
# ---------------------------------------------------------------------------

class ExecutionEngine:
    """Walks the rule tree using a browser adapter.

    The adapter is either ``_RFBrowserAdapter`` (default — wraps
    robotframework-browser) or ``_StealthAdapter`` (raw Playwright with
    playwright-stealth init scripts for sites with bot detection).
    """

    def __init__(self, ctx: DeploymentContext, headed: bool = False,
                 stealth: bool = False):
        self.ctx = ctx
        self.headed = headed
        self.stealth = stealth
        self._adapter: Any = None
        self._timing = os.environ.get(
            "WISE_RPA_TIMING", "1"
        ).lower() not in ("0", "false", "no")
        self._max_run_time = int(os.environ.get("WISE_RPA_TIMEOUT", "120"))
        self._run_start: float = 0
        self._timed_out = False
        self._stealth_bridge: _StealthBrowserBridge | None = None
        self._aspects = AspectRegistry()
        # Slow-motion mode: pause between actions for debugging/demos
        _slow_raw = os.environ.get("WISE_RPA_SLOW", "")
        self._slow_ms = int(_slow_raw) if _slow_raw.isdigit() else 0
        self._slow_screenshot = os.environ.get(
            "WISE_RPA_SLOW_SCREENSHOT", ""
        ).lower() in ("1", "true", "yes")

    def _bl(self) -> Any:
        """Lazy-init the browser backend.

        Uses ``_make_adapter()`` to create the right adapter:
          - stealth=True  → ``_NodriverAdapter`` (raw CDP, anti-bot)
          - stealth=False → ``_PlaywrightAdapter`` (fast patchright)

        When stealth is off AND RF-Browser library is available, uses that
        directly for maximum compatibility with ``call_keyword`` blocks.
        """
        if self._adapter is None:
            if self.stealth:
                self._adapter = _make_adapter(stealth=True)
                self._PageLoadStates = self._adapter.load_states
                self._stealth_bridge = _StealthBrowserBridge(self._adapter)
            else:
                # Try RF-Browser first (full Playwright, fastest)
                try:
                    self._adapter = _get_rf_browser()
                    from Browser.utils.data_types import PageLoadStates
                    self._PageLoadStates = PageLoadStates
                except Exception:
                    # Fallback to our own Playwright adapter
                    self._adapter = _make_adapter(stealth=False)
                    self._PageLoadStates = self._adapter.load_states
                    self._stealth_bridge = _StealthBrowserBridge(self._adapter)
        return self._adapter

    def run(self, resume_mode: str = "auto") -> None:
        output_dir = self.ctx.output_dir or f"output/{self.ctx.name}"
        os.makedirs(output_dir, exist_ok=True)

        # --- Init persistent store + aspects --------------------------------
        cp_path = Path(output_dir) / "_checkpoint.json"
        if resume_mode == "fresh" and cp_path.exists():
            cp_path.unlink()
            cp_path = None  # type: ignore[assignment]
        elif resume_mode == "require" and not cp_path.exists():
            raise RuntimeError(
                f"--resume specified but no checkpoint found in {output_dir}")
        elif resume_mode == "auto" and not cp_path.exists():
            pass  # no checkpoint to load, fresh run

        store = PersistentArtifactStore(
            cp_path if resume_mode != "fresh" else None
        )
        self.ctx.artifact_store = store  # type: ignore[assignment]

        # Register aspects
        self._setup_aspects(store)

        self._run_start = time.time()

        bl = self._bl()
        browser_opts: dict[str, Any] = {"headless": not self.headed}
        bl.new_browser(**browser_opts)

        try:
            self._execute_resources()
            self._write_outputs(output_dir)
            self._check_quality_gates()
            if not self._timed_out:
                store.delete_checkpoint()
            else:
                logger.info("  [CHECKPOINT] kept — run was cut short by timeout")
        finally:
            bl.close_browser("ALL")

    def _setup_aspects(self, store: PersistentArtifactStore) -> None:
        """Register all cross-cutting aspects."""
        # Instrumentation
        if self._timing:
            self._aspects.register(Aspect(
                name="instrument",
                after_action=lambda a, r, res, dt: (
                    logger.warn(f"  [INSTRUMENT] {a.type}"
                                f"({(a.locator or a.value or '')[:40]}) "
                                f"SLOW {dt:.2f}s")
                    if dt > 0.5 else None
                ),
            ))

        # Slow-motion
        if self._slow_ms:
            slow_ms = self._slow_ms
            slow_ss = self._slow_screenshot
            def _slow_after(action: Any, rule: Any, res: Any, dt: float) -> None:
                time.sleep(slow_ms / 1000.0)
                if slow_ss:
                    try:
                        bl = self._bl()
                        bl.take_screenshot(
                            filename=f"slow_{getattr(res, 'name', 'x')}_"
                                     f"{action.type}.png")
                    except Exception:
                        pass
            self._aspects.register(Aspect(name="slow_motion",
                                         after_action=_slow_after))

        # Checkpoint
        self._aspects.register(Aspect(
            name="checkpoint",
            before_resource=lambda res: store.is_resource_complete(res.name),
            after_resource=lambda res: store.mark_resource_complete(res.name),
            before_url=lambda res, url: store.is_url_processed(res.name, url),
            after_url=lambda res, url, ok: (
                store.commit_url() if ok else store.rollback_url()
            ),
        ))

    def _execute_resources(self) -> None:
        """Execute resources in topological order based on artifact dependencies.

        Ordering is derived from artifact ``consumes`` declarations:
        if resource B's output artifacts consume artifact X, and resource A
        emits to X, then A must run before B.
        """
        resources = self.ctx.resources
        if not resources:
            return

        # Build: artifact_name → resource that emits to it
        art_producer: dict[str, str] = {}
        for res in resources:
            for rule in res.rules.values():
                for target in rule.emit_targets:
                    art_producer[target] = res.name

        # Build dependency edges using _find_consumed_artifact
        in_degree: dict[str, int] = {r.name: 0 for r in resources}
        dependents: dict[str, list[str]] = {r.name: [] for r in resources}

        for res in resources:
            consumed_art = self._find_consumed_artifact(res)
            if consumed_art:
                producer = art_producer.get(consumed_art)
                if producer and producer != res.name:
                    dependents[producer].append(res.name)
                    in_degree[res.name] += 1

        # Kahn's algorithm
        queue = [r.name for r in resources if in_degree[r.name] == 0]
        ordered: list[str] = []

        while queue:
            name = queue.pop(0)
            ordered.append(name)
            for dep in dependents.get(name, []):
                in_degree[dep] -= 1
                if in_degree[dep] == 0:
                    queue.append(dep)

        if len(ordered) != len(resources):
            cycle_members = [r.name for r in resources if r.name not in ordered]
            raise RuntimeError(
                f"Cycle detected in resource dependencies: {cycle_members}"
            )

        # Execute in topological order (aspect hooks handle checkpoint skip)
        res_by_name = {r.name: r for r in resources}
        for name in ordered:
            res = res_by_name[name]
            if self._aspects.fire_before_resource(res):
                logger.info(f"  [RESUME] skipping completed resource: {name}")
                continue
            self._execute_resource(res)
            if not self._timed_out:
                self._aspects.fire_after_resource(res)

    def _execute_resource(self, res: ResourceContext) -> None:
        logger.info(f"Executing resource: {res.name}")
        bl = self._bl()
        globals_ = res.globals_
        timeout = globals_.get("timeout_ms", "30000")
        page_delay = int(globals_.get("page_load_delay_ms", 0))

        ctx_opts: dict[str, Any] = {}
        user_agent = globals_.get("user_agent")
        if user_agent:
            ctx_opts["userAgent"] = user_agent

        bl.new_context(**ctx_opts)
        bl.set_browser_timeout(f"{timeout}ms")

        bl.new_page()

        store = self.ctx.artifact_store

        try:
            # Run state setup (auth, consent) if configured
            self._run_setup(bl)

            entry_urls = self._resolve_entry_urls(res)

            for entry_url in entry_urls:
                # Global timeout guard
                if self._run_start and (time.time() - self._run_start) > self._max_run_time:
                    logger.warn(f"  Global timeout ({self._max_run_time}s) — "
                                "checkpoint preserved for resume")
                    self._timed_out = True
                    break

                # Aspect hooks: checkpoint skip if already processed
                if self._aspects.fire_before_url(res, entry_url):
                    logger.info(f"  [RESUME] skipping: {entry_url[:80]}")
                    continue

                # Begin staging — records go to buffer, not committed store
                if isinstance(store, PersistentArtifactStore):
                    store.begin_url(res.name, entry_url)

                success = True
                logger.info(f"  Navigating to: {entry_url}")
                try:
                    bl.go_to(entry_url)
                    self._wait_page_ready(bl, page_delay)

                    root_rules = self._build_rule_tree(res)
                    executed: set[str] = set()
                    for root in root_rules:
                        self._walk_rule(root, res, None, entry_url,
                                        executed=executed)
                except Exception as e:
                    logger.warn(f"  URL failed: {e}")
                    success = False

                # Aspect hooks: commit staging on success, rollback on failure
                self._aspects.fire_after_url(res, entry_url, success)
        finally:
            bl.close_context()

    def _find_consumed_artifact(self, res: ResourceContext) -> str | None:
        """Find which artifact this resource consumes.

        The source of truth is the artifact-level ``consumes`` declaration.
        A resource consumes artifact X if any artifact it emits to declares
        ``consumes=X``. Falls back to ``res.consumes`` if set explicitly.
        """
        # Explicit resource-level consumes (legacy, from 'Given I consume artifact')
        if res.consumes:
            return res.consumes

        # Derive from artifact declarations: find artifacts this resource emits to
        res_emit_targets: set[str] = set()
        for rule in res.rules.values():
            res_emit_targets.update(rule.emit_targets)

        # Which of those artifacts declares consumes?
        for art_name, art in self.ctx.artifacts.items():
            if art.consumes and art_name in res_emit_targets:
                return art.consumes

        return None

    def _resolve_entry_urls(self, res: ResourceContext) -> list[str]:
        """Resolve entry URLs.

        Three cases:
        1. Static URL (no template) → [url]
        2. Template URL with {field} → expand per consumed artifact record
        3. No URL but resource consumes → iterate consumed records for URL fields
        """
        url = res.entry_url

        # Case 3: no entry URL, but consumes an artifact → iterate its records
        if not url:
            consumed_art = self._find_consumed_artifact(res)
            if consumed_art and consumed_art in self.ctx.artifact_store:
                records = self.ctx.artifact_store[consumed_art]
                urls = []
                for record in records:
                    data = record.get("data", {})
                    for v in data.values():
                        if isinstance(v, str) and v.startswith(("http://", "https://")):
                            urls.append(v)
                            break
                return urls
            return []

        # Case 2: template URL with {field} → expand per consumed record
        if "{" in url and "}" in url:
            consumed_art = self._find_consumed_artifact(res)
            if consumed_art and consumed_art in self.ctx.artifact_store:
                records = self.ctx.artifact_store[consumed_art]
                urls = []
                for record in records:
                    data = record.get("data", record)
                    rendered = url
                    for k, v in data.items():
                        rendered = rendered.replace("{" + k + "}", str(v))
                    # Also expand {artifacts.name.field} cross-refs
                    for art_name, art_records in self.ctx.artifact_store.items():
                        if art_records:
                            latest = art_records[-1].get("data", {})
                            for fk, fv in latest.items():
                                ref = "{artifacts." + art_name + "." + fk + "}"
                                rendered = rendered.replace(ref, str(fv))
                    if "{" not in rendered:
                        urls.append(rendered)
                return urls
            return []

        # Case 1: static URL
        return [url]

    @staticmethod
    def _resolve_node_order(nodes: list[RuleNode]) -> list[str]:
        """Topologically sort nodes using Kahn's algorithm.

        Edges come from two sources:
        1. Parent declarations — ``rule.parents`` lists predecessors.
        2. Artifact dependencies — a node that ``emit_targets`` an artifact
           must run before any node that consumes it (future: ``consumes``
           field on RuleNode).

        Raises ``ValueError`` on cycles.
        """
        if not nodes:
            return []

        node_names = {n.name for n in nodes}

        # adjacency list and in-degree map
        adj: dict[str, list[str]] = {n.name: [] for n in nodes}
        in_deg: dict[str, int] = {n.name: 0 for n in nodes}

        def _add_edge(src: str, dst: str) -> None:
            if src not in node_names or dst not in node_names:
                return
            if src == dst:
                raise ValueError(
                    f"Cycle detected in node dependencies: {src}"
                )
            if dst not in adj[src]:
                adj[src].append(dst)
                in_deg[dst] += 1

        # 1. Parent edges: parent → child
        for n in nodes:
            for parent in n.parents:
                _add_edge(parent, n.name)

        # 2. Artifact edges: emitter → consumer (placeholder for Task 2)
        # emitter: dict[str, str] = {}
        # for n in nodes:
        #     for target in n.emit_targets:
        #         emitter[target] = n.name

        # Kahn's algorithm — stable: preserves input order for ties
        queue = [n.name for n in nodes if in_deg[n.name] == 0]
        order: list[str] = []

        while queue:
            name = queue.pop(0)
            order.append(name)
            for nxt in adj[name]:
                in_deg[nxt] -= 1
                if in_deg[nxt] == 0:
                    queue.append(nxt)

        if len(order) != len(nodes):
            missing = [n.name for n in nodes if n.name not in set(order)]
            raise ValueError(
                f"Cycle detected in node dependencies: {', '.join(missing)}"
            )

        return order

    def _find_children(self, parent_name: str,
                       all_rules: dict[str, RuleNode]) -> list[RuleNode]:
        """Return children of *parent_name*, topologically sorted."""
        children = [r for r in all_rules.values()
                    if parent_name in r.parents]
        if not children:
            return []
        sorted_names = self._resolve_node_order(children)
        return [all_rules[n] for n in sorted_names]

    def _build_rule_tree(self, res: ResourceContext) -> list[RuleNode]:
        """Return root-level rules in topological order.

        Also populates ``rule.children`` on every node so that expansion
        methods can walk immediate children per element.
        """
        # Populate children on all nodes (topologically sorted)
        for rule in res.rules.values():
            rule.children = self._find_children(rule.name, res.rules)

        # Validate: full topo sort over all rules detects cycles early
        self._resolve_node_order(list(res.rules.values()))

        # Return roots in the order they appear in root_names
        roots = []
        for rname in res.root_names:
            if rname in res.rules:
                roots.append(res.rules[rname])
        return roots

    def _walk_rule(self, rule: RuleNode, res: ResourceContext,
                   scope: str | None, current_url: str, *,
                   executed: set[str] | None = None,
                   context: dict[str, Any] | None = None) -> list[dict]:
        """Walk a rule node. scope is a CSS selector string or None for page.

        *executed* tracks which nodes have already run so that multi-parent
        nodes execute only once (after all parents complete).
        *context* carries parent-extracted fields to children.
        """
        if executed is not None:
            if rule.name in executed:
                return []
            executed.add(rule.name)

        # Global timeout guard
        if self._run_start and (time.time() - self._run_start) > self._max_run_time:
            logger.warn(f"  Global timeout ({self._max_run_time}s) exceeded — aborting")
            return []

        t_rule = time.time() if self._timing else 0

        ctx = dict(context or {})
        records: list[dict] = []
        bl = self._bl()

        # on_enter hook
        if rule.options.get("on_enter") == "screenshot":
            try:
                bl.take_screenshot(filename=f"on_enter_{rule.name}.png")
            except Exception:
                pass

        # Guards (Type 1 preconditions) with retry
        t0 = time.time() if self._timing else 0
        guards_ok = self._check_guards(rule, current_url)
        if not guards_ok and rule.retry_max > 0:
            for attempt in range(1, rule.retry_max + 1):
                logger.info(f"  Retry {attempt}/{rule.retry_max} for rule '{rule.name}'")
                time.sleep(rule.retry_delay_ms / 1000.0)
                self._execute_steps(rule, res, context=ctx)
                guards_ok = self._check_guards(rule, current_url)
                if guards_ok:
                    break
        if self._timing:
            dt = time.time() - t0
            logger.warn(f"  [INSTRUMENT] {rule.name}: state_check={dt:.2f}s ok={guards_ok}")
        if not guards_ok:
            if rule.options.get("on_fail") == "screenshot":
                try:
                    bl.take_screenshot(filename=f"on_fail_{rule.name}_guard.png")
                except Exception:
                    pass
            if rule.guard_policy == "abort":
                logger.warn(f"  Guard FAILED for rule '{rule.name}' — ABORTING walk")
                raise RuntimeError(f"Guard failed (abort policy) for rule '{rule.name}'")
            logger.warn(f"  Guard FAILED for rule '{rule.name}' — skipping")
            return records

        # Steps (actions interleaved with Type 2 observation gates)
        # Apply per-rule timeout if declared
        rule_timeout_ms = rule.options.get("timeout_ms")
        rule_deadline = (time.time() + int(rule_timeout_ms) / 1000.0) if rule_timeout_ms else None

        t0 = time.time() if self._timing else 0
        n_actions = sum(1 for s in rule.steps if isinstance(s, Action))
        try:
            self._execute_steps(rule, res, context=ctx, deadline=rule_deadline)
        except TimeoutError:
            logger.warn(f"  Rule '{rule.name}' timed out after {rule_timeout_ms}ms")
            if rule.options.get("on_fail") == "screenshot":
                try:
                    bl.take_screenshot(filename=f"on_fail_{rule.name}_timeout.png")
                except Exception:
                    pass
        if self._timing:
            dt = time.time() - t0
            logger.warn(f"  [INSTRUMENT] {rule.name}: actions={dt:.2f}s ({n_actions} actions)")

        # Refresh current_url after actions (click may have navigated)
        try:
            current_url = self._bl().get_url()
        except Exception:
            pass

        t0 = time.time() if self._timing else 0
        if rule.expansion:
            records = self._handle_expansion(rule, res, current_url,
                                             executed=executed, context=ctx)
        else:
            record = self._extract_from_scope(rule, scope, current_url)

            # Merge extracted data into context for children
            if record and record.get("data"):
                ctx.update(record["data"])

            child_records: dict[str, list] = {}
            for child in rule.children:
                child_data = self._walk_rule(child, res, scope, current_url,
                                             executed=executed, context=ctx)
                if child_data:
                    child_records[child.name] = child_data

            if record or child_records:
                if record is None:
                    record = {}
                if child_records:
                    record["_children"] = child_records
                records.append(record)
        if self._timing:
            dt = time.time() - t0
            phase = "expansion" if rule.expansion else "extract+children"
            logger.warn(f"  [INSTRUMENT] {rule.name}: {phase}={dt:.2f}s records={len(records)}")

        for target in rule.emit_targets:
            self._emit_records(target, rule, records, current_url)

        if self._timing:
            dt_total = time.time() - t_rule
            if dt_total > 0.5:
                logger.warn(f"  [INSTRUMENT] {rule.name}: TOTAL={dt_total:.2f}s")

        return records

    def _check_guards(self, rule: RuleNode, current_url: str) -> bool:
        """Check Type 1 guards (preconditions). Returns False if any fail."""
        if not rule.guards:
            return True
        bl = self._bl()
        self._dismiss_interrupts(bl)
        try:
            actual_url = bl.get_url()
            if actual_url:
                current_url = actual_url
        except Exception:
            pass
        for check in rule.guards:
            if not self._eval_state_check(check, current_url, bl):
                return False
        return True

    def _eval_state_check(self, check: StateCheck, current_url: str,
                          bl: Any) -> bool:
        """Evaluate a single state check. Used by guards and inline observations."""
        if check.type == "url_matches":
            return check.pattern in current_url
        elif check.type == "url_contains":
            return check.pattern in current_url
        elif check.type == "url_not_contains":
            return check.pattern not in current_url
        elif check.type == "selector_exists":
            try:
                resolved = self._resolve_fallback_selector(check.pattern, None)
                count = bl.get_element_count(resolved)
                if count == 0:
                    self._dismiss_interrupts(bl)
                    bl.wait_for_elements_state(resolved, "attached", "10s")
                    count = bl.get_element_count(resolved)
                return count > 0
            except Exception:
                return False
        elif check.type == "table_headers":
            return True  # placeholder
        return True

    def _get_interrupt_selectors(self, rule: RuleNode) -> list[str]:
        """Return the effective interrupt selectors for a rule.

        Per-rule scoping: rule.interrupt_paused suppresses all dismiss.
        rule.interrupt_override replaces the global list.
        Otherwise inherit from ctx.interrupt_selectors.
        """
        if rule.interrupt_paused:
            return []
        if rule.interrupt_override is not None:
            return rule.interrupt_override
        return self.ctx.interrupt_selectors

    def _execute_steps(self, rule: RuleNode, res: ResourceContext,
                       context: dict[str, Any] | None = None,
                       deadline: float | None = None) -> None:
        """Execute steps: actions interleaved with Type 2 observation gates."""
        if not rule.steps:
            return
        bl = self._bl()
        interrupt_sels = self._get_interrupt_selectors(rule)
        for step in rule.steps:
            if deadline and time.time() > deadline:
                raise TimeoutError(f"Rule '{rule.name}' exceeded timeout")
            # Inline observation gate (Type 2 StateCheck between actions)
            if isinstance(step, StateCheck):
                try:
                    current_url = bl.get_url()
                except Exception:
                    current_url = ""
                ok = self._eval_state_check(step, current_url, bl)
                if not ok:
                    logger.warn(f"  Observation gate failed: {step.type}={step.pattern}")
                continue

            # Action
            action = step
            # Dismiss interrupts before each action — popups can appear
            # at any moment and block clicks/interactions
            if interrupt_sels:
                self._dismiss_interrupts_with(bl, interrupt_sels)
            try:
                self._do_action_instrumented(action, res, context=context)
            except Exception as e:
                # Recovery: dismiss interrupts and retry once — a popup
                # may have appeared between the dismiss and the action
                if interrupt_sels:
                    self._dismiss_interrupts_with(bl, interrupt_sels)
                    try:
                        self._do_action_instrumented(action, res, context=context)
                        continue
                    except Exception:
                        pass
                logger.warn(f"  Action {action.type} failed: {e}")

    def _do_action_instrumented(self, action: Action, res: ResourceContext,
                               context: dict[str, Any] | None = None) -> None:
        """AOP wrapper: fires aspect hooks around _do_action."""
        self._aspects.fire_before_action(action, None, res)
        t0 = time.time()
        try:
            self._do_action(action, res, context=context)
        except Exception:
            dt = time.time() - t0
            if self._timing:
                label = f"{action.type}({(action.locator or action.value or '')[:40]})"
                logger.warn(f"  [INSTRUMENT] {label} FAILED {dt:.2f}s")
            raise
        dt = time.time() - t0
        self._aspects.fire_after_action(action, None, res, dt)

    def _do_action(self, action: Action, res: ResourceContext,
                   context: dict[str, Any] | None = None) -> None:
        bl = self._bl()

        if action.type == "click":
            bl.click(action.locator)
        elif action.type == "type":
            bl.fill_text(action.locator, action.value or "")
        elif action.type == "select":
            bl.select_options_by(action.locator, "value", action.value or "")
        elif action.type == "scroll":
            bl.evaluate_javascript(None, "window.scrollBy(0, window.innerHeight)")
            try:
                bl.wait_for_load_state(self._PageLoadStates.networkidle, "5s")
            except Exception:
                pass
        elif action.type == "wait":
            bl.wait_for_load_state(self._PageLoadStates.networkidle)
        elif action.type == "wait_ms":
            # Legacy — prefer await= or split rules instead
            bl.wait_for_load_state(self._PageLoadStates.networkidle,
                                   f"{int(action.value or 5000)}ms")
        elif action.type == "open":
            bl.go_to(action.value or "")
            self._wait_page_ready(bl, int(res.globals_.get("page_load_delay_ms", 0)))
        elif action.type == "open_bound":
            # Resolve field value from context or artifact store
            field_name = action.value or ""
            url = ""
            if context and field_name in (context or {}):
                url = str(context[field_name])
            elif "." in field_name:
                # Try artifacts.name.field reference
                parts = field_name.split(".")
                if len(parts) >= 2:
                    art_name = parts[-2]
                    fld = parts[-1]
                    records = self.ctx.artifact_store.get(art_name, [])
                    if records:
                        url = str(records[-1].get("data", {}).get(fld, ""))
            if url:
                bl.go_to(url)
                self._wait_page_ready(bl, int(res.globals_.get("page_load_delay_ms", 0)))
        elif action.type == "hover":
            bl.hover(action.locator)
        elif action.type == "focus":
            bl.focus(action.locator)
        elif action.type == "dblclick":
            bl.dblclick(action.locator)
        elif action.type == "press_keys":
            bl.press_keys(action.locator, *action.args)
        elif action.type == "screenshot":
            bl.take_screenshot(filename=action.value or "screenshot.png")
        elif action.type == "upload":
            bl.upload_file(action.locator, action.value or "")
        elif action.type == "click_text":
            # Click by visible text. Tries Playwright's text selector first
            # (real mouse event), falls back to JS with MouseEvent dispatch.
            text = action.value or ""
            clicked = False
            # Try Playwright native text selector (exact then substring)
            for sel in [f'text="{text}"', f'text={text}']:
                try:
                    count = bl.get_element_count(sel)
                    if count > 0:
                        bl.click(sel)
                        clicked = True
                        break
                except Exception:
                    pass
            if not clicked:
                # JS fallback with proper MouseEvent
                script = (
                    f"() => {{ const t = {json.dumps(text)}; "
                    f"for (const el of document.querySelectorAll("
                    f"'button, a, [role=button], div[tabindex]')) {{ "
                    f"if (el.offsetParent !== null && el.textContent.includes(t)) "
                    f"{{ el.dispatchEvent(new MouseEvent('click', {{bubbles:true}})); "
                    f"return true; }} }} return false; }}"
                )
                result = bl.evaluate_javascript(None, script)
                if not result:
                    logger.warn(f"  click_text: no element found for '{text}'")
        elif action.type == "add_url_params":
            # Add query params to current URL and navigate
            params = action.value or ""
            script = (
                f"() => {{ const u = new URL(window.location.href); "
                f"new URLSearchParams('{params}').forEach((v, k) => "
                f"u.searchParams.set(k, v)); "
                f"window.location.href = u.toString(); }}"
            )
            try:
                bl.evaluate_javascript(None, script)
            except Exception:
                pass  # navigation may destroy context
            self._wait_page_ready(bl)
        elif action.type == "set_stepper":
            # Click a stepper button N times via JS to avoid Playwright's
            # stability checks — stepper buttons re-render after each click,
            # causing "element not stable" / "detached from DOM" errors.
            count = int(action.value or 0)
            locator = action.locator
            bl.wait_for_elements_state(locator, "attached", "5s")
            for _ in range(count):
                click_script = (
                    f"(() => {{ const el = document.querySelector({json.dumps(locator)}); "
                    f"if (el) {{ el.click(); return true; }} return false; }})()"
                )
                bl.evaluate_javascript(None, click_script)
                # Wait for re-render before next click
                bl.wait_for_elements_state(locator, "attached", "2s")
        elif action.type == "select_date":
            self._do_select_date(bl, action)
        elif action.type == "browser_step":
            # Passthrough: call any method on the browser library
            method_name = action.value or ""
            method = getattr(bl, method_name, None)
            if method and callable(method):
                logger.info(f"  Browser step: {method_name}({', '.join(str(a) for a in action.args)})")
                method(*action.args)
            else:
                logger.warn(f"  Browser step: method '{method_name}' not found")
        elif action.type == "evaluate_js":
            # Run JavaScript on the live page via the adapter
            script = action.value or ""
            logger.info(f"  Evaluate JS: {script[:80]}{'...' if len(script) > 80 else ''}")
            url_before = bl.get_url()
            try:
                bl.evaluate_javascript(None, script)
            except Exception as e:
                err = str(e).lower()
                if "navigation" in err or "context" in err or "destroyed" in err or "detached" in err:
                    logger.info("  Evaluate JS triggered navigation")
                else:
                    raise
            # Detect if JS triggered a page navigation
            try:
                bl.wait_for_load_state(self._PageLoadStates.domcontentloaded, "2s")
            except Exception:
                pass
            try:
                url_after = bl.get_url()
            except Exception:
                url_after = ""
            navigated = url_after != url_before
            if not navigated:
                # Also heuristic-detect navigation intent from script content
                nav_hints = ("location", "navigate", "href", "assign", "replace")
                navigated = any(h in script.lower() for h in nav_hints)
            if navigated:
                logger.info("  Waiting for page load after navigation...")
                self._wait_page_ready(bl)
        elif action.type == "call_keyword":
            # Invoke an arbitrary RF keyword by name (browser is live)
            kw_name = action.value or ""
            logger.info(f"  Call keyword: {kw_name}({', '.join(str(a) for a in action.args)})")
            if self.stealth and self._stealth_bridge:
                # In stealth mode, run the keyword through the bridge which
                # intercepts Browser library calls and routes them to the
                # stealth adapter's live page.
                self._run_keyword_with_bridge(kw_name, action.args)
            else:
                from robot.libraries.BuiltIn import BuiltIn
                BuiltIn().run_keyword(kw_name, *action.args)

        # ── Inline observation gate (await=<selector>) ───────────────
        # After any action, if await= is declared, wait for that selector
        # before the engine advances to the next action.  MDP: s,a→o
        # where 'await' is the expected observation 'o'.
        await_sel = action.options.get("await")
        if await_sel:
            resolved = self._resolve_fallback_selector(await_sel, None)
            bl.wait_for_elements_state(resolved, "attached", "10s")

    def _run_keyword_with_bridge(self, kw_name: str, args: tuple) -> None:
        """Run an RF keyword in stealth mode.

        Temporarily swaps the Browser library's ``_instance`` with the stealth
        bridge so that raw Browser calls (Click, Fill Text, etc.) inside the
        keyword resolve against the stealth adapter's live page.

        How it works (RF 7.x):
        - RF stores libraries in ``namespace._kw_store.libraries`` (OrderedDict
          keyed by name).  Each library is a ``TestLibrary`` whose ``.instance``
          property returns ``._instance``.
        - Dynamic keywords call ``self.owner.instance`` every time their
          ``.method`` property is accessed (not cached).  Swapping
          ``lib._instance`` therefore redirects all keyword dispatch for the
          duration of the call.

        Browser control hierarchy (3-layer stack):
          Layer 3: RF Keywords (Click, Fill Text, …) — user-facing
          Layer 2: Adapter interface (click(), fill_text(), …)
          Layer 1a: RFBrowserAdapter → RF-Browser lib → Playwright (via gRPC)
          Layer 1b: StealthAdapter   → Patchright (patched Playwright, in-proc)
        In stealth mode, Layer 1a has no page.  The bridge routes Layer 3
        keywords to Layer 1b so ``call_keyword`` blocks work correctly.
        """
        from robot.libraries.BuiltIn import BuiltIn
        bi = BuiltIn()
        bridge = self._stealth_bridge
        lib_entry = None

        # Locate the Browser TestLibrary in RF's keyword store.
        # RF 7.x: namespace._kw_store.libraries is an OrderedDict.
        try:
            ns = bi._namespace
            libs = ns._kw_store.libraries
            # Try direct key lookup first (most reliable)
            for key, lib in libs.items():
                if getattr(lib, 'name', '') == 'Browser':
                    lib_entry = lib
                    break
        except Exception as e:
            logger.warn(f"  Could not locate Browser library for stealth bridge: {e}")

        if lib_entry is None:
            # Fallback: run without bridge — keyword will use whatever is
            # registered, which may fail in stealth mode.
            logger.warn("  Browser library not found in keyword store — "
                        "running call_keyword without stealth bridge")
            bi.run_keyword(kw_name, *args)
            return

        # Swap and run
        original_instance = lib_entry._instance
        lib_entry._instance = bridge
        logger.info(f"  Stealth bridge: swapped Browser instance for '{kw_name}'")

        try:
            bi.run_keyword(kw_name, *args)
        finally:
            lib_entry._instance = original_instance
            logger.info(f"  Stealth bridge: restored Browser instance after '{kw_name}'")

    def _handle_expansion(self, rule: RuleNode, res: ResourceContext,
                          current_url: str, *,
                          executed: set[str] | None = None,
                          context: dict[str, Any] | None = None) -> list[dict]:
        exp = rule.expansion
        assert exp is not None  # caller guarantees
        if exp.over == "elements":
            return self._expand_elements(rule, res, current_url,
                                         executed=executed, context=context)
        elif exp.over == "pages_next":
            return self._expand_pages_next(rule, res, current_url,
                                           executed=executed, context=context)
        elif exp.over == "pages_numeric":
            return self._expand_pages_numeric(rule, res, current_url,
                                              executed=executed, context=context)
        elif exp.over == "combinations":
            return self._expand_combinations(rule, res, current_url,
                                             executed=executed, context=context)
        return []

    def _expand_elements(self, rule: RuleNode, res: ResourceContext,
                         current_url: str, *,
                         executed: set[str] | None = None,
                         context: dict[str, Any] | None = None) -> list[dict]:
        bl = self._bl()
        exp = rule.expansion
        assert exp is not None
        selector = exp.scope or ""
        limit = exp.limit
        use_bfs = (exp.order == "bfs")
        records: list[dict] = []

        # exclude_if: skip elements where a child selector matches (e.g. sponsored items)
        exclude_if = exp.options.get("exclude_if")

        try:
            # Wait for at least one element to appear (Playwright native wait)
            try:
                timeout_ms = int(res.globals_.get("timeout_ms", "30000"))
                bl.wait_for_elements_state(selector, "attached",
                                           timeout=f"{timeout_ms}ms")
            except Exception:
                pass  # May not appear — count will be 0
            count = bl.get_element_count(selector)
        except Exception as e:
            logger.warn(f"  Element expansion failed for '{selector}': {e}")
            return records

        count = count or 0
        if limit and count > limit:
            count = limit

        def _should_exclude(elem_sel: str) -> bool:
            """Check if element should be excluded based on exclude_if selector."""
            if not exclude_if:
                return False
            try:
                child_sel = f"{elem_sel} >> {exclude_if}"
                return bl.get_element_count(child_sel) > 0
            except Exception:
                return False

        # Fast path: batch extraction in a single JS call when possible
        # (leaf rule with field specs, no children, no exclude_if, no table)
        can_batch = (
            rule.field_specs
            and not rule.children
            and not exclude_if
            and not rule.table_spec
            and not rule.ai_extraction
        )
        if can_batch:
            records = self._batch_extract_from_elements(
                rule, selector, count, current_url
            )
            logger.info(f"    Batch extracted {len(records)} records")
            return records

        if use_bfs:
            # BFS: extract ALL elements first, then walk children per element
            extracted: list[tuple[str, dict | None, dict]] = []
            for i in range(count):
                elem_selector = f"{selector} >> nth={i}"
                if _should_exclude(elem_selector):
                    logger.info(f"  Excluded element {i} by exclude_if='{exclude_if}'")
                    continue
                record = self._extract_from_element(rule, elem_selector, current_url)
                elem_ctx = dict(context or {})
                if record and record.get("data"):
                    elem_ctx.update(record["data"])
                extracted.append((elem_selector, record, elem_ctx))

            for elem_selector, record, elem_ctx in extracted:
                child_records: dict[str, list] = {}
                for child in rule.children:
                    child_data = self._walk_rule(child, res, elem_selector,
                                                 current_url, executed=executed,
                                                 context=elem_ctx)
                    if child_data:
                        child_records[child.name] = child_data
                if record is not None:
                    if child_records:
                        record["_children"] = child_records
                    records.append(record)
        else:
            # DFS: process each element fully before next
            for i in range(count):
                elem_selector = f"{selector} >> nth={i}"
                if _should_exclude(elem_selector):
                    logger.info(f"  Excluded element {i} by exclude_if='{exclude_if}'")
                    continue
                record = self._extract_from_element(rule, elem_selector, current_url)

                elem_ctx = dict(context or {})
                if record and record.get("data"):
                    elem_ctx.update(record["data"])

                dfs_children: dict[str, list] = {}
                for child in rule.children:
                    child_data = self._walk_rule(child, res, elem_selector,
                                                 current_url, executed=executed,
                                                 context=elem_ctx)
                    if child_data:
                        dfs_children[child.name] = child_data

                if record is not None:
                    if dfs_children:
                        record["_children"] = dfs_children
                    records.append(record)

        return records

    def _expand_pages_next(self, rule: RuleNode, res: ResourceContext,
                           current_url: str, *,
                           executed: set[str] | None = None,
                           context: dict[str, Any] | None = None) -> list[dict]:
        bl = self._bl()
        exp = rule.expansion
        assert exp is not None
        next_selector = exp.locator
        limit = exp.limit
        records = []

        for page_num in range(limit):
            page_url = bl.get_url()
            logger.info(f"    Page {page_num + 1}: {page_url}")

            # Fresh executed set per page — children must re-run on each page
            page_executed: set[str] = set()
            for child in rule.children:
                child_data = self._walk_rule(child, res, None, page_url,
                                             executed=page_executed, context=context)
                if child_data:
                    records.extend(child_data)

            try:
                count = bl.get_element_count(next_selector)
                if count == 0:
                    logger.info("    No next button found, stopping pagination")
                    break

                # Grab a fingerprint of the current page content so we can
                # detect when the AJAX swap completes after clicking Next.
                # Use the child expansion scope as the staleness anchor.
                stale_scope = None
                for child in rule.children:
                    if child.expansion and child.expansion.over == "elements":
                        stale_scope = child.expansion.scope
                        break
                old_text = ""
                if stale_scope:
                    try:
                        old_text = bl.get_text(f"{stale_scope} >> nth=0")
                    except Exception:
                        pass

                bl.click(next_selector)

                # Wait for content swap: poll until the first element's text
                # changes (staleness) or networkidle, whichever comes first.
                page_delay = int(res.globals_.get("page_load_delay_ms", 500))
                timeout_s = max(page_delay, 5000) / 1000.0
                if stale_scope and old_text:
                    deadline = time.time() + timeout_s
                    while time.time() < deadline:
                        try:
                            new_text = bl.get_text(f"{stale_scope} >> nth=0")
                        except Exception:
                            new_text = ""
                        if new_text and new_text != old_text:
                            break
                        try:
                            bl.wait_for_load_state(
                                self._PageLoadStates.networkidle, "500ms")
                        except Exception:
                            pass
                else:
                    try:
                        bl.wait_for_load_state(
                            self._PageLoadStates.networkidle,
                            f"{max(page_delay, 5000)}ms")
                    except Exception:
                        pass

                self._dismiss_interrupts(bl)
            except Exception as e:
                logger.info(f"    Pagination ended: {e}")
                break

        return records

    def _expand_pages_numeric(self, rule: RuleNode, res: ResourceContext,
                              current_url: str, *,
                              executed: set[str] | None = None,
                              context: dict[str, Any] | None = None) -> list[dict]:
        bl = self._bl()
        exp = rule.expansion
        assert exp is not None
        control_selector = exp.locator
        start = exp.start
        limit = exp.limit
        records = []

        for page_num in range(start, start + limit):
            page_url = bl.get_url()
            logger.info(f"    Numeric page {page_num}: {page_url}")

            # Fresh executed set per page — children must re-run on each page
            page_executed: set[str] = set()
            for child in rule.children:
                child_data = self._walk_rule(child, res, None, page_url,
                                             executed=page_executed, context=context)
                if child_data:
                    records.extend(child_data)

            next_num = page_num + 1
            if next_num >= start + limit:
                break

            try:
                count = bl.get_element_count(control_selector)
                clicked = False
                for idx in range(count):
                    link_sel = f"{control_selector} >> nth={idx}"
                    try:
                        href = bl.get_attribute(link_sel, "href") or ""
                    except Exception:
                        href = ""
                    text = bl.get_text(link_sel)
                    if f"p={next_num}" in href or text.strip() == str(next_num):
                        bl.click(link_sel)
                        self._wait_page_ready(bl, int(res.globals_.get("page_load_delay_ms", 0)))
                        clicked = True
                        break
                if not clicked:
                    logger.info(f"    No link for page {next_num}, stopping")
                    break
            except Exception as e:
                logger.info(f"    Numeric pagination ended: {e}")
                break

        return records

    def _expand_combinations(self, rule: RuleNode, res: ResourceContext,
                             current_url: str, *,
                             executed: set[str] | None = None,
                             context: dict[str, Any] | None = None) -> list[dict]:
        """Cartesian product of combination axes."""
        bl = self._bl()
        exp = rule.expansion
        assert exp is not None
        axes = exp.axes
        if not axes:
            return []

        # Build value lists per axis (support "auto" discovery)
        axis_values: list[list[str]] = []
        for axis in axes:
            if axis.values == ["auto"]:
                # Discover values from the control element
                try:
                    if axis.action == "select":
                        vals = bl.evaluate_javascript(
                            axis.control,
                            "(el) => Array.from(el.options).map(o => o.value)"
                        )
                    else:
                        count = bl.get_element_count(axis.control)
                        vals = []
                        for i in range(count):
                            text = bl.get_text(f"{axis.control} >> nth={i}").strip()
                            if text:
                                vals.append(text)
                except Exception as e:
                    logger.warn(f"  Auto-discover failed for {axis.control}: {e}")
                    vals = []
            else:
                vals = list(axis.values)

            # Apply skip (drop first N values, e.g. placeholder "Select...")
            if axis.skip > 0:
                vals = vals[axis.skip:]

            # Apply exclude (drop values matching any exclude pattern)
            if axis.exclude:
                vals = [v for v in vals if v not in axis.exclude]

            # Auto-exclude empty strings for select dropdowns
            if axis.action == "select":
                vals = [v for v in vals if v]

            logger.info(f"  Axis {axis.control}: {len(vals)} values → {vals}")
            axis_values.append(vals)

            # Emit discovered values to artifact if configured
            if axis.emit and axis.emit in self.ctx.artifact_store:
                for v in vals:
                    self.ctx.artifact_store[axis.emit].append({
                        "node": rule.name,
                        "url": current_url,
                        "data": {"control": axis.control, "value": v},
                        "extracted_at": datetime.now(timezone.utc).isoformat(),
                    })
            elif axis.emit:
                # Auto-initialize if artifact was registered but store not yet created
                self.ctx.artifact_store[axis.emit] = []
                for v in vals:
                    self.ctx.artifact_store[axis.emit].append({
                        "node": rule.name,
                        "url": current_url,
                        "data": {"control": axis.control, "value": v},
                        "extracted_at": datetime.now(timezone.utc).isoformat(),
                    })

        # Cartesian product
        import itertools
        combos = list(itertools.product(*axis_values))
        records = []

        for combo in combos:
            # Apply each axis action
            for axis, value in zip(axes, combo):
                try:
                    if axis.action == "type":
                        bl.fill_text(axis.control, value)
                    elif axis.action == "select":
                        bl.select_options_by(axis.control, "value", value)
                    elif axis.action == "click":
                        # Find the matching element by text
                        count = bl.get_element_count(axis.control)
                        for i in range(count):
                            sel = f"{axis.control} >> nth={i}"
                            text = bl.get_text(sel).strip()
                            if text == value:
                                bl.click(sel)
                                break
                except Exception as e:
                    logger.warn(f"  Combo action {axis.action}={value} failed: {e}")

            # Wait for AJAX to settle after combo actions (tab clicks etc.)
            try:
                bl.wait_for_load_state(self._PageLoadStates.networkidle, "5s")
            except Exception:
                pass

            # Build per-combo context
            combo_ctx = dict(context or {})
            for axis, value in zip(axes, combo):
                combo_ctx[f"_combo_{axis.control}"] = value

            # Extract from this rule's own field specs (if any) for each combo
            if rule.field_specs:
                record = self._extract_from_scope(rule, None, current_url)
                if record:
                    records.append(record)

            # Walk children for this combination
            combo_executed: set[str] = set()
            for child in rule.children:
                child_data = self._walk_rule(child, res, None, current_url,
                                             executed=combo_executed, context=combo_ctx)
                if child_data:
                    records.extend(child_data)

        return records

    def _extract_from_scope(self, rule: RuleNode, scope: str | None,
                            current_url: str) -> dict | None:
        if not rule.field_specs and not rule.table_spec and not rule.ai_extraction:
            return None

        data = {}
        if rule.field_specs:
            for fs in rule.field_specs:
                data[fs.name] = self._extract_field(fs, scope)

        if rule.table_spec:
            table_result = self._extract_table_data(rule.table_spec, scope, current_url, rule.name)
            if table_result:
                data.update(table_result.get("data", {}))

        # Run post_extract hooks (before AI — e.g. to_markdown converts HTML first)
        data = self._invoke_hooks("post_extract", data)

        if rule.ai_extraction:
            ai_result = self._run_ai_extraction(rule.ai_extraction, data)
            if ai_result:
                data.update(ai_result)

        if not data:
            return None

        return {
            "node": rule.name,
            "url": current_url,
            "data": data,
            "extracted_at": datetime.now(timezone.utc).isoformat(),
        }

    def _batch_extract_from_elements(self, rule: RuleNode, scope_selector: str,
                                    count: int, current_url: str) -> list[dict]:
        """Extract all fields from all scope elements in ONE browser call.

        Builds a JS script that iterates over scope elements and extracts
        each field, returning the full data array. This replaces N×M
        individual browser round-trips with a single evaluate call.
        """
        if not rule.field_specs:
            return []

        bl = self._bl()

        # Build JS extraction logic for each field
        field_extractors = []
        for fs in rule.field_specs:
            loc = fs.locator
            if loc == ".":
                loc_js = "el"
            else:
                # Handle fallback selectors — pick first that resolves
                candidates = [c.strip() for c in loc.split(" | ")] if " | " in loc else [loc]
                loc_parts = " || ".join(
                    f"el.querySelector({json.dumps(c)})" for c in candidates
                )
                loc_js = f"({loc_parts})"

            if fs.extractor == "text":
                extract_js = f"(() => {{ const t = {loc_js}; return t ? t.textContent.trim() : ''; }})()"
            elif fs.extractor == "attr":
                extract_js = f"(() => {{ const t = {loc_js}; return t ? (t.getAttribute({json.dumps(fs.attr or '')}) || '') : ''; }})()"
            elif fs.extractor == "link":
                extract_js = (
                    f"(() => {{ const t = {loc_js}; if (!t) return ''; "
                    f"const h = t.getAttribute('href') || ''; "
                    f"if (!h || h.startsWith('http') || h.startsWith('javascript:')) return h; "
                    f"try {{ return new URL(h, window.location.href).href; }} catch {{ return h; }} }})()"
                )
            elif fs.extractor == "html":
                extract_js = f"(() => {{ const t = {loc_js}; return t ? t.innerHTML : ''; }})()"
            elif fs.extractor == "image":
                extract_js = f"(() => {{ const t = {loc_js}; return t ? (t.getAttribute('src') || '') : ''; }})()"
            elif fs.extractor == "number":
                extract_js = (
                    f"(() => {{ const t = {loc_js}; if (!t) return ''; "
                    f"const m = t.textContent.match(/[\\d.,]+/); return m ? m[0] : ''; }})()"
                )
            elif fs.extractor == "grouped":
                if loc == ".":
                    extract_js = "[el.textContent.trim()]"
                else:
                    first_candidate = candidates[0] if " | " in fs.locator else fs.locator
                    extract_js = (
                        f"Array.from(el.querySelectorAll({json.dumps(first_candidate)}))"
                        f".map(e => e.textContent.trim()).filter(Boolean)"
                    )
            else:
                extract_js = "''"

            field_extractors.append((fs.name, extract_js))

        # Build the batch script
        field_lines = ",\n".join(
            f"        {json.dumps(name)}: {expr}" for name, expr in field_extractors
        )
        script = (
            f"() => {{\n"
            f"  const els = document.querySelectorAll({json.dumps(scope_selector)});\n"
            f"  const results = [];\n"
            f"  const limit = {count};\n"
            f"  for (let i = 0; i < Math.min(els.length, limit); i++) {{\n"
            f"    const el = els[i];\n"
            f"    results.push({{\n{field_lines}\n    }});\n"
            f"  }}\n"
            f"  return results;\n"
            f"}}"
        )

        try:
            raw_data = bl.evaluate_javascript(None, script)
        except Exception as e:
            logger.warn(f"  Batch extraction failed: {e}")
            return []

        if not raw_data or not isinstance(raw_data, list):
            return []

        now_iso = datetime.now(timezone.utc).isoformat()
        records = []
        for item in raw_data:
            if not item:
                continue
            data = self._invoke_hooks("post_extract", item)
            records.append({
                "node": rule.name,
                "url": current_url,
                "data": data,
                "extracted_at": now_iso,
            })
        return records

    def _extract_from_element(self, rule: RuleNode, elem_selector: str,
                              current_url: str) -> dict | None:
        data = {}
        if rule.field_specs:
            for fs in rule.field_specs:
                data[fs.name] = self._extract_field(fs, elem_selector)
        elif rule.table_spec:
            table_result = self._extract_table_data(rule.table_spec, elem_selector, current_url, rule.name)
            if table_result:
                data = table_result.get("data", {})

        if not data:
            return None

        # Run post_extract hooks
        data = self._invoke_hooks("post_extract", data)

        return {
            "node": rule.name,
            "url": current_url,
            "data": data,
            "extracted_at": datetime.now(timezone.utc).isoformat(),
        }

    def _resolve_fallback_selector(self, raw: str, scope: str | None) -> str:
        """If *raw* contains `` | `` (space-pipe-space), try each candidate
        in order and return the first one that matches elements on the page.
        If none match, return the first candidate (preserving error messages).
        Plain selectors (no pipe) pass through with zero overhead.
        """
        if " | " not in raw:
            return f"{scope} >> {raw}" if scope else raw

        bl = self._bl()
        candidates = [c.strip() for c in raw.split(" | ")]
        total = len(candidates)
        for idx, candidate in enumerate(candidates):
            sel = f"{scope} >> {candidate}" if scope else candidate
            try:
                if bl.get_element_count(sel) > 0:
                    logger.info(f"  Fallback selector: using '{candidate}' (option {idx+1}/{total})")
                    return sel
            except Exception:
                pass  # candidate may be invalid CSS — skip to next
        # None matched — fall back to first candidate
        first = candidates[0]
        return f"{scope} >> {first}" if scope else first

    def _extract_field(self, fs: FieldSpec, scope: str | None) -> Any:
        bl = self._bl()
        try:
            # "." means self/current element — use scope directly
            if fs.locator == "." and scope:
                selector = scope
            elif fs.locator == "." and not scope:
                # No scope + self-selector → page-level; return URL for link, else ""
                if fs.extractor == "link":
                    return bl.get_url()
                return ""
            else:
                selector = self._resolve_fallback_selector(fs.locator, scope)

            if fs.extractor == "text":
                count = bl.get_element_count(selector)
                if count == 0:
                    return ""
                return bl.get_text(selector).strip()
            elif fs.extractor == "attr":
                count = bl.get_element_count(selector)
                if count == 0 or not fs.attr:
                    return ""
                return (bl.get_attribute(selector, fs.attr) or "").strip()
            elif fs.extractor == "grouped":
                count = bl.get_element_count(selector)
                results = []
                for i in range(count):
                    text = bl.get_text(f"{selector} >> nth={i}").strip()
                    if text:
                        results.append(text)
                return results
            elif fs.extractor == "html":
                count = bl.get_element_count(selector)
                if count == 0:
                    return ""
                return bl.get_property(selector, "innerHTML") or ""
            elif fs.extractor == "link":
                count = bl.get_element_count(selector)
                if count == 0:
                    return ""
                href = bl.get_attribute(selector, "href") or ""
                # Always resolve to absolute URL
                if href and not href.startswith(("http://", "https://", "javascript:")):
                    from urllib.parse import urljoin
                    try:
                        page_url = bl.get_url()
                        href = urljoin(page_url, href)
                    except Exception:
                        pass
                return href
            elif fs.extractor == "image":
                count = bl.get_element_count(selector)
                if count == 0:
                    return ""
                return bl.get_attribute(selector, "src") or ""
            elif fs.extractor == "number":
                count = bl.get_element_count(selector)
                if count == 0:
                    return ""
                text = bl.get_text(selector).strip()
                # Extract numeric value, handling commas and whitespace
                cleaned = re.sub(r"[^\d.\-]", "", text)
                try:
                    return float(cleaned) if "." in cleaned else int(cleaned)
                except (ValueError, TypeError):
                    return text
        except Exception as e:
            logger.warn(f"    Extract {fs.name} failed: {e}")
        return ""

    def _extract_table_data(self, tspec: TableSpec, scope: str | None,
                            current_url: str, rule_name: str) -> dict | None:
        bl = self._bl()
        try:
            table_sel = f"{scope} >> {tspec.locator}" if scope else tspec.locator
            count = bl.get_element_count(table_sel)
            if count == 0:
                # Try scope itself as table
                if scope:
                    table_sel = scope
                    count = bl.get_element_count(table_sel)
                if count == 0:
                    return None

            # Get all rows via JS for efficiency
            rows_data = bl.evaluate_javascript(
                table_sel,
                """(table) => {
                    const rows = table.querySelectorAll('tr');
                    return Array.from(rows).map(row => {
                        const cells = row.querySelectorAll('th, td');
                        return Array.from(cells).map(c => c.textContent.trim());
                    });
                }"""
            )

            if not rows_data or len(rows_data) <= tspec.header_row:
                return None

            headers = rows_data[tspec.header_row]

            field_map: dict[str, int] = {}
            for fs in tspec.fields:
                for i, h in enumerate(headers):
                    if h == fs.header:
                        field_map[fs.name] = i
                        break

            table_records = []
            for row_cells in rows_data[tspec.header_row + 1:]:
                if not any(row_cells):
                    continue
                rec = {}
                for fname, idx in field_map.items():
                    rec[fname] = row_cells[idx] if idx < len(row_cells) else ""
                table_records.append(rec)

            return {
                "node": rule_name,
                "url": current_url,
                "data": {tspec.name: table_records},
                "extracted_at": datetime.now(timezone.utc).isoformat(),
            }
        except Exception as e:
            logger.warn(f"    Table extraction failed: {e}")
            return None

    # Built-in AI mode prompts
    _AI_MODES = {
        "extract": "Extract structured data from the following content.",
        "cleanup": "Clean and normalize this content. Remove HTML artifacts, "
                   "fix formatting, and return well-structured markdown.",
        "classify": "Classify the following content.",
        "refine": "Refine and improve the quality of this text. Fix grammar, "
                  "improve clarity, normalize formatting.",
    }

    def _call_ai(self, prompt: str) -> str:
        """Call AI via the configured adapter. Adapters tried in order:

        1. ``cli:<command>`` — arbitrary CLI, prompt on stdin
        2. ``aichat`` — aichat CLI (default if available)
        3. ``anthropic`` — Anthropic Python SDK
        4. ``openai`` — OpenAI Python SDK
        """
        import subprocess as _sp
        adapter = self.ctx.ai_adapter if hasattr(self.ctx, "ai_adapter") else ""

        # Explicit CLI adapter: "cli:my-tool --flag"
        if adapter.startswith("cli:"):
            cmd = adapter[4:].strip()
            result = _sp.run(cmd, shell=True, input=prompt,
                             capture_output=True, text=True)
            return result.stdout.strip()

        # aichat (default when available)
        if adapter in ("aichat", ""):
            aichat = os.popen("which aichat 2>/dev/null").read().strip()
            if aichat:
                result = _sp.run([aichat], input=prompt,
                                 capture_output=True, text=True, timeout=120)
                if result.returncode == 0:
                    return result.stdout.strip()
                logger.warn(f"aichat failed: {result.stderr[:200]}")
                if adapter == "aichat":
                    return ""
                # Fall through to SDK adapters

        # anthropic SDK
        if adapter in ("anthropic", ""):
            try:
                import anthropic
                client = anthropic.Anthropic()
                response = client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=1024,
                    messages=[{"role": "user", "content": prompt}],
                )
                return response.content[0].text.strip()
            except Exception as e:
                logger.warn(f"anthropic adapter failed: {e}")
                if adapter == "anthropic":
                    return ""

        # openai SDK
        if adapter in ("openai", ""):
            try:
                import openai
                client = openai.OpenAI()
                response = client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=1024,
                )
                return response.choices[0].message.content.strip()
            except Exception as e:
                logger.warn(f"openai adapter failed: {e}")

        return ""

    def _run_ai_extraction(self, config: dict, existing_data: dict) -> dict | None:
        """Run AI extraction on previously extracted text.

        Parameters (via ``...    key=value`` in .robot):
            input       — field name to use as input (default: all fields as JSON)
            prompt      — instruction for the AI
            mode        — built-in mode: extract, cleanup, classify, refine
            schema      — JSON schema hint (forces JSON output)
            categories  — classification categories (forces JSON output)
            max_size    — max chars before truncation (default 50000, 0=unlimited)
            chunk_size  — split input into chunks of this size, process each,
                          concatenate results (default 0=no chunking)
            output      — response format hint: json, markdown, text (default: json)
        """
        opts = _parse_options(tuple(config.get("specs", [])))
        name = config.get("name", "ai_result")
        mode = opts.get("mode")
        prompt = opts.get("prompt") or self._AI_MODES.get(mode or "extract",
                                                           "Extract structured data from the text.")
        input_field = opts.get("input")
        schema = opts.get("schema")
        categories = opts.get("categories")
        max_size = int(opts.get("max_size", "50000"))
        chunk_size = int(opts.get("chunk_size", "0"))
        output_fmt = opts.get("output", "json")

        input_text = ""
        if input_field and input_field in existing_data:
            input_text = str(existing_data[input_field])
        elif not input_field:
            input_text = json.dumps(existing_data, ensure_ascii=False)

        if not input_text:
            return None

        # Reject if over max_size (0 = unlimited) — input is too large, skip AI
        if max_size and len(input_text) > max_size:
            logger.warn(f"    AI input too large ({len(input_text)} > {max_size}), skipping")
            return None

        # Build output instruction based on format and options
        output_instruction = ""
        if schema:
            output_instruction = f"\n\nReturn JSON matching this schema: {schema}"
            output_instruction += "\n\nRespond with ONLY valid JSON, no explanation."
        elif categories:
            output_instruction = f"\n\nClassify into one of: {categories}"
            output_instruction += "\n\nRespond with ONLY valid JSON, no explanation."
        elif output_fmt == "json":
            output_instruction = "\n\nRespond with ONLY valid JSON, no explanation."
        elif output_fmt == "markdown":
            output_instruction = "\n\nRespond in clean markdown."
        # output_fmt == "text" → no instruction, freeform

        # Chunking: split input, process each chunk, concatenate
        if chunk_size and len(input_text) > chunk_size:
            chunks = [input_text[i:i + chunk_size]
                      for i in range(0, len(input_text), chunk_size)]
            results = []
            for idx, chunk in enumerate(chunks):
                chunk_prompt = (f"{prompt}\n\n(Chunk {idx + 1}/{len(chunks)})"
                                f"\n\nInput:\n{chunk}{output_instruction}")
                result = self._call_ai(chunk_prompt)
                if result:
                    results.append(result)
            result_text = "\n\n".join(results)
        else:
            full_prompt = f"{prompt}\n\nInput:\n{input_text}{output_instruction}"
            result_text = self._call_ai(full_prompt)

        if not result_text:
            return None

        # Parse based on output format
        if output_fmt == "json" or schema or categories:
            try:
                parsed = json.loads(result_text)
                return {name: parsed}
            except json.JSONDecodeError:
                return {name: result_text}
        return {name: result_text}

    def _run_setup(self, bl: Any) -> None:
        """Execute state setup actions (auth, consent)."""
        if not self.ctx.setup_actions:
            return
        # Check skip condition
        if self.ctx.setup_skip_when:
            try:
                count = bl.get_element_count(self.ctx.setup_skip_when)
                if count > 0:
                    logger.info("  Setup skipped — skip_when selector found")
                    return
            except Exception:
                pass
        for sa in self.ctx.setup_actions:
            try:
                if sa.action == "open":
                    bl.go_to(sa.url)
                elif sa.action == "input":
                    bl.fill_text(sa.css, sa.value)
                elif sa.action == "password":
                    bl.fill_text(sa.css, sa.value)
                elif sa.action == "click":
                    bl.click(sa.css)
            except Exception as e:
                logger.warn(f"  Setup action {sa.action} failed: {e}")
        try:
            bl.wait_for_load_state(self._PageLoadStates.networkidle)
        except Exception:
            pass

    def _do_select_date(self, bl: Any, action: Action) -> None:
        """Navigate a datepicker to the target month, then click the day.

        Works with ARIA-compliant datepickers where:
        - Month headings are visible as h2/h3 elements
        - A forward button advances the calendar
        - Day buttons have aria-labels containing the date info
        """
        from datetime import date as dt_date

        date_str = action.value or ""
        opts = action.options
        forward_sel = opts.get("forward", 'button[aria-label*="Move forward"]')
        heading_sel = opts.get("heading", "h2")
        max_clicks = int(opts.get("max_clicks", "15"))

        # Parse date
        parts = date_str.split("-")
        year, month, day = int(parts[0]), int(parts[1]), int(parts[2])
        d = dt_date(year, month, day)
        month_year = d.strftime("%B %Y")  # e.g. "November 2026"
        day_str = str(day)  # no leading zero

        # Wait for the calendar to be mounted (forward button visible)
        try:
            bl.wait_for_elements_state(forward_sel, "attached", "5s")
        except Exception:
            logger.warn(f"  Calendar forward button not found: {forward_sel}")
            return

        # Navigate forward until target month heading is visible
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
        for click_i in range(max_clicks):
            result = bl.evaluate_javascript(None, check_script)
            if result and result.get("found"):
                break
            if self._timing and click_i == 0:
                logger.warn(f"  [INSTRUMENT] select_date: looking for {month_year}, visible: {result}")
            # Snapshot current headings before clicking forward
            old_headings = result.get("headings", []) if result else []
            clicked_fwd = bl.evaluate_javascript(None, fwd_script)
            if not clicked_fwd:
                break
            # Poll until heading text changes (calendar re-rendered)
            deadline = time.time() + 3.0
            while time.time() < deadline:
                new_result = bl.evaluate_javascript(None, check_script)
                new_headings = new_result.get("headings", []) if new_result else []
                if new_headings != old_headings:
                    break

        # Click the day button
        # ARIA pattern: aria-label starts with "DAY, " and contains "MONTH YEAR"
        click_script = (
            f"(() => {{ "
            f"for (const b of document.querySelectorAll('button')) {{ "
            f"const l = b.getAttribute('aria-label') || ''; "
            f"if (l.startsWith({json.dumps(day_str + ', ')}) && "
            f"l.includes({json.dumps(month_year)})) "
            f"{{ b.click(); return true; }} }} return false; }})()"
        )
        clicked = bl.evaluate_javascript(None, click_script)
        if not clicked:
            logger.warn(f"  Could not find day button for {date_str}")

    def _wait_page_ready(self, bl: Any, page_delay_ms: int = 0) -> None:
        """Wait for page to be ready after navigation.

        Uses domcontentloaded (fast — DOM structure ready) then dismisses
        interrupts.  Content readiness is handled downstream by each
        rule's state check or expansion wait_for_elements_state.
        """
        try:
            bl.wait_for_load_state(self._PageLoadStates.domcontentloaded)
        except Exception:
            pass
        self._dismiss_interrupts(bl)

    def _dismiss_interrupts(self, bl: Any) -> None:
        """Auto-dismiss overlay selectors using global config."""
        self._dismiss_interrupts_with(bl, self.ctx.interrupt_selectors)

    def _dismiss_interrupts_with(self, bl: Any, selectors: list[str]) -> None:
        """Auto-dismiss overlay selectors (cookie banners, modals)."""
        for selector in selectors:
            try:
                count = bl.get_element_count(selector)
                if count > 0:
                    bl.click(selector)
                    logger.info(f"  Dismissed interrupt: {selector}")
            except Exception:
                pass

    def _invoke_hooks(self, lifecycle_point: str, data: dict) -> dict:
        """Invoke registered hooks at a lifecycle point.

        Hooks can transform data via config-driven rules:
        - rename=old_field:new_field — rename a field
        - drop=field_name — remove a field
        - strip_html=field_name — strip HTML tags from a field
        - default=field_name:value — set default if field empty
        - lowercase=field_name — lowercase a field
        - regex=field_name:pattern:replacement — regex replace
        """
        result = dict(data)
        for hook in self.ctx.hooks:
            if hook.lifecycle_point != lifecycle_point:
                continue
            logger.info(f"  Hook '{hook.name}' at {lifecycle_point}")
            cfg = hook.config
            for key, val in cfg.items():
                try:
                    if key == "rename" and ":" in val:
                        old, new = val.split(":", 1)
                        if old in result:
                            result[new] = result.pop(old)
                    elif key == "drop" and val in result:
                        del result[val]
                    elif key == "strip_html" and val in result:
                        result[val] = re.sub(r"<[^>]+>", "", str(result[val]))
                    elif key == "to_markdown" and val in result:
                        try:
                            from markdownify import markdownify as md
                            result[val] = md(str(result[val]),
                                             heading_style="ATX",
                                             strip=["script", "style", "nav"])
                        except ImportError:
                            result[val] = re.sub(r"<[^>]+>", "", str(result[val]))
                    elif key == "default" and ":" in val:
                        field, default_val = val.split(":", 1)
                        if not result.get(field):
                            result[field] = default_val
                    elif key == "lowercase" and val in result:
                        result[val] = str(result[val]).lower()
                    elif key == "regex" and val.count(":") >= 2:
                        parts = val.split(":", 2)
                        field, pattern, replacement = parts
                        if field in result:
                            result[field] = re.sub(pattern, replacement, str(result[field]))
                except Exception as e:
                    logger.warn(f"  Hook transform {key}={val} failed: {e}")
        return result

    def _emit_records(self, target: str, rule: RuleNode,
                      records: list[dict], current_url: str) -> None:
        if target not in self.ctx.artifact_store:
            self.ctx.artifact_store[target] = []

        flatten_field = rule.emit_flatten_by.get(target)
        merge_key = rule.emit_merge_on.get(target)

        for record in records:
            if not record:
                continue

            if flatten_field:
                data = record.get("data", {})
                items = data.get(flatten_field, [])
                if isinstance(items, list):
                    for item in items:
                        flat_rec = {
                            "node": record.get("node", rule.name),
                            "url": record.get("url", current_url),
                            "data": item if isinstance(item, dict) else {flatten_field: item},
                            "extracted_at": record.get("extracted_at",
                                                       datetime.now(timezone.utc).isoformat()),
                        }
                        self.ctx.artifact_store[target].append(flat_rec)
                continue

            if merge_key:
                data = record.get("data", {})
                key_val = data.get(merge_key)
                if key_val:
                    found = False
                    for existing in self.ctx.artifact_store[target]:
                        if existing.get("data", {}).get(merge_key) == key_val:
                            existing["data"].update(data)
                            found = True
                            break
                    if found:
                        continue

            self.ctx.artifact_store[target].append(record)

    def _write_outputs(self, output_dir: str) -> None:
        summary: dict[str, Any] = {
            "deployment": self.ctx.name,
            "artifacts": {},
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }

        for art_name, art in self.ctx.artifacts.items():
            records = self.ctx.artifact_store.get(art_name, [])
            summary["artifacts"][art_name] = {
                "record_count": len(records),
                "output": art.output,
                "structure": art.structure,
            }

            if not art.output:
                continue

            if art.dedupe and records:
                seen = set()
                deduped = []
                for r in records:
                    key_val = r.get("data", {}).get(art.dedupe)
                    if key_val and key_val not in seen:
                        seen.add(key_val)
                        deduped.append(r)
                    elif not key_val:
                        deduped.append(r)
                records = deduped

            output_data = records
            if art.query:
                try:
                    import jmespath
                    output_data = jmespath.search(art.query, records)
                except ImportError:
                    logger.warn("jmespath not installed, skipping query transform")
                except Exception as e:
                    logger.warn(f"JMESPath query failed for {art_name}: {e}")

            if art.structure == "flat":
                flat = []
                for r in (output_data if isinstance(output_data, list) else [output_data]):
                    if isinstance(r, dict):
                        flat.append(r)
                output_data = flat

            # Determine output path and format
            fmt = art.format or "json"
            ext = {"json": ".json", "jsonl": ".jsonl", "csv": ".csv",
                   "markdown": ".md"}.get(fmt, ".json")

            override = self.ctx.write_overrides.get(art_name)
            if override:
                out_path = Path(override)
            else:
                out_path = Path(output_dir) / f"{self.ctx.name}_{art_name}{ext}"

            out_path.parent.mkdir(parents=True, exist_ok=True)

            if fmt == "jsonl":
                with open(out_path, "w") as f:
                    for item in (output_data if isinstance(output_data, list)
                                 else [output_data]):
                        f.write(json.dumps(item, ensure_ascii=False) + "\n")
            elif fmt == "csv":
                import csv as csv_mod
                rows = output_data if isinstance(output_data, list) else [output_data]
                if rows:
                    # Flatten data dicts for CSV
                    flat_rows = []
                    for r in rows:
                        if isinstance(r, dict) and "data" in r:
                            flat_rows.append(r["data"])
                        elif isinstance(r, dict):
                            flat_rows.append(r)
                    if flat_rows:
                        keys = list(flat_rows[0].keys())
                        with open(out_path, "w", newline="") as f:
                            writer = csv_mod.DictWriter(f, fieldnames=keys)
                            writer.writeheader()
                            writer.writerows(flat_rows)
            elif fmt == "markdown":
                with open(out_path, "w") as f:
                    for item in (output_data if isinstance(output_data, list)
                                 else [output_data]):
                        data = item.get("data", item) if isinstance(item, dict) else item
                        if isinstance(data, dict):
                            title = data.get("title", "")
                            body = data.get("body", "")
                            if title:
                                f.write(f"# {title}\n\n")
                            if body:
                                f.write(f"{body}\n\n")
                        else:
                            f.write(f"{data}\n\n")
            else:  # json
                with open(out_path, "w") as f:
                    json.dump(output_data, f, indent=2, ensure_ascii=False)

            logger.info(f"  Wrote {len(records)} records to {out_path}")

        summary_path = Path(output_dir) / f"{self.ctx.name}.json"
        with open(summary_path, "w") as f:
            json.dump(summary, f, indent=2)
        logger.info(f"  Wrote deployment summary to {summary_path}")

    def _check_quality_gates(self) -> None:
        qg = self.ctx.quality_gate
        total_records = sum(len(v) for v in self.ctx.artifact_store.values())

        if qg.min_records and total_records < qg.min_records:
            logger.warn(
                f"QUALITY GATE FAIL: min_records={qg.min_records}, "
                f"actual={total_records}"
            )

        for field_name, min_pct in qg.filled_pcts.items():
            total = 0
            filled = 0
            for records in self.ctx.artifact_store.values():
                for r in records:
                    data = r.get("data", {})
                    if field_name in data:
                        total += 1
                        val = data[field_name]
                        if val and val != "" and val != []:
                            filled += 1
            if total > 0:
                actual_pct = (filled / total) * 100
                if actual_pct < min_pct:
                    logger.warn(
                        f"QUALITY GATE WARN: {field_name} filled={actual_pct:.1f}% "
                        f"< required={min_pct}%"
                    )


# ---------------------------------------------------------------------------
# Robot Framework keyword library
# ---------------------------------------------------------------------------

@library(scope="SUITE", auto_keywords=False)
class WiseRpaBDD:
    def __init__(self) -> None:
        self.events: list[tuple[str, tuple[Any, ...]]] = []
        self._deployment: DeploymentContext | None = None
        self._current_resource: ResourceContext | None = None
        self._current_rule: RuleNode | None = None
        self._rule_has_actions: bool = False  # Tracks if current rule has actions (for guard vs observation routing)
        self._rule_stack: list[str] = []  # Track rule creation order

    def _record(self, name: str, *values: Any) -> None:
        self.events.append((name, values))
        rendered = ", ".join(str(v) for v in values if v not in (None, ""))
        logger.info(f"{name}: {rendered}" if rendered else name)

    # -- Deployment lifecycle --

    @keyword('Given I start deployment "${deployment}"')
    def start_deployment(self, deployment: str) -> None:
        self._record("start_deployment", deployment)
        self._deployment = DeploymentContext(name=deployment)

    @keyword('And I set AI adapter "${adapter}"')
    def set_ai_adapter(self, adapter: str) -> None:
        """Set the AI backend: aichat, anthropic, openai, or cli:<command>."""
        self._record("set_ai_adapter", adapter)
        if self._deployment:
            self._deployment.ai_adapter = adapter

    @keyword("Then I finalize deployment")
    def finalize_deployment(self) -> None:
        self._record("finalize_deployment")
        if not self._deployment:
            logger.error("No deployment context — nothing to finalize")
            return

        # Identify root rules for each resource before execution
        self._finalize_resource_roots()

        _truthy = ("1", "true", "yes")
        _falsy = ("0", "false", "no")
        headed = os.environ.get("WISE_RPA_HEADED", "").lower() in _truthy
        stealth = os.environ.get("WISE_RPA_STEALTH", "").lower() not in _falsy
        resume_mode = os.environ.get("WISE_RPA_RESUME_MODE", "auto")
        output_dir = os.environ.get("WISE_RPA_OUTPUT_DIR", "")
        if output_dir:
            self._deployment.output_dir = output_dir
        engine = ExecutionEngine(self._deployment, headed=headed,
                                 stealth=stealth)
        engine.run(resume_mode=resume_mode)

    # -- Artifact registration --

    @keyword('Given I register artifact "${artifact}"')
    def register_artifact(self, artifact: str, *fields: str) -> None:
        self._record("register_artifact", artifact, *fields)
        if not self._deployment:
            return
        parsed_fields = []
        current: dict[str, str] = {}
        for f in fields:
            if "=" not in f:
                continue
            k, v = f.split("=", 1)
            if k == "field" and current.get("field"):
                parsed_fields.append(current.copy())
                current = {}
            current[k] = v
        if current.get("field"):
            parsed_fields.append(current.copy())

        self._deployment.artifacts[artifact] = ArtifactSchema(
            name=artifact, fields=parsed_fields
        )
        self._deployment.artifact_store[artifact] = []

    @keyword('And I set artifact options for "${artifact}"')
    def set_artifact_options(self, artifact: str, *options: str) -> None:
        self._record("set_artifact_options", artifact, *options)
        if not self._deployment:
            return
        art = self._deployment.artifacts.get(artifact)
        if not art:
            return
        opts = _parse_options(options)
        art.output = opts.get("output", "").lower() in ("true", "1", "yes")
        art.format = opts.get("format", art.format)
        art.structure = opts.get("structure", art.structure)
        art.dedupe = opts.get("dedupe", art.dedupe)
        art.query = opts.get("query", art.query)
        art.consumes = opts.get("consumes", art.consumes)
        art.description = opts.get("description", art.description)

    # -- Resource management --

    @keyword('Given I start resource "${resource}"')
    def start_resource(self, resource: str) -> None:
        self._record("start_resource", resource)
        if not self._deployment:
            return
        self._current_resource = ResourceContext(name=resource)
        self._deployment.resources.append(self._current_resource)
        self._rule_stack = []

    @keyword('Given I start resource "${resource}" at "${entry}"')
    def start_resource_at(self, resource: str, entry: str) -> None:
        self._record("start_resource_at", resource, entry)
        if not self._deployment:
            return
        rc = ResourceContext(name=resource, entry_url=entry)
        # Detect template URLs
        if "{" in entry and "}" in entry:
            rc.entry_template = entry
        self._current_resource = rc
        self._deployment.resources.append(rc)
        self._rule_stack = []

    @keyword('Given I consume artifact "${artifact}"')
    def consume_artifact(self, artifact: str) -> None:
        self._record("consume_artifact", artifact)
        if self._current_resource:
            self._current_resource.consumes = artifact

    @keyword('Given I resolve entry from "${reference}"')
    def resolve_entry_from(self, reference: str) -> None:
        self._record("resolve_entry_from", reference)

    @keyword('Given I iterate over parent records from "${parent_case}"')
    def iterate_over_parent_records(self, parent_case: str) -> None:
        self._record("iterate_over_parent_records", parent_case)
        if self._current_resource:
            self._current_resource.iterates_parent = parent_case

    @keyword("And I set resource globals")
    def set_resource_globals(self, *globals_: str) -> None:
        self._record("set_resource_globals", *globals_)
        if not self._current_resource:
            return
        self._current_resource.globals_ = _parse_options(globals_)

    # -- Rule management --

    @keyword('I define rule "${rule}"')
    def begin_rule(self, rule: str) -> None:
        self._record("begin_rule", rule)
        if not self._current_resource:
            return
        node = RuleNode(name=rule)
        self._current_resource.rules[rule] = node
        self._current_rule = node
        self._rule_has_actions = False
        self._rule_stack.append(rule)

    @keyword('And I declare parents "${parents}"')
    def declare_parents(self, parents: str) -> None:
        self._record("declare_parents", parents)
        if not self._current_rule:
            return
        self._current_rule.parents = [p.strip() for p in parents.split(",")]

    @keyword("And I set retry ${max} times with ${delay} ms delay")
    def set_retry(self, max: Any, delay: Any) -> None:
        self._record("set_retry", max, delay)
        if self._current_rule:
            self._current_rule.retry_max = int(max)
            self._current_rule.retry_delay_ms = int(delay)

    @keyword('And I set guard policy "${policy}"')
    def set_guard_policy(self, policy: str) -> None:
        """Set guard failure policy: "skip" (default) or "abort"."""
        self._record("set_guard_policy", policy)
        if self._current_rule:
            self._current_rule.guard_policy = policy

    @keyword("And I scope interrupts")
    def scope_interrupts(self, *specs: str) -> None:
        """Override global dismiss selectors for this rule only.

        Usage in .robot::

            I define rule "interactive_form"
                And I scope interrupts
                ...    dismiss="button.cookie-accept"
                ...    dismiss=".modal-close"
        """
        self._record("scope_interrupts", *specs)
        if not self._current_rule:
            return
        selectors: list[str] = []
        for spec in specs:
            if "=" not in spec:
                continue
            k, v = spec.split("=", 1)
            if k == "dismiss":
                selectors.append(_strip_quotes(v))
        self._current_rule.interrupt_override = selectors

    @keyword("And I pause interrupts")
    def pause_interrupts(self) -> None:
        """Skip all dismiss for this rule's steps.

        Usage in .robot::

            I define rule "datepicker_interaction"
                And I pause interrupts
                When I click ...
        """
        self._record("pause_interrupts")
        if self._current_rule:
            self._current_rule.interrupt_paused = True

    @keyword("And I set rule options")
    def set_rule_options(self, *specs: str) -> None:
        """Set declarative lifecycle options for the current rule.

        Usage in .robot::

            I define rule "checkout"
                And I set rule options
                ...    on_enter=screenshot
                ...    on_fail=screenshot
                ...    timeout_ms=30000
        """
        self._record("set_rule_options", *specs)
        if not self._current_rule:
            return
        for spec in specs:
            if "=" not in spec:
                continue
            k, v = spec.split("=", 1)
            self._current_rule.options[k.strip()] = v.strip()

    # -- State checks --

    def _add_state_check(self, check: StateCheck) -> None:
        """Route a state check to guards or steps based on parse position.

        Before any action in the rule → guard (Type 1: precondition).
        After an action → step (Type 2: observation gate between actions).
        """
        if not self._current_rule:
            return
        if self._rule_has_actions:
            self._current_rule.steps.append(check)
        else:
            self._current_rule.guards.append(check)

    @keyword('Given url contains "${pattern}"')
    def url_contains(self, pattern: str) -> None:
        self._record("url_contains", pattern)
        self._add_state_check(StateCheck(type="url_contains", pattern=pattern))

    @keyword('Given url matches "${pattern}"')
    def url_matches(self, pattern: str) -> None:
        self._record("url_matches", pattern)
        self._add_state_check(StateCheck(type="url_matches", pattern=pattern))

    @keyword('But url does not contain "${pattern}"')
    def url_does_not_contain(self, pattern: str) -> None:
        self._record("url_does_not_contain", pattern)
        self._add_state_check(StateCheck(type="url_not_contains", pattern=pattern))

    @keyword('And selector "${selector}" exists')
    def selector_exists(self, selector: str) -> None:
        self._record("selector_exists", selector)
        self._add_state_check(StateCheck(type="selector_exists", pattern=selector))

    @keyword('And table headers are "${headers}"')
    def table_headers_are(self, headers: str) -> None:
        self._record("table_headers_are", headers)
        self._add_state_check(StateCheck(type="table_headers", pattern=headers))

    # -- Actions --

    def _add_action(self, action: Action) -> None:
        """Append an action to the current rule's steps list."""
        if not self._current_rule:
            return
        self._rule_has_actions = True
        self._current_rule.steps.append(action)

    @keyword('When I open "${url}"')
    def open_url(self, url: str) -> None:
        self._record("open_url", url)
        self._add_action(Action(type="open", value=url))

    @keyword('When I open the bound field "${field}"')
    def open_bound_field(self, field: str) -> None:
        self._record("open_bound_field", field)
        self._add_action(
            Action(type="open_bound", value=field)
        )

    @keyword('When I click locator "${locator}"')
    def click_locator(self, locator: str, *options: str) -> None:
        self._record("click_locator", locator, *options)
        self._add_action(
            Action(type="click", locator=locator, options=_parse_options(options))
        )

    @keyword('When I type "${value}" into locator "${locator}"')
    def type_into_locator(self, value: str, locator: str, *options: str) -> None:
        self._record("type_into_locator", value, locator, *options)
        self._add_action(
            Action(type="type", locator=locator, value=value,
            options=_parse_options(options))
        )

    @keyword('When I type secret "${value}" into locator "${locator}"')
    def type_secret_into_locator(self, value: str, locator: str, *options: str) -> None:
        self._record("type_secret_into_locator", "***", locator, *options)
        self._add_action(
            Action(type="type", locator=locator, value=value,
            options=_parse_options(options))
        )

    @keyword("When I scroll down")
    def scroll_down(self) -> None:
        self._record("scroll_down")
        self._add_action(Action(type="scroll"))

    @keyword("When I wait for idle")
    def wait_for_idle(self) -> None:
        self._record("wait_for_idle")
        self._add_action(Action(type="wait"))

    @keyword("When I wait ${ms} ms")
    def wait_ms(self, ms: Any) -> None:
        self._record("wait_ms", ms)
        self._add_action(
            Action(type="wait_ms", value=str(ms))
        )

    @keyword('When I select "${value}" from locator "${locator}"')
    def select_from_locator(self, value: str, locator: str, *options: str) -> None:
        self._record("select_from_locator", value, locator, *options)
        self._add_action(
            Action(type="select", locator=locator, value=value,
            options=_parse_options(options))
        )

    @keyword('When I check locator "${locator}"')
    def check_locator(self, locator: str, *options: str) -> None:
        self._record("check_locator", locator, *options)
        self._add_action(
            Action(type="click", locator=locator, options=_parse_options(options))
        )

    @keyword('When I hover locator "${locator}"')
    def hover_locator(self, locator: str) -> None:
        self._record("hover_locator", locator)
        self._add_action(Action(type="hover", locator=locator))

    @keyword('When I focus locator "${locator}"')
    def focus_locator(self, locator: str) -> None:
        self._record("focus_locator", locator)
        self._add_action(Action(type="focus", locator=locator))

    @keyword('When I double click locator "${locator}"')
    def dblclick_locator(self, locator: str) -> None:
        self._record("dblclick_locator", locator)
        self._add_action(Action(type="dblclick", locator=locator))

    @keyword('When I press keys "${locator}"')
    def press_keys_locator(self, locator: str, *keys: str) -> None:
        """Press keyboard keys on a focused element.

        Example: ``When I press keys "#search"    Enter``
        """
        self._record("press_keys", locator, *keys)
        self._add_action(
            Action(type="press_keys", locator=locator, args=keys)
        )

    @keyword("When I take screenshot")
    def take_screenshot(self, *options: str) -> None:
        self._record("take_screenshot", *options)
        opts = _parse_options(options)
        self._add_action(
            Action(type="screenshot", value=opts.get("filename", "screenshot.png"))
        )

    @keyword('When I upload file "${path}" to locator "${locator}"')
    def upload_file_to_locator(self, path: str, locator: str) -> None:
        self._record("upload_file", path, locator)
        self._add_action(
            Action(type="upload", locator=locator, value=path)
        )

    # -- High-level interaction keywords (reduce need for evaluate_js) --

    @keyword('When I click text "${text}"')
    def click_text(self, text: str, *options: str) -> None:
        """Click the first visible element whose text content matches.

        Uses JS to find by text, avoiding CSS limitations.
        Example: ``When I click text "Got it"``
        Options: await=<selector> — wait for selector after click.
        """
        self._record("click_text", text, *options)
        self._add_action(
            Action(type="click_text", value=text, args=(),
            options=_parse_options(options))
        )

    @keyword('When I add url params "${params}"')
    def add_url_params(self, params: str) -> None:
        """Add query parameters to the current URL and navigate.

        The engine handles navigation wait and SPA hydration automatically.
        Example: ``When I add url params "price_max=3000&superhost=true"``
        """
        self._record("add_url_params", params)
        self._add_action(
            Action(type="add_url_params", value=params, args=())
        )

    @keyword('When I set stepper "${locator}" to ${count}')
    def set_stepper(self, locator: str, count: str) -> None:
        """Click a stepper/increment button N times.

        Example: ``When I set stepper "[data-testid='stepper-adults']" to 2``
        """
        self._record("set_stepper", locator, count)
        self._add_action(
            Action(type="set_stepper", value=count, locator=locator, args=())
        )

    @keyword('When I select date "${date}" from datepicker')
    def select_date_from_datepicker(self, date: str, *options: str) -> None:
        """Navigate a datepicker forward to the target month and click the day.

        The date is in YYYY-MM-DD format. The engine navigates the calendar
        forward (clicking a forward button) until the target month heading
        is visible, then clicks the day button matching the date.

        Options (continuation rows):
        - ``forward=<css>`` — forward navigation button (default: ``button[aria-label*="Move forward"]``)
        - ``heading=<css>`` — month heading selector (default: ``h2``)
        - ``back=<css>`` — backward button (not used by default)
        - ``max_clicks=<N>`` — max forward clicks before giving up (default: 15)

        Example::

            When I select date "${CHECKIN}" from datepicker
            ...    forward=button[aria-label*="Move forward"]
            ...    heading=h2
        """
        opts = _parse_options(options)
        self._record("select_date", date, *options)
        self._add_action(
            Action(type="select_date", value=date,
            args=(), options=opts)
        )

    # -- Browser step & call keyword (deferred passthrough) --

    @keyword('And I browser step "${method}"')
    def browser_step(self, method: str, *args: str) -> None:
        """Defer a raw Browser library method call into the current rule.

        Executes during the rule walk when the browser is live.
        Example: ``And I browser step "Press Keys" "#search" "Enter"``
        """
        self._record("browser_step", method, *args)
        self._add_action(
            Action(type="browser_step", value=method, args=args)
        )

    @keyword('And I evaluate js "${script}"')
    def evaluate_js(self, script: str) -> None:
        """Defer a JavaScript expression to run on the live page.

        Works with both the RF-Browser adapter and the stealth adapter.
        Supports async scripts (async () => { ... }).
        Example: ``And I evaluate js "document.querySelector('#btn').click()"``
        """
        self._record("evaluate_js", script)
        self._add_action(
            Action(type="evaluate_js", value=script, args=())
        )

    @keyword('And I call keyword "${name}"')
    def call_keyword(self, name: str, *args: str) -> None:
        """Defer an arbitrary Robot Framework keyword call into the current rule.

        The keyword runs during the rule walk when the browser is live,
        so it can use raw Browser library keywords freely.
        Example: ``And I call keyword "Login To Site"``
        """
        self._record("call_keyword", name, *args)
        self._add_action(
            Action(type="call_keyword", value=name, args=args)
        )

    # -- Expansion --

    @keyword('When I expand over elements "${scope}"')
    def expand_over_elements(self, scope: str, *options: str) -> None:
        self._record("expand_over_elements", scope, *options)
        if not self._current_rule:
            return
        opts = _parse_options(options)
        self._current_rule.expansion = Expansion(
            over="elements", scope=scope,
            limit=int(opts.get("limit", 0)) or 10000,
            options=opts,
        )

    @keyword('When I expand over elements "${scope}" with order "${order}"')
    def expand_over_elements_with_order(self, scope: str, order: str, *options: str) -> None:
        self._record("expand_over_elements_with_order", scope, order, *options)
        if not self._current_rule:
            return
        opts = _parse_options(options)
        self._current_rule.expansion = Expansion(
            over="elements", scope=scope, order=order,
            limit=int(opts.get("limit", 0)) or 10000,
            options=opts,
        )

    @keyword('When I paginate by next button "${locator}" up to ${limit} pages')
    def paginate_by_next_button(self, locator: str, limit: Any, *options: str) -> None:
        self._record("paginate_by_next_button", locator, limit, *options)
        if not self._current_rule:
            return
        self._current_rule.expansion = Expansion(
            over="pages_next", locator=locator,
            limit=int(limit), options=_parse_options(options),
        )

    @keyword('When I paginate by numeric control "${locator}" from ${start} up to ${limit} pages')
    def paginate_by_numeric_control(self, locator: str, start: Any, limit: Any, *options: str) -> None:
        self._record("paginate_by_numeric_control", locator, start, limit, *options)
        if not self._current_rule:
            return
        self._current_rule.expansion = Expansion(
            over="pages_numeric", locator=locator,
            start=int(start), limit=int(limit),
            options=_parse_options(options),
        )

    @keyword("When I expand over combinations")
    def expand_over_combinations(self, *axes: str) -> None:
        self._record("expand_over_combinations", *axes)
        if not self._current_rule:
            return
        # Parse axis specs: action=type control="#sel" values=a|b|c exclude=X|Y skip=1
        parsed_axes: list[CombinationAxis] = []
        current: dict[str, str] = {}
        def _flush_axis(cur: dict[str, str]) -> CombinationAxis:
            return CombinationAxis(
                action=cur["action"],
                control=_strip_quotes(cur.get("control", "")),
                values=cur.get("values", "").split("|"),
                exclude=cur.get("exclude", "").split("|") if cur.get("exclude") else [],
                skip=int(cur.get("skip", 0)),
                emit=cur.get("emit", ""),
            )
        for spec in axes:
            if "=" not in spec:
                continue
            k, v = spec.split("=", 1)
            if k == "action" and current.get("action"):
                parsed_axes.append(_flush_axis(current))
                current = {}
            current[k] = v
        if current.get("action"):
            parsed_axes.append(_flush_axis(current))
        self._current_rule.expansion = Expansion(
            over="combinations", axes=parsed_axes,
        )

    # -- Extraction --

    @keyword("Then I extract fields")
    def extract_fields(self, *specs: str) -> None:
        self._record("extract_fields", *specs)
        if not self._current_rule:
            return
        self._current_rule.field_specs = _parse_field_specs(specs)

    @keyword('Then I extract table "${name}" from "${locator}"')
    def extract_table(self, name: str, locator: str, *specs: str) -> None:
        self._record("extract_table", name, locator, *specs)
        if not self._current_rule:
            return
        header_row, fields = _parse_table_specs(specs)
        self._current_rule.table_spec = TableSpec(
            name=name, locator=locator,
            header_row=header_row, fields=fields,
        )

    @keyword('Then I extract with AI "${name}"')
    def extract_with_ai(self, name: str, *specs: str) -> None:
        self._record("extract_with_ai", name, *specs)
        if self._current_rule:
            self._current_rule.ai_extraction = {"name": name, "specs": list(specs)}

    # -- Emit --

    @keyword('And I emit to artifact "${artifact}"')
    def emit_to_artifact(self, artifact: str) -> None:
        self._record("emit_to_artifact", artifact)
        if self._current_rule:
            self._current_rule.emit_targets.append(artifact)

    @keyword('And I emit to artifact "${artifact}" flattened by "${field}"')
    def emit_to_artifact_flattened(self, artifact: str, field: str) -> None:
        self._record("emit_to_artifact_flattened", artifact, field)
        if self._current_rule:
            self._current_rule.emit_targets.append(artifact)
            self._current_rule.emit_flatten_by[artifact] = field

    @keyword('And I merge into artifact "${artifact}" on key "${key}"')
    def merge_into_artifact(self, artifact: str, key: str) -> None:
        self._record("merge_into_artifact", artifact, key)
        if self._current_rule:
            self._current_rule.emit_targets.append(artifact)
            self._current_rule.emit_merge_on[artifact] = key

    @keyword('Then I write artifact "${artifact}" to "${path}"')
    def write_artifact(self, artifact: str, path: str) -> None:
        self._record("write_artifact", artifact, path)
        if self._deployment:
            self._deployment.write_overrides[artifact] = path

    # -- Quality gates --

    @keyword("And I set quality gate min records to ${count}")
    def set_quality_gate_min_records(self, count: Any) -> None:
        self._record("set_quality_gate_min_records", count)
        if self._deployment:
            self._deployment.quality_gate.min_records = int(count)

    @keyword('And I set filled percentage for "${field}" to ${percent}')
    def set_filled_percentage(self, field: str, percent: Any) -> None:
        self._record("set_filled_percentage", field, percent)
        if self._deployment:
            self._deployment.quality_gate.filled_pcts[field] = float(percent)

    @keyword('And I set max failed percentage to ${percent}')
    def set_max_failed_percentage(self, percent: Any) -> None:
        self._record("set_max_failed_percentage", percent)
        if self._deployment:
            self._deployment.quality_gate.max_failed_pct = float(percent)

    # -- Hooks and configuration --

    @keyword('And I register hook "${name}" at "${lifecycle_point}"')
    def register_hook(self, name: str, lifecycle_point: str, *config: str) -> None:
        self._record("register_hook", name, lifecycle_point, *config)
        if self._deployment:
            self._deployment.hooks.append(HookDef(
                name=name, lifecycle_point=lifecycle_point,
                config=_parse_options(config),
            ))

    @keyword("Given I configure state setup")
    def configure_state_setup(self, *actions: str) -> None:
        self._record("configure_state_setup", *actions)
        if not self._deployment:
            return
        for spec in actions:
            if "=" not in spec:
                continue
            k, v = spec.split("=", 1)
            v = _strip_quotes(v)
            if k == "skip_when":
                self._deployment.setup_skip_when = v
            elif k == "action":
                # Parse: action=open url="..." or action=click css="..."
                # The action type is the value, rest comes in subsequent opts
                self._deployment.setup_actions.append(SetupAction(action=v))
            elif k in ("url", "css", "value") and self._deployment.setup_actions:
                setattr(self._deployment.setup_actions[-1], k, v)

    @keyword("And I configure interrupts")
    def configure_interrupts(self, *dismissals: str) -> None:
        self._record("configure_interrupts", *dismissals)
        if not self._deployment:
            return
        for spec in dismissals:
            if "=" not in spec:
                continue
            k, v = spec.split("=", 1)
            if k == "dismiss":
                self._deployment.interrupt_selectors.append(_strip_quotes(v))

    @keyword("Then I close the browser")
    def close_browser(self) -> None:
        self._record("close_browser")

    # -- Resource test case hook --
    # When RF runs a test case with [Setup] that calls start_resource_at,
    # all subsequent keywords in that test case are in that resource context.
    # Rules without parents are root rules.
    # At the end we need to identify roots for each resource.

    def _finalize_resource_roots(self) -> None:
        """Called before execution to identify root rules for each resource."""
        if not self._deployment:
            return
        for res in self._deployment.resources:
            for name, rule in res.rules.items():
                if not rule.parents:
                    res.root_names.append(name)


# ---------------------------------------------------------------------------
# BDD format validation (standalone, no browser needed)
# ---------------------------------------------------------------------------

_BDD_PREFIXES = ("Given ", "When ", "Then ", "And ", "But ")
_STRUCTURAL_PREFIXES = ("I define rule ",)
_SECTION_RE = re.compile(r"^\*\*\* (.+) \*\*\*$")
_CELL_SPLIT_RE = re.compile(r"\s{2,}|\t+")


def _starts_with_bdd(text: str) -> bool:
    return (any(text.startswith(p) for p in _BDD_PREFIXES)
            or any(text.startswith(p) for p in _STRUCTURAL_PREFIXES))


def validate_bdd(path: Path) -> list[str]:
    """Check that every executable step uses Given/When/Then/And/But."""
    errors: list[str] = []
    section = ""
    for lineno, raw_line in enumerate(path.read_text().splitlines(), start=1):
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _SECTION_RE.match(stripped)
        if match:
            section = match.group(1).lower()
            continue
        if section == "settings":
            if stripped.startswith(("Suite Setup", "Test Setup",
                                    "Suite Teardown", "Test Teardown")):
                parts = [p for p in _CELL_SPLIT_RE.split(stripped) if p]
                if len(parts) >= 2 and not _starts_with_bdd(parts[1]):
                    errors.append(f"{path}:{lineno}: setup/teardown must use BDD keyword")
            continue
        if section == "keywords":
            # User-defined keywords may contain raw RF steps — skip validation
            continue
        if section == "test cases":
            if stripped.startswith("..."):
                continue
            if not raw_line.startswith(" "):
                continue
            parts = [p for p in _CELL_SPLIT_RE.split(stripped) if p]
            if not parts:
                continue
            if parts[0] in {"[Setup]", "[Teardown]"}:
                if len(parts) < 2 or not _starts_with_bdd(parts[1]):
                    errors.append(f"{path}:{lineno}: setup/teardown step must use BDD keyword")
                continue
            if parts[0].startswith("["):
                continue
            if not _starts_with_bdd(parts[0]):
                errors.append(f"{path}:{lineno}: step must start with BDD prefix")
    return errors


# ---------------------------------------------------------------------------
# Test harness RF keyword library (for e2e agent tests)
# ---------------------------------------------------------------------------

@library(scope="SUITE", auto_keywords=False)
class WiseRpaBDDTest:
    """RF keyword library for testing agent-generated .robot suites."""

    ROBOT_LIBRARY_SCOPE = "SUITE"

    def __init__(self, model: str = "sonnet", max_turns: int = 50,
                 backend: str = "claude"):
        self._model = model
        self._max_turns = int(max_turns)
        self._backend = backend
        self._generated_path: Path | None = None
        self._generated_content: str = ""

    def _require_generated(self) -> Path:
        if not self._generated_path:
            raise RuntimeError("No generated suite — call 'Generate Suite From Requirement' first")
        return self._generated_path

    @keyword("Generate Suite From Requirement")
    def generate_suite_from_requirement(self, requirement: str,
                                         output_path: str = "") -> str:
        """Invoke the AI agent with a requirement and capture the .robot output."""
        import tempfile
        if output_path:
            out = Path(output_path)
        else:
            fd = tempfile.NamedTemporaryFile(suffix=".robot", delete=False)
            fd.close()
            out = Path(fd.name)
        out.parent.mkdir(parents=True, exist_ok=True)

        logger.info(f"Invoking agent: {requirement[:80]}...")
        _cli_create_core(requirement, out, model=self._model,
                           max_turns=self._max_turns, backend=self._backend)

        if out.exists() and out.stat().st_size > 0:
            self._generated_path = out
            self._generated_content = out.read_text()
            logger.info(f"Agent produced: {out} ({out.stat().st_size} bytes)")
        else:
            raise RuntimeError(f"Agent did not produce a .robot file at {out}")
        return str(out)

    @keyword("Generated Suite Should Pass BDD Validation")
    def generated_suite_should_pass_bdd_validation(self) -> None:
        path = self._require_generated()
        errors = validate_bdd(path)
        if errors:
            raise AssertionError(
                "BDD validation failed:\n" + "\n".join(f"  {e}" for e in errors))
        logger.info("BDD validation: PASS")

    @keyword("Generated Suite Should Pass Dryrun")
    def generated_suite_should_pass_dryrun(self) -> None:
        import subprocess
        path = self._require_generated()
        script_dir = Path(__file__).resolve().parent
        result = subprocess.run(
            ["robot", "--dryrun",
             "--output", "NONE", "--log", "NONE", "--report", "NONE",
             "--pythonpath", str(script_dir), str(path)],
            capture_output=True, text=True)
        if result.returncode != 0:
            raise AssertionError(
                f"Dryrun failed (rc={result.returncode}):\n"
                f"{result.stdout}\n{result.stderr}")
        logger.info("Dryrun: PASS")

    @keyword("Generated Suite Should Match Golden Baseline")
    def generated_suite_should_match_golden_baseline(self, golden_path: str) -> None:
        golden = Path(golden_path)
        if not golden.exists():
            raise FileNotFoundError(f"Golden baseline not found: {golden}")
        golden_kw = set(re.findall(
            r'(?:Given|When|Then|And|But)\s+(.+?)(?:\s{2,}|$)',
            golden.read_text(), re.MULTILINE))
        gen_kw = set(re.findall(
            r'(?:Given|When|Then|And|But)\s+(.+?)(?:\s{2,}|$)',
            self._generated_content, re.MULTILINE))
        missing = golden_kw - gen_kw
        if missing:
            raise AssertionError(
                f"Missing keywords from golden:\n"
                + "\n".join(f"  - {k}" for k in sorted(missing)))
        logger.info("Golden baseline: PASS")


def _load_orient_cache() -> str:
    """Load pre-baked orient material from SKILL.md.

    Reads the all-in-one SKILL.md once at generate time so the agent
    doesn't need to spend LLM turns reading files.
    """
    skill_dir = Path(__file__).resolve().parent.parent
    p = skill_dir / "SKILL.md"
    if p.exists():
        return p.read_text()
    return ""


def _build_generate_prompt(requirement: str, output: Path,
                           fast: bool = False) -> str:
    """Build the agent prompt for suite generation.

    If fast=True, inlines the keyword reference and format docs into the
    prompt so the agent skips orient file reads (~6s saving).
    """
    skill_dir = Path(__file__).resolve().parent.parent

    header = f"{requirement}\n\nWrite the generated .robot suite to: {output}\n\n"

    if fast:
        orient_block = (
            "## Pre-loaded skill reference (no need to read files)\n\n"
            + _load_orient_cache()
            + "\n\n"
            "You already have the full keyword API and format docs above. "
            "Do NOT read any files from the repo — everything you need is in this prompt. "
            "Go straight to /rrpa-explore with agent-browser.\n\n"
            "## agent-browser cheatsheet (use `npx agent-browser` or just `agent-browser`)\n"
            "```\n"
            "npx agent-browser open <url>          # navigate (persistent session)\n"
            "npx agent-browser snapshot -c -d 3    # accessibility tree with classes\n"
            "npx agent-browser get count '<css>'   # count matching elements\n"
            "npx agent-browser get text '<css>'    # get text content\n"
            "npx agent-browser get html '<css>'    # get outer HTML\n"
            "npx agent-browser eval '<js>'         # run JS expression\n"
            "npx agent-browser click '<css>'       # click element\n"
            "```\n"
            "Chain with && to reuse session.\n\n"
        )
    else:
        orient_block = (
            "1. /rrpa-orient — read SKILL.md to understand available "
            "WiseRpaBDD keywords, patterns, and validation.\n"
        )

    return (
        header
        + "Follow the wise-rpa-bdd skill phases:\n"
        + orient_block
        + "2. /rrpa-explore — use `agent-browser` CLI via Bash to explore the live site. "
        "Do NOT use curl or WebFetch. Do NOT guess selectors or URLs. "
        "Every selector AND entry URL must come from agent-browser inspection. "
        "For sites requiring search or login, interact with the UI to discover "
        "the real URL (with place_id, session tokens, etc.) — never construct "
        "URLs by guessing parameter names. "
        "Be efficient — confirm selectors on representative pages, "
        "do not crawl every page. agent-browser keeps a persistent session.\n"
        "3. /rrpa-draft — draft the .robot suite using WiseRpaBDD keywords "
        "grounded in explore evidence. Include quality gates.\n"
        "   GENERALIZABILITY: put all dynamic values (city, dates, prices, "
        "guest count, etc.) in *** Variables *** so users can override from "
        "the command line with --variable. Never hardcode place_id, session "
        "tokens, or values that change per search.\n"
        "   KEYWORD PREFERENCE: prefer deferred BDD keywords (When I click "
        "locator, When I type, etc.) over And I evaluate js. Use evaluate_js "
        "only for patterns the framework can't express yet (calendar loops, "
        "URL param navigation). Use And I configure interrupts for popup "
        "dismissal, not inline JS dismiss hacks.\n"
        "   CONSTRUCT PATTERNS — choose the right one for the site:\n"
        "   - Basic (pagination): single resource, paginate by next/numeric, "
        "extract fields per page. E.g. quotes, book listings.\n"
        "   - Interactive (forms): state checks between actions, await= on "
        "form submissions, And I pause interrupts on interactive panels. "
        "E.g. search forms, login flows, date pickers.\n"
        "   - Multi-resource (discovery→detail): resource 1 extracts URLs, "
        "resource 2 consumes them via {field} template. E.g. product "
        "listing → product detail pages.\n"
        "   - Stealth (anti-bot): WISE_RPA_STEALTH=1 (default), uses nodriver "
        "(raw CDP, no Playwright artifacts). For DataDome/Cloudflare sites. "
        "Set WISE_RPA_STEALTH=0 for Playwright (faster, no anti-bot bypass).\n"
        f"4. /rrpa-review — run `robot --dryrun --pythonpath {skill_dir / 'scripts'}` "
        "to verify all keywords resolve. Fix and loop until clean.\n"
        "5. /rrpa-re-explore (if needed) — after dryrun passes, go back to "
        "agent-browser to verify any selectors you're unsure about, check "
        "for popups/overlays that need interrupts, or confirm pagination "
        "behavior. Then revise the suite and re-review. This step is "
        "especially important for complex sites with auth flows, overlays, "
        "or dynamically loaded content.\n"
        "Do NOT run the suite against a live site for full scraping."
    )


_BACKEND_DEFAULTS = {
    "claude": "sonnet",
    "codex": "gpt-5.4-mini",
    "aichat": "",
}


def _run_agent_cli(prompt: str, backend: str = "claude",
                    model: str = "", max_turns: int = 50) -> int:
    """Run an agent CLI with a prompt. All backends are subprocess calls.

    Backends:
      claude  — claude CLI (honors ~/.claude/ settings and skills)
      codex   — OpenAI Codex CLI
      aichat  — aichat CLI
    """
    import subprocess
    import shutil

    model = model or _BACKEND_DEFAULTS.get(backend, "")
    skill_dir = Path(__file__).resolve().parent.parent

    if backend == "claude":
        exe = shutil.which("claude")
        if not exe:
            raise FileNotFoundError("claude CLI not found in PATH")
        cmd = [exe, "--dangerously-skip-permissions",
               "--model", model, "--max-turns", str(max_turns),
               "-p", prompt]
        return subprocess.run(cmd, cwd=str(skill_dir)).returncode

    elif backend == "codex":
        exe = shutil.which("codex")
        if not exe:
            raise FileNotFoundError("codex CLI not found in PATH")
        cmd = [exe, "exec", "--dangerously-bypass-approvals-and-sandbox",
               "-m", model, "-c", "model_reasoning_effort=low",
               "-C", str(skill_dir), prompt]
        return subprocess.run(cmd).returncode

    elif backend == "aichat":
        exe = shutil.which("aichat")
        if not exe:
            raise FileNotFoundError("aichat CLI not found in PATH")
        cmd = [exe, "-r", "coder", prompt]
        return subprocess.run(cmd, cwd=str(skill_dir)).returncode

    else:
        raise ValueError(f"Unknown backend: {backend}")


def _cli_create_core(requirement: str, output: Path, model: str = "",
                     max_turns: int = 50, backend: str = "claude",
                     fast: bool = False) -> int:
    """Core agent creation logic shared by CLI and RF keyword."""
    prompt = _build_generate_prompt(requirement, output, fast=fast)
    return _run_agent_cli(prompt, backend=backend, model=model,
                          max_turns=max_turns)


# ---------------------------------------------------------------------------
# CLI — Endow phase
# ---------------------------------------------------------------------------

_SUITE_SEED = """\
*** Settings ***
Documentation     {description}
Library           WiseRpaBDD
Suite Setup       Given I start deployment "${{DEPLOYMENT}}"
Suite Teardown    Then I finalize deployment

*** Variables ***
${{DEPLOYMENT}}       {name}
${{ARTIFACT}}         records

# --- Requirement ---
# {requirement}
# ------------------

*** Test Cases ***
# To be generated: WiseRpaBDD.py generate {name}/
"""


def _cli_init(args: list[str]) -> int:
    """Scaffold a new project with an optional requirement seed."""
    import argparse
    parser = argparse.ArgumentParser(prog="WiseRpaBDD.py init")
    parser.add_argument("name", help="Project name (becomes directory name)")
    parser.add_argument("requirement", nargs="?", default="",
                        help="Requirement prompt (inline string or path to .txt/.md)")
    parsed = parser.parse_args(args)

    project = Path(parsed.name)
    if project.exists():
        print(f"Error: directory already exists: {project}")
        return 1

    # Read requirement from file if path given
    req = parsed.requirement
    if req:
        req_path = Path(req)
        if req_path.is_file():
            req = req_path.read_text().strip()
            print(f"Read requirement from: {req_path}")

    project.mkdir(parents=True)
    (project / "evidence").mkdir()
    (project / "output").mkdir()

    # Save requirement as standalone file for generate to pick up
    if req:
        (project / "requirement.md").write_text(req + "\n")

    desc = req.split("\n")[0][:80] if req else parsed.name
    suite = _SUITE_SEED.format(name=parsed.name, description=desc,
                               requirement=req.replace("\n", "\n# ") if req else "(none)")
    (project / "suite.robot").write_text(suite)

    print(f"Created project: {project}/")
    print(f"  suite.robot       seed suite")
    if req:
        print(f"  requirement.md    requirement prompt")
    print(f"  evidence/         for explore snapshots")
    print(f"  output/           for run output")
    print()
    hint_path = f'"{project}/"' if " " in str(project) else f"{project}/"
    print(f"Next: WiseRpaBDD.py generate {hint_path}")
    return 0


def _cli_doctor(args: list[str]) -> int:
    """Validate environment: check all required tools are installed."""
    if args and (args[0] in ("--help", "-h")):
        print("Usage: WiseRpaBDD.py doctor")
        print()
        print("Checks that all required tools and libraries are installed.")
        return 0

    import shutil
    import subprocess as sp

    checks: list[tuple[str, bool, str]] = []

    # Python version
    import sys
    py_ok = sys.version_info >= (3, 11)
    checks.append((f"Python {sys.version_info.major}.{sys.version_info.minor}",
                    py_ok, "Requires Python >= 3.11"))

    # robotframework
    try:
        import robot  # type: ignore
        checks.append((f"robotframework {robot.version.VERSION}", True, ""))
    except ImportError:
        checks.append(("robotframework", False, "uv pip install robotframework"))

    # robotframework-browser
    try:
        import Browser  # type: ignore  # noqa: N811
        checks.append(("robotframework-browser", True, ""))
    except ImportError:
        checks.append(("robotframework-browser", False,
                        "uv pip install robotframework-browser && rfbrowser init"))

    # nodriver
    try:
        import nodriver  # type: ignore
        checks.append(("nodriver", True, ""))
    except ImportError:
        checks.append(("nodriver", False, "uv pip install nodriver"))

    # patchright
    try:
        import patchright  # type: ignore
        checks.append(("patchright", True, ""))
    except ImportError:
        checks.append(("patchright", False, "uv pip install patchright"))

    # agent-browser
    ab_ok = False
    ab = shutil.which("agent-browser")
    if ab:
        ab_ok = True
    else:
        try:
            r = sp.run(["npx", "agent-browser", "--version"],
                        capture_output=True, text=True, timeout=15)
            ab_ok = r.returncode == 0
        except (FileNotFoundError, sp.TimeoutExpired):
            ab_ok = False
    checks.append(("agent-browser", ab_ok,
                    "npm install -g @anthropic-ai/agent-browser"))

    # report
    ok_all = True
    for name, ok, hint in checks:
        mark = "\033[32m✓\033[0m" if ok else "\033[31m✗\033[0m"
        line = f"  {mark} {name}"
        if not ok:
            line += f"  — {hint}"
            ok_all = False
        print(line)

    if ok_all:
        print("\nEnvironment ready. Run: WiseRpaBDD.py init <name> [requirement]")
    else:
        # Collect install commands for missing deps
        pip_pkgs = []
        npm_pkgs = []
        for name, ok, hint in checks:
            if ok or not hint:
                continue
            if "uv pip" in hint:
                # extract package name from "uv pip install <pkg>"
                pkg = hint.split("uv pip install ")[-1].split(" &&")[0]
                pip_pkgs.append(pkg)
            elif "npm install" in hint:
                npm_pkgs.append(hint)

        print()
        if pip_pkgs:
            print(f"  Fix:  uv pip install {' '.join(pip_pkgs)}")
        if npm_pkgs:
            for cmd in npm_pkgs:
                print(f"  Fix:  {cmd}")
        print()
        print("Then re-run: WiseRpaBDD.py doctor")
    return 0 if ok_all else 1


# ---------------------------------------------------------------------------
# CLI — Explore phase (explore + create in one flow)
# ---------------------------------------------------------------------------

def _explore_url(url: str, evidence_dir: Path) -> Path | None:
    """Explore a URL via agent-browser and save evidence. Returns evidence path."""
    import subprocess as sp
    from datetime import datetime
    from urllib.parse import urlparse

    evidence_dir.mkdir(parents=True, exist_ok=True)
    domain = urlparse(url).netloc.replace(":", "_")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    print(f"Exploring {url} ...")
    r = sp.run(["npx", "agent-browser", "open", url],
               capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        print(f"Error opening URL: {r.stderr.strip()}")
        return None

    print("Capturing accessibility snapshot ...")
    r = sp.run(["npx", "agent-browser", "snapshot", "-c", "-d", "3"],
               capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        print(f"Error capturing snapshot: {r.stderr.strip()}")
        return None

    snapshot = r.stdout.strip()
    out_file = evidence_dir / f"{domain}_{ts}.txt"
    out_file.write_text(snapshot)
    print(f"Evidence saved: {out_file} ({len(snapshot)} chars)")

    sp.run(["npx", "agent-browser", "close"],
           capture_output=True, text=True, timeout=10)
    return out_file


# ---------------------------------------------------------------------------
# CLI — Exploit phase
# ---------------------------------------------------------------------------

def _cli_run(args: list[str]) -> int:
    """Run a .robot suite live (full browser execution)."""
    if not args or args == ["--help"] or args == ["-h"]:
        print("Usage: WiseRpaBDD.py run <suite.robot> [--fresh|--resume] [robot-args...]")
        print()
        print("  --fresh   Ignore existing checkpoint, start clean")
        print("  --resume  Require checkpoint, error if none exists")
        print("  (default) Auto-resume if checkpoint exists")
        print()
        print("All other flags are passed through to robot.")
        return 0 if "--help" in args or "-h" in args else 1
    import subprocess

    resume_mode = "auto"
    filtered_args = []
    for arg in args:
        if arg == "--fresh":
            resume_mode = "fresh"
        elif arg == "--resume":
            resume_mode = "require"
        else:
            filtered_args.append(arg)

    # Separate robot options from suite paths (files/dirs come last)
    robot_opts = []
    suite_paths = []
    output_dir = ""
    i = 0
    while i < len(filtered_args):
        arg = filtered_args[i]
        if arg.startswith("--"):
            robot_opts.append(arg)
            # Consume the next arg as the option's value if it exists
            if i + 1 < len(filtered_args) and not filtered_args[i + 1].startswith("--"):
                if arg == "--outputdir":
                    output_dir = filtered_args[i + 1]
                robot_opts.append(filtered_args[i + 1])
                i += 1
        else:
            suite_paths.append(arg)
        i += 1

    # If no --outputdir specified, inject one so robot and engine share it
    if not output_dir:
        output_dir = "output"
        robot_opts.extend(["--outputdir", output_dir])

    env = os.environ.copy()
    env["WISE_RPA_RESUME_MODE"] = resume_mode
    env["WISE_RPA_OUTPUT_DIR"] = output_dir

    script_dir = Path(__file__).resolve().parent
    cmd = ["robot", "--pythonpath", str(script_dir)] + robot_opts + suite_paths
    return subprocess.run(cmd, env=env).returncode


def _cli_check(args: list[str]) -> int:
    """BDD lint + keyword dryrun in one pass."""
    import subprocess
    if not args or args == ["--help"] or args == ["-h"]:
        print("Usage: WiseRpaBDD.py check <suite.robot> [...]")
        print()
        print("Runs BDD format lint, then robot --dryrun if lint passes.")
        return 0 if "--help" in args or "-h" in args else 1

    # Collect .robot files from args (skip flags)
    robot_files = [a for a in args if not a.startswith("--")]

    # Phase 1: BDD format lint
    print("── BDD lint ──")
    rc = 0
    for arg in robot_files:
        p = Path(arg)
        if not p.exists():
            print(f"  File not found: {p}")
            rc = 1
            continue
        errors = validate_bdd(p)
        if errors:
            for e in errors:
                print(f"  {e}")
            rc = 1
        else:
            print(f"  ✓ {p}")

    if rc != 0:
        print("\nBDD lint failed — fix errors above before dryrun.")
        return rc

    # Phase 2: keyword resolution dryrun
    print("\n── Dryrun ──")
    script_dir = Path(__file__).resolve().parent
    cmd = [
        "robot", "--dryrun", "--pythonpath", str(script_dir),
        "--output", "NONE", "--log", "NONE", "--report", "NONE",
    ] + args
    dr_rc = subprocess.run(cmd).returncode
    if dr_rc == 0:
        print("  ✓ All keywords resolved")
    return dr_rc


def _cli_generate(args: list[str]) -> int:
    """Agent explores site + generates .robot suite for a project."""
    import argparse
    parser = argparse.ArgumentParser(prog="WiseRpaBDD.py generate")
    parser.add_argument("project", help="Project directory (from init)")
    parser.add_argument("--requirement", "-r", default="",
                        help="Override requirement (inline string or path to file)")
    parser.add_argument("--backend", default="claude",
                        choices=["claude", "codex", "aichat"],
                        help="Agent backend (default: claude)")
    parser.add_argument("--model", default="",
                        help="Model name (default: per-backend)")
    parser.add_argument("--max-turns", type=int, default=50)
    parser.add_argument("--fast", action="store_true",
                        help="Pre-bake keyword docs into prompt (skip orient reads)")
    parsed = parser.parse_args(args)

    project = Path(parsed.project)
    if not project.is_dir():
        print(f"Error: not a project directory: {project}")
        print(f"Run: WiseRpaBDD.py init {project}")
        return 1

    # Resolve requirement: CLI override > project/requirement.md
    req = parsed.requirement
    if req:
        req_path = Path(req)
        if req_path.is_file():
            req = req_path.read_text().strip()
            print(f"Read requirement from: {req_path}")
    else:
        req_file = project / "requirement.md"
        if req_file.is_file():
            req = req_file.read_text().strip()
            print(f"Using project requirement: {req_file}")
        else:
            print(f"Error: no requirement found. Pass --requirement or "
                  f"create {req_file}")
            return 1

    out = project / "suite.robot"

    try:
        _cli_create_core(req, out,
                         model=parsed.model, max_turns=parsed.max_turns,
                         backend=parsed.backend, fast=parsed.fast)
    except ImportError as e:
        print(f"Error: missing dependency for '{parsed.backend}' backend: {e}")
        return 1

    if out.exists() and out.stat().st_size > 0:
        print(f"Generated: {out} ({out.stat().st_size} bytes)")
        return 0
    else:
        print(f"Error: agent did not produce a .robot file at {out}")
        return 1


def main() -> int:
    import sys
    if len(sys.argv) < 2 or sys.argv[1] in ("--help", "-h"):
        print("WiseRpaBDD — BDD-driven browser automation (RPA + scraping)")
        print()
        print("Usage: WiseRpaBDD.py <command> [args...]")
        print()
        print("  doctor                                    Check & setup environment")
        print("  init      <name> [requirement]            Scaffold project + seed suite")
        print("  generate  <project/> [-r requirement]     Agent explores + generates suite")
        print("  check     <suite.robot> [...]             BDD lint + keyword dryrun")
        print("  run       <suite.robot> [--fresh|--resume] Run suite live")
        print()
        print("Getting started:")
        print("  1. WiseRpaBDD.py doctor        # check environment, install deps")
        print("  2. WiseRpaBDD.py init my-project \"Scrape quotes from toscrape.com\"")
        print("  3. WiseRpaBDD.py generate my-project/")
        print("  4. WiseRpaBDD.py check my-project/suite.robot")
        print("  5. WiseRpaBDD.py run my-project/suite.robot")
        return 0 if "--help" in sys.argv or "-h" in sys.argv else 1

    cmd = sys.argv[1]
    rest = sys.argv[2:]

    dispatch = {
        "init": _cli_init,
        "doctor": _cli_doctor,
        "generate": _cli_generate,
        "create": _cli_generate,  # alias
        "check": _cli_check,
        "run": _cli_run,
    }

    handler = dispatch.get(cmd)
    if handler:
        return handler(rest)
    else:
        print(f"Unknown command: {cmd}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
