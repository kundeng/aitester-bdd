"""Walker — evaluates a Verification's rule DAG against a live browser.

Ported from the WISE RPA BDD engine. The WISE engine has been exercised
across many real sites; the gotcha-fixes ported here are not theoretical.
Do not strip them without measuring the regression.

Plan-then-Execute Phase B:

  1. Topo-sort rules by parent declarations.
  2. For each rule:
     a. Split items at the first Action: pre-Action StateChecks are **guards**
        (no wait, fail = skip rule with optional retry-redo); from the first
        Action onward is the **body** (Actions interleaved with inline
        StateChecks; post-Action StateChecks are observations/assertions —
        wait with timeout, fail = fail the rule).
     b. Run guards. If a guard fails and `rule.retry_max > 0`, replay the
        body and re-check guards up to that many times (ported from WISE;
        the same flow on real sites handles transient AJAX / late content).
     c. Dismiss interrupts (popups, banners) before every action.
        Per-rule scoping: `interrupt_paused` suppresses, `interrupt_override`
        replaces the verification's global list.
     d. If an action raises: dismiss interrupts and retry the action ONCE.
        Real sites pop modals between the dismiss and the action.
     e. Refresh `current_url` after actions — clicks may have navigated.
     f. on_enter/on_fail screenshot hooks.
     g. Per-rule `timeout_ms` deadline; global run timeout.

Testing-specific (NOT in WISE, which is a scraper):

  * Post-action StateCheck failure FAILS the rule with structured evidence
    (RuleResult). WISE only warns ("Observation gate failed") — fine for
    scraping, wrong for testing.
  * Verdict aggregates RuleResults across the run with screenshots,
    expected/observed values, and the offending step repr.

One concept for "did the page reach the expected state?": StateCheck.
Position determines wait behavior and failure scope.
"""
from __future__ import annotations

import logging
import re
import time
from typing import TYPE_CHECKING, Optional

from aitester_bdd.engine.browser import BrowserAdapter
from aitester_bdd.engine.verdict import RuleResult, Verdict

if TYPE_CHECKING:
    from aitester_bdd.AITester import Action, Rule, Scenario, StateCheck, Verification

log = logging.getLogger("aitester_bdd.engine.walk")

DEFAULT_OBSERVATION_TIMEOUT_MS = 5000
DEFAULT_GUARD_TIMEOUT_MS = 200
DEFAULT_RUN_TIMEOUT_S = 300  # global cap — override via AITESTER_RUN_TIMEOUT env


# ---------------------------------------------------------------------------
# Topo sort — Kahn's algorithm, stable (ported from WISE _resolve_node_order)
# ---------------------------------------------------------------------------

def _topo_sort(rules_by_name: dict[str, "Rule"]) -> list[str]:
    """Parents-before-children ordering. Unknown parents are placed at their
    cite position and flagged at walk time. Cycles raise ValueError."""
    sorted_names: list[str] = []
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(name: str) -> None:
        if name in visited:
            return
        if name in visiting:
            raise ValueError(f"cyclic parent dependency at rule {name!r}")
        if name not in rules_by_name:
            sorted_names.append(name)
            visited.add(name)
            return
        visiting.add(name)
        for p in rules_by_name[name].parents:
            visit(p)
        visiting.discard(name)
        visited.add(name)
        sorted_names.append(name)

    for n in list(rules_by_name.keys()):
        visit(n)
    return sorted_names


# ---------------------------------------------------------------------------
# StateCheck dispatch — testing-specific (covers all kinds the LLM authors).
# Guards call this with a short timeout; observations call with a long one.
# ---------------------------------------------------------------------------

def _eval_state_check(
    browser: BrowserAdapter, sc: "StateCheck", *, timeout_ms: int
) -> tuple[bool, str, str]:
    """Evaluate one StateCheck. Returns (passed, expected_repr, observed_repr).

    Selector-bearing kinds resolve the locator through `resolve_fallback_selector`
    first (ported from WISE) so `"a | b"` pipe-fallback works everywhere.
    """
    kind = sc.kind
    css = sc.locator
    expected = sc.expected
    extra = sc.extra

    # Resolve pipe-fallback selector once for selector-bearing kinds.
    css_resolved = browser.resolve_fallback_selector(css) if css else css

    # ── URL ────────────────────────────────────────────────────────────
    if kind == "url_contains":
        obs = browser.url()
        ok = expected in obs
        if not ok and timeout_ms >= 500:
            end = time.time() + timeout_ms / 1000
            while time.time() < end and not ok:
                time.sleep(0.1)
                obs = browser.url()
                ok = expected in obs
        return (ok, f"url contains {expected!r}", obs)
    if kind == "url_matches":
        obs = browser.url()
        return (bool(re.search(expected, obs)), f"url matches {expected!r}", obs)
    if kind == "url_not_contains":
        obs = browser.url()
        return (expected not in obs, f"url not contains {expected!r}", obs)

    # ── Element existence ─────────────────────────────────────────────
    if kind == "selector_exists":
        ok = browser.wait_for_elements_state(css_resolved, "attached", timeout_ms=timeout_ms)
        return (ok, f"selector {css!r} exists", "present" if ok else "absent")
    if kind == "selector_missing":
        ok = browser.wait_for_elements_state(css_resolved, "detached", timeout_ms=timeout_ms)
        return (ok, f"selector {css!r} does not exist", "absent" if ok else "present")

    # ── Counts ─────────────────────────────────────────────────────────
    if kind == "count_eq":
        obs = browser.get_count(css_resolved)
        return (obs == int(expected), f"count == {expected}", str(obs))
    if kind == "count_at_least":
        obs = browser.get_count(css_resolved)
        return (obs >= int(expected), f"count >= {expected}", str(obs))
    if kind == "count_at_most":
        obs = browser.get_count(css_resolved)
        return (obs <= int(expected), f"count <= {expected}", str(obs))

    # ── Element text ──────────────────────────────────────────────────
    if kind == "has_text":
        obs = browser.get_text(css_resolved).strip()
        return (obs == expected, expected, obs)
    if kind == "contains":
        obs = browser.get_text(css_resolved)
        return (expected in obs, f"contains {expected!r}", obs)
    if kind == "matches":
        obs = browser.get_text(css_resolved)
        return (bool(re.search(expected, obs)), f"matches {expected!r}", obs)
    if kind == "not_contains":
        obs = browser.get_text(css_resolved)
        return (expected not in obs, f"not contains {expected!r}", obs)

    # ── Element state ──────────────────────────────────────────────────
    if kind == "visible":
        ok = browser.is_visible(css_resolved)
        return (ok, "visible", "visible" if ok else "hidden")
    if kind == "hidden":
        ok = not browser.is_visible(css_resolved)
        return (ok, "hidden", "hidden" if ok else "visible")
    if kind == "enabled":
        ok = browser.is_enabled(css_resolved)
        return (ok, "enabled", "enabled" if ok else "disabled")
    if kind == "disabled":
        ok = not browser.is_enabled(css_resolved)
        return (ok, "disabled", "disabled" if ok else "enabled")
    if kind == "checked":
        ok = browser.is_checked(css_resolved)
        return (ok, "checked", "checked" if ok else "unchecked")

    # ── Class / attribute ─────────────────────────────────────────────
    if kind == "has_class":
        cls = browser.get_class(css_resolved)
        ok = expected in cls.split()
        return (ok, f"has class {expected!r}", cls)
    if kind == "not_class":
        cls = browser.get_class(css_resolved)
        ok = expected not in cls.split()
        return (ok, f"does not have class {expected!r}", cls)
    if kind == "attr_eq":
        attr = extra.get("attr", "")
        obs = browser.get_attribute(css_resolved, attr)
        return (obs == expected, f"{attr}={expected!r}", obs)
    if kind == "attr_contains":
        attr = extra.get("attr", "")
        obs = browser.get_attribute(css_resolved, attr)
        return (expected in obs, f"{attr} contains {expected!r}", obs)

    # ── Form values ───────────────────────────────────────────────────
    if kind == "input_value":
        obs = browser.get_value(css_resolved)
        return (obs == expected, expected, obs)
    if kind == "select_selected":
        obs = browser.get_value(css_resolved)
        return (obs == expected, expected, obs)

    # ── Network ────────────────────────────────────────────────────────
    if kind == "last_status":
        obs = browser.last_response_status()
        return (obs == int(expected), expected, str(obs))
    if kind == "last_body_contains":
        obs = browser.last_response_body()
        return (expected in obs, f"contains {expected!r}", obs[:200])

    # ── API direct (httpx) ─────────────────────────────────────────────
    if kind == "api_returns":
        return _eval_api_returns(extra.get("path", ""), extra.get("field", ""), expected)

    # ── Semantic (AI-judged) — escape hatch via the LLM adapter
    if kind == "semantic":
        return _eval_semantic(browser, sc)
    if kind == "visual_semantic":
        return _eval_visual_semantic(browser, sc)

    return (False, f"unknown state check {kind}", "")


