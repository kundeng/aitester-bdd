"""Integration tests for the TIER 1 artifact pipeline:

  register artifact → extract fields → emit (with flatten/merge/dedupe)
  → hook transforms → quality gate evaluation → artifact file written

Uses FakeBrowser so no live target required.
"""
from __future__ import annotations

import json
from pathlib import Path

from tests.unit.test_walker_with_fake_browser import FakeBrowser


def _make(scenario_name="flow", entry_url="http://x"):
    from aitester_bdd.AITester import AITester

    t = AITester()
    t.start_verification('"v"')
    t.start_scenario_at(f'"{scenario_name}"', f'"{entry_url}"')
    return t


def test_extract_fields_emit_to_artifact_writes_jsonl(monkeypatch, tmp_path):
    """Full happy path: extract a record, emit to artifact, file is written."""
    from aitester_bdd.engine import walk
    from aitester_bdd.engine.walk import walk_verification

    monkeypatch.setenv("AITESTER_EMIT_DIR", str(tmp_path))
    walk.reset_llm_cache()

    t = _make()
    # Register an artifact
    t.register_artifact('"cases"',
                        "field=id type=string required=true",
                        "field=status type=string required=true")
    t.set_artifact_options('"cases"', "description=One per case")
    # Define a rule that captures + emits
    t.define_rule('"capture"')
    t.when_open('"http://x/cases"')
    t.extract_fields(
        "field=id     extractor=attr  locator=[data-row]  attr=data-id",
        "field=status extractor=text  locator=.status",
    )
    t.emit_to_artifact('"cases"')

    fake = FakeBrowser(
        current_url="http://x/cases",
        text_for={".status": "approved"},
    )
    # Wire attr to return the id
    fake_attrs = {("[data-row]", "data-id"): "MAIN-0001"}
    orig = fake.get_attribute
    fake.get_attribute = lambda css, attr: fake_attrs.get((css, attr), orig(css, attr))

    monkeypatch.setattr(walk, "BrowserAdapter", lambda: fake)

    verdict = walk_verification(t.get_verification())
    assert verdict.passed, verdict.format_failure()

    # JSONL file should exist
    jsonl_path = tmp_path / "cases.jsonl"
    assert jsonl_path.exists(), f"expected {jsonl_path} to exist; cwd={list(tmp_path.iterdir())}"
    records = [json.loads(line) for line in jsonl_path.read_text().splitlines() if line.strip()]
    assert len(records) == 1
    assert records[0]["data"]["id"] == "MAIN-0001"
    assert records[0]["data"]["status"] == "approved"
    assert records[0]["rule"] == "capture"
    assert records[0]["scenario"] == "flow"


def test_quality_gate_min_records_fails_scenario(monkeypatch, tmp_path):
    """min_records gate with 0 records fails the scenario."""
    from aitester_bdd.engine import walk
    from aitester_bdd.engine.walk import walk_verification

    monkeypatch.setenv("AITESTER_EMIT_DIR", str(tmp_path))
    walk.reset_llm_cache()

    t = _make()
    t.register_artifact('"cases"', "field=id type=string required=true")
    t.set_artifact_options('"cases"')
    t.quality_gate_min_records("5")  # require >= 5 records

    # Define a rule but capture nothing
    t.define_rule('"empty"')
    t.when_open('"http://x/"')

    fake = FakeBrowser(current_url="http://x/")
    monkeypatch.setattr(walk, "BrowserAdapter", lambda: fake)

    verdict = walk_verification(t.get_verification())
    assert verdict.failed
    # Find the quality-gate failure
    qg_failures = [
        r for r in verdict.results
        if r.failure_step_kind == "quality_gate"
    ]
    assert len(qg_failures) == 1
    assert "cases" in qg_failures[0].rule_name
    assert qg_failures[0].expected == "5"
    assert qg_failures[0].observed == "0"


def test_emit_flattened_by_array_produces_one_record_per_element(monkeypatch, tmp_path):
    from aitester_bdd.engine import walk
    from aitester_bdd.engine.walk import walk_verification

    monkeypatch.setenv("AITESTER_EMIT_DIR", str(tmp_path))
    walk.reset_llm_cache()

    t = _make()
    t.register_artifact('"tags"', "field=name type=string required=true")
    t.define_rule('"capture_tags"')
    t.when_open('"http://x/case/1"')
    t.extract_fields(
        "field=tags extractor=grouped locator=.tag-chip",
    )
    t.emit_flattened('"tags"', '"tags"')

    # FakeBrowser needs to support grouped extraction: get_count + get_text per nth
    fake = FakeBrowser(
        current_url="http://x/case/1",
        count_for={".tag-chip": 3},
        text_for={
            ".tag-chip >> nth=0": "urgent",
            ".tag-chip >> nth=1": "audit",
            ".tag-chip >> nth=2": "follow-up",
        },
    )
    monkeypatch.setattr(walk, "BrowserAdapter", lambda: fake)

    verdict = walk_verification(t.get_verification())
    assert verdict.passed, verdict.format_failure()

    jsonl_path = tmp_path / "tags.jsonl"
    assert jsonl_path.exists()
    records = [json.loads(line) for line in jsonl_path.read_text().splitlines() if line.strip()]
    # Three records (one per tag), each with `tags: "<tag>"` because we flattened
    # a list of strings (not list of dicts).
    assert len(records) == 3
    tag_values = sorted(r["data"]["tags"] for r in records)
    assert tag_values == ["audit", "follow-up", "urgent"]


