"""Topo-sort + parent semantics."""
from __future__ import annotations

import pytest


def test_topo_sort_orders_parents_first():
    from aitester_bdd.AITester import Rule
    from aitester_bdd.engine.walk import _topo_sort

    rules = {
        "child": Rule(name="child", parents=["parent"]),
        "parent": Rule(name="parent"),
        "grandchild": Rule(name="grandchild", parents=["child"]),
    }
    order = _topo_sort(rules)
    assert order.index("parent") < order.index("child") < order.index("grandchild")


def test_topo_sort_detects_cycle():
    from aitester_bdd.AITester import Rule
    from aitester_bdd.engine.walk import _topo_sort

    rules = {
        "a": Rule(name="a", parents=["b"]),
        "b": Rule(name="b", parents=["a"]),
    }
    with pytest.raises(ValueError, match="cyclic"):
        _topo_sort(rules)


def test_topo_sort_unknown_parent_does_not_crash():
    from aitester_bdd.AITester import Rule
    from aitester_bdd.engine.walk import _topo_sort

    rules = {"a": Rule(name="a", parents=["does_not_exist"])}
    order = _topo_sort(rules)
    assert "a" in order
    assert "does_not_exist" in order
