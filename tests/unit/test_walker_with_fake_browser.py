"""Walker — verify it evaluates StateChecks correctly against a fake browser.

Uses a Fake browser that returns scripted responses so we exercise the
position-determined guard-vs-observation logic without needing a real
Playwright/RF-Browser environment.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class FakeBrowser:
    """Records calls; returns scripted values."""

    current_url: str = ""
    selector_present: dict[str, bool] = field(default_factory=dict)
    text_for: dict[str, str] = field(default_factory=dict)
    count_for: dict[str, int] = field(default_factory=dict)
    actions_called: list[tuple[str, str]] = field(default_factory=list)

    def new_session(self, **kw) -> None: ...
    def close(self) -> None: ...
    def open(self, url: str) -> None:
        self.current_url = url
        self.actions_called.append(("open", url))
    def reload(self) -> None:
        self.actions_called.append(("reload", ""))
    def go_back(self) -> None: ...
    def url(self) -> str:
        return self.current_url
    def click(self, css: str) -> None:
        self.actions_called.append(("click", css))
    def click_text(self, t: str) -> None:
        self.actions_called.append(("click_text", t))
    def double_click(self, css: str) -> None: ...
    def type(self, css: str, v: str, *, secret: bool = False) -> None:
        self.actions_called.append(("type" if not secret else "type_secret", f"{css}={v}"))
    def select(self, css, v) -> None: ...
    def check(self, css) -> None: ...
    def uncheck(self, css) -> None: ...
    def hover(self, css) -> None: ...
    def focus(self, css) -> None: ...
    def press(self, css, keys) -> None: ...
    def upload(self, css, p) -> None: ...
    def scroll(self) -> None: ...
    def wait_for_idle(self) -> None: ...
    def evaluate_js(self, s) -> None: ...
    def screenshot(self, filename=None) -> str:
        return filename or "shot.png"
    def wait_for_selector(self, css: str, *, present: bool = True, timeout_ms: int = 5000) -> bool:
        existing = self.selector_present.get(css, False)
        return existing if present else (not existing)
    def get_text(self, css: str) -> str:
        return self.text_for.get(css, "")
    def get_attribute(self, css, attr) -> str:
        return ""
    def get_value(self, css) -> str:
        return ""
    def get_class(self, css) -> str:
        return ""
    def get_count(self, css) -> int:
        return self.count_for.get(css, 0)
    def is_visible(self, css) -> bool:
        return self.selector_present.get(css, False)
    def is_enabled(self, css) -> bool:
        return True
    def is_checked(self, css) -> bool:
        return False
    def last_response_status(self):
        return None
    def last_response_body(self) -> str:
        return ""
    # WISE-ported surface
    def wait_for_load_state(self, state="domcontentloaded", *, timeout="10s") -> None: ...
    def wait_for_elements_state(self, selector, state="attached", *, timeout_ms=5000) -> bool:
        present = self.selector_present.get(selector, False)
        if state in ("attached", "visible"):
            return present
        return not present
    def resolve_fallback_selector(self, raw: str, scope: str = "") -> str:
        """Honor the pipe-fallback in tests: pick the first candidate that
        the FakeBrowser actually has live (count > 0 or present-True or
        non-empty text), mirroring the real BrowserAdapter behavior.

        Scope is composed in front via `>>` to mirror the real adapter."""
        if raw.strip() == "." and scope:
            return scope
        if " | " not in raw:
            return f"{scope} >> {raw}" if scope else raw
        for c in (s.strip() for s in raw.split(" | ")):
            candidate = f"{scope} >> {c}" if scope else c
            if (
                self.selector_present.get(candidate, False)
                or self.count_for.get(candidate, 0) > 0
                or self.text_for.get(candidate, "")
                or (not scope and (
                    self.selector_present.get(c, False)
                    or self.count_for.get(c, 0) > 0
                    or self.text_for.get(c, "")
                ))
            ):
                return candidate
        first = raw.split(" | ")[0].strip()
        return f"{scope} >> {first}" if scope else first
    def set_stepper(self, selector: str, count: int) -> None:
        for _ in range(count):
            self.actions_called.append(("set_stepper", selector))


def _build(verification_name="v"):
    from aitester_bdd.AITester import AITester

    t = AITester()
    t.start_verification(f'"{verification_name}"')
    return t


def test_walker_passes_simple_rule_with_observation_gate(monkeypatch):
    """Login click followed by 'and selector exists' observation should pass."""
    from aitester_bdd.engine import walk
    from aitester_bdd.engine.walk import walk_verification

    t = _build()
    t.start_scenario_at('"flow"', '"http://x"')
    t.define_rule('"login"')
    t.when_click_locator('"button[type=submit]"')
    t.and_selector_exists('"[data-testid=overview]"')

    fake = FakeBrowser(
        current_url="http://x/",
        selector_present={"[data-testid=overview]": True},
    )
    monkeypatch.setattr(walk, "BrowserAdapter", lambda: fake)

    verdict = walk_verification(t.get_verification())
    assert verdict.passed, verdict.format_failure()
    assert verdict.results[0].rule_name == "login"
    assert ("click", "button[type=submit]") in fake.actions_called


def test_walker_fails_when_observation_times_out(monkeypatch):
    """Post-action selector check that's not present should fail the rule."""
    from aitester_bdd.engine import walk
    from aitester_bdd.engine.walk import walk_verification

    t = _build()
    t.start_scenario_at('"flow"', '"http://x"')
    t.define_rule('"login"')
    t.when_click_locator('"button[type=submit]"')
    t.and_selector_exists('"[data-testid=overview]"')

    fake = FakeBrowser(current_url="http://x/", selector_present={})  # selector NOT present
    monkeypatch.setattr(walk, "BrowserAdapter", lambda: fake)

    verdict = walk_verification(t.get_verification())
    assert verdict.failed
    r = verdict.results[0]
    assert r.passed is False
    assert r.failure_step_kind == "observation_or_assertion"


