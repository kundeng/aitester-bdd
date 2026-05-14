"""AITester keyword library — verify it builds the rule tree correctly."""
from __future__ import annotations


def test_lifecycle_and_rule_construction():
    from aitester_bdd.AITester import AITester

    t = AITester()
    t.start_verification('"smoke"')
    t.start_scenario_at('"flow"', '"http://localhost:5173"')
    t.define_rule('"login"')
    t.when_open('"http://localhost:5173/login"')
    t.when_type('"admin"', '"input[name=user]"')
    t.and_selector_exists('"button"')
    t.then_has_text('"h1"', '"Welcome"')

    v = t.get_verification()
    assert v.name == "smoke"
    assert len(v.scenarios) == 1
    sc = v.scenarios[0]
    assert sc.name == "flow"
    assert sc.entry_url == "http://localhost:5173"
    assert "login" in sc.rules
    rule = sc.rules["login"]
    # 4 items: open, type, selector_exists, has_text
    kinds = [getattr(it, "kind", None) for it in rule.items]
    assert "open" in kinds
    assert "type" in kinds
    assert "selector_exists" in kinds
    assert "has_text" in kinds


def test_parent_declaration():
    from aitester_bdd.AITester import AITester

    t = AITester()
    t.start_verification('"v"')
    t.start_scenario_at('"s"', '"http://x"')
    t.define_rule('"a"')
    t.define_rule('"b"')
    t.declare_parents('"a"')

    v = t.get_verification()
    assert v.scenarios[0].rules["b"].parents == ["a"]


def test_position_does_not_change_keyword_emission():
    """State checks emit the same StateCheck regardless of grammar word."""
    from aitester_bdd.AITester import AITester, StateCheck

    t = AITester()
    t.start_verification('"v"')
    t.start_scenario_at('"s"', '"http://x"')
    t.define_rule('"r"')
    # Different grammar words, same kind
    t.given_url_contains('"/foo"')
    t.and_url_contains('"/foo"')
    t.then_url_contains('"/foo"')
    rule = t.get_verification().scenarios[0].rules["r"]
    assert len(rule.items) == 3
    for it in rule.items:
        assert isinstance(it, StateCheck)
        assert it.kind == "url_contains"
        assert it.expected == "/foo"
