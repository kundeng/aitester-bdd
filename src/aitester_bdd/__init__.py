"""aitester-bdd — LLM-driven BDD test authoring for Robot Framework.

Public API:
    Agent  — drives discovery + authoring against a target app
    Suite  — a parsed .robot suite (rule DAG)
    Runner — executes a suite via robot
    Verdict — pass/fail result with per-rule diagnostics
"""
from __future__ import annotations

__version__ = "0.1.0"

# Public API surface kept intentionally small.
# Detailed exports added as modules land.
__all__ = ["__version__"]