def test_walker_guard_failure_skips_rule(monkeypatch):
    """Pre-action guard that fails skips the rule (no action runs)."""
    from aitester_bdd.engine import walk
    from aitester_bdd.engine.walk import walk_verification

    t = _build()
    t.start_scenario_at('"flow"', '"http://x"')
    t.define_rule('"only_when_on_overview"')
    t.given_url_contains('"/case/"')   # GUARD — fails: url is "http://x/"
    t.when_click_locator('"button"')   # action should NOT run

    fake = FakeBrowser(current_url="http://x/")
    monkeypatch.setattr(walk, "BrowserAdapter", lambda: fake)

    verdict = walk_verification(t.get_verification())
    assert verdict.failed
    r = verdict.results[0]
    assert r.failure_step_kind == "guard"
    assert ("click", "button") not in fake.actions_called


def test_walker_parent_chain_skips_child_on_parent_failure(monkeypatch):
    """If parent fails, child should be marked skipped, not retried."""
    from aitester_bdd.engine import walk
    from aitester_bdd.engine.walk import walk_verification

    t = _build()
    t.start_scenario_at('"flow"', '"http://x"')
    t.define_rule('"login"')
    t.when_click_locator('"button"')
    t.and_selector_exists('"[data-testid=overview]"')  # observation will fail
    t.define_rule('"approve"')
    t.declare_parents('"login"')
    t.when_click_locator('".approve"')

    fake = FakeBrowser(current_url="http://x/", selector_present={})  # nothing exists
    monkeypatch.setattr(walk, "BrowserAdapter", lambda: fake)

    verdict = walk_verification(t.get_verification())
    assert verdict.failed
    by_rule = {r.rule_name: r for r in verdict.results}
    assert by_rule["login"].failure_step_kind == "observation_or_assertion"
    assert by_rule["approve"].failure_step_kind == "parent_failed"
    # approve's click should not have run
    assert (".approve",) not in [(a[1],) for a in fake.actions_called]


def test_walker_text_assertion(monkeypatch):
    from aitester_bdd.engine import walk
    from aitester_bdd.engine.walk import walk_verification

    t = _build()
    t.start_scenario_at('"flow"', '"http://x"')
    t.define_rule('"check"')
    t.when_open('"http://x/"')
    t.then_has_text('"h1"', '"Hello"')

    fake = FakeBrowser(current_url="http://x/", text_for={"h1": "Hello"})
    monkeypatch.setattr(walk, "BrowserAdapter", lambda: fake)

    verdict = walk_verification(t.get_verification())
    assert verdict.passed, verdict.format_failure()


def test_walker_count_assertion(monkeypatch):
    from aitester_bdd.engine import walk
    from aitester_bdd.engine.walk import walk_verification

    t = _build()
    t.start_scenario_at('"flow"', '"http://x"')
    t.define_rule('"check"')
    t.when_open('"http://x/"')
    t.then_count_at_least('"li"', "3")

    fake = FakeBrowser(current_url="http://x/", count_for={"li": 5})
    monkeypatch.setattr(walk, "BrowserAdapter", lambda: fake)

    verdict = walk_verification(t.get_verification())
    assert verdict.passed, verdict.format_failure()
