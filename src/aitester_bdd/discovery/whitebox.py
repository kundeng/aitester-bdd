"""White-box discovery: read source code, enumerate routes/components.

Currently supports:
- FastAPI route enumeration (Python AST)
- React TSX `data-testid` extraction (regex; full AST is a v0.2 nicety)
- Cross-reference against a live snapshot for selector grounding (delegated
  to the LLM author).

Mode B output is a richer DiscoveryResult than Mode A: it has lists of
backend routes and frontend testid hooks the LLM can use to author from.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("aitester_bdd.discovery.whitebox")


@dataclass
class WhiteBoxResult:
    source_root: Path
    backend_routes: list[dict[str, str]] = field(default_factory=list)
    frontend_testids: list[dict[str, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _scan_fastapi_routes(source_root: Path) -> list[dict[str, str]]:
    """Regex-scan for `@app.<verb>(...)` and `@router.<verb>(...)` patterns.

    Full AST parsing is more robust; regex is good enough for v0.1.
    """
    out: list[dict[str, str]] = []
    pat = re.compile(r'@(?:app|router)\.(get|post|put|patch|delete)\(\s*[fF]?["\']([^"\']+)["\']')
    for py_file in source_root.rglob("*.py"):
        try:
            text = py_file.read_text()
        except Exception:
            continue
        for m in pat.finditer(text):
            verb = m.group(1).upper()
            path = m.group(2)
            out.append({
                "verb": verb,
                "path": path,
                "file": str(py_file.relative_to(source_root)),
            })
    return out


def _scan_tsx_testids(source_root: Path) -> list[dict[str, str]]:
    """Regex-scan TSX/JSX for `data-testid="..."` attributes."""
    out: list[dict[str, str]] = []
    pat = re.compile(r'data-testid=["\']([^"\']+)["\']')
    for tsx_file in source_root.rglob("*.tsx"):
        try:
            text = tsx_file.read_text()
        except Exception:
            continue
        for m in pat.finditer(text):
            out.append({
                "testid": m.group(1),
                "file": str(tsx_file.relative_to(source_root)),
            })
    return out


def discover_whitebox(source_root: str | Path) -> WhiteBoxResult:
    """Static analysis of a source tree. Returns route + testid inventory."""
    root = Path(source_root).resolve()
    result = WhiteBoxResult(source_root=root)
    result.backend_routes = _scan_fastapi_routes(root)
    result.frontend_testids = _scan_tsx_testids(root)
    if not result.backend_routes:
        result.notes.append("No FastAPI routes found — is the source root correct?")
    if not result.frontend_testids:
        result.notes.append("No data-testid attributes found in TSX — selector grounding may rely on aria-label / role fallback.")
    return result
