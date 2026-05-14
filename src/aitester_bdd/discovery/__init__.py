"""Discovery — two modes for producing draft .robot skeletons.

Mode A (black-box):
    Input: base_url + story (no source).
    Method: open URL via agent-browser, snapshot, identify clickable
            entry points, propose a draft suite with TODO selectors.

Mode B (white-box):
    Input: base_url + story + source_root.
    Method: read framework-specific source (FastAPI routes, React TSX
            components, Zustand stores), enumerate entry points, then
            snapshot for selector grounding.
"""
from aitester_bdd.discovery.blackbox import discover_blackbox  # noqa: F401