# ---------------------------------------------------------------------------
# Semantic / visual_semantic — sparingly used, opt-in by env config.
#
# AITESTER_LLM_MODEL defaults to the claude-code-proxy
# ("openai/cc/claude-opus-4-7" against OPENAI_BASE_URL=
# http://localhost:20128/v1). Override to swap providers.
# ---------------------------------------------------------------------------


_UNSET = object()
_LLM_CACHE: object = _UNSET


def _get_llm():
    """Lazy-load the LLM adapter.

    Default config points at the claude-code-proxy on localhost; override
    via AITESTER_LLM_MODEL / OPENAI_BASE_URL. Init failures are reported
    in the StateCheck observation, not silently swallowed."""
    global _LLM_CACHE
    if _LLM_CACHE is not _UNSET:
        return _LLM_CACHE
    try:
        from aitester_bdd.llm.aiagent_adapter import AIAgentLLM
        _LLM_CACHE = AIAgentLLM()
    except Exception as exc:
        log.warning("LLM adapter init failed: %s", exc)
        _LLM_CACHE = None
    return _LLM_CACHE


def reset_llm_cache() -> None:
    """Reset lazy LLM cache — used by tests to inject mocks."""
    global _LLM_CACHE
    _LLM_CACHE = _UNSET


def _eval_semantic(browser: BrowserAdapter, sc: "StateCheck") -> tuple[bool, str, str]:
    """Text-mode semantic check. Pages text → LLM judge → pass/fail."""
    criterion = sc.expected
    llm = _get_llm()
    if llm is None:
        return (
            False, f"semantic({criterion!r})",
            "AI judge not configured — set AITESTER_LLM_MODEL to opt in",
        )

    # Build the observation: URL + scoped text.
    url = browser.url()
    if sc.extra.get("scope") == "locator" and sc.locator:
        css = browser.resolve_fallback_selector(sc.locator)
        text = browser.get_text(css)
        observation = f"URL: {url}\nScope: {sc.locator!r}\n---\n{text}"
    else:
        text = browser.get_text("body")
        observation = f"URL: {url}\n---\n{text}"

    try:
        passed = llm.judge(criterion=criterion, observation=observation)
    except Exception as exc:
        return (False, f"semantic({criterion!r})", f"judge raised: {exc}")
    return (
        passed,
        f"semantic({criterion!r})",
        "PASS" if passed else "FAIL",
    )


def _eval_visual_semantic(
    browser: BrowserAdapter, sc: "StateCheck",
) -> tuple[bool, str, str]:
    """Multimodal semantic check: PNG screenshot → LLM judge → pass/fail."""
    criterion = sc.expected
    llm = _get_llm()
    if llm is None:
        return (
            False, f"visual_semantic({criterion!r})",
            "AI judge not configured — set AITESTER_LLM_MODEL to opt in",
        )

    # Take a screenshot to a temp file, then read its bytes.
    import os
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        shot_path = f.name
    try:
        browser.screenshot(shot_path)
        with open(shot_path, "rb") as fh:
            png_bytes = fh.read()
        if not png_bytes:
            return (False, f"visual_semantic({criterion!r})", "empty screenshot")
        try:
            passed = llm.judge_visual(criterion=criterion, png_bytes=png_bytes)
        except Exception as exc:
            return (False, f"visual_semantic({criterion!r})", f"judge raised: {exc}")
        return (
            passed,
            f"visual_semantic({criterion!r})",
            "PASS" if passed else "FAIL",
        )
    finally:
        try:
            os.unlink(shot_path)
        except Exception:
            pass


def _eval_api_returns(path: str, field: str, expected: str) -> tuple[bool, str, str]:
    """Direct API check via httpx using token from env."""
    import os
    import httpx

    base = os.environ.get("AITESTER_API_BASE_URL", "http://localhost:5175")
    token = os.environ.get("AITESTER_API_TOKEN", "")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        r = httpx.get(f"{base}{path}", headers=headers, timeout=10)
        if r.status_code != 200:
            return (False, expected, f"http {r.status_code}")
        body = r.json()
        obs = body.get(field, "")
        return (str(obs) == expected, expected, str(obs))
    except Exception as exc:
        return (False, expected, f"error: {exc}")


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

