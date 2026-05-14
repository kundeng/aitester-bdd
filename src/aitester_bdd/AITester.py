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
class EmitField:
    """One field captured by an Emit step.

    source = text|attr|count|html|value|class|is_visible|is_enabled|is_checked|js
    locator is the CSS selector for everything except `js` (which uses `expr`).
    attr is only required for source=attr.
    """

    name: str
    source: str
    locator: str = ""
    attr: str = ""
    expr: str = ""


@dataclass
class Emit:
    """Capture structured page state without changing it.

    Walker never fails the rule on an Emit (it's an observation, not an
    assertion). Each Emit's data lands in `<output_dir>/emit.jsonl` with
    `trigger=explicit`. The walker also writes `trigger=on_failure`
    records automatically when a rule fails.
    """

    name: str
    fields: list[EmitField] = field(default_factory=list)


@dataclass
class FieldSpec:
    """One field captured by `Then I extract fields`.

    Extractors (mirrors WISE, but `image` is dropped as testing rarely
    needs raw image src):
      text     — `inner_text()` of the locator (trimmed)
      attr     — `getAttribute(attr_name)` on the locator
      grouped  — array of texts from all matches of locator (within scope)
      html     — `outerHTML` of the locator
      link     — href, resolved to absolute URL via urljoin
      number   — text content regex-extracted to int/float
      value    — input's `value` property
      class    — `class` attribute
    """

    name: str
    extractor: str  # text | attr | grouped | html | link | number | value | class
    locator: str
    attr: str = ""


@dataclass
class TableFieldSpec:
    """One column-to-field mapping in `Then I extract table`.

    `header` is the visible text of the column in the table's header row.
    Walker matches this against the actual header texts to find the
    column index, then takes that cell from each data row.
    """

    name: str
    header: str


@dataclass
class TableSpec:
    """Capture a HTML table's rows as records.

    `header_row` is the row index containing column headers (0-based).
    Each subsequent row becomes one record with named fields per the
    `fields` mapping.
    """

    name: str
    locator: str
    header_row: int = 0
    fields: list[TableFieldSpec] = field(default_factory=list)


@dataclass
class ArtifactSchema:
    """Named accumulation bag — multiple rules across a scenario can
    `emit to artifact "${name}"` and their records pile up here.

    `fields` is a list of `{field, type, required}` dicts declared at
    `register artifact` time. Schema is documentary in v1 (not validated
    at emit time); the agent reads it to know what shape to capture.

    `dedupe` is an optional field name; if set, the writer drops records
    where the dedupe field value has been seen before in the same bag.

    `description` is human prose explaining what this artifact captures
    — read by the diagnose aspect when summarizing what went wrong.
    """

    name: str
    fields: list[dict] = field(default_factory=list)
    dedupe: str = ""
    description: str = ""
    output: bool = True  # write to disk at scenario teardown


@dataclass
class QualityGate:
    """Test assertions on an artifact, evaluated at scenario teardown.

    Unlike WISE (which only warns), failing a quality gate FAILS the
    scenario with a synthetic RuleResult — these are real test
    assertions.

    `min_records` — artifact must have at least this many records
    `filled_pcts` — per-field, % of records that must have non-empty value
    `max_failed_pct` — across an expansion run, max % of failed iterations
    """

    min_records: Optional[int] = None
    filled_pcts: dict[str, float] = field(default_factory=dict)
    max_failed_pct: Optional[float] = None


@dataclass
class HookDef:
    """Hook that fires at a named lifecycle point during the walk.

    `lifecycle_point`:
      post_extract — after `_extract_from_scope` builds a record, before
                     the record is emitted to its artifacts. Used to
                     normalize / clean up captured data.

    `config` is a dict of transforms applied in order:
      rename=old:new
      drop=field
      strip_html=field
      lowercase=field
      default=field:value
      regex=field:pattern:replacement
    """

    name: str
    lifecycle_point: str  # post_extract (v1 supports this one)
    config: dict[str, str] = field(default_factory=dict)