def test_dedupe_drops_repeated_records(monkeypatch, tmp_path):
    from aitester_bdd.engine import walk
    from aitester_bdd.engine.walk import walk_verification

    monkeypatch.setenv("AITESTER_EMIT_DIR", str(tmp_path))
    walk.reset_llm_cache()

    t = _make()
    t.register_artifact('"cases"', "field=id type=string required=true")
    t.set_artifact_options('"cases"', "dedupe=id")

    # Two rules emit the SAME id — dedupe should drop the duplicate at write time
    t.define_rule('"first"')
    t.when_open('"http://x/"')
    t.extract_fields("field=id extractor=text locator=#case-id-1")
    t.emit_to_artifact('"cases"')

    t.define_rule('"second"')
    t.declare_parents('"first"')
    t.extract_fields("field=id extractor=text locator=#case-id-2")
    t.emit_to_artifact('"cases"')

    fake = FakeBrowser(
        current_url="http://x/",
        text_for={"#case-id-1": "MAIN-007", "#case-id-2": "MAIN-007"},
    )
    monkeypatch.setattr(walk, "BrowserAdapter", lambda: fake)

    walk_verification(t.get_verification())

    jsonl_path = tmp_path / "cases.jsonl"
    records = [json.loads(line) for line in jsonl_path.read_text().splitlines() if line.strip()]
    assert len(records) == 1, f"dedupe failed: got {records}"
    assert records[0]["data"]["id"] == "MAIN-007"


def test_post_extract_hook_renames_field(monkeypatch, tmp_path):
    """Hook lifecycle works end-to-end — rename transform applied between
    extract and emit."""
    from aitester_bdd.engine import walk
    from aitester_bdd.engine.walk import walk_verification

    monkeypatch.setenv("AITESTER_EMIT_DIR", str(tmp_path))
    walk.reset_llm_cache()

    t = _make()
    t.register_artifact('"cases"', "field=id type=string required=true")
    # Register a normalize hook: rename + lowercase + default
    t.register_hook('"normalize"', '"post_extract"',
                    "rename=case_id:id",
                    "lowercase=status",
                    "default=owner:unknown")

    t.define_rule('"capture"')
    t.when_open('"http://x/"')
    t.extract_fields(
        "field=case_id extractor=text  locator=#cid",
        "field=status  extractor=text  locator=.st",
    )
    t.emit_to_artifact('"cases"')

    fake = FakeBrowser(
        current_url="http://x/",
        text_for={"#cid": "MAIN-42", ".st": "APPROVED"},
    )
    monkeypatch.setattr(walk, "BrowserAdapter", lambda: fake)

    walk_verification(t.get_verification())

    jsonl_path = tmp_path / "cases.jsonl"
    records = [json.loads(line) for line in jsonl_path.read_text().splitlines() if line.strip()]
    assert len(records) == 1
    d = records[0]["data"]
    assert d["id"] == "MAIN-42"           # rename: case_id -> id
    assert "case_id" not in d
    assert d["status"] == "approved"       # lowercased
    assert d["owner"] == "unknown"         # default filled


def test_merge_into_artifact_combines_records_by_key(monkeypatch, tmp_path):
    from aitester_bdd.engine import walk
    from aitester_bdd.engine.walk import walk_verification

    monkeypatch.setenv("AITESTER_EMIT_DIR", str(tmp_path))
    walk.reset_llm_cache()

    t = _make()
    t.register_artifact('"cases"', "field=id type=string", "field=title type=string", "field=owner type=string")
    # Rule A: emit id + title
    t.define_rule('"basics"')
    t.when_open('"http://x/"')
    t.extract_fields(
        "field=id    extractor=text locator=#cid",
        "field=title extractor=text locator=.title",
    )
    t.emit_to_artifact('"cases"')
    # Rule B: merge in id + owner, by id key
    t.define_rule('"owner"')
    t.declare_parents('"basics"')
    t.extract_fields(
        "field=id    extractor=text locator=#cid",
        "field=owner extractor=text locator=.owner",
    )
    t.merge_into_artifact('"cases"', '"id"')

    fake = FakeBrowser(
        current_url="http://x/",
        text_for={
            "#cid": "MAIN-99",
            ".title": "Big Bug",
            ".owner": "alice",
        },
    )
    monkeypatch.setattr(walk, "BrowserAdapter", lambda: fake)

    walk_verification(t.get_verification())

    jsonl_path = tmp_path / "cases.jsonl"
    records = [json.loads(line) for line in jsonl_path.read_text().splitlines() if line.strip()]
    assert len(records) == 1, f"merge should leave one record; got {records}"
    d = records[0]["data"]
    assert d["id"] == "MAIN-99"
    assert d["title"] == "Big Bug"
    assert d["owner"] == "alice"