def _eval_action(browser: BrowserAdapter, action: "Action") -> None:
    """Execute one action against the live browser.

    Selector-bearing actions resolve the target through fallback resolution
    (`"a | b"` pipe syntax) before dispatching.
    """
    kind = action.kind
    target = browser.resolve_fallback_selector(action.target) if action.target else action.target

    if kind == "open":
        browser.open(target or action.target)
        browser.wait_for_load_state("domcontentloaded", timeout="10s")
    elif kind == "reload":
        browser.reload()
    elif kind == "back":
        browser.go_back()
    elif kind == "click":
        browser.click(target)
    elif kind == "click_text":
        browser.click_text(action.target)
    elif kind == "dblclick":
        browser.double_click(target)
    elif kind == "type":
        browser.type(target, action.value, secret=False)
    elif kind == "type_secret":
        browser.type(target, action.value, secret=True)
    elif kind == "select":
        browser.select(target, action.value)
    elif kind == "check":
        browser.check(target)
    elif kind == "uncheck":
        browser.uncheck(target)
    elif kind == "hover":
        browser.hover(target)
    elif kind == "focus":
        browser.focus(target)
    elif kind == "press":
        browser.press(target, action.options.get("keys", []))
    elif kind == "upload":
        browser.upload(target, action.value)
    elif kind == "scroll":
        browser.scroll()
    elif kind == "wait_idle":
        browser.wait_for_idle()
    elif kind == "screenshot":
        browser.screenshot(action.options.get("filename"))
    elif kind == "js":
        # Returns "__NAVIGATED__" if JS triggered page change (handled, not raised).
        browser.evaluate_js(action.value)
    elif kind == "set_stepper":
        browser.set_stepper(target, int(action.value or "0"))
    elif kind == "add_params":
        browser.add_url_params(action.value)
    elif kind == "select_date":
        # action.value = ISO date (YYYY-MM-DD); options carry forward_sel etc.
        opts = action.options or {}
        kw_args = {}
        if "forward" in opts:
            kw_args["forward_sel"] = opts["forward"]
        if "heading" in opts:
            kw_args["heading_sel"] = opts["heading"]
        if "max_clicks" in opts:
            try:
                kw_args["max_clicks"] = int(opts["max_clicks"])
            except (ValueError, TypeError):
                pass
        browser.select_date(action.value, **kw_args)
    elif kind == "browser_step":
        browser.browser_step(action.target, action.options.get("args", []))
    elif kind == "call_keyword":
        browser.call_keyword(action.target, action.options.get("args", []))
    else:
        log.warning("unknown action %s", kind)


def _await_after_action(browser: BrowserAdapter, action: "Action") -> None:
    """Honor inline `await=<selector>` option after click/type.

    Ported from WISE. After an action, if `await=` is declared, wait for
    that selector before advancing. This is the MDP synchronization gate
    (s,a → o, where 'await' is the expected observation 'o').
    """
    sel = action.options.get("await")
    if sel:
        resolved = browser.resolve_fallback_selector(sel)
        browser.wait_for_elements_state(resolved, "attached", timeout_ms=DEFAULT_OBSERVATION_TIMEOUT_MS)


# ---------------------------------------------------------------------------
# Interrupt handling — ported from WISE
# ---------------------------------------------------------------------------

def _effective_interrupt_selectors(
    rule: "Rule", verification: "Verification"
) -> list[str]:
    """Resolve which dismiss-selectors apply to this rule.

    Per-rule scoping (ported from WISE):
      - rule.interrupt_paused: empty list (suppress all dismissals)
      - rule.interrupt_override is not None: use that list
      - otherwise: inherit verification.interrupts.dismiss_selectors
    """
    if rule.interrupt_paused:
        return []
    if rule.interrupt_override is not None:
        return rule.interrupt_override
    return verification.interrupts.dismiss_selectors


def _dismiss_interrupts(browser: BrowserAdapter, selectors: list[str]) -> None:
    """Click any visible interrupt selectors (cookie banners, modals).

    Ported from WISE. Kept as a free function for tests that monkeypatch
    it directly. The walker's own dismiss calls are inlined in
    `_execute_body._dismiss` so they can fire `after_dismiss` aspects.
    """
    for sel in selectors:
        try:
            if browser.get_count(sel) > 0:
                browser.click(sel)
                log.info("Dismissed interrupt: %s", sel)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Rule body split — items list → (guards, body)
# ---------------------------------------------------------------------------

def _split_rule_items(rule: "Rule") -> tuple[list["StateCheck"], list]:
    """Split the rule's items into (guards, body).

    Guards = StateChecks before the first Action (no Emit, no Action).
    Body   = everything from the first Action onward (Actions + inline
             StateChecks that act as observation gates / assertions +
             Emit observations that capture but do not gate).

    Emit items before any Action are still treated as body items (they
    are observations, not guards; they would be useless as guards since
    they don't gate anything).
    """
    from aitester_bdd.AITester import Action, Emit, StateCheck

    guards: list[StateCheck] = []
    body: list = []
    saw_action_or_emit = False
    for it in rule.items:
        if saw_action_or_emit:
            body.append(it)
            continue
        if isinstance(it, Action):
            saw_action_or_emit = True
            body.append(it)
        elif isinstance(it, Emit):
            saw_action_or_emit = True
            body.append(it)
        elif isinstance(it, StateCheck):
            guards.append(it)
        else:
            log.warning("unknown item in rule %r: %r", rule.name, it)
    return guards, body


# ---------------------------------------------------------------------------
# Guards — pre-action StateChecks with retry-redo
# ---------------------------------------------------------------------------

def _check_guards(
    browser: BrowserAdapter,
    rule: "Rule",
    verification: "Verification",
    guards: list["StateCheck"],
    *,
    registry: Any = None,
) -> tuple[bool, Optional["StateCheck"], str, str]:
    """Evaluate all guards. Returns (passed, failed_check_or_None, expected, observed).

    Dismisses interrupts once before checking. Each guard uses the short
    guard timeout (no significant waiting — guards are 'is the world already
    in the right state?'). Fires `after_state_check` per guard.
    """
    sels = _effective_interrupt_selectors(rule, verification)
    for sel in sels:
        try:
            if browser.get_count(sel) > 0:
                browser.click(sel)
                log.info("Dismissed interrupt: %s", sel)
                if registry is not None:
                    registry.fire_after_dismiss(rule, sel)
        except Exception:
            pass
    for g in guards:
        ok, expected, observed = _eval_state_check(browser, g, timeout_ms=DEFAULT_GUARD_TIMEOUT_MS)
        if registry is not None:
            registry.fire_after_state_check(
                rule, g, ok, expected, observed, position="guard",
            )
        if not ok:
            return (False, g, expected, observed)
    return (True, None, "", "")


# ---------------------------------------------------------------------------
# Body — actions + inline observation gates + post-action assertions
# ---------------------------------------------------------------------------