@dataclass
class Rule:
    """A named block of guards, actions, observations/assertions.

    Position-determined: items list mixes StateChecks and Actions in declaration
    order. Walker splits internally — StateChecks BEFORE the first Action are
    guards (no wait, fail = skip the rule with retry-redo); everything from
    the first Action onward is the body (actions interleaved with inline
    observation gates and post-action assertions; fail = fail the rule).

    Per-rule policy fields mirror WISE (ported with battle-tested defaults):
      - retry_max / retry_delay_ms: retry guards N times, replaying steps each retry
      - interrupt_paused: skip auto-dismiss of overlays for this rule
      - interrupt_override: replace the global dismiss-selectors for this rule
      - guard_policy: 'skip' (default) or 'abort' (raise to stop the whole walk)
      - options: 'timeout_ms', 'on_enter', 'on_fail' (the latter two: 'screenshot')

    Capture pipeline (ported from WISE for the testing use case):
      - field_specs: `Then I extract fields` declarations
      - table_spec: `Then I extract table` declaration (one per rule)
      - emit_targets: artifacts this rule pushes its extracted record into
      - emit_flatten_by: {artifact_name: field_name} — flatten that array
                        field into one record per element
      - emit_merge_on: {artifact_name: key_field} — merge into existing
                       artifact record matching this key
    """

    name: str
    parents: list[str] = field(default_factory=list)
    items: list[Any] = field(default_factory=list)  # StateCheck | Action | Emit in declaration order
    retry_max: int = 0
    retry_delay_ms: int = 1000
    interrupt_paused: bool = False
    interrupt_override: Optional[list[str]] = None  # None = inherit verification's list
    guard_policy: str = "skip"
    options: dict[str, str] = field(default_factory=dict)
    # Capture pipeline (TIER 1 — artifact model)
    field_specs: list[FieldSpec] = field(default_factory=list)
    table_spec: Optional[TableSpec] = None
    emit_targets: list[str] = field(default_factory=list)
    emit_flatten_by: dict[str, str] = field(default_factory=dict)
    emit_merge_on: dict[str, str] = field(default_factory=dict)

    @property
    def has_action(self) -> bool:
        return any(isinstance(it, Action) for it in self.items)

    @property
    def has_capture(self) -> bool:
        """True if this rule extracts a record (fields or table)."""
        return bool(self.field_specs) or self.table_spec is not None


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
class Verification:
    """The whole run — one per .robot suite."""

    name: str = ""
    scenarios: list[Scenario] = field(default_factory=list)
    state_setup: StateSetup = field(default_factory=StateSetup)
    interrupts: InterruptConfig = field(default_factory=InterruptConfig)
    hooks: list[HookDef] = field(default_factory=list)
    # Artifact model — declared per-verification, scoped per-scenario at write time.
    artifacts: dict[str, ArtifactSchema] = field(default_factory=dict)
    artifact_store: dict[str, list] = field(default_factory=dict)
    quality_gates: dict[str, QualityGate] = field(default_factory=dict)  # per-artifact
    write_overrides: dict[str, str] = field(default_factory=dict)  # artifact_name -> path
    current_scenario: Optional[int] = None
    current_artifact: Optional[str] = None  # most-recently-mentioned artifact (for QG attach)


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


def _parse_field_specs(raw: tuple[str, ...]) -> list:
    """Parse `field=N extractor=E locator=L attr=A ...` rows.

    Same continuation-row grammar as WISE's `_parse_field_specs`. Each
    `field=` starts a new FieldSpec; sibling `extractor=/locator=/attr=`
    belong to the most recent `field=`. Robust to both 2+-space and
    1-space arg separators.
    """
    tokens: list[str] = []
    for spec in raw:
        for tok in spec.split():
            if tok:
                tokens.append(tok)
    fields: list = []
    current: dict[str, str] = {}

    def flush() -> None:
        if current.get("field"):
            fields.append(
                FieldSpec(
                    name=current["field"],
                    extractor=current.get("extractor", "text"),
                    locator=_strip_quotes(current.get("locator", "")),
                    attr=_strip_quotes(current.get("attr", "")),
                )
            )

    for tok in tokens:
        if "=" not in tok:
            continue
        k, _, v = tok.partition("=")
        k = k.strip()
        if k == "field":
            flush()
            current = {"field": v.strip()}
        else:
            current[k] = v.strip()
    flush()
    return fields


