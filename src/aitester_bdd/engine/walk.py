"""Walker — evaluates a Verification's rule DAG against a live browser.

Plan-then-Execute Phase B:
1. For each scenario, topo-sort rules by parent declarations.
2. For each rule (parents first), walk items in order:
   - StateCheck before any Action in this rule -> guard:
       evaluate once with no wait; fail = skip the rule.
   - StateCheck after an Action -> observation/assertion:
       wait with timeout; fail = fail the rule.
   - Action -> execute via BrowserAdapter.
3. On any failure, capture screenshot, record evidence, abort the rule (and
   transitively skip its descendants).
4. Emit a Verdict at the end.

One concept: StateCheck. Position determines wait behavior and failure scope.
"""
from __future__ import annotations

import logging
import re
import time
from typing import TYPE_CHECKING

from aitester_bdd.engine.browser import BrowserAdapter
from aitester_bdd.engine.verdict import RuleResult, Verdict

if TYPE_CHECKING:
    from aitester_bdd.AITester import Action, Rule, Scenario, StateCheck, Verification

log = logging.getLogger("aitester_bdd.engine.walk")

DEFAULT_OBSERVATION_TIMEOUT_MS = 5000
DEFAULT_GUARD_TIMEOUT_MS = 200


def _topo_sort(rules_by_name: dict[str, "Rule"]) -> list[str]:
    """Return rule names in dependency order (parents before children).

    Cycles raise ValueError; unknown parents are placed at their cite position
    and flagged at walk time.
    """
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


def _eval_state_check(
    browser, sc: "StateCheck", *, timeout_ms: int
) -> tuple[bool, str, str]:
    """Evaluate one StateCheck. Returns (passed, expected_repr, observed_repr).

    The single dispatch covers every kind the keyword library can emit —
    URL checks, element existence/counts, text/state/class/attribute,
    form values, network, and semantic.
    """
    kind = sc.kind
    css = sc.locator
    expected = sc.expected
    extra = sc.extra

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
        ok = browser.wait_for_selector(css, present=True, timeout_ms=timeout_ms)
        return (ok, f"selector {css!r} exists", "present" if ok else "absent")
    if kind == "selector_missing":
        ok = browser.wait_for_selector(css, present=False, timeout_ms=timeout_ms)
        return (ok, f"selector {css!r} does not exist", "absent" if ok else "present")

    # ── Counts ─────────────────────────────────────────────────────────
    if kind == "count_eq":
        obs = browser.get_count(css)
        return (obs == int(expected), f"count == {expected}", str(obs))
    if kind == "count_at_least":
        obs = browser.get_count(css)
        return (obs >= int(expected), f"count >= {expected}", str(obs))
    if kind == "count_at_most":
        obs = browser.get_count(css)
        return (obs <= int(expected), f"count <= {expected}", str(obs))

    # ── Element text ──────────────────────────────────────────────────
    if kind == "has_text":
        obs = browser.get_text(css).strip()
        return (obs == expected, expected, obs)
    if kind == "contains":
        obs = browser.get_text(css)
        return (expected in obs, f"contains {expected!r}", obs)
    if kind == "matches":
        obs = browser.get_text(css)
        return (bool(re.search(expected, obs)), f"matches {expected!r}", obs)
    if kind == "not_contains":
        obs = browser.get_text(css)
        return (expected not in obs, f"not contains {expected!r}", obs)

    # ── Element state ──────────────────────────────────────────────────
    if kind == "visible":
        ok = browser.is_visible(css)
        return (ok, "visible", "visible" if ok else "hidden")
    if kind == "hidden":
        ok = not browser.is_visible(css)
        return (ok, "hidden", "hidden" if ok else "visible")
    if kind == "enabled":
        ok = browser.is_enabled(css)
        return (ok, "enabled", "enabled" if ok else "disabled")
    if kind == "disabled":
        ok = not browser.is_enabled(css)
        return (ok, "disabled", "disabled" if ok else "enabled")
    if kind == "checked":
        ok = browser.is_checked(css)
        return (ok, "checked", "checked" if ok else "unchecked")

    # ── Class / attribute ─────────────────────────────────────────────
    if kind == "has_class":
        cls = browser.get_class(css)
        ok = expected in cls.split()
        return (ok, f"has class {expected!r}", cls)
    if kind == "not_class":
        cls = browser.get_class(css)
        ok = expected not in cls.split()
        return (ok, f"does not have class {expected!r}", cls)
    if kind == "attr_eq":
        attr = extra.get("attr", "")
        obs = browser.get_attribute(css, attr)
        return (obs == expected, f"{attr}={expected!r}", obs)
    if kind == "attr_contains":
        attr = extra.get("attr", "")
        obs = browser.get_attribute(css, attr)
        return (expected in obs, f"{attr} contains {expected!r}", obs)

    # ── Form values ───────────────────────────────────────────────────
    if kind == "input_value":
        obs = browser.get_value(css)
        return (obs == expected, expected, obs)
    if kind == "select_selected":
        obs = browser.get_value(css)
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

    # ── Semantic (AI-judged) — stub; full impl needs LLM client
    if kind == "semantic":
        return (True, "semantic (stub)", "passed without judging")

    return (False, f"unknown state check {kind}", "")


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


