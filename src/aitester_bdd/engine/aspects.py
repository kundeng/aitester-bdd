"""Aspect-Oriented Programming registry — ported from WISE BDD.

The walker is an MDP. Aspects are cross-cutting concerns that hook into
named transitions of that MDP without touching the deterministic logic
in walk.py.

Hooks (all optional on every Aspect):

  before_scenario(verification, scenario) -> bool
      Return True to skip this scenario entirely.

  after_scenario(verification, scenario, passed: bool) -> None
      Fires once after every scenario, regardless of outcome.

  before_rule(verification, scenario, rule, already_passed: set[str]) -> bool
      Return True to skip this rule (skipped rules never fire on_rule_*).

  after_rule(verification, scenario, rule, rule_result) -> None
      Fires after every rule, passed or failed.

  before_action(rule, action) -> None
      Fires immediately before an Action item executes (after
      dismiss-interrupts, before the call lands).

  after_action(rule, action, dt_seconds: float, raised: bool) -> None
      Fires after each Action item completes (or raises, in which case
      raised=True). dt_seconds is wall-clock for that single action.

  after_state_check(rule, state_check, ok: bool, expected: str, observed: str,
                    position: str)  -> None
      position is "guard" or "observation"; ok is the deterministic
      result. Fires for every StateCheck the walker evaluates.

  after_emit(rule, emit_obj, data: dict) -> None
      Fires after each explicit `And I emit "..."` step captures.

  after_dismiss(rule, selector: str) -> None
      Fires whenever an interrupt selector was matched and clicked.

  on_rule_failure(verification, scenario, rule, failure_step_kind: str,
                  failure_step_repr: str, expected: str, observed: str,
                  failed_item, walk_log) -> str | None
      Fires whenever a rule fails. Returns an optional string the walker
      attaches to RuleResult.ai_diagnosis. Multiple aspects can hook;
      the first non-empty return wins (later aspects still fire for
      side effects).

Aspect registration order matters:
  - `before_*` hooks fire in registration order; the first to return
    True wins (later aspects don't fire for that transition).
  - `after_*` hooks fire in registration order; all run.
  - `on_rule_failure` fires in registration order; first non-empty
    return wins, others run for side effects (logging, sinks).

WISE had this exact pattern for `before_url`/`after_url` etc. — same
"return True to skip, first wins" semantics. We renamed the hook names
to match our scope (scenario/rule instead of url/resource) and added
the testing-specific hooks (`on_rule_failure`, `after_state_check`,
`after_emit`, `after_dismiss`).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

log = logging.getLogger("aitester_bdd.engine.aspects")


@dataclass
class Aspect:
    """A cross-cutting concern wrapped as a bundle of optional hooks.

    All hooks default to None — set only the ones the aspect cares
    about. The registry handles dispatch.
    """

    name: str

    before_scenario: Optional[Callable[..., bool]] = None
    after_scenario: Optional[Callable[..., None]] = None

    before_rule: Optional[Callable[..., bool]] = None
    after_rule: Optional[Callable[..., None]] = None

    before_action: Optional[Callable[..., None]] = None
    after_action: Optional[Callable[..., None]] = None

    after_state_check: Optional[Callable[..., None]] = None
    after_emit: Optional[Callable[..., None]] = None
    after_dismiss: Optional[Callable[..., None]] = None

    on_rule_failure: Optional[Callable[..., Optional[str]]] = None


class AspectRegistry:
    """Holds aspects and fires hooks at named transition points.

    Each `fire_*` method swallows exceptions raised by individual
    aspects — a misbehaving aspect must NOT crash the walk. Errors are
    logged at WARNING.
    """

    def __init__(self) -> None:
        self._aspects: list[Aspect] = []

    def register(self, aspect: Aspect) -> None:
        self._aspects.append(aspect)

    # ── Scenario lifecycle ─────────────────────────────────────────

    def fire_before_scenario(self, verification: Any, scenario: Any) -> bool:
        for a in self._aspects:
            if a.before_scenario is None:
                continue
            try:
                if a.before_scenario(verification, scenario):
                    return True
            except Exception as exc:
                log.warning("aspect %r before_scenario raised: %s", a.name, exc)
        return False

    def fire_after_scenario(
        self, verification: Any, scenario: Any, passed: bool,
    ) -> None:
        for a in self._aspects:
            if a.after_scenario is None:
                continue
            try:
                a.after_scenario(verification, scenario, passed)
            except Exception as exc:
                log.warning("aspect %r after_scenario raised: %s", a.name, exc)

    # ── Rule lifecycle ─────────────────────────────────────────────

    def fire_before_rule(
        self, verification: Any, scenario: Any, rule: Any, already_passed: set,
    ) -> bool:
        for a in self._aspects:
            if a.before_rule is None:
                continue
            try:
                if a.before_rule(verification, scenario, rule, already_passed):
                    return True
            except Exception as exc:
                log.warning("aspect %r before_rule raised: %s", a.name, exc)
        return False

    def fire_after_rule(
        self, verification: Any, scenario: Any, rule: Any, rule_result: Any,
    ) -> None:
        for a in self._aspects:
            if a.after_rule is None:
                continue
            try:
                a.after_rule(verification, scenario, rule, rule_result)
            except Exception as exc:
                log.warning("aspect %r after_rule raised: %s", a.name, exc)

    # ── Action lifecycle ───────────────────────────────────────────

    def fire_before_action(self, rule: Any, action: Any) -> None:
        for a in self._aspects:
            if a.before_action is None:
                continue
            try:
                a.before_action(rule, action)
            except Exception as exc:
                log.warning("aspect %r before_action raised: %s", a.name, exc)

    def fire_after_action(
        self, rule: Any, action: Any, dt_seconds: float, raised: bool,
    ) -> None:
        for a in self._aspects:
            if a.after_action is None:
                continue
            try:
                a.after_action(rule, action, dt_seconds, raised)
            except Exception as exc:
                log.warning("aspect %r after_action raised: %s", a.name, exc)

    # ── State check, emit, dismiss ─────────────────────────────────

    def fire_after_state_check(
        self, rule: Any, state_check: Any, ok: bool,
        expected: str, observed: str, position: str,
    ) -> None:
        for a in self._aspects:
            if a.after_state_check is None:
                continue
            try:
                a.after_state_check(rule, state_check, ok, expected, observed, position)
            except Exception as exc:
                log.warning("aspect %r after_state_check raised: %s", a.name, exc)

    def fire_after_emit(self, rule: Any, emit_obj: Any, data: dict) -> None:
        for a in self._aspects:
            if a.after_emit is None:
                continue
            try:
                a.after_emit(rule, emit_obj, data)
            except Exception as exc:
                log.warning("aspect %r after_emit raised: %s", a.name, exc)

    def fire_after_dismiss(self, rule: Any, selector: str) -> None:
        for a in self._aspects:
            if a.after_dismiss is None:
                continue
            try:
                a.after_dismiss(rule, selector)
            except Exception as exc:
                log.warning("aspect %r after_dismiss raised: %s", a.name, exc)

    # ── Failure ────────────────────────────────────────────────────

    def fire_on_rule_failure(
        self, verification: Any, scenario: Any, rule: Any,
        failure_step_kind: str, failure_step_repr: str,
        expected: str, observed: str, failed_item: Any, walk_log: Any,
    ) -> str:
        """All aspects fire; first non-empty string return is kept and
        attached to RuleResult.ai_diagnosis. Side-effect aspects (sinks,
        logs) can still fire even after the chosen one."""
        diagnosis = ""
        for a in self._aspects:
            if a.on_rule_failure is None:
                continue
            try:
                out = a.on_rule_failure(
                    verification, scenario, rule,
                    failure_step_kind, failure_step_repr,
                    expected, observed, failed_item, walk_log,
                )
                if out and not diagnosis:
                    diagnosis = str(out)
            except Exception as exc:
                log.warning("aspect %r on_rule_failure raised: %s", a.name, exc)
        return diagnosis
