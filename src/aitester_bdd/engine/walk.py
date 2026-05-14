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

    Ported from WISE. Called before guards and before every action.
    Each selector is tried once per call — if a modal pops up mid-action,
    the action handler's retry path will call this again.
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

    Guards = StateChecks before the first Action.
    Body   = everything from the first Action onward (Actions + inline
             StateChecks that act as observation gates / assertions).
    """
    from aitester_bdd.AITester import Action, StateCheck

    guards: list[StateCheck] = []
    body: list = []
    saw_action = False
    for it in rule.items:
        if saw_action:
            body.append(it)
            continue
        if isinstance(it, Action):
            saw_action = True
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
) -> tuple[bool, Optional["StateCheck"], str, str]:
    """Evaluate all guards. Returns (passed, failed_check_or_None, expected, observed).

    Dismisses interrupts once before checking. Each guard uses the short
    guard timeout (no significant waiting — guards are 'is the world already
    in the right state?').
    """
    _dismiss_interrupts(browser, _effective_interrupt_selectors(rule, verification))
    for g in guards:
        ok, expected, observed = _eval_state_check(browser, g, timeout_ms=DEFAULT_GUARD_TIMEOUT_MS)
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
) -> tuple[bool, str, str, Optional["StateCheck | Action"], str, str]:
    """Execute the rule body — actions interleaved with inline state checks.

    Returns (passed, failure_step_kind, failure_message, failed_item,
             expected, observed).

    Post-action StateChecks are observations/assertions: failing them
    FAILS the rule with structured evidence (testing-specific; WISE only
    warned).

    Actions: dismiss interrupts → run action → on raise, dismiss + retry
    once. Honors `await=<selector>` option after each action.
    """
    from aitester_bdd.AITester import Action, StateCheck

    interrupt_sels = _effective_interrupt_selectors(rule, verification)

    for step in body:
        if deadline is not None and time.time() > deadline:
            return (False, "rule_timeout",
                    f"per-rule timeout exceeded ({rule.options.get('timeout_ms')}ms)",
                    step, "", "")

        if isinstance(step, StateCheck):
            # Inline observation gate / post-action assertion.
            ok, expected, observed = _eval_state_check(
                browser, step, timeout_ms=DEFAULT_OBSERVATION_TIMEOUT_MS
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
            _dismiss_interrupts(browser, interrupt_sels)

        try:
            _eval_action(browser, step)
            _await_after_action(browser, step)
        except Exception as exc_first:
            # Recovery: a popup may have appeared between the dismiss
            # and the action. Dismiss + retry once. (Ported from WISE.)
            if interrupt_sels:
                _dismiss_interrupts(browser, interrupt_sels)
                try:
                    _eval_action(browser, step)
                    _await_after_action(browser, step)
                    continue
                except Exception as exc_retry:
                    return (False, "action",
                            f"action raised twice: {type(exc_retry).__name__}: {exc_retry}",
                            step, "", "")
            return (False, "action",
                    f"action raised: {type(exc_first).__name__}: {exc_first}",
                    step, "", "")

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
) -> RuleResult:
    """Walk one rule with full WISE semantics ported."""
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
        browser, rule, verification, guards
    )
    if not ok and rule.retry_max > 0:
        for attempt in range(1, rule.retry_max + 1):
            log.info("Retry %d/%d for rule %r (guard %r failed)",
                     attempt, rule.retry_max, rule.name,
                     failed_guard.kind if failed_guard else "?")
            time.sleep(rule.retry_delay_ms / 1000.0)
            # Replay the body before re-checking guards (WISE semantics —
            # actions may bring the world into the guarded state).
            _execute_body(browser, rule, verification, body, deadline=rule_deadline)
            ok, failed_guard, expected, observed = _check_guards(
                browser, rule, verification, guards
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
        return RuleResult(
            rule_name=rule.name, scenario_name=scenario.name, passed=False,
            failure_step_kind="guard",
            failure_step_repr=(
                f"{failed_guard.kind} {failed_guard.locator or failed_guard.expected}"
                if failed_guard else ""
            ),
            failure_message="pre-action guard failed; rule skipped",
            expected=expected, observed=observed,
            duration_ms=(time.time() - start) * 1000,
        )

    # ── Body (actions + inline observations/assertions) ───────────────
    passed, fk, fmsg, fitem, fexp, fobs = _execute_body(
        browser, rule, verification, body, deadline=rule_deadline
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
        return RuleResult(
            rule_name=rule.name, scenario_name=scenario.name, passed=False,
            failure_step_kind=fk,
            failure_step_repr=repr_str,
            failure_message=fmsg,
            expected=fexp, observed=fobs,
            screenshot=shot,
            duration_ms=(time.time() - start) * 1000,
        )

    return RuleResult(
        rule_name=rule.name, scenario_name=scenario.name, passed=True,
        duration_ms=(time.time() - start) * 1000,
    )


# ---------------------------------------------------------------------------
# Entry point
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


def walk_verification(verification: "Verification") -> Verdict:
    """Walk all scenarios in a Verification; return a Verdict.

    Global run timeout via AITESTER_RUN_TIMEOUT (seconds, default 300).
    Browser session is opened once and torn down at the end (assumes one
    .robot suite per process).
    """
    import os

    run_timeout_s = int(os.environ.get("AITESTER_RUN_TIMEOUT", str(DEFAULT_RUN_TIMEOUT_S)))
    run_deadline = time.time() + run_timeout_s if run_timeout_s else None

    verdict = Verdict(verification_name=verification.name)
    browser = BrowserAdapter()
    browser.new_session(headless=True)
    try:
        # Suite-level state setup (auth, consent) — runs once before any
        # scenario. Ported from WISE.
        _run_state_setup(browser, verification)
        for sc in verification.scenarios:
            if sc.entry_url:
                browser.open(sc.entry_url)
                browser.wait_for_load_state("domcontentloaded", timeout="10s")
                # Dismiss interrupts after initial load too
                _dismiss_interrupts(browser, verification.interrupts.dismiss_selectors)

            order = _topo_sort(sc.rules)
            already_passed: set[str] = set()
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
                    continue
                result = _walk_rule(
                    browser, sc, rule, verification,
                    already_passed=already_passed,
                    run_deadline=run_deadline,
                )
                verdict.results.append(result)
                if result.passed:
                    already_passed.add(rname)
    finally:
        browser.close()
    return verdict
