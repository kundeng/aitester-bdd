"""WalkLog — append-only ordered record of MDP transitions.

The walker is a Markov Decision Process: each rule is a sequence of
`(state, action, observation, next_state)` tuples. WalkLog records
every one of those transitions so failure diagnosis has the full
episode trace, not just a "now" snapshot.

Entries are small dicts with a `kind` discriminator. Examples:

    {"kind": "rule_enter", "rule": "approve", "parents": ["login"], ...}
    {"kind": "before_action", "rule": "approve", "action": "click", "target": ".btn"}
    {"kind": "after_action",  "rule": "approve", "action": "click", "target": ".btn", "dt_ms": 87, "raised": false}
    {"kind": "state_check",   "rule": "approve", "check": "selector_exists", "locator": "h1", "position": "observation", "ok": true, ...}
    {"kind": "dismiss",       "rule": "approve", "selector": ".cookie-banner"}
    {"kind": "emit",          "rule": "approve", "name": "metrics", "data": {...}}
    {"kind": "rule_exit",     "rule": "approve", "passed": true, "duration_ms": 1230}

Two consumers:

  1. `_diagnose_failure` reads recent entries to give the LLM the
     episode trace leading up to the failure.
  2. Scenario teardown writes the full log to
     `<output_dir>/walk_log.jsonl` for postmortem analysis.

Memory bound: WalkLog keeps everything in memory. Default cap is 2000
entries (override via `AITESTER_WALK_LOG_MAX` env); older entries are
flushed to the on-disk JSONL incrementally.
"""
from __future__ import annotations

import json
import logging
import os
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger("aitester_bdd.engine.walk_log")


DEFAULT_MAX_ENTRIES = 2000


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class WalkLog:
    """In-memory ring of MDP transition records + JSONL persistence."""

    def __init__(self, *, max_entries: int | None = None, sink_path: Path | None = None) -> None:
        cap = max_entries or int(os.environ.get("AITESTER_WALK_LOG_MAX", DEFAULT_MAX_ENTRIES))
        self._entries: deque[dict] = deque(maxlen=cap)
        self._sink_path = sink_path
        if sink_path is not None:
            sink_path.parent.mkdir(parents=True, exist_ok=True)
            self._fp = sink_path.open("a", encoding="utf-8")
        else:
            self._fp = None

    # ── Recording ──────────────────────────────────────────────────

    def record(self, kind: str, **data: Any) -> None:
        entry = {"ts": _now_iso(), "kind": kind, **data}
        self._entries.append(entry)
        if self._fp is not None:
            try:
                self._fp.write(json.dumps(entry, ensure_ascii=False, default=str))
                self._fp.write("\n")
                self._fp.flush()
            except Exception as exc:
                log.debug("walk_log sink write failed: %s", exc)

    # ── Reading ────────────────────────────────────────────────────

    def entries(self) -> list[dict]:
        return list(self._entries)

    def recent(self, n: int) -> list[dict]:
        return list(self._entries)[-n:]

    def entries_for_rule(self, rule_name: str) -> list[dict]:
        return [e for e in self._entries if e.get("rule") == rule_name]

    def format_for_diagnosis(
        self, *, current_rule: str, parent_rules: Iterable[str] | None = None,
        max_recent: int = 40,
    ) -> str:
        """Render the trace into a compact human/LLM-readable block."""
        lines: list[str] = []

        # Parent rules — show pass/fail summary only.
        if parent_rules:
            lines.append("# Parent rules")
            for p in parent_rules:
                exit_entries = [
                    e for e in self._entries
                    if e.get("kind") == "rule_exit" and e.get("rule") == p
                ]
                if exit_entries:
                    last = exit_entries[-1]
                    lines.append(
                        f"  - {p}: {'PASS' if last.get('passed') else 'FAIL'} "
                        f"({last.get('duration_ms', 0)}ms)"
                    )
                else:
                    lines.append(f"  - {p}: (no exit recorded)")
            lines.append("")

        # Current rule — full trajectory.
        lines.append(f"# Trajectory for rule {current_rule!r}")
        rule_entries = self.entries_for_rule(current_rule)
        if not rule_entries:
            # Failure happened before any rule-scoped entries — show
            # the most-recent N from the whole log instead.
            rule_entries = self.recent(max_recent)
            lines.append("(no entries for this rule yet — showing recent log)")
        for e in rule_entries[-max_recent:]:
            lines.append(_format_entry(e))

        return "\n".join(lines)

    # ── Persistence ────────────────────────────────────────────────

    def close(self) -> None:
        if self._fp is not None:
            try:
                self._fp.close()
            except Exception:
                pass
            self._fp = None


def _format_entry(e: dict) -> str:
    kind = e.get("kind", "?")
    base = f"  [{e.get('ts','?')[-12:-1]}] {kind:18s}"
    if kind == "rule_enter":
        return f"{base} {e.get('rule')!r} parents={e.get('parents', [])}"
    if kind == "rule_exit":
        return f"{base} {e.get('rule')!r} passed={e.get('passed')} duration_ms={e.get('duration_ms')}"
    if kind == "before_action":
        return f"{base} {e.get('action')} target={e.get('target')!r}"
    if kind == "after_action":
        return (
            f"{base} {e.get('action')} target={e.get('target')!r} "
            f"dt_ms={e.get('dt_ms')} raised={e.get('raised')}"
        )
    if kind == "state_check":
        return (
            f"{base} {e.get('check')} pos={e.get('position')} ok={e.get('ok')} "
            f"locator={e.get('locator')!r} expected={e.get('expected')!r} "
            f"observed={e.get('observed')!r}"
        )
    if kind == "dismiss":
        return f"{base} selector={e.get('selector')!r}"
    if kind == "emit":
        keys = list((e.get('data') or {}).keys())
        return f"{base} name={e.get('name')!r} fields={keys}"
    if kind == "retry":
        return f"{base} attempt={e.get('attempt')} reason={e.get('reason')!r}"
    return f"{base} {e}"


