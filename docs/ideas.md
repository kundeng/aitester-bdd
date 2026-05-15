# Ideas (parking lot)

Items here are not committed work — just things we've thought about.
File a real GitHub issue if/when one of these graduates to being scheduled.

---

## Inception: self-hosted explore + execute keywords

Make the framework self-testable. Add two **private** Robot keywords next to the
existing `aitester author` / `aitester run` CLI:

```robot
When I explore "${story}" against "${url}" into "${suite_path}"
When I execute "${suite_path}"
Then verdict last passed
```

- Thin wrappers over `authoring.agent_loop.author_with_agent` and
  `engine.walk.run_suite` (the same code paths the CLI uses today).
- The skill (SKILL.md) is already embeddable via `aitester_bdd.skill.load_skill()`;
  meta-tests can reference the loader without duplicating content.
- Ship a small suite under `aitester_bdd/meta/` (e.g. `meta/explore.robot`,
  `meta/execute.robot`) that exercises both keywords against a fixed simple
  target (example.com or a local httpbin).
- CLI commands collapse to convenience over the keywords.

**Cost / friction:**

- `I explore` invokes a real LLM — ~30-60s + LLM cost per run. Best as a
  nightly soak gate, not every-commit CI.
- Meta-tests must assert on *structure* (suite parses, executes, passes /
  fails as expected), not exact selectors — the LLM will produce slightly
  different suites across runs.
- ~250 LOC + 2-3 fixture stories + 1 small CLI subcommand for `aitester meta`.

**Why park it:** worth doing once aitester-bdd is stable enough to be
its own customer. Not blocking the migration of the prismi3 tests off the
vendored `src/aitester/`.
