"""Verdict — the result of walking a Verification.

Per-rule pass/fail with evidence on failure.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class RuleResult:
    rule_name: str
    scenario_name: str
    passed: bool
    failure_step_kind: str = ""
    failure_step_repr: str = ""
    failure_message: str = ""
    observed: str = ""
    expected: str = ""
    screenshot: str | None = None
    duration_ms: float = 0.0


@dataclass
class Verdict:
    verification_name: str = ""
    results: list[RuleResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(r.passed for r in self.results)

    @property
    def failed(self) -> bool:
        return not self.passed

    @property
    def pass_count(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def fail_count(self) -> int:
        return sum(1 for r in self.results if not r.passed)

    def format_summary(self) -> str:
        return (
            f"Verdict: {self.pass_count}/{len(self.results)} rules passed "
            f"(verification={self.verification_name!r})"
        )

    def format_failure(self) -> str:
        if not self.failed:
            return self.format_summary()
        parts = [self.format_summary(), ""]
        for r in self.results:
            if r.passed:
                continue
            parts.append(
                f"  ✗ {r.scenario_name} / {r.rule_name}: {r.failure_step_kind} — {r.failure_message}"
            )
            if r.expected or r.observed:
                parts.append(f"      expected: {r.expected!r}")
                parts.append(f"      observed: {r.observed!r}")
            if r.failure_step_repr:
                parts.append(f"      step:     {r.failure_step_repr}")
            if r.screenshot:
                parts.append(f"      screenshot: {r.screenshot}")
        return "\n".join(parts)