def _execute_body(
    browser: BrowserAdapter,
    rule: "Rule",
    verification: "Verification",
    body: list,
    *,
    deadline: Optional[float] = None,
    scenario_name: str = "",
    registry: Any = None,
) -> tuple[bool, str, str, Optional["StateCheck | Action"], str, str]:
    """Execute the rule body — actions interleaved with inline state checks.

    Returns (passed, failure_step_kind, failure_message, failed_item,
             expected, observed).

    Post-action StateChecks are observations/assertions: failing them
    FAILS the rule with structured evidence (testing-specific; WISE only
    warned). Emit items are observations only — they capture page state
    into emit.jsonl and never fail the rule.

    Actions: dismiss interrupts → run action → on raise, dismiss + retry
    once. Honors `await=<selector>` option after each action.

    Aspects fire at every transition (`before_action`, `after_action`,
    `after_state_check`, `after_emit`, `after_dismiss`).
    """
    from aitester_bdd.AITester import Action, Emit, StateCheck
    from aitester_bdd.engine.emit import _build_data, emit_explicit

    interrupt_sels = _effective_interrupt_selectors(rule, verification)

    def _dismiss(b, sels):
        for sel in sels:
            try:
                if b.get_count(sel) > 0:
                    b.click(sel)
                    log.info("Dismissed interrupt: %s", sel)
                    if registry is not None:
                        registry.fire_after_dismiss(rule, sel)
            except Exception:
                pass

    for step in body:
        if deadline is not None and time.time() > deadline:
            return (False, "rule_timeout",
                    f"per-rule timeout exceeded ({rule.options.get('timeout_ms')}ms)",
                    step, "", "")

        if isinstance(step, Emit):
            # Observation only — never fails the rule. Capture even if
            # underlying queries error out; failures land as None/0 in
            # the emit record, not as test failures.
            try:
                emit_explicit(
                    browser, scenario=scenario_name, rule=rule.name, emit_obj=step,
                )
                if registry is not None:
                    # Re-run capture lightly for the aspect — keeps WalkLog
                    # in sync with what landed in emit.jsonl.
                    data = _build_data(browser, step.fields)
                    registry.fire_after_emit(rule, step, data)
            except Exception as exc:
                log.warning("emit %r failed to write: %s", step.name, exc)
            continue

        if isinstance(step, StateCheck):
            # Inline observation gate / post-action assertion.
            ok, expected, observed = _eval_state_check(
                browser, step, timeout_ms=DEFAULT_OBSERVATION_TIMEOUT_MS
            )
            if registry is not None:
                registry.fire_after_state_check(
                    rule, step, ok, expected, observed, position="observation",
                )
            if not ok:
                return (
                    False, "observation_or_assertion",
                    f"post-action state check failed: {step.kind}",
                    step, expected, observed,
                )
            continue

        if not isinstance(step, Action):
            continue

        # Dismiss interrupts before every action — modals can appear at
        # any moment and block clicks. (Ported from WISE.)
        if interrupt_sels:
            _dismiss(browser, interrupt_sels)

        if registry is not None:
            registry.fire_before_action(rule, step)

        t0 = time.time()
        raised = False
        try:
            _eval_action(browser, step)
            _await_after_action(browser, step)
        except Exception as exc_first:
            raised = True
            # Recovery: dismiss + retry once.
            if interrupt_sels:
                _dismiss(browser, interrupt_sels)
                try:
                    _eval_action(browser, step)
                    _await_after_action(browser, step)
                    if registry is not None:
                        registry.fire_after_action(rule, step, time.time() - t0, raised)
                    continue
                except Exception as exc_retry:
                    if registry is not None:
                        registry.fire_after_action(rule, step, time.time() - t0, True)
                    return (False, "action",
                            f"action raised twice: {type(exc_retry).__name__}: {exc_retry}",
                            step, "", "")
            if registry is not None:
                registry.fire_after_action(rule, step, time.time() - t0, raised)
            return (False, "action",
                    f"action raised: {type(exc_first).__name__}: {exc_first}",
                    step, "", "")
        if registry is not None:
            registry.fire_after_action(rule, step, time.time() - t0, raised)

    return (True, "", "", None, "", "")


# ---------------------------------------------------------------------------
# Per-rule walk
# ---------------------------------------------------------------------------

