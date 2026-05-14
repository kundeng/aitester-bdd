"""Integration tests for TIER 2 expansion:

  When I expand over elements ...
  When I expand over elements ... with order ...
  When I expand over combinations

Each one captures one record per iteration into the rule's artifacts.
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


class _ScopeAwareFakeBrowser(FakeBrowser):
    """FakeBrowser that understands `${scope} >> nth=${i}` and
    `${scope} >> ${child}` selectors for testing expansion."""

    def __init__(self, **kw):
        # Strip our own kwargs before handing to the dataclass parent.
        self.per_element_text: dict = kw.pop("per_element_text", {})
        self.per_element_attr: dict = kw.pop("per_element_attr", {})
        super().__init__(**kw)

    def get_text(self, css: str) -> str:
        # Resolve scoped selectors of form "${scope} >> nth=${i} >> ${child}"
        if " >> nth=" in css:
            # Parse: "scope >> nth=N >> child"
            parts = css.split(" >> ")
            scope = parts[0]
            nth_i = int(parts[1].split("=")[1])
            child = " >> ".join(parts[2:]) if len(parts) > 2 else "."
            return self.per_element_text.get((scope, nth_i, child), "")
        return super().get_text(css)

    def get_attribute(self, css: str, attr: str) -> str:
        if " >> nth=" in css:
            parts = css.split(" >> ")
            scope = parts[0]
            nth_i = int(parts[1].split("=")[1])
            child = " >> ".join(parts[2:]) if len(parts) > 2 else "."
            return self.per_element_attr.get((scope, nth_i, child, attr), "")
        return super().get_attribute(css, attr)


def test_expand_over_elements_emits_one_record_per_element(monkeypatch, tmp_path):
    """3 case rows → 3 records in the artifact, each with its own id/title."""
    from aitester_bdd.engine import walk
    from aitester_bdd.engine.walk import walk_verification

    monkeypatch.setenv("AITESTER_EMIT_DIR", str(tmp_path))
    walk.reset_llm_cache()

    t = _make()
    t.register_artifact('"cases"',
                        "field=id    type=string required=true",
                        "field=title type=string required=true")
    t.define_rule('"list_cases"')
    t.when_open('"http://x/cases"')
    t.expand_over_elements('"[data-row]"', "limit=10")
    t.extract_fields(
        "field=id    extractor=attr locator=.    attr=data-id",
        "field=title extractor=text locator=.title",
    )
    t.emit_to_artifact('"cases"')

    fake = _ScopeAwareFakeBrowser(
        current_url="http://x/cases",
        count_for={"[data-row]": 3},
        per_element_text={
            ("[data-row]", 0, ".title"): "First Case",
            ("[data-row]", 1, ".title"): "Second Case",
            ("[data-row]", 2, ".title"): "Third Case",
        },
        per_element_attr={
            ("[data-row]", 0, ".", "data-id"): "C-001",
            ("[data-row]", 1, ".", "data-id"): "C-002",
            ("[data-row]", 2, ".", "data-id"): "C-003",
        },
    )
    # wait_for_elements_state always passes for this fake
    fake.selector_present[ "[data-row]"] = True
    monkeypatch.setattr(walk, "BrowserAdapter", lambda: fake)

    verdict = walk_verification(t.get_verification())
    assert verdict.passed, verdict.format_failure()

    jsonl_path = tmp_path / "cases.jsonl"
    assert jsonl_path.exists()
    records = [json.loads(line) for line in jsonl_path.read_text().splitlines() if line.strip()]
    assert len(records) == 3
    ids = sorted(r["data"]["id"] for r in records)
    titles = sorted(r["data"]["title"] for r in records)
    assert ids == ["C-001", "C-002", "C-003"]
    assert titles == ["First Case", "Second Case", "Third Case"]
    # _iter stamps for differential debugging
    iters = sorted(r["data"]["_iter"] for r in records)
    assert iters == [0, 1, 2]


def test_expand_over_elements_respects_limit(monkeypatch, tmp_path):
    """count=10 but limit=3 → only 3 records emitted."""
    from aitester_bdd.engine import walk
    from aitester_bdd.engine.walk import walk_verification

    monkeypatch.setenv("AITESTER_EMIT_DIR", str(tmp_path))
    walk.reset_llm_cache()

    t = _make()
    t.register_artifact('"rows"', "field=label type=string")
    t.define_rule('"capture"')
    t.when_open('"http://x/"')
    t.expand_over_elements('".row"', "limit=3")
    t.extract_fields("field=label extractor=text locator=.")
    t.emit_to_artifact('"rows"')

    fake = _ScopeAwareFakeBrowser(
        current_url="http://x/",
        count_for={".row": 10},
        per_element_text={
            (".row", i, "."): f"r{i}" for i in range(10)
        },
    )
    fake.selector_present[".row"] = True
    monkeypatch.setattr(walk, "BrowserAdapter", lambda: fake)

    walk_verification(t.get_verification())

    records = [json.loads(line) for line in (tmp_path / "rows.jsonl").read_text().splitlines() if line.strip()]
    assert len(records) == 3
    labels = sorted(r["data"]["label"] for r in records)
    assert labels == ["r0", "r1", "r2"]


def test_expand_over_elements_exclude_if_skips_matching(monkeypatch, tmp_path):
    """exclude_if=.system-row skips rows that contain a .system-row child."""
    from aitester_bdd.engine import walk
    from aitester_bdd.engine.walk import walk_verification

    monkeypatch.setenv("AITESTER_EMIT_DIR", str(tmp_path))
    walk.reset_llm_cache()

    t = _make()
    t.register_artifact('"rows"', "field=id type=string")
    t.define_rule('"capture"')
    t.when_open('"http://x/"')
    t.expand_over_elements('".row"', "limit=10", "exclude_if=.system-row")
    t.extract_fields("field=id extractor=attr locator=. attr=data-id")
    t.emit_to_artifact('"rows"')

    # 4 rows total; rows 1 and 3 are system rows (have .system-row child)
    class IndexedExcludeFake(_ScopeAwareFakeBrowser):
        def get_count(self, css: str) -> int:
            # ".row >> nth=N >> .system-row" → 1 for N in {1, 3}, else 0
            if " >> nth=" in css and css.endswith(" >> .system-row"):
                nth_i = int(css.split(" >> nth=")[1].split(" >> ")[0])
                return 1 if nth_i in (1, 3) else 0
            return super().get_count(css)

    fake = IndexedExcludeFake(
        current_url="http://x/",
        count_for={".row": 4},
        per_element_attr={
            (".row", i, ".", "data-id"): f"R-{i}" for i in range(4)
        },
    )
    fake.selector_present[".row"] = True
    monkeypatch.setattr(walk, "BrowserAdapter", lambda: fake)

    walk_verification(t.get_verification())

    records = [json.loads(line) for line in (tmp_path / "rows.jsonl").read_text().splitlines() if line.strip()]
    ids = sorted(r["data"]["id"] for r in records)
    # Rows 0 and 2 keep; 1 and 3 excluded
    assert ids == ["R-0", "R-2"], f"got {ids}"


def test_expand_over_combinations_with_explicit_values(monkeypatch, tmp_path):
    """2 axes × 2 values × 2 values = 4 combos → 4 records emitted."""
    from aitester_bdd.engine import walk
    from aitester_bdd.engine.walk import walk_verification

    monkeypatch.setenv("AITESTER_EMIT_DIR", str(tmp_path))
    walk.reset_llm_cache()

    t = _make()
    t.register_artifact('"combos"', "field=role type=string", "field=kind type=string")
    t.define_rule('"sweep"')
    t.when_open('"http://x/"')
    t.expand_over_combinations(
        "action=select", 'control="#role"', "values=admin|viewer",
        "action=select", 'control="#kind"', "values=alpha|beta",
    )
    # Capture the chosen values from the page state — fake will reflect them
    t.extract_fields(
        "field=role extractor=value locator=#role",
        "field=kind extractor=value locator=#kind",
    )
    t.emit_to_artifact('"combos"')

    class ComboFake(_ScopeAwareFakeBrowser):
        def __init__(self, **kw):
            super().__init__(**kw)
            self._role = ""
            self._kind = ""
            self.selects = []

        def select(self, css: str, value: str) -> None:
            self.selects.append((css, value))
            if css == "#role":
                self._role = value
            elif css == "#kind":
                self._kind = value

        def get_value(self, css: str) -> str:
            if css == "#role":
                return self._role
            if css == "#kind":
                return self._kind
            return ""

    fake = ComboFake(current_url="http://x/")
    monkeypatch.setattr(walk, "BrowserAdapter", lambda: fake)

    verdict = walk_verification(t.get_verification())
    assert verdict.passed, verdict.format_failure()

    records = [json.loads(line) for line in (tmp_path / "combos.jsonl").read_text().splitlines() if line.strip()]
    assert len(records) == 4
    pairs = sorted((r["data"]["role"], r["data"]["kind"]) for r in records)
    assert pairs == [
        ("admin", "alpha"), ("admin", "beta"),
        ("viewer", "alpha"), ("viewer", "beta"),
    ]
    # Combo tags are stamped per-record
    for r in records:
        assert r["data"].get("_combo_#role") in ("admin", "viewer")
        assert r["data"].get("_combo_#kind") in ("alpha", "beta")


def test_expand_over_combinations_auto_discovers_select_options(monkeypatch, tmp_path):
    """values=auto reads <option> values via JS eval."""
    from aitester_bdd.engine import walk
    from aitester_bdd.engine.walk import walk_verification

    monkeypatch.setenv("AITESTER_EMIT_DIR", str(tmp_path))
    walk.reset_llm_cache()

    t = _make()
    t.register_artifact('"discovered"', "field=role type=string")
    t.define_rule('"discover_sweep"')
    t.when_open('"http://x/"')
    t.expand_over_combinations(
        "action=select", 'control="#role"', "values=auto", "skip=1",
    )
    t.extract_fields("field=role extractor=value locator=#role")
    t.emit_to_artifact('"discovered"')

    class AutoFake(_ScopeAwareFakeBrowser):
        def __init__(self, **kw):
            super().__init__(**kw)
            self._role = ""

        def evaluate_js(self, script: str):
            # Auto-discover: return the <option> values for #role
            if "#role" in script and "options" in script:
                return ["", "admin", "viewer", "auditor"]
            return None

        def select(self, css: str, value: str) -> None:
            if css == "#role":
                self._role = value

        def get_value(self, css: str) -> str:
            return self._role if css == "#role" else ""

    fake = AutoFake(current_url="http://x/")
    monkeypatch.setattr(walk, "BrowserAdapter", lambda: fake)

    walk_verification(t.get_verification())

    records = [json.loads(line) for line in (tmp_path / "discovered.jsonl").read_text().splitlines() if line.strip()]
    # 4 discovered values, skip=1 drops the first "" (placeholder) → 3 combos
    roles = sorted(r["data"]["role"] for r in records)
    assert roles == ["admin", "auditor", "viewer"], f"got {roles}"
