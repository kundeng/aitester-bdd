"""Emit sink — writes structured page state captures to emit.jsonl.

Explicit emit only: the author writes `And I emit "..."` in the .robot
when the story's intent goes beyond pass/fail (diagnostic probe,
differential baseline, bug-repro instrumentation). The walker captures
the named fields at the position of the Emit item in the rule body.

Failure post-mortem is handled separately — see walk._diagnose_failure
(AOP aspect that hands the failure context to the LLM and writes a
human-readable explanation to failures.jsonl). Auto-dumping every
locator's state mechanically was the wrong shape; LLM-written diagnosis
is what the user actually wants to read after a CI failure.

Output: `<output_dir>/emit.jsonl`, one JSON record per emit call.

  output_dir picked from (first set wins):
    1. AITESTER_EMIT_DIR env
    2. Robot Framework's ${OUTPUT_DIR} (when running under RF)
    3. current working directory

Each captured field is capped at AITESTER_EMIT_MAX_BYTES (default 2048)
to keep the JSONL grep-able. Truncated fields get a sibling
`<name>_truncated: true` marker.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("aitester_bdd.engine.emit")


DEFAULT_MAX_BYTES = 2048


# ---------------------------------------------------------------------------
# Where the JSONL lives
# ---------------------------------------------------------------------------


def _output_dir() -> Path:
    """Resolve where to write emit.jsonl."""
    explicit = os.environ.get("AITESTER_EMIT_DIR")
    if explicit:
        return Path(explicit)
    # If running inside Robot Framework, honor its --outputdir.
    try:
        from robot.libraries.BuiltIn import BuiltIn

        d = BuiltIn().get_variable_value("${OUTPUT_DIR}")
        if d:
            return Path(d)
    except Exception:
        pass
    return Path.cwd()


def emit_file_path() -> Path:
    return _output_dir() / "emit.jsonl"


# ---------------------------------------------------------------------------
# Value capture
# ---------------------------------------------------------------------------


def _max_bytes() -> int:
    try:
        return int(os.environ.get("AITESTER_EMIT_MAX_BYTES", DEFAULT_MAX_BYTES))
    except (ValueError, TypeError):
        return DEFAULT_MAX_BYTES


def _truncate(value: Any) -> tuple[Any, bool]:
    """Truncate string values to AITESTER_EMIT_MAX_BYTES. Other types
    pass through unchanged."""
    if not isinstance(value, str):
        return value, False
    limit = _max_bytes()
    if len(value.encode("utf-8")) <= limit:
        return value, False
    return value.encode("utf-8")[:limit].decode("utf-8", errors="ignore"), True


def capture_field(browser, field: "EmitField") -> Any:
    """Run one EmitField against the live browser; return its value.

    Returns "" / 0 / False on browser errors rather than raising — emit
    is observation, not assertion; a missing field is data, not a failure.
    """
    src = field.source
    css = browser.resolve_fallback_selector(field.locator) if field.locator else ""

    try:
        if src == "text":
            return browser.get_text(css)
        if src == "attr":
            return browser.get_attribute(css, field.attr)
        if src == "count":
            return browser.get_count(css)
        if src == "html":
            v = browser.evaluate_js(
                f"(document.querySelector({json.dumps(css)})||{{}}).outerHTML || ''"
            )
            return str(v or "")
        if src == "value":
            return browser.get_value(css)
        if src == "class":
            return browser.get_class(css)
        if src == "is_visible":
            return bool(browser.is_visible(css))
        if src == "is_enabled":
            return bool(browser.is_enabled(css))
        if src == "is_checked":
            return bool(browser.is_checked(css))
        if src == "js":
            return browser.evaluate_js(field.expr)
        log.warning("unknown emit source %r", src)
        return None
    except Exception as exc:
        log.debug("emit capture failed for %r: %s", field.name, exc)
        return None


# ---------------------------------------------------------------------------
# Record assembly + write
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _build_data(browser, fields: list["EmitField"]) -> dict:
    data: dict[str, Any] = {}
    for f in fields:
        v = capture_field(browser, f)
        v, was_truncated = _truncate(v)
        data[f.name] = v
        if was_truncated:
            data[f"{f.name}_truncated"] = True
    return data


def _write(record: dict) -> None:
    path = emit_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fp:
        fp.write(json.dumps(record, ensure_ascii=False, default=str))
        fp.write("\n")


def emit_explicit(
    browser, *, scenario: str, rule: str, emit_obj: "Emit",
) -> None:
    """Capture the named fields from the live page; append to emit.jsonl."""
    t0 = time.time()
    data = _build_data(browser, emit_obj.fields)
    record = {
        "ts": _now_iso(),
        "trigger": "explicit",
        "scenario": scenario,
        "rule": rule,
        "name": emit_obj.name,
        "url": browser.url(),
        "data": data,
        "duration_ms": int((time.time() - t0) * 1000),
    }
    _write(record)


def emit_on_failure(
    browser, *, scenario: str, rule: str, failed_item: Any,
    failure_step_kind: str, expected: str, observed: str,
) -> None:
    """Capture page state at the moment of a rule failure.

    Scans the failed rule's items for any unique `locator` / `target` /
    `extra.path` and snapshots their text + count + visibility. Always
    captures URL + last network response shape.
    """
    from aitester_bdd.AITester import Action, Emit as _Emit, StateCheck

    locators: list[str] = []
    seen: set[str] = set()

    def add(loc: str) -> None:
        if not loc or loc in seen:
            return
        seen.add(loc)
        locators.append(loc)

    rule_obj = getattr(failed_item, "_rule_ref", None)
    items: list = []
    if rule_obj is not None:
        items = rule_obj.items
    else:
        # We only have the failed item itself.
        items = [failed_item] if failed_item is not None else []

    for it in items:
        if isinstance(it, StateCheck):
            add(it.locator)
        elif isinstance(it, Action):
            add(it.target)
        elif isinstance(it, _Emit):
            for f in it.fields:
                add(f.locator)

    data: dict[str, Any] = {}
    for loc in locators[:12]:  # cap to keep records bounded
        try:
            text = browser.get_text(loc)
            text_t, trunc = _truncate(text)
            data[f"{loc}::text"] = text_t
            if trunc:
                data[f"{loc}::text_truncated"] = True
            data[f"{loc}::count"] = browser.get_count(loc)
            try:
                data[f"{loc}::visible"] = bool(browser.is_visible(loc))
            except Exception:
                pass
        except Exception:
            continue

    # Network state.
    status = None
    try:
        status = browser.last_response_status()
    except Exception:
        pass
    body = ""
    try:
        body = browser.last_response_body() or ""
    except Exception:
        pass
    if status is not None:
        data["__last_response_status"] = status
    if body:
        body_t, trunc = _truncate(body)
        data["__last_response_body"] = body_t
        if trunc:
            data["__last_response_body_truncated"] = True

    record = {
        "ts": _now_iso(),
        "trigger": "on_failure",
        "scenario": scenario,
        "rule": rule,
        "name": f"failure:{failure_step_kind}",
        "url": browser.url(),
        "failure": {
            "kind": failure_step_kind,
            "expected": expected,
            "observed": observed,
            "item_repr": _repr_item(failed_item),
        },
        "data": data,
    }
    _write(record)


def _repr_item(item: Any) -> str:
    from aitester_bdd.AITester import Action, Emit as _Emit, StateCheck

    if isinstance(item, StateCheck):
        return f"StateCheck {item.kind} {item.locator or item.expected!r}"
    if isinstance(item, Action):
        return f"Action {item.kind} {item.target!r}"
    if isinstance(item, _Emit):
        return f"Emit {item.name!r}"
    return repr(item)