def _walk_rule(
    browser: BrowserAdapter,
    scenario: "Scenario",
    rule: "Rule",
    verification: "Verification",
    *,
    already_passed: set[str],
    run_deadline: Optional[float] = None,
    registry: Any = None,
    walk_log: Any = None,
) -> RuleResult:
    """Walk one rule with full WISE semantics + aspect hooks."""
    from aitester_bdd.AITester import Action, StateCheck

    start = time.time()

    # Global run timeout
    if run_deadline is not None and time.time() > run_deadline:
        return RuleResult(
            rule_name=rule.name, scenario_name=scenario.name, passed=False,
            failure_step_kind="run_timeout",
            failure_message="global run timeout exceeded",
            duration_ms=(time.time() - start) * 1000,
        )

    # Parent gating
    for p in rule.parents:
        if p not in already_passed:
            return RuleResult(
                rule_name=rule.name, scenario_name=scenario.name, passed=False,
                failure_step_kind="parent_failed",
                failure_message=f"parent rule {p!r} did not pass",
                duration_ms=(time.time() - start) * 1000,
            )

    # before_rule aspect — also gives aspects a chance to skip the rule.
    if registry is not None:
        if registry.fire_before_rule(verification, scenario, rule, already_passed):
            return RuleResult(
                rule_name=rule.name, scenario_name=scenario.name, passed=True,
                failure_message="skipped by aspect",
                duration_ms=(time.time() - start) * 1000,
            )

    # on_enter hook
    if rule.options.get("on_enter") == "screenshot":
        try:
            browser.screenshot(f"on_enter_{rule.name}.png")
        except Exception:
            pass

    guards, body = _split_rule_items(rule)

    # Per-rule deadline (if declared via rule.options.timeout_ms)
    rule_timeout_ms = rule.options.get("timeout_ms")
    rule_deadline = (
        time.time() + (int(rule_timeout_ms) / 1000.0)
        if rule_timeout_ms else None
    )

    # ── Guards (with retry-redo, ported from WISE) ────────────────────
    ok, failed_guard, expected, observed = _check_guards(
        browser, rule, verification, guards, registry=registry,
    )
    if not ok and rule.retry_max > 0:
        for attempt in range(1, rule.retry_max + 1):
            log.info("Retry %d/%d for rule %r (guard %r failed)",
                     attempt, rule.retry_max, rule.name,
                     failed_guard.kind if failed_guard else "?")
            if walk_log is not None:
                walk_log.record(
                    "retry", rule=rule.name, attempt=attempt,
                    reason=f"guard {failed_guard.kind if failed_guard else '?'} failed",
                )
            time.sleep(rule.retry_delay_ms / 1000.0)
            # Replay the body before re-checking guards (WISE semantics —
            # actions may bring the world into the guarded state).
            _execute_body(
                browser, rule, verification, body,
                deadline=rule_deadline, scenario_name=scenario.name,
                registry=registry,
            )
            ok, failed_guard, expected, observed = _check_guards(
                browser, rule, verification, guards, registry=registry,
            )
            if ok:
                break

    if not ok:
        if rule.options.get("on_fail") == "screenshot":
            try:
                browser.screenshot(f"on_fail_{rule.name}_guard.png")
            except Exception:
                pass
        if rule.guard_policy == "abort":
            raise RuntimeError(f"Guard failed (abort policy) for rule {rule.name!r}")
        repr_str = (
            f"{failed_guard.kind} {failed_guard.locator or failed_guard.expected}"
            if failed_guard else ""
        )
        diagnosis = ""
        if registry is not None:
            diagnosis = registry.fire_on_rule_failure(
                verification, scenario, rule,
                "guard", repr_str, expected, observed, failed_guard, walk_log,
            )
        result = RuleResult(
            rule_name=rule.name, scenario_name=scenario.name, passed=False,
            failure_step_kind="guard",
            failure_step_repr=repr_str,
            failure_message="pre-action guard failed; rule skipped",
            expected=expected, observed=observed,
            ai_diagnosis=diagnosis,
            duration_ms=(time.time() - start) * 1000,
        )
        if registry is not None:
            registry.fire_after_rule(verification, scenario, rule, result)
        return result

    # ── Body (actions + inline observations/assertions) ───────────────
    passed, fk, fmsg, fitem, fexp, fobs = _execute_body(
        browser, rule, verification, body,
        deadline=rule_deadline, scenario_name=scenario.name,
        registry=registry,
    )

    if not passed:
        shot = None
        if rule.options.get("on_fail") == "screenshot" or fk in ("action", "observation_or_assertion"):
            try:
                shot = browser.screenshot(f"fail_{scenario.name}_{rule.name}_{fk}.png")
            except Exception:
                pass
        repr_str = ""
        if isinstance(fitem, StateCheck):
            repr_str = f"{fitem.kind} {fitem.locator or fitem.expected}"
        elif isinstance(fitem, Action):
            repr_str = f"{fitem.kind} {fitem.target}"
        diagnosis = ""
        if registry is not None:
            diagnosis = registry.fire_on_rule_failure(
                verification, scenario, rule,
                fk, repr_str, fexp, fobs, fitem, walk_log,
            )
        result = RuleResult(
            rule_name=rule.name, scenario_name=scenario.name, passed=False,
            failure_step_kind=fk,
            failure_step_repr=repr_str,
            failure_message=fmsg,
            expected=fexp, observed=fobs,
            screenshot=shot,
            ai_diagnosis=diagnosis,
            duration_ms=(time.time() - start) * 1000,
        )
        if registry is not None:
            registry.fire_after_rule(verification, scenario, rule, result)
        return result

    # ── Capture pipeline (artifact model — TIER 1+2 from WISE) ───────
    # Three modes:
    #   1. Rule has expansion → iterate per element/combo, one record each
    #   2. Rule has capture (field_specs/table_spec) but no expansion → one record
    #   3. Neither — no capture
    if rule.expansion is not None:
        try:
            _run_expansion(browser, rule, verification, scenario)
        except Exception as exc:
            log.warning("expansion failed for rule %r: %s", rule.name, exc)
    elif rule.has_capture:
        try:
            record = _extract_record(browser, rule)
            if record is not None:
                record = _invoke_hooks_post_extract(verification, record)
                _emit_to_artifacts(verification, rule, record, scenario)
        except Exception as exc:
            log.warning("capture pipeline failed for rule %r: %s", rule.name, exc)

    result = RuleResult(
        rule_name=rule.name, scenario_name=scenario.name, passed=True,
        duration_ms=(time.time() - start) * 1000,
    )
    if registry is not None:
        registry.fire_after_rule(verification, scenario, rule, result)
    return result


# ---------------------------------------------------------------------------
# Capture pipeline — extract → hook transforms → emit to artifacts
# ---------------------------------------------------------------------------


def _extract_record(browser: BrowserAdapter, rule: Any, *, scope: str = "") -> dict | None:
    """Build a record from the rule's field_specs and/or table_spec.

    field_specs produce one record with named fields.
    table_spec produces a list of records (one per data row) which we
    flatten into the record under the table's name as a list.

    `scope` (TIER 2) prefixes each field's locator for per-element
    capture during expansion. Convention: `scope` is a CSS selector
    that targets the iteration element; field locators are relative.
    Special case: `locator="."` means "the element itself."
    """
    data: dict = {}
    for fs in rule.field_specs:
        data[fs.name] = _extract_field(browser, fs, scope=scope)
    if rule.table_spec is not None:
        rows = _extract_table_rows(browser, rule.table_spec)
        data[rule.table_spec.name] = rows
    if not data:
        return None
    return {
        "data": data,
        "rule": rule.name,
        "url": browser.url(),
        "extracted_at": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        ).isoformat(),
    }


def _extract_field(browser: BrowserAdapter, fs: Any, *, scope: str = "") -> Any:
    """Dispatch field extraction by extractor type.

    When `scope` is set (TIER 2 expansion), the effective selector is
    `${scope} >> ${fs.locator}` — Playwright-style scoped query.
    Special: `fs.locator == "."` resolves to `scope` itself.
    """
    from urllib.parse import urljoin

    if scope and fs.locator == ".":
        css = scope
    elif scope and fs.locator:
        resolved = browser.resolve_fallback_selector(fs.locator)
        css = f"{scope} >> {resolved}"
    elif fs.locator:
        css = browser.resolve_fallback_selector(fs.locator)
    else:
        css = ""
    try:
        if fs.extractor == "text":
            return browser.get_text(css).strip() if css else ""
        if fs.extractor == "attr":
            return browser.get_attribute(css, fs.attr) if css and fs.attr else ""
        if fs.extractor == "value":
            return browser.get_value(css) if css else ""
        if fs.extractor == "class":
            return browser.get_class(css) if css else ""
        if fs.extractor == "html":
            v = browser.evaluate_js(
                f"(document.querySelector({_json_str(css)})||{{}}).outerHTML || ''"
            )
            return str(v or "")
        if fs.extractor == "link":
            href = browser.get_attribute(css, "href") if css else ""
            if href and not href.startswith(("http://", "https://", "javascript:", "#")):
                page_url = browser.url()
                try:
                    href = urljoin(page_url, href)
                except Exception:
                    pass
            return href
        if fs.extractor == "number":
            text = browser.get_text(css) if css else ""
            cleaned = re.sub(r"[^\d.\-]", "", text)
            try:
                return float(cleaned) if "." in cleaned else int(cleaned)
            except (ValueError, TypeError):
                return text
        if fs.extractor == "grouped":
            count = browser.get_count(css) if css else 0
            results = []
            for i in range(count):
                t = browser.get_text(f"{css} >> nth={i}").strip()
                if t:
                    results.append(t)
            return results
    except Exception as exc:
        log.debug("extract_field %r failed: %s", fs.name, exc)
    return ""