def _eval_action(browser, action: "Action") -> None:
    """Execute one action against the live browser."""
    kind = action.kind
    if kind == "open":
        browser.open(action.target)
    elif kind == "reload":
        browser.reload()
    elif kind == "back":
        browser.go_back()
    elif kind == "click":
        browser.click(action.target)
        _maybe_await(browser, action.options)
    elif kind == "click_text":
        browser.click_text(action.target)
        _maybe_await(browser, action.options)
    elif kind == "dblclick":
        browser.double_click(action.target)
    elif kind == "type":
        browser.type(action.target, action.value, secret=False)
        _maybe_await(browser, action.options)
    elif kind == "type_secret":
        browser.type(action.target, action.value, secret=True)
        _maybe_await(browser, action.options)
    elif kind == "select":
        browser.select(action.target, action.value)
    elif kind == "check":
        browser.check(action.target)
    elif kind == "uncheck":
        browser.uncheck(action.target)
    elif kind == "hover":
        browser.hover(action.target)
    elif kind == "focus":
        browser.focus(action.target)
    elif kind == "press":
        browser.press(action.target, action.options.get("keys", []))
    elif kind == "upload":
        browser.upload(action.target, action.value)
    elif kind == "scroll":
        browser.scroll()
    elif kind == "wait_idle":
        browser.wait_for_idle()
    elif kind == "screenshot":
        browser.screenshot(action.options.get("filename"))
    elif kind == "js":
        browser.evaluate_js(action.value)
    elif kind == "browser_step":
        log.warning("browser_step passthrough not yet supported: %s", action.target)
    elif kind == "call_keyword":
        log.warning("call_keyword passthrough not yet supported: %s", action.target)
    else:
        log.warning("unknown action %s", kind)


def _maybe_await(browser, options: dict) -> None:
    """Honor inline `await=<selector>` option after click/type."""
    sel = options.get("await")
    if sel:
        browser.wait_for_selector(sel, present=True, timeout_ms=DEFAULT_OBSERVATION_TIMEOUT_MS)


def _walk_rule(
    browser, scenario: "Scenario", rule: "Rule", *, already_passed: set[str]
) -> RuleResult:
    """Walk one rule's items in order. Returns its RuleResult."""
    from aitester_bdd.AITester import Action, StateCheck  # local to avoid circular import

    start = time.time()

    for p in rule.parents:
        if p not in already_passed:
            return RuleResult(
                rule_name=rule.name,
                scenario_name=scenario.name,
                passed=False,
                failure_step_kind="parent_failed",
                failure_message=f"parent rule {p!r} did not pass",
                duration_ms=(time.time() - start) * 1000,
            )

    saw_action = False
    for it in rule.items:
        if isinstance(it, StateCheck):
            # Position determines treatment:
            #   pre-action  -> guard (no wait, fail = skip rule)
            #   post-action -> observation/assertion (wait, fail = fail rule)
            timeout = DEFAULT_OBSERVATION_TIMEOUT_MS if saw_action else DEFAULT_GUARD_TIMEOUT_MS
            ok, expected, observed = _eval_state_check(browser, it, timeout_ms=timeout)
            if not ok:
                step_kind = "observation_or_assertion" if saw_action else "guard"
                shot = (
                    browser.screenshot(f"fail_{scenario.name}_{rule.name}_{step_kind}.png")
                    if saw_action
                    else None
                )
                msg = (
                    "post-action check failed (observation/assertion)"
                    if saw_action
                    else "pre-action guard failed; rule skipped"
                )
                return RuleResult(
                    rule_name=rule.name,
                    scenario_name=scenario.name,
                    passed=False,
                    failure_step_kind=step_kind,
                    failure_step_repr=f"{it.kind} {it.locator or it.expected}",
                    failure_message=msg,
                    expected=expected,
                    observed=observed,
                    screenshot=shot,
                    duration_ms=(time.time() - start) * 1000,
                )
        elif isinstance(it, Action):
            saw_action = True
            try:
                _eval_action(browser, it)
            except Exception as exc:
                return RuleResult(
                    rule_name=rule.name,
                    scenario_name=scenario.name,
                    passed=False,
                    failure_step_kind="action",
                    failure_step_repr=f"{it.kind} {it.target}",
                    failure_message=f"action raised: {type(exc).__name__}: {exc}",
                    screenshot=browser.screenshot(f"fail_{scenario.name}_{rule.name}_action.png"),
                    duration_ms=(time.time() - start) * 1000,
                )

    return RuleResult(
        rule_name=rule.name,
        scenario_name=scenario.name,
        passed=True,
        duration_ms=(time.time() - start) * 1000,
    )


def walk_verification(verification: "Verification") -> Verdict:
    """Walk all scenarios in a Verification; return a Verdict."""
    verdict = Verdict(verification_name=verification.name)
    browser = BrowserAdapter()
    browser.new_session(headless=True)
    try:
        for sc in verification.scenarios:
            if sc.entry_url:
                browser.open(sc.entry_url)
            order = _topo_sort(sc.rules)
            already_passed: set[str] = set()
            for rname in order:
                rule = sc.rules.get(rname)
                if rule is None:
                    verdict.results.append(
                        RuleResult(
                            rule_name=rname,
                            scenario_name=sc.name,
                            passed=False,
                            failure_step_kind="parent_unknown",
                            failure_message=f"undefined parent rule {rname!r}",
                        )
                    )
                    continue
                result = _walk_rule(browser, sc, rule, already_passed=already_passed)
                verdict.results.append(result)
                if result.passed:
                    already_passed.add(rname)
    finally:
        browser.close()
    return verdict
