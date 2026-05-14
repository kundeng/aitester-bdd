"""AITester — Robot Framework keyword library for BDD verification suites.

Plan-then-Execute model (architecture borrowed from WISE RPA BDD):
- Phase A: RF keywords called sequentially build an in-memory rule tree.
- Phase B: `Then I finalize verification` walks the tree using a live browser
  and evaluates checks; emits a Verdict.

The engine has ONE concept for "did the page reach the expected state?" —
the position-determined StateCheck:
- Before any action in the rule -> guard (no wait, fail = skip rule)
- After an action               -> observation/assertion (wait with timeout,
                                    fail = fail rule)

Robot's `Then` is a human-reader grammar word; the engine treats it the same
as a post-action `And`. There is no separate "Assertion" concept.

This file is the public face for `.robot` files. Internal engine primitives
live under aitester_bdd.engine.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from robot.api import logger
from robot.api.deco import keyword, library

log = logging.getLogger("aitester_bdd")


# ---------------------------------------------------------------------------
# Plan dataclasses — the rule tree built up at definition time.
# ---------------------------------------------------------------------------


@dataclass
class StateCheck:
    """Position-determined state check.

    The single concept the engine uses for guards, observation gates, and
    assertions. The walker's position (before-action vs after-action)
    determines whether the check is wait-free (guard) or timeout-bounded
    (observation/assertion).
    """

    kind: str
    # Kinds (single flat enum):
    #   URL                : url_contains | url_matches | url_not_contains
    #   Element existence  : selector_exists | selector_missing
    #   Element counts     : count_eq | count_at_least | count_at_most
    #   Element text       : has_text | contains | matches | not_contains
    #   Element state      : visible | hidden | enabled | disabled | checked
    #   Element class      : has_class | not_class
    #   Element attribute  : attr_eq | attr_contains
    #   Form               : input_value | select_selected
    #   Network            : last_status | last_body_contains
    #   Backend (live API) : api_returns
    #   Semantic (AI)      : semantic
    locator: str = ""           # CSS selector for element-scoped checks (empty for url_*, last_*, api_*)
    expected: str = ""          # the expected value / pattern / count / status
    extra: dict[str, Any] = field(default_factory=dict)  # e.g., {"attr": "data-state"}


@dataclass
class Action:
    """A browser action."""

    kind: str  # open | reload | back | click | click_text | dblclick |
               # type | type_secret | select | check | uncheck | hover | focus |
               # press | upload | scroll | wait_idle | screenshot |
               # js | call_keyword | browser_step
    target: str = ""
    value: str = ""
    options: dict[str, Any] = field(default_factory=dict)


@dataclass
class Rule:
    """A named block of guards, actions, observations/assertions."""

    name: str
    parents: list[str] = field(default_factory=list)
    items: list[Any] = field(default_factory=list)  # StateCheck | Action in declaration order

    @property
    def has_action(self) -> bool:
        return any(isinstance(it, Action) for it in self.items)


@dataclass
class Scenario:
    """One test-case scope — a tree of rules, all sharing entry URL + setup."""

    name: str
    entry_url: str = ""
    rules: dict[str, Rule] = field(default_factory=dict)
    current_rule: Optional[str] = None


@dataclass
class StateSetup:
    """Auth/state-setup actions to run before any scenario in the suite."""

    skip_when: str = ""
    actions: list[dict[str, str]] = field(default_factory=list)


@dataclass
class InterruptConfig:
    """Surgical dismiss selectors for overlays (cookie banners etc)."""

    dismiss_selectors: list[str] = field(default_factory=list)


@dataclass
class Hook:
    name: str
    point: str  # before_scenario | after_scenario | before_rule | after_rule | on_failure
    keyword_name: str = ""


@dataclass
class Verification:
    """The whole run — one per .robot suite."""

    name: str = ""
    scenarios: list[Scenario] = field(default_factory=list)
    state_setup: StateSetup = field(default_factory=StateSetup)
    interrupts: InterruptConfig = field(default_factory=InterruptConfig)
    hooks: list[Hook] = field(default_factory=list)
    current_scenario: Optional[int] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_options(raw: tuple[str, ...]) -> dict[str, str]:
    """Parse k=v args (e.g. `await=#foo timeout=5000`) into a dict."""
    out: dict[str, str] = {}
    for item in raw:
        if "=" not in item:
            continue
        k, _, v = item.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _strip_quotes(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and ((s[0] == s[-1] == '"') or (s[0] == s[-1] == "'")):
        return s[1:-1]
    return s


# ---------------------------------------------------------------------------
# The Library
# ---------------------------------------------------------------------------


@library(scope="SUITE", auto_keywords=False)
class AITester:
    """LLM-authored BDD verification suite runtime.

    Keywords build a rule tree during test definition; `Then I finalize
    verification` walks the tree against a live browser.
    """

    def __init__(self) -> None:
        self._v = Verification()

    # ------------------------------------------------------------------
    # Verification Lifecycle
    # ------------------------------------------------------------------

    @keyword("Given I start verification \"${name}\"")
    def start_verification(self, name: str) -> None:
        """Initialize a verification run (call in Suite Setup)."""
        self._v = Verification(name=_strip_quotes(name))
        logger.info(f"[aitester] verification started: {self._v.name}")

    @keyword("Then I finalize verification")
    def finalize_verification(self) -> None:
        """Walk the rule tree against a live browser; emit Verdict (Suite Teardown)."""
        from aitester_bdd.engine.walk import walk_verification

        verdict = walk_verification(self._v)
        if verdict.failed:
            raise AssertionError(verdict.format_failure())

    @keyword("Given I start scenario \"${name}\" at \"${url}\"")
    def start_scenario_at(self, name: str, url: str) -> None:
        """Begin one scenario at an entry URL (Test Setup)."""
        s = Scenario(name=_strip_quotes(name), entry_url=_strip_quotes(url))
        self._v.scenarios.append(s)
        self._v.current_scenario = len(self._v.scenarios) - 1

    @keyword("Given I start scenario \"${name}\"")
    def start_scenario(self, name: str) -> None:
        """Begin one scenario without static entry URL."""
        s = Scenario(name=_strip_quotes(name))
        self._v.scenarios.append(s)
        self._v.current_scenario = len(self._v.scenarios) - 1

    # ------------------------------------------------------------------
    # Rules
    # ------------------------------------------------------------------

    @keyword("I define rule \"${name}\"")
    def define_rule(self, name: str) -> None:
        """Begin a named rule block within the current scenario."""
        if self._v.current_scenario is None:
            raise RuntimeError("define rule called outside any scenario; use [Setup] Given I start scenario first")
        sc = self._v.scenarios[self._v.current_scenario]
        rname = _strip_quotes(name)
        sc.rules[rname] = Rule(name=rname)
        sc.current_rule = rname

    @keyword("And I declare parents \"${names}\"")
    def declare_parents(self, names: str) -> None:
        """Declare comma-separated parent rule names for the current rule."""
        rule = self._current_rule()
        for p in _strip_quotes(names).split(","):
            p = p.strip()
            if p:
                rule.parents.append(p)

    # ------------------------------------------------------------------
    # State Checks — URL
    # ------------------------------------------------------------------

    @keyword("Given url contains \"${pattern}\"")
    def given_url_contains(self, pattern: str) -> None:
        self._current_rule().items.append(StateCheck("url_contains", expected=_strip_quotes(pattern)))

    @keyword("And url contains \"${pattern}\"")
    def and_url_contains(self, pattern: str) -> None:
        self._current_rule().items.append(StateCheck("url_contains", expected=_strip_quotes(pattern)))

    @keyword("Then url contains \"${pattern}\"")
    def then_url_contains(self, pattern: str) -> None:
        self._current_rule().items.append(StateCheck("url_contains", expected=_strip_quotes(pattern)))

    @keyword("Given url matches \"${pattern}\"")
    def given_url_matches(self, pattern: str) -> None:
        self._current_rule().items.append(StateCheck("url_matches", expected=_strip_quotes(pattern)))

    @keyword("Then url matches \"${pattern}\"")
    def then_url_matches(self, pattern: str) -> None:
        self._current_rule().items.append(StateCheck("url_matches", expected=_strip_quotes(pattern)))

    @keyword("But url does not contain \"${pattern}\"")
    def but_url_does_not_contain(self, pattern: str) -> None:
        self._current_rule().items.append(StateCheck("url_not_contains", expected=_strip_quotes(pattern)))

    # ------------------------------------------------------------------
    # State Checks — Element Existence
    # ------------------------------------------------------------------

    @keyword("And selector \"${css}\" exists")
    def and_selector_exists(self, css: str) -> None:
        self._current_rule().items.append(StateCheck("selector_exists", locator=_strip_quotes(css)))

    @keyword("Given selector \"${css}\" exists")
    def given_selector_exists(self, css: str) -> None:
        self._current_rule().items.append(StateCheck("selector_exists", locator=_strip_quotes(css)))

    @keyword("Then selector \"${css}\" exists")
    def then_selector_exists(self, css: str) -> None:
        self._current_rule().items.append(StateCheck("selector_exists", locator=_strip_quotes(css)))

    @keyword("But selector \"${css}\" does not exist")
    def but_selector_does_not_exist(self, css: str) -> None:
        self._current_rule().items.append(StateCheck("selector_missing", locator=_strip_quotes(css)))

    # ------------------------------------------------------------------
    # State Checks — Counts
    # ------------------------------------------------------------------

    @keyword("Then count of locator \"${css}\" equals ${n}")
    def then_count_eq(self, css: str, n: str) -> None:
        self._current_rule().items.append(StateCheck("count_eq", locator=_strip_quotes(css), expected=str(n)))

    @keyword("Then count of locator \"${css}\" is at least ${n}")
    def then_count_at_least(self, css: str, n: str) -> None:
        self._current_rule().items.append(StateCheck("count_at_least", locator=_strip_quotes(css), expected=str(n)))

    @keyword("Then count of locator \"${css}\" is at most ${n}")
    def then_count_at_most(self, css: str, n: str) -> None:
        self._current_rule().items.append(StateCheck("count_at_most", locator=_strip_quotes(css), expected=str(n)))

    # ------------------------------------------------------------------
    # State Checks — Element Text
    # ------------------------------------------------------------------

    @keyword("Then locator \"${css}\" has text \"${text}\"")
    def then_has_text(self, css: str, text: str) -> None:
        self._current_rule().items.append(StateCheck("has_text", locator=_strip_quotes(css), expected=_strip_quotes(text)))

    @keyword("Then locator \"${css}\" contains \"${substring}\"")
    def then_contains(self, css: str, substring: str) -> None:
        self._current_rule().items.append(StateCheck("contains", locator=_strip_quotes(css), expected=_strip_quotes(substring)))

    @keyword("Then locator \"${css}\" matches \"${regex}\"")
    def then_matches(self, css: str, regex: str) -> None:
        self._current_rule().items.append(StateCheck("matches", locator=_strip_quotes(css), expected=_strip_quotes(regex)))

    @keyword("But locator \"${css}\" does not contain \"${substring}\"")
    def but_not_contains(self, css: str, substring: str) -> None:
        self._current_rule().items.append(StateCheck("not_contains", locator=_strip_quotes(css), expected=_strip_quotes(substring)))

    # ------------------------------------------------------------------
    # State Checks — Element State
    # ------------------------------------------------------------------

    @keyword("Then locator \"${css}\" is visible")
    def then_visible(self, css: str) -> None:
        self._current_rule().items.append(StateCheck("visible", locator=_strip_quotes(css)))

    @keyword("Then locator \"${css}\" is hidden")
    def then_hidden(self, css: str) -> None:
        self._current_rule().items.append(StateCheck("hidden", locator=_strip_quotes(css)))

    @keyword("Then locator \"${css}\" is enabled")
    def then_enabled(self, css: str) -> None:
        self._current_rule().items.append(StateCheck("enabled", locator=_strip_quotes(css)))

    @keyword("Then locator \"${css}\" is disabled")
    def then_disabled(self, css: str) -> None:
        self._current_rule().items.append(StateCheck("disabled", locator=_strip_quotes(css)))

    @keyword("Then locator \"${css}\" is checked")
    def then_checked(self, css: str) -> None:
        self._current_rule().items.append(StateCheck("checked", locator=_strip_quotes(css)))

    @keyword("Then locator \"${css}\" has class \"${name}\"")
    def then_has_class(self, css: str, name: str) -> None:
        self._current_rule().items.append(StateCheck("has_class", locator=_strip_quotes(css), expected=_strip_quotes(name)))

    @keyword("But locator \"${css}\" does not have class \"${name}\"")
    def but_not_class(self, css: str, name: str) -> None:
        self._current_rule().items.append(StateCheck("not_class", locator=_strip_quotes(css), expected=_strip_quotes(name)))

    @keyword("Then locator \"${css}\" has attribute \"${attr}\" equal to \"${value}\"")
    def then_attr_eq(self, css: str, attr: str, value: str) -> None:
        self._current_rule().items.append(
            StateCheck("attr_eq", locator=_strip_quotes(css), expected=_strip_quotes(value), extra={"attr": _strip_quotes(attr)})
        )

    @keyword("Then locator \"${css}\" has attribute \"${attr}\" containing \"${substring}\"")
    def then_attr_contains(self, css: str, attr: str, substring: str) -> None:
        self._current_rule().items.append(
            StateCheck("attr_contains", locator=_strip_quotes(css), expected=_strip_quotes(substring), extra={"attr": _strip_quotes(attr)})
        )

    @keyword("Then input \"${css}\" has value \"${value}\"")
    def then_input_value(self, css: str, value: str) -> None:
        self._current_rule().items.append(StateCheck("input_value", locator=_strip_quotes(css), expected=_strip_quotes(value)))

    @keyword("Then select \"${css}\" has selected \"${value}\"")
    def then_select_selected(self, css: str, value: str) -> None:
        self._current_rule().items.append(StateCheck("select_selected", locator=_strip_quotes(css), expected=_strip_quotes(value)))

    # ------------------------------------------------------------------
    # State Checks — Network / API
    # ------------------------------------------------------------------

    @keyword("Then last response status equals ${code}")
    def then_last_status(self, code: str) -> None:
        self._current_rule().items.append(StateCheck("last_status", expected=str(code)))

    @keyword("Then last response body contains \"${text}\"")
    def then_last_body_contains(self, text: str) -> None:
        self._current_rule().items.append(StateCheck("last_body_contains", expected=_strip_quotes(text)))

    @keyword("Then api \"${path}\" returns \"${field}\" equal to \"${value}\"")
    def then_api_returns(self, path: str, field_: str, value: str) -> None:
        self._current_rule().items.append(
            StateCheck("api_returns", expected=_strip_quotes(value),
                       extra={"path": _strip_quotes(path), "field": _strip_quotes(field_)})
        )

    # ------------------------------------------------------------------
    # State Checks — Semantic (AI-judged)
    # ------------------------------------------------------------------

    @keyword("Then content of locator \"${css}\" semantically matches \"${prompt}\"")
    def then_semantic_locator(self, css: str, prompt: str) -> None:
        self._current_rule().items.append(
            StateCheck("semantic", locator=_strip_quotes(css), expected=_strip_quotes(prompt), extra={"scope": "locator"})
        )

    @keyword("Then page semantically matches \"${prompt}\"")
    def then_semantic_page(self, prompt: str) -> None:
        self._current_rule().items.append(
            StateCheck("semantic", expected=_strip_quotes(prompt), extra={"scope": "page"})
        )

    # ------------------------------------------------------------------
    # Actions — Navigation
    # ------------------------------------------------------------------

    @keyword("When I open \"${url}\"")
    def when_open(self, url: str) -> None:
        self._current_rule().items.append(Action("open", target=_strip_quotes(url)))

    @keyword("When I reload")
    def when_reload(self) -> None:
        self._current_rule().items.append(Action("reload"))

    @keyword("When I go back")
    def when_go_back(self) -> None:
        self._current_rule().items.append(Action("back"))

    @keyword("When I add url params \"${params}\"")
    def when_add_url_params(self, params: str) -> None:
        self._current_rule().items.append(Action("add_params", value=_strip_quotes(params)))

    # ------------------------------------------------------------------
    # Actions — Interaction
    # ------------------------------------------------------------------

    @keyword("When I click locator \"${css}\"")
    def when_click_locator(self, css: str, *opts: str) -> None:
        self._current_rule().items.append(
            Action("click", target=_strip_quotes(css), options=_parse_options(opts))
        )

    @keyword("When I click text \"${text}\"")
    def when_click_text(self, text: str, *opts: str) -> None:
        self._current_rule().items.append(
            Action("click_text", target=_strip_quotes(text), options=_parse_options(opts))
        )

    @keyword("When I double click locator \"${css}\"")
    def when_double_click_locator(self, css: str) -> None:
        self._current_rule().items.append(Action("dblclick", target=_strip_quotes(css)))

    @keyword("When I type \"${value}\" into locator \"${css}\"")
    def when_type(self, value: str, css: str, *opts: str) -> None:
        self._current_rule().items.append(
            Action(
                "type",
                target=_strip_quotes(css),
                value=_strip_quotes(value),
                options=_parse_options(opts),
            )
        )

    @keyword("When I type secret \"${value}\" into locator \"${css}\"")
    def when_type_secret(self, value: str, css: str, *opts: str) -> None:
        self._current_rule().items.append(
            Action(
                "type_secret",
                target=_strip_quotes(css),
                value=_strip_quotes(value),
                options=_parse_options(opts),
            )
        )

    @keyword("When I select \"${value}\" from locator \"${css}\"")
    def when_select(self, value: str, css: str) -> None:
        self._current_rule().items.append(
            Action("select", target=_strip_quotes(css), value=_strip_quotes(value))
        )

    @keyword("When I check locator \"${css}\"")
    def when_check(self, css: str) -> None:
        self._current_rule().items.append(Action("check", target=_strip_quotes(css)))

    @keyword("When I uncheck locator \"${css}\"")
    def when_uncheck(self, css: str) -> None:
        self._current_rule().items.append(Action("uncheck", target=_strip_quotes(css)))

    @keyword("When I hover locator \"${css}\"")
    def when_hover(self, css: str) -> None:
        self._current_rule().items.append(Action("hover", target=_strip_quotes(css)))

    @keyword("When I focus locator \"${css}\"")
    def when_focus(self, css: str) -> None:
        self._current_rule().items.append(Action("focus", target=_strip_quotes(css)))

    @keyword("When I press keys \"${css}\"")
    def when_press_keys(self, css: str, *keys: str) -> None:
        self._current_rule().items.append(
            Action("press", target=_strip_quotes(css), options={"keys": list(keys)})
        )

    @keyword("When I upload file \"${path}\" to locator \"${css}\"")
    def when_upload_file(self, path: str, css: str) -> None:
        self._current_rule().items.append(
            Action("upload", target=_strip_quotes(css), value=_strip_quotes(path))
        )

    @keyword("When I scroll down")
    def when_scroll_down(self) -> None:
        self._current_rule().items.append(Action("scroll"))

    @keyword("When I wait for idle")
    def when_wait_for_idle(self) -> None:
        self._current_rule().items.append(Action("wait_idle"))

    @keyword("When I take screenshot")
    def when_take_screenshot(self, *opts: str) -> None:
        self._current_rule().items.append(Action("screenshot", options=_parse_options(opts)))

    # ------------------------------------------------------------------
    # Hooks & Interrupts
    # ------------------------------------------------------------------

    @keyword("And I configure interrupts")
    def configure_interrupts(self, *opts: str) -> None:
        kv = _parse_options(opts)
        if "dismiss" in kv:
            self._v.interrupts.dismiss_selectors.append(kv["dismiss"])

    @keyword("And I configure state setup")
    def configure_state_setup(self, *args: str) -> None:
        kv = _parse_options(args)
        if "skip_when" in kv:
            self._v.state_setup.skip_when = kv["skip_when"]
        if "action" in kv:
            self._v.state_setup.actions.append(kv)

    @keyword("And I register hook \"${name}\" at \"${point}\"")
    def register_hook(self, name: str, point: str, *opts: str) -> None:
        kv = _parse_options(opts)
        self._v.hooks.append(
            Hook(name=_strip_quotes(name), point=_strip_quotes(point), keyword_name=kv.get("keyword", ""))
        )

    # ------------------------------------------------------------------
    # Passthrough escape hatches
    # ------------------------------------------------------------------

    @keyword("And I browser step \"${method}\"")
    def browser_step(self, method: str, *args: str) -> None:
        self._current_rule().items.append(
            Action("browser_step", target=_strip_quotes(method), options={"args": list(args)})
        )

    @keyword("And I call keyword \"${name}\"")
    def call_keyword(self, name: str, *args: str) -> None:
        self._current_rule().items.append(
            Action("call_keyword", target=_strip_quotes(name), options={"args": list(args)})
        )

    @keyword("And I evaluate js \"${script}\"")
    def evaluate_js(self, script: str) -> None:
        self._current_rule().items.append(Action("js", value=_strip_quotes(script)))

    # ------------------------------------------------------------------
    # Internal access
    # ------------------------------------------------------------------

    def get_verification(self) -> Verification:
        """Access the built-up Verification — used by walker and tests."""
        return self._v

    def _current_rule(self) -> Rule:
        if self._v.current_scenario is None:
            raise RuntimeError(
                "no current scenario; missing [Setup] Given I start scenario \"name\" at \"...\""
            )
        sc = self._v.scenarios[self._v.current_scenario]
        if sc.current_rule is None:
            raise RuntimeError("no current rule; missing I define rule \"name\"")
        return sc.rules[sc.current_rule]