def _json_str(s: str) -> str:
    """Safe JSON-encode for embedding in JS."""
    import json
    return json.dumps(s)


def _extract_table_rows(browser: BrowserAdapter, tspec: Any) -> list[dict]:
    """Pull rows from a <table>: locate, querySelectorAll('tr'), map header text
    to column index, build per-row records."""
    css = browser.resolve_fallback_selector(tspec.locator)
    js = (
        f"() => {{ const t = document.querySelector({_json_str(css)}); "
        f"if (!t) return null; "
        f"const rows = t.querySelectorAll('tr'); "
        f"return Array.from(rows).map(r => Array.from(r.querySelectorAll('th,td'))"
        f".map(c => (c.textContent || '').trim())); }}"
    )
    try:
        raw = browser.evaluate_js(js)
    except Exception:
        return []
    if not raw or not isinstance(raw, list):
        return []
    if len(raw) <= tspec.header_row:
        return []
    headers = raw[tspec.header_row]
    if not isinstance(headers, list):
        return []
    # Map field.header -> column index
    col_for: dict[str, int] = {}
    for fs in tspec.fields:
        for idx, h in enumerate(headers):
            if h == fs.header:
                col_for[fs.name] = idx
                break
    records: list[dict] = []
    for row in raw[tspec.header_row + 1:]:
        if not isinstance(row, list) or not any(row):
            continue
        rec: dict = {}
        for fname, idx in col_for.items():
            rec[fname] = row[idx] if idx < len(row) else ""
        records.append(rec)
    return records


def _invoke_hooks_post_extract(verification: Any, record: dict) -> dict:
    """Run post_extract hook transforms on the record's `data` field.

    Transforms (applied in registration order, then in dict insertion
    order within each hook):
      rename=old:new
      drop=field
      strip_html=field
      lowercase=field
      default=field:value
      regex=field:pattern:replacement
    """
    data = dict(record.get("data") or {})
    for hook in verification.hooks:
        if hook.lifecycle_point != "post_extract":
            continue
        for key, val in hook.config.items():
            try:
                if key == "rename" and ":" in val:
                    old, new = val.split(":", 1)
                    if old in data:
                        data[new] = data.pop(old)
                elif key == "drop" and val in data:
                    del data[val]
                elif key == "strip_html" and val in data:
                    data[val] = re.sub(r"<[^>]+>", "", str(data[val]))
                elif key == "lowercase" and val in data:
                    data[val] = str(data[val]).lower()
                elif key == "default" and ":" in val:
                    fname, default_val = val.split(":", 1)
                    if not data.get(fname):
                        data[fname] = default_val
                elif key == "regex" and val.count(":") >= 2:
                    parts = val.split(":", 2)
                    fname, pattern, replacement = parts
                    if fname in data:
                        data[fname] = re.sub(pattern, replacement, str(data[fname]))
            except Exception as exc:
                log.warning("hook %r transform %s=%s failed: %s", hook.name, key, val, exc)
    record["data"] = data
    return record


def _emit_to_artifacts(verification: Any, rule: Any, record: dict, scenario: Any) -> None:
    """Push the record into every artifact in rule.emit_targets, applying
    flatten and merge semantics per WISE _emit_records.

    Table records are special: the table spec's own name was added to
    emit_targets; we flatten the rows list under that name into the
    artifact (one record per table row).
    """
    data = record.get("data") or {}
    for target in rule.emit_targets:
        verification.artifact_store.setdefault(target, [])
        bag = verification.artifact_store[target]

        # Table-auto-emit: if data[target] is a list and target matches
        # the table's name, flatten without per-rule flatten declaration.
        if (
            rule.table_spec is not None
            and rule.table_spec.name == target
            and isinstance(data.get(target), list)
        ):
            for row in data[target]:
                if isinstance(row, dict):
                    bag.append({
                        "data": row,
                        "rule": rule.name,
                        "url": record.get("url", ""),
                        "extracted_at": record.get("extracted_at", ""),
                        "scenario": scenario.name,
                    })
            continue

        # Explicit flatten_by
        flatten_field = rule.emit_flatten_by.get(target)
        if flatten_field:
            items = data.get(flatten_field, [])
            if isinstance(items, list):
                for item in items:
                    item_data = item if isinstance(item, dict) else {flatten_field: item}
                    bag.append({
                        "data": item_data,
                        "rule": rule.name,
                        "url": record.get("url", ""),
                        "extracted_at": record.get("extracted_at", ""),
                        "scenario": scenario.name,
                    })
            continue

        # Merge by key
        merge_key = rule.emit_merge_on.get(target)
        if merge_key:
            key_val = data.get(merge_key)
            if key_val is not None:
                for existing in bag:
                    if (existing.get("data") or {}).get(merge_key) == key_val:
                        existing["data"].update(data)
                        break
                else:
                    bag.append({**record, "scenario": scenario.name})
            else:
                bag.append({**record, "scenario": scenario.name})
            continue

        # Plain emit
        bag.append({**record, "scenario": scenario.name})


# ---------------------------------------------------------------------------
# Expansion (TIER 2) — parametric capture: iterate elements / combinations,
# producing one record per iteration via the rule's field_specs.
#
# v1: each iteration captures into the rule's artifacts. Per-element child
# walking with scope+context propagation is deferred (TIER 2.5).
# ---------------------------------------------------------------------------


def _run_expansion(
    browser: BrowserAdapter, rule: Any, verification: Any, scenario: Any,
) -> None:
    """Dispatch by expansion mode."""
    exp = rule.expansion
    if exp.over == "elements":
        _run_expansion_elements(browser, rule, verification, scenario)
    elif exp.over == "combinations":
        _run_expansion_combinations(browser, rule, verification, scenario)
    else:
        log.warning("unknown expansion mode %r", exp.over)


def _run_expansion_elements(
    browser: BrowserAdapter, rule: Any, verification: Any, scenario: Any,
) -> None:
    """Iterate elements at `expansion.scope`, capture one record per element."""
    exp = rule.expansion
    scope = browser.resolve_fallback_selector(exp.scope) if exp.scope else ""
    if not scope:
        log.warning("expansion has empty scope for rule %r", rule.name)
        return

    # Wait for at least one element to appear before counting.
    browser.wait_for_elements_state(scope, "attached", timeout_ms=10_000)
    try:
        count = int(browser.get_count(scope) or 0)
    except Exception as exc:
        log.warning("expansion count failed for %r: %s", scope, exc)
        return

    if exp.limit and count > exp.limit:
        count = exp.limit

    for i in range(count):
        elem_scope = f"{scope} >> nth={i}"
        # exclude_if: skip elements where this child selector matches
        if exp.exclude_if:
            try:
                child_count = browser.get_count(f"{elem_scope} >> {exp.exclude_if}")
                if child_count > 0:
                    log.debug("expansion: skipped element %d (exclude_if matched)", i)
                    continue
            except Exception:
                pass

        try:
            record = _extract_record(browser, rule, scope=elem_scope)
        except Exception as exc:
            log.warning("per-element extract failed at i=%d: %s", i, exc)
            continue
        if record is None:
            continue
        record = _invoke_hooks_post_extract(verification, record)
        # Stamp the iteration index for differential debugging.
        record.setdefault("data", {})
        record["data"].setdefault("_iter", i)
        _emit_to_artifacts(verification, rule, record, scenario)


