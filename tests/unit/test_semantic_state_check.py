"""Semantic StateCheck — verifies the walker actually calls the LLM
adapter when a `semantic` check is hit, and respects its pass/fail
return. No real API calls — we inject a fake LLM via the walker's
`reset_llm_cache` + monkeypatch.
"""
from __future__ import annotations

from typing import Any

from tests.unit.test_walker_with_fake_browser import FakeBrowser


class FakeLLM:
    """Records judge calls; returns whatever `default` says."""

    def __init__(self, *, default: bool = True) -> None:
        self.default = default
        self.text_calls: list[tuple[str, str]] = []
        self.visual_calls: list[tuple[str, int]] = []

    def judge(self, *, criterion: str, observation: str) -> bool:
        self.text_calls.append((criterion, observation))
        return self.default

    def judge_visual(self, *, criterion: str, png_bytes: bytes) -> bool:
        self.visual_calls.append((criterion, len(png_bytes)))
        return self.default


def _make(scenario_name="flow", entry_url="http://x"):
    from aitester_bdd.AITester import AITester

    t = AITester()
    t.start_verification('"v"')
    t.start_scenario_at(f'"{scenario_name}"', f'"{entry_url}"')
    return t


def test_semantic_llm_call_failure_fails_rule_with_clear_message(monkeypatch):
    """If the LLM raises (e.g. proxy down, auth error), the rule fails
    with the error visible in the observation — never silently passes."""
    from aitester_bdd.engine import walk
    from aitester_bdd.engine.walk import walk_verification

    class RaisingLLM:
        def judge(self, *, criterion, observation):
            raise RuntimeError("proxy connection refused")
        def judge_visual(self, *, criterion, png_bytes):
            raise RuntimeError("proxy connection refused")

    walk.reset_llm_cache()
    walk._LLM_CACHE = RaisingLLM()

    t = _make()
    t.define_rule('"sem"')
    t.when_open('"http://x/"')
    t.then_semantic_page('"the dashboard is loaded"')

    fake = FakeBrowser(current_url="http://x/")
    monkeypatch.setattr(walk, "BrowserAdapter", lambda: fake)

    verdict = walk_verification(t.get_verification())
    assert verdict.failed
    r = verdict.results[0]
    assert r.failure_step_kind == "observation_or_assertion"
    assert "proxy connection refused" in r.observed


def test_semantic_pass_when_llm_returns_true(monkeypatch):
    """If the LLM says PASS, the rule passes; observation captures it."""
    from aitester_bdd.engine import walk
    from aitester_bdd.engine.walk import walk_verification

    monkeypatch.setenv("AITESTER_LLM_MODEL", "test/fake")
    walk.reset_llm_cache()

    fake_llm = FakeLLM(default=True)
    # Inject the fake by warming the cache.
    walk._LLM_CACHE = fake_llm

    t = _make()
    t.define_rule('"sem"')
    t.when_open('"http://x/"')
    t.then_semantic_page('"the dashboard is loaded"')

    fake = FakeBrowser(
        current_url="http://x/",
        text_for={"body": "Welcome to the dashboard. Active cases: 12."},
    )
    monkeypatch.setattr(walk, "BrowserAdapter", lambda: fake)

    verdict = walk_verification(t.get_verification())
    assert verdict.passed, verdict.format_failure()
    assert len(fake_llm.text_calls) == 1
    criterion, observation = fake_llm.text_calls[0]
    assert criterion == "the dashboard is loaded"
    assert "URL: http://x/" in observation
    assert "Welcome to the dashboard" in observation


def test_semantic_fail_when_llm_returns_false(monkeypatch):
    from aitester_bdd.engine import walk
    from aitester_bdd.engine.walk import walk_verification

    monkeypatch.setenv("AITESTER_LLM_MODEL", "test/fake")
    walk.reset_llm_cache()
    walk._LLM_CACHE = FakeLLM(default=False)

    t = _make()
    t.define_rule('"sem"')
    t.when_open('"http://x/"')
    t.then_semantic_page('"an error message is shown"')

    fake = FakeBrowser(current_url="http://x/", text_for={"body": "everything looks fine"})
    monkeypatch.setattr(walk, "BrowserAdapter", lambda: fake)

    verdict = walk_verification(t.get_verification())
    assert verdict.failed
    r = verdict.results[0]
    assert r.failure_step_kind == "observation_or_assertion"
    assert "semantic" in r.failure_step_repr


def test_semantic_locator_scope_passes_only_subtree_text(monkeypatch):
    """When scope=locator, observation contains only the locator's text."""
    from aitester_bdd.engine import walk
    from aitester_bdd.engine.walk import walk_verification

    monkeypatch.setenv("AITESTER_LLM_MODEL", "test/fake")
    walk.reset_llm_cache()
    fake_llm = FakeLLM(default=True)
    walk._LLM_CACHE = fake_llm

    t = _make()
    t.define_rule('"sem"')
    t.when_open('"http://x/"')
    t.then_semantic_locator('".banner"', '"the banner says approval succeeded"')

    fake = FakeBrowser(
        current_url="http://x/",
        text_for={
            "body": "noise noise noise",
            ".banner": "approval succeeded",
        },
        selector_present={".banner": True},
    )
    monkeypatch.setattr(walk, "BrowserAdapter", lambda: fake)

    walk_verification(t.get_verification())
    assert len(fake_llm.text_calls) == 1
    _, observation = fake_llm.text_calls[0]
    assert "approval succeeded" in observation
    assert "noise noise noise" not in observation


def test_visual_semantic_passes_screenshot_bytes(monkeypatch, tmp_path):
    """visual_semantic feeds PNG bytes to llm.judge_visual."""
    from aitester_bdd.engine import walk
    from aitester_bdd.engine.walk import walk_verification

    monkeypatch.setenv("AITESTER_LLM_MODEL", "test/fake")
    walk.reset_llm_cache()
    fake_llm = FakeLLM(default=True)
    walk._LLM_CACHE = fake_llm

    # Patch FakeBrowser.screenshot to actually write a non-empty file.
    class VisualFakeBrowser(FakeBrowser):
        def screenshot(self, filename=None) -> str:
            from pathlib import Path
            path = filename or str(tmp_path / "shot.png")
            Path(path).write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
            return path

    t = _make()
    t.define_rule('"vis"')
    t.when_open('"http://x/"')
    t.then_visual_semantic('"the chart shows an upward trend"')

    fake = VisualFakeBrowser(current_url="http://x/")
    monkeypatch.setattr(walk, "BrowserAdapter", lambda: fake)

    verdict = walk_verification(t.get_verification())
    assert verdict.passed, verdict.format_failure()
    assert len(fake_llm.visual_calls) == 1
    criterion, byte_len = fake_llm.visual_calls[0]
    assert criterion == "the chart shows an upward trend"
    assert byte_len > 0