# ---------------------------------------------------------------------------
# Built-in aspects
# ---------------------------------------------------------------------------


def make_trajectory_aspect(walk_log: WalkLog):
    """Aspect that writes every transition into the WalkLog."""
    from aitester_bdd.engine.aspects import Aspect

    def before_rule(verification, scenario, rule, already_passed):
        walk_log.record(
            "rule_enter", scenario=scenario.name, rule=rule.name,
            parents=list(rule.parents),
            parents_passed=[p for p in rule.parents if p in already_passed],
        )
        return False  # never skip

    def after_rule(verification, scenario, rule, rule_result):
        walk_log.record(
            "rule_exit", scenario=scenario.name, rule=rule.name,
            passed=rule_result.passed,
            failure_step_kind=rule_result.failure_step_kind or None,
            duration_ms=int(rule_result.duration_ms),
        )

    def before_action(rule, action):
        walk_log.record(
            "before_action", rule=rule.name,
            action=action.kind, target=action.target,
        )

    def after_action(rule, action, dt_seconds, raised):
        walk_log.record(
            "after_action", rule=rule.name,
            action=action.kind, target=action.target,
            dt_ms=int(dt_seconds * 1000), raised=raised,
        )

    def after_state_check(rule, sc, ok, expected, observed, position):
        walk_log.record(
            "state_check", rule=rule.name,
            check=sc.kind, locator=sc.locator, position=position, ok=ok,
            expected=expected[:200] if expected else "",
            observed=observed[:200] if observed else "",
        )

    def after_emit(rule, emit_obj, data):
        walk_log.record(
            "emit", rule=rule.name,
            name=emit_obj.name, data=data,
        )

    def after_dismiss(rule, selector):
        walk_log.record(
            "dismiss", rule=rule.name, selector=selector,
        )

    return Aspect(
        name="trajectory",
        before_rule=before_rule,
        after_rule=after_rule,
        before_action=before_action,
        after_action=after_action,
        after_state_check=after_state_check,
        after_emit=after_emit,
        after_dismiss=after_dismiss,
    )


def make_instrument_aspect(slow_threshold_s: float = 0.5):
    """Aspect that logs WARN when a single action exceeds threshold.
    Ported from WISE's `instrument` aspect."""
    from aitester_bdd.engine.aspects import Aspect

    def after_action(rule, action, dt_seconds, raised):
        if dt_seconds > slow_threshold_s:
            log.warning(
                "[instrument] %s/%s %s(%s) SLOW %.2fs%s",
                rule.name, action.kind, action.kind,
                (action.target or action.value or "")[:40],
                dt_seconds,
                " RAISED" if raised else "",
            )

    return Aspect(name="instrument", after_action=after_action)


def make_diagnose_aspect(get_llm: Any, output_dir_fn: Any, get_story: Any | None = None):
    """Aspect that calls llm.diagnose on rule failure.

    `get_llm()` returns the LLM client (or None — skip diagnosis).
    `output_dir_fn()` returns the directory for `failures.jsonl`.
    `get_story(verification)` optionally returns the original user story
    for richer prompt context.
    """
    from aitester_bdd.engine.aspects import Aspect

    def on_rule_failure(
        verification, scenario, rule,
        failure_step_kind, failure_step_repr,
        expected, observed, failed_item, walk_log,
    ):
        if os.environ.get("AITESTER_AI_DIAGNOSIS", "on").lower() in ("off", "0", "false", "no"):
            return ""
        llm = get_llm()
        if llm is None:
            return ""

        story = get_story(verification) if get_story else ""
        traj = walk_log.format_for_diagnosis(
            current_rule=rule.name, parent_rules=rule.parents,
        ) if walk_log else ""

        context = (
            (f"# Story (author intent)\n{story}\n\n" if story else "")
            + f"# Verification\n{verification.name!r}\n"
            + f"# Scenario\n{scenario.name!r}\n"
            + f"# Failed rule\n{rule.name!r}\n"
            + f"# Failure\nkind={failure_step_kind} step={failure_step_repr}\n"
            + f"expected={expected!r}\nobserved={observed!r}\n\n"
            + f"# MDP trajectory\n{traj}\n"
        )

        diagnosis = ""
        try:
            diagnosis = llm.diagnose(context=context)
        except Exception as exc:
            log.warning("diagnose llm call raised: %s", exc)

        # Append to failures.jsonl regardless of whether llm returned.
        try:
            path = output_dir_fn() / "failures.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fp:
                fp.write(json.dumps({
                    "ts": _now_iso(),
                    "verification": verification.name,
                    "scenario": scenario.name,
                    "rule": rule.name,
                    "failure": {
                        "kind": failure_step_kind,
                        "step": failure_step_repr,
                        "expected": expected,
                        "observed": observed,
                    },
                    "ai_diagnosis": diagnosis,
                }, ensure_ascii=False, default=str))
                fp.write("\n")
        except Exception as exc:
            log.debug("failures.jsonl write failed: %s", exc)

        return diagnosis

    return Aspect(name="diagnose", on_rule_failure=on_rule_failure)