def _run_expansion_combinations(
    browser: BrowserAdapter, rule: Any, verification: Any, scenario: Any,
) -> None:
    """Cartesian product of axes. Per combo, apply axis actions then
    capture+emit a record. Page returns to the post-action state after
    each combo before the next combo's actions apply.
    """
    import itertools
    import json

    exp = rule.expansion
    if not exp.axes:
        return

    # Resolve each axis's value list.
    axis_values: list[list[str]] = []
    for ax in exp.axes:
        vals = list(ax.values)
        if vals == ["auto"]:
            vals = _discover_axis_values(browser, ax)
        if ax.skip > 0:
            vals = vals[ax.skip:]
        if ax.exclude:
            vals = [v for v in vals if v not in ax.exclude]
        if ax.action == "select":
            # Drop empty values for select dropdowns
            vals = [v for v in vals if v]
        axis_values.append(vals)

    for combo in itertools.product(*axis_values):
        # Apply each axis action to set this combo
        for ax, value in zip(exp.axes, combo):
            try:
                _apply_axis_action(browser, ax, value)
            except Exception as exc:
                log.warning("axis action %s=%s failed: %s", ax.action, value, exc)

        # Wait for AJAX (combo actions often trigger filtering)
        try:
            browser.wait_for_load_state("networkidle", timeout="5s")
        except Exception:
            pass

        # Capture record for this combination
        try:
            record = _extract_record(browser, rule)
        except Exception as exc:
            log.warning("per-combo extract failed: %s", exc)
            continue
        if record is None:
            record = {"data": {}, "rule": rule.name, "url": browser.url(),
                      "extracted_at": _now_iso()}
        # Tag combo values on the record for differential debugging
        record.setdefault("data", {})
        for ax, value in zip(exp.axes, combo):
            record["data"][f"_combo_{ax.control}"] = value

        record = _invoke_hooks_post_extract(verification, record)
        _emit_to_artifacts(verification, rule, record, scenario)


def _discover_axis_values(browser: BrowserAdapter, axis: Any) -> list[str]:
    """Auto-discover values for `values=auto` axis.

    For action=select: read the dropdown's <option> values via JS.
    For action=click/type: read text content of every matching element.
    """
    import json
    try:
        if axis.action == "select":
            v = browser.evaluate_js(
                f"(() => {{ const el = document.querySelector({json.dumps(axis.control)}); "
                f"return el ? Array.from(el.options).map(o => o.value) : []; }})()"
            )
            if isinstance(v, list):
                return [str(x) for x in v]
            return []
        # type / click: discover by visible text
        count = browser.get_count(axis.control)
        out: list[str] = []
        for i in range(count):
            t = browser.get_text(f"{axis.control} >> nth={i}").strip()
            if t:
                out.append(t)
        return out
    except Exception as exc:
        log.warning("auto-discover failed for %r: %s", axis.control, exc)
        return []


def _apply_axis_action(browser: BrowserAdapter, axis: Any, value: str) -> None:
    """Set this axis to `value` via the configured action."""
    if axis.action == "type":
        browser.type(axis.control, value)
    elif axis.action == "select":
        browser.select(axis.control, value)
    elif axis.action == "click":
        # Click the matching element by text within the control set
        try:
            count = browser.get_count(axis.control)
            for i in range(count):
                sel = f"{axis.control} >> nth={i}"
                txt = browser.get_text(sel).strip()
                if txt == value:
                    browser.click(sel)
                    return
        except Exception:
            pass
        log.warning("click axis: no element with text %r under %r", value, axis.control)
    else:
        log.warning("unknown axis action %r", axis.action)


def _now_iso() -> str:
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Quality gates — evaluated at scenario teardown.
# ---------------------------------------------------------------------------


def _check_quality_gates(verification: Any, scenario: Any) -> list:
    """Evaluate every QualityGate, return a list of synthetic RuleResults
    for any failures. Each failure fails the scenario."""
    from aitester_bdd.engine.verdict import RuleResult

    failures: list = []
    for art_name, qg in verification.quality_gates.items():
        records = verification.artifact_store.get(art_name, [])
        n = len(records)

        if qg.min_records is not None and n < qg.min_records:
            failures.append(RuleResult(
                rule_name=f"quality_gate:{art_name}:min_records",
                scenario_name=scenario.name,
                passed=False,
                failure_step_kind="quality_gate",
                failure_step_repr=f"min_records >= {qg.min_records}",
                failure_message=f"artifact {art_name!r} has {n} records, required >= {qg.min_records}",
                expected=str(qg.min_records),
                observed=str(n),
            ))

        for fname, min_pct in qg.filled_pcts.items():
            total = 0
            filled = 0
            for r in records:
                d = r.get("data") or {}
                if fname in d:
                    total += 1
                    v = d[fname]
                    if v not in (None, "", [], {}):
                        filled += 1
            if total > 0:
                actual = (filled / total) * 100
                if actual < min_pct:
                    failures.append(RuleResult(
                        rule_name=f"quality_gate:{art_name}:filled_pct:{fname}",
                        scenario_name=scenario.name,
                        passed=False,
                        failure_step_kind="quality_gate",
                        failure_step_repr=f"filled_pct({fname}) >= {min_pct}%",
                        failure_message=(
                            f"artifact {art_name!r} field {fname!r} filled "
                            f"{actual:.1f}%, required >= {min_pct}%"
                        ),
                        expected=f"{min_pct}%",
                        observed=f"{actual:.1f}%",
                    ))
    return failures


def _write_artifacts(verification: Any) -> None:
    """Write each artifact (with `output=True`) to <output_dir>/<name>.jsonl
    at scenario teardown. Honors dedupe."""
    import json
    from aitester_bdd.engine.emit import _output_dir

    output_dir = _output_dir()
    for art_name, art in verification.artifacts.items():
        if not art.output:
            continue
        records = verification.artifact_store.get(art_name, [])
        if not records:
            continue
        if art.dedupe:
            seen: set = set()
            deduped = []
            for r in records:
                key_val = (r.get("data") or {}).get(art.dedupe)
                if key_val is None:
                    deduped.append(r)
                elif key_val not in seen:
                    seen.add(key_val)
                    deduped.append(r)
            records = deduped
        override = verification.write_overrides.get(art_name)
        path = __import__("pathlib").Path(override) if override else output_dir / f"{art_name}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as fp:
            for r in records:
                fp.write(json.dumps(r, ensure_ascii=False, default=str))
                fp.write("\n")
        log.info("wrote artifact %r (%d records) to %s", art_name, len(records), path)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# AOP — failure / trajectory / instrumentation aspects live in
