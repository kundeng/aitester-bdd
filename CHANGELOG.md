# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.0] - 2026-05-20

### Changed

- **Default runtime backend: `playwright`** (was `agent-browser`). Consistent engine for pinned and fluid tests. Reliable `get_text`, native Playwright waits, in-process speed. Requires `aitester init-browser` on first run.
- **Guard timeout: 10s** (was 200ms). Matches upstream WISE RPA BDD's `wait_for_elements_state("attached", "10s")`. Guards now wait for SPA hydration.
- **Text observations poll** — `has_text`, `contains`, `matches`, `not_contains` now poll until match or timeout (was single read). Catches post-navigation text changes.

### Added

- **Playwright explore tools** — when running inside RF, `I explore` uses typed Python tools (`browser_click`, `browser_get_text`, `browser_snapshot`, etc.) that call the same RF Browser instance the walker uses. No subprocess, no session handoff. Mixed suites (pinned login → fluid explore) share one browser session.
- **Auto-import Browser library** — suites don't need `Library Browser` when using the `playwright` backend. The walker auto-imports it via `BuiltIn().import_library("Browser")`.
- **JS-level wait fallback** — `wait_for_elements_state` falls back to in-page JS polling for SPA apps where hash routing creates elements after initial page load.
- **`fill_secret` / `type_secret` handling** — RF Browser API compatibility for secret password inputs.

### Fixed

- `has_text` observation after navigation returned stale text from the previous view (single read, no poll).
- Guard timeout too short (200ms) for SPA route transitions — elements appeared after guards timed out.
- `type secret` keyword failed on Playwright backend (`fill_text` doesn't accept `secret` kwarg).

## [0.2.0] - 2026-05-16

### Added

- **`I explore` keyword** — fluid LLM-driven test execution within the rule DAG. The walker hands its browser session to the agent loop at topo-sort time; no separate browser lifecycle.
- **`I explore and author` keyword** — same as explore, but also writes a pinned `.robot` suite from the journey.
- **`I ask LLM` action + `llm_response_contains` / `llm_response_semantic` state checks** — interact with LLMs as a plan-phase action (deferred execution, not immediate).
- **WalkContext dataclass** — centralizes runtime configuration (headed, step_delay_ms, run_timeout_s, disabled_aspects). Replaces scattered `os.environ.get()` calls.
- **`step_delay` aspect** — step delay is now a proper AOP aspect (fires via `after_action` hook), not an inline `time.sleep`.
- **`--headed` and `--step-delay` CLI flags** on `aitester run` for visual observation.
- **Three runtime backends** — `agent-browser` (default, zero-install), `playwright` (in-process speed), `nodriver` (bot-detection-resistant). Declared via `${ENGINE}` variable in the suite.
- **Session isolation** — each adapter instance gets its own session UUID; cookies never leak between test runs or authoring sessions.
- **Scenario isolation** — `clear_state()` between scenarios so each Robot test case starts clean.
- **`state_setup` configuration** — suite-level auth/consent actions that run once before any scenario.
- **Quality gates** — `min_records`, `filled_pct`, `max_failed_pct` assertions on captured artifacts.
- **Expansion (TIER 2)** — parametric capture over elements or Cartesian combinations.
- **`visual_semantic` state check** — multimodal screenshot-to-LLM judge.
- **Shell action + assertions** — `When I run shell`, `Then last shell exit`, stdout/stderr checks.
- **GitHub Actions CI** — lint + test on Python 3.11/3.12/3.13, build verification, docs deployment.
- **`py.typed` marker** — enables downstream type checking.

### Changed

- **Headed mode now works for all backends** — previously the walker always passed `headless=True` regardless of env var. Now `WalkContext.headed` flows correctly to `BrowserAdapter.new_session()`.
- **Aspect disabling centralized** — `AITESTER_DISABLE_ASPECTS` is read once at `WalkContext` construction, not per-registry-build.

### Fixed

- Duplicate `SKILL.md` in wheel (force-include removed; `packages` directive already includes it).
- Test suite: `declare_parents` → `and_declare_parents`, `set_rule_timeout` → `and_set_rule_timeout` renames propagated to all tests.
- 138 lint errors resolved (unused imports, `Optional` → `X | None`, import ordering, undefined forward refs).

## [0.1.0] - 2026-04-01

### Added

- Initial release: keyword library, walker, BrowserAdapter, AspectRegistry, trajectory/instrument/diagnose aspects.
- Authoring agent loop (DeepAgents + LangGraph) with `aitester author` CLI.
- `aitester run` CLI for executing authored suites.
- Rule DAG with parent-child composition, guards, retry-redo, interrupt dismissal.
- SKILL.md grammar reference for the authoring agent.
- Wikipedia quickstart example.