def _parse_table_specs(raw: tuple[str, ...]) -> tuple[int, list]:
    """Parse `header_row=N field=X header=Y ...` rows.

    Returns (header_row, list[TableFieldSpec]).
    """
    tokens: list[str] = []
    for spec in raw:
        for tok in spec.split():
            if tok:
                tokens.append(tok)
    header_row = 0
    fields: list = []
    current: dict[str, str] = {}

    def flush() -> None:
        if current.get("field"):
            fields.append(
                TableFieldSpec(
                    name=current["field"],
                    header=_strip_quotes(current.get("header", current["field"])),
                )
            )

    for tok in tokens:
        if "=" not in tok:
            continue
        k, _, v = tok.partition("=")
        k = k.strip()
        if k == "header_row":
            try:
                header_row = int(v)
            except (ValueError, TypeError):
                pass
            continue
        if k == "field":
            flush()
            current = {"field": v.strip()}
        else:
            current[k] = v.strip()
    flush()
    return header_row, fields


def _parse_emit_fields(raw: tuple[str, ...]) -> list:
    """Parse `field=N source=S locator=L attr=A expr=E ...` rows.

    Each `field=` starts a new EmitField. Following `source=/locator=/
    attr=/expr=` keys belong to the most recent `field=`. Mirrors WISE's
    `_parse_field_specs` shape so the agent's grammar is familiar.

    Robust to either spacing: if RF gives us each k=v as a separate arg
    (2+ spaces between, the strict-RF case) OR if multiple k=v pairs
    arrive in a single arg (1-space separator, the lenient case), both
    work. Tokens are re-split on whitespace before key parsing.
    """
    # Flatten: split any space-glued args into their constituent k=v tokens.
    tokens: list[str] = []
    for spec in raw:
        for tok in spec.split():
            if tok:
                tokens.append(tok)

    fields: list = []
    current: dict[str, str] = {}

    def flush() -> None:
        if current.get("field"):
            fields.append(
                EmitField(
                    name=current["field"],
                    source=current.get("source", "text"),
                    locator=_strip_quotes(current.get("locator", "")),
                    attr=_strip_quotes(current.get("attr", "")),
                    expr=current.get("expr", ""),
                )
            )

    for tok in tokens:
        if "=" not in tok:
            continue
        k, _, v = tok.partition("=")
        k = k.strip()
        if k == "field":
            flush()
            current = {"field": v.strip()}
        else:
            current[k] = v.strip()
    flush()
    return fields


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
    # Per-rule policy (ported from WISE — retry, timeout, interrupt scope)
    # ------------------------------------------------------------------

    @keyword("And I set retry ${max} times with ${delay} ms delay")
    def set_retry(self, max_: str, delay: str) -> None:
        """If guards fail, replay body + re-check guards up to ${max} times."""
        rule = self._current_rule()
        rule.retry_max = int(max_)
        rule.retry_delay_ms = int(delay)

    @keyword("And I set guard policy \"${policy}\"")
    def set_guard_policy(self, policy: str) -> None:
        """'skip' (default — return failure) or 'abort' (raise to stop walk)."""
        self._current_rule().guard_policy = _strip_quotes(policy)

    @keyword("And I pause interrupts")
    def pause_interrupts(self) -> None:
        """Suppress dismiss-overlays for this rule (e.g., when testing the modal itself)."""
        self._current_rule().interrupt_paused = True

    @keyword("And I scope interrupts to \"${selectors}\"")
    def scope_interrupts(self, selectors: str) -> None:
        """Replace the verification-wide dismiss-selector list for this rule.
        Pass a comma-separated list of selectors."""
        sels = [s.strip() for s in _strip_quotes(selectors).split(",") if s.strip()]
        self._current_rule().interrupt_override = sels

    @keyword("And I set rule timeout ${ms} ms")
    def set_rule_timeout(self, ms: str) -> None:
        """Per-rule deadline; rule fails if body exceeds it."""
        self._current_rule().options["timeout_ms"] = str(int(ms))

    @keyword("And I screenshot on enter")
    def screenshot_on_enter(self) -> None:
        self._current_rule().options["on_enter"] = "screenshot"

    @keyword("And I screenshot on fail")
    def screenshot_on_fail(self) -> None:
        self._current_rule().options["on_fail"] = "screenshot"

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
    # State Checks — Semantic (AI-judged escape hatch)
    #
    # Use sparingly — deterministic checks (text/count/exists) are
    # cheaper, faster, and more reliable. These are for cases that
    # genuinely need AI-level judgment (semantic meaning of text,
    # visual recognition).
    #
    # Requires AITESTER_LLM_MODEL env to opt in. Without it, the
    # walker fails the check with a "not configured" message rather
    # than silently passing.
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

    @keyword("Then screenshot semantically matches \"${prompt}\"")
    def then_visual_semantic(self, prompt: str) -> None:
        """Take a screenshot and ask the LLM if it satisfies the criterion.
        Use only when text-level checks can't express the assertion
        (visual layout, chart shape, image content, etc.)."""
        self._current_rule().items.append(
            StateCheck("visual_semantic", expected=_strip_quotes(prompt))
        )

    # ------------------------------------------------------------------
    # Emit — capture structured page state to <output_dir>/emit.jsonl
    #
    # Observation only — never fails the rule. Use when the test's
    # intention goes beyond pass/fail (diagnostic probe, differential
    # baseline, bug-repro instrumentation). See SKILL § "Emit" for the
    # decision tree.
    # ------------------------------------------------------------------

    @keyword("And I emit \"${name}\"")
    def and_emit(self, name: str, *specs: str) -> None:
        """Direct emit — ad-hoc capture, writes one record to emit.jsonl.

        Continuation rows (one per field):
          field=<name> source=<text|attr|count|html|value|class|
                              is_visible|is_enabled|is_checked|js>
          locator=<css>     # required for everything except js
          attr=<attr>       # required for source=attr
          expr=<js>         # required for source=js (free-form expression)

        Example:
            And I emit "dashboard_state"
            ...    field=case_count    source=count    locator=".case-row"
            ...    field=first_title   source=text     locator=".case-row:first-child .title"
            ...    field=status        source=attr     locator="[data-testid=status]"  attr=data-state

        For accumulating, schema-typed captures across multiple rules
        with optional dedupe / flatten / merge / quality-gate assertions,
        use the artifact pipeline instead (§ 4 Artifacts / Extract / Emit).
        """
        self._current_rule().items.append(
            Emit(name=_strip_quotes(name), fields=_parse_emit_fields(specs))
        )

    # ------------------------------------------------------------------
    # Artifact pipeline — capture structured records into named bags,
    # accumulating across rules. Quality gates assert against the bag.
    # ------------------------------------------------------------------

    @keyword("Given I register artifact \"${artifact}\"")
    def register_artifact(self, artifact: str, *fields: str) -> None:
        """Declare an artifact bag with a typed schema.

        Continuation rows (one per field):
          field=<name> type=<string|number|url|array|boolean> required=<true|false>

        The schema is documentary in v1 (the agent reads it to know what
        to capture; the engine does not validate against it at emit time).

        Example:
            Given I register artifact "approved_cases"
            ...    field=id         type=string  required=true
            ...    field=status     type=string  required=true
            ...    field=approver   type=string  required=false
        """
        name = _strip_quotes(artifact)
        parsed: list[dict] = []
        current: dict[str, str] = {}
        for spec in fields:
            for tok in spec.split():
                if "=" not in tok:
                    continue
                k, _, v = tok.partition("=")
                k = k.strip()
                if k == "field" and current.get("field"):
                    parsed.append(current.copy())
                    current = {}
                current[k] = v.strip()
        if current.get("field"):
            parsed.append(current.copy())
        self._v.artifacts[name] = ArtifactSchema(name=name, fields=parsed)
        self._v.artifact_store.setdefault(name, [])
        self._v.current_artifact = name

    @keyword("And I set artifact options for \"${artifact}\"")
    def set_artifact_options(self, artifact: str, *options: str) -> None:
        """Configure an already-registered artifact.

        Options:
          dedupe=<field>        # drop records where this field has been seen
          description=<text>    # human prose for the diagnose aspect
          output=<true|false>   # write to <output_dir>/<name>.jsonl at scenario teardown
                                # (default true)

        Example:
            And I set artifact options for "approved_cases"
            ...    dedupe=id
            ...    description="One record per approved case visible in the dashboard"
        """
        name = _strip_quotes(artifact)
        art = self._v.artifacts.get(name)
        if not art:
            return
        kv = _parse_options(options)
        if "dedupe" in kv:
            art.dedupe = kv["dedupe"]
        if "description" in kv:
            art.description = kv["description"]
        if "output" in kv:
            art.output = kv["output"].lower() in ("true", "1", "yes")

    @keyword("Then I extract fields")
    def extract_fields(self, *specs: str) -> None:
        """Declare the field-extraction spec for the current rule.

        Continuation rows (one per field):
          field=<name>
          extractor=<text|attr|grouped|html|link|number|value|class>
          locator=<css>
          attr=<attr-name>   # required for extractor=attr

        Walker runs this at the end of the rule body (after actions /
        observation gates have passed) to build a record. The record is
        then handed to any `emit to artifact` declared on the same rule.

        Example:
            Then I extract fields
            ...    field=id      extractor=attr   locator="[data-testid=case-row]"  attr=data-case-id
            ...    field=title   extractor=text   locator=".case-title"
            ...    field=link    extractor=link   locator="a.detail"
            ...    field=count   extractor=number locator=".items .badge"
            And I emit to artifact "case_list"
        """
        self._current_rule().field_specs = _parse_field_specs(specs)

    @keyword("Then I extract table \"${name}\" from \"${locator}\"")
    def extract_table(self, name: str, locator: str, *specs: str) -> None:
        """Capture a HTML table's rows as records into the named artifact.

        Continuation rows:
          header_row=<N>           # row index of headers (default 0)
          field=<name> header=<header_text>

        Each data row in the table becomes one record. The walker
        matches `header_text` against the actual header texts at
        `header_row` to find the column index for `field=<name>`.

        Records are auto-flattened into the named artifact at scenario
        teardown — no separate `emit to artifact` needed.

        Example:
            Then I extract table "case_summary" from "table[data-testid=summary]"
            ...    header_row=0
            ...    field=metric  header=Metric
            ...    field=value   header=Today
        """
        header_row, fields = _parse_table_specs(specs)
        self._current_rule().table_spec = TableSpec(
            name=_strip_quotes(name),
            locator=_strip_quotes(locator),
            header_row=header_row,
            fields=fields,
        )
        # Table auto-emits to its named artifact
        rule = self._current_rule()
        tname = _strip_quotes(name)
        if tname not in rule.emit_targets:
            rule.emit_targets.append(tname)
        # Auto-register the artifact if not declared
        self._v.artifacts.setdefault(tname, ArtifactSchema(name=tname))
        self._v.artifact_store.setdefault(tname, [])

    @keyword("And I emit to artifact \"${artifact}\"")
    def emit_to_artifact(self, artifact: str) -> None:
        """Push the rule's extracted record into the named artifact bag.

        Use after `Then I extract fields` on the same rule. The walker
        applies any `register hook at post_extract` transforms before
        pushing.
        """
        name = _strip_quotes(artifact)
        rule = self._current_rule()
        if name not in rule.emit_targets:
            rule.emit_targets.append(name)
        self._v.artifacts.setdefault(name, ArtifactSchema(name=name))
        self._v.artifact_store.setdefault(name, [])
        self._v.current_artifact = name

    @keyword("And I emit to artifact \"${artifact}\" flattened by \"${field}\"")
    def emit_flattened(self, artifact: str, field: str) -> None:
        """Push the rule's extracted record into the artifact, one record
        per element of the named array field.

        Use when an extractor returned a `grouped` array and you want
        one record per element rather than one record containing the
        array.

        Example:
            Then I extract fields
            ...    field=tags  extractor=grouped  locator=".tag-chip"
            And I emit to artifact "case_tags" flattened by "tags"
        """
        name = _strip_quotes(artifact)
        rule = self._current_rule()
        if name not in rule.emit_targets:
            rule.emit_targets.append(name)
        rule.emit_flatten_by[name] = _strip_quotes(field)
        self._v.artifacts.setdefault(name, ArtifactSchema(name=name))
        self._v.artifact_store.setdefault(name, [])
        self._v.current_artifact = name

    @keyword("And I merge into artifact \"${artifact}\" on key \"${key}\"")
    def merge_into_artifact(self, artifact: str, key: str) -> None:
        """Merge the rule's extracted record INTO an existing artifact
        record matching this key field. New fields are added; existing
        fields are overwritten.

        Use for multi-rule capture: rule A emits base fields, rule B
        emits additional fields, merge by id.

        Example:
            I define rule "case_basics"
                Then I extract fields  field=id extractor=attr locator=. attr=data-id
                ...                    field=title extractor=text locator=h3
                And I emit to artifact "cases"
            I define rule "case_owner"
                And I declare parents "case_basics"
                Then I extract fields  field=id extractor=attr locator=. attr=data-id
                ...                    field=owner extractor=text locator=".owner"
                And I merge into artifact "cases" on key "id"
        """
        name = _strip_quotes(artifact)
        rule = self._current_rule()
        if name not in rule.emit_targets:
            rule.emit_targets.append(name)
        rule.emit_merge_on[name] = _strip_quotes(key)
        self._v.artifacts.setdefault(name, ArtifactSchema(name=name))
        self._v.artifact_store.setdefault(name, [])
        self._v.current_artifact = name

    @keyword("And I set quality gate min records to ${count}")
    def quality_gate_min_records(self, count: str) -> None:
        """Assertion: the most-recently-mentioned artifact must contain
        at least this many records at scenario teardown. Failure creates
        a synthetic RuleResult that fails the scenario."""
        if self._v.current_artifact:
            qg = self._v.quality_gates.setdefault(
                self._v.current_artifact, QualityGate()
            )
            qg.min_records = int(count)

    @keyword("And I set filled percentage for \"${field}\" to ${percent}")
    def quality_gate_filled_pct(self, field_: str, percent: str) -> None:
        """Assertion: at least this percentage of records in the current
        artifact must have non-empty values for the named field."""
        if self._v.current_artifact:
            qg = self._v.quality_gates.setdefault(
                self._v.current_artifact, QualityGate()
            )
            qg.filled_pcts[_strip_quotes(field_)] = float(percent)

    @keyword("And I set max failed percentage to ${percent}")
    def quality_gate_max_failed_pct(self, percent: str) -> None:
        """Assertion: across an expansion run, no more than this
        percentage of iterations may fail."""
        if self._v.current_artifact:
            qg = self._v.quality_gates.setdefault(
                self._v.current_artifact, QualityGate()
            )
            qg.max_failed_pct = float(percent)

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

    @keyword("When I set stepper \"${css}\" to ${count}")
    def when_set_stepper(self, css: str, count: str) -> None:
        """Click a self-re-rendering stepper N times via JS click."""
        self._current_rule().items.append(
            Action("set_stepper", target=_strip_quotes(css), value=str(int(count)))
        )

    @keyword("When I select date \"${date_iso}\"")
    def when_select_date(self, date_iso: str, *opts: str) -> None:
        """Navigate an ARIA datepicker to the target month + click the day.
        Options: forward=<css>, heading=<css>, max_clicks=<n>."""
        self._current_rule().items.append(
            Action("select_date", value=_strip_quotes(date_iso), options=_parse_options(opts))
        )

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
        """Register a lifecycle hook that transforms extracted records.

        v1 supported lifecycle point: `post_extract` (fires after a rule
        builds its record via `Then I extract fields` / `Then I extract
        table`, before the record is emitted to its artifacts).

        Continuation rows are key=value transforms applied in order:
          rename=<old>:<new>             # rename a field
          drop=<field>                   # remove a field
          strip_html=<field>             # strip HTML tags
          lowercase=<field>              # lowercase string field
          default=<field>:<value>        # set default if field empty
          regex=<field>:<pattern>:<replacement>  # regex replace

        Example:
            And I register hook "normalize" at "post_extract"
            ...    rename=case_id:id
            ...    drop=_internal_marker
            ...    strip_html=description
            ...    lowercase=status
            ...    regex=link:^/:https://app.example.com/
        """
        # Flatten args, allow either strict (2+-space) or lenient
        # (1-space) RF continuation-row separators.
        cfg: dict[str, str] = {}
        for spec in opts:
            for tok in spec.split():
                if "=" not in tok:
                    continue
                k, _, v = tok.partition("=")
                cfg[k.strip()] = _strip_quotes(v.strip())
        self._v.hooks.append(
            HookDef(
                name=_strip_quotes(name),
                lifecycle_point=_strip_quotes(point),
                config=cfg,
            )
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