# engine/aspects.py and engine/walk_log.py. The walker fires hooks via
# the AspectRegistry it carries. See _build_default_registry() below
# for the standard wiring.
# ---------------------------------------------------------------------------


def _run_state_setup(browser: BrowserAdapter, verification: "Verification") -> None:
    """Execute suite-level state setup (auth, consent) before any scenario.

    Ported from WISE _run_setup. If `state_setup.skip_when` is a CSS
    selector that matches the current page (e.g., a logged-in marker),
    setup is skipped. Otherwise each action runs in order:
      - action=open url=...      → navigate
      - action=input css=... value=...  → fill text
      - action=password css=... value=...  → fill text (treated as secret)
      - action=click css=...     → click

    All errors are logged + skipped so a misconfigured setup doesn't
    block the test run with a cryptic adapter error.
    """
    ss = verification.state_setup
    if not ss.actions:
        return
    if ss.skip_when:
        try:
            if browser.get_count(ss.skip_when) > 0:
                log.info("state_setup: skipped — skip_when %r matched", ss.skip_when)
                return
        except Exception:
            pass
    for sa in ss.actions:
        action = sa.get("action", "")
        try:
            if action == "open":
                browser.open(sa.get("url", ""))
            elif action == "input":
                browser.type(sa.get("css", ""), sa.get("value", ""), secret=False)
            elif action == "password":
                browser.type(sa.get("css", ""), sa.get("value", ""), secret=True)
            elif action == "click":
                browser.click(sa.get("css", ""))
            else:
                log.warning("state_setup: unknown action %r", action)
        except Exception as exc:
            log.warning("state_setup %s failed: %s", action, exc)
    browser.wait_for_load_state("networkidle", timeout="10s")


def _build_default_registry(walk_log: Any):
    """Wire up the standard aspects: trajectory recording, AI failure
    diagnosis, slow-action instrumentation. Override by setting
    AITESTER_DISABLE_ASPECTS=trajectory,diagnose,instrument (csv)."""
    import os
    from aitester_bdd.engine.aspects import AspectRegistry
    from aitester_bdd.engine.walk_log import (
        make_diagnose_aspect, make_instrument_aspect, make_trajectory_aspect,
    )
    from aitester_bdd.engine.emit import _output_dir

    disabled = set(
        a.strip() for a in os.environ.get("AITESTER_DISABLE_ASPECTS", "").split(",") if a.strip()
    )
    registry = AspectRegistry()
    if "trajectory" not in disabled:
        registry.register(make_trajectory_aspect(walk_log))
    if "instrument" not in disabled:
        registry.register(make_instrument_aspect())
    if "diagnose" not in disabled:
        registry.register(
            make_diagnose_aspect(
                get_llm=_get_llm,
                output_dir_fn=_output_dir,
                get_story=lambda v: getattr(v, "story", "") or "",
            )
        )
    return registry


def walk_verification(verification: "Verification") -> Verdict:
    """Walk all scenarios in a Verification; return a Verdict.

    Global run timeout via AITESTER_RUN_TIMEOUT (seconds, default 300).
    Browser session is opened once and torn down at the end (assumes one
    .robot suite per process).

    Aspects wired by default:
      - trajectory: records every MDP transition into walk_log.jsonl
      - instrument: WARNs when a single action exceeds 0.5s
      - diagnose: on every rule failure, asks the LLM "why?" and stores
        the answer on RuleResult.ai_diagnosis + appends failures.jsonl

    Disable any combination via AITESTER_DISABLE_ASPECTS=trajectory,...
    """
    import os
    from aitester_bdd.engine.emit import _output_dir
    from aitester_bdd.engine.walk_log import WalkLog

    run_timeout_s = int(os.environ.get("AITESTER_RUN_TIMEOUT", str(DEFAULT_RUN_TIMEOUT_S)))
    run_deadline = time.time() + run_timeout_s if run_timeout_s else None

    walk_log = WalkLog(sink_path=_output_dir() / "walk_log.jsonl")
    registry = _build_default_registry(walk_log)

    verdict = Verdict(verification_name=verification.name)
    browser = BrowserAdapter()
    browser.new_session(headless=True)
    try:
        # Suite-level state setup (auth, consent) — runs once before any
        # scenario. Ported from WISE.
        _run_state_setup(browser, verification)
        for sc in verification.scenarios:
            if registry.fire_before_scenario(verification, sc):
                continue

            if sc.entry_url:
                browser.open(sc.entry_url)
                browser.wait_for_load_state("domcontentloaded", timeout="10s")
                # Dismiss interrupts after initial load too
                for sel in verification.interrupts.dismiss_selectors:
                    try:
                        if browser.get_count(sel) > 0:
                            browser.click(sel)
                            registry.fire_after_dismiss(None, sel)
                    except Exception:
                        pass

            order = _topo_sort(sc.rules)
            already_passed: set[str] = set()
            scenario_passed = True
            for rname in order:
                rule = sc.rules.get(rname)
                if rule is None:
                    verdict.results.append(
                        RuleResult(
                            rule_name=rname, scenario_name=sc.name, passed=False,
                            failure_step_kind="parent_unknown",
                            failure_message=f"undefined parent rule {rname!r}",
                        )
                    )
                    scenario_passed = False
                    continue
                result = _walk_rule(
                    browser, sc, rule, verification,
                    already_passed=already_passed,
                    run_deadline=run_deadline,
                    registry=registry, walk_log=walk_log,
                )
                verdict.results.append(result)
                if result.passed:
                    already_passed.add(rname)
                else:
                    scenario_passed = False

            # ── Scenario teardown: artifact-model commitments ─────────
            # Quality gates: evaluate each artifact's assertions; failures
            # become synthetic RuleResults that fail the scenario.
            qg_failures = _check_quality_gates(verification, sc)
            for qf in qg_failures:
                verdict.results.append(qf)
                scenario_passed = False

            registry.fire_after_scenario(verification, sc, scenario_passed)

        # ── Verification teardown: write artifact files ───────────────
        # Writes each artifact (output=True) to <output_dir>/<name>.jsonl.
        # Happens once after all scenarios complete so multi-scenario
        # runs accumulate into single artifact files.
        _write_artifacts(verification)
    finally:
        browser.close()
        walk_log.close()
    return verdict
