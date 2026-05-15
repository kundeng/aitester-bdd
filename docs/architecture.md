# Architecture

## Three layers

```
┌──────────────────────────────────────────────────────────────────┐
│  Authoring (LLM-in-the-loop, one-shot per suite)                 │
│  ─────────────────────────────────────────────────────────────   │
│  DeepAgents loop on top of LangGraph:                            │
│    - SKILL.md as system prompt                                   │
│    - TodoListMiddleware (planning) + LocalShellBackend (execute) │
│    - Tool surface: `execute` (bash) + write_robot_suite +        │
│      report_bug. No per-operation Python wrappers — the agent    │
│      shells out to `agent-browser <subcommand> --json`,          │
│      batching with `&&`.                                         │
│  Retry harness wraps the inner loop (max_attempts, default 2)    │
│  so a crash / recursion-limit retries with feedback.             │
└──────────────────────────────────────────────────────────────────┘
                              │
                              │ produces
                              ▼
┌──────────────────────────────────────────────────────────────────┐
│  Runtime (deterministic, no LLM)                                 │
│  ─────────────────────────────────────────────────────────────   │
│  • Robot Framework parses + walks the .robot file                │
│  • aitester-bdd keyword library: rule DAG, observation gates,    │
│    position-determined state checks, retry-with-redo guards      │
│  • One of three browser backends (declared via ${ENGINE}):       │
│      - agent-browser (default) — CLI subprocess, zero install    │
│      - playwright (in-process) — install rfbrowser + browsers    │
│      - nodriver (raw CDP) — bot-detection-resistant              │
└──────────────────────────────────────────────────────────────────┘
                              │
                              │ emits
                              ▼
┌──────────────────────────────────────────────────────────────────┐
│  Diagnostics                                                     │
│  ─────────────────────────────────────────────────────────────   │
│  • RF log.html / report.html / output.xml                        │
│  • Verdict report (pass/fail per rule, evidence on failure)      │
│  • Optional LLM post-mortem on failure (read RF log, suggest fix)│
└──────────────────────────────────────────────────────────────────┘
```

## Heritage from WISE RPA BDD

The engine borrows the Plan-then-Execute rule DAG model from the WISE RPA BDD skill — Robot Framework keywords build an in-memory plan during test case definition, then a single Suite Teardown step (`Then I finalize`) walks the plan against a live browser. This separates the **spec** (the rule tree) from the **execution** (the browser walk), which is what makes:

- LLM-authored output deterministic (the LLM commits to a plan up front)
- Observation gates clean (position-determined: before-action = guard, after-action = sync gate)
- Parent-child rule composition meaningful (rules can declare prerequisite rules; the walker handles ordering)
- Aspects (timing, screenshots, slow-mo, checkpoint) cross-cutting (modify behavior without touching rules)

## What's different from WISE

WISE's vocabulary is for **extraction**: artifacts, emits, merges, quality gates on extracted records. aitester-bdd's vocabulary is for **verification**: assertions on state, no data collection. The keyword library is a from-scratch design; only the underlying engine primitives carry over.

## Two discovery modes

### Black-box (Mode A)

Input: a story + base_url. No source access. Agent crawls via accessibility snapshots, identifies entry points by looking.

Use: validating customer-facing behavior of a deployed app you don't own; smoke-testing in foreign environments.

Limitation: invisible code paths (admin-only routes, conditional features) stay invisible.

### White-box (Mode B)

Input: a story + base_url + source_root. Reads framework-specific source (FastAPI routes, React TSX components, Zustand stores). Cross-references against live snapshots to ground selectors.

Use: testing apps you own with full source access. More complete coverage.

Mode B internally falls back to Mode A for selector grounding — TSX source doesn't always tell you the rendered class (shadcn wraps, Tailwind transforms), so a live snapshot is still required for the actual selector.

## Skill as grammar

The `SKILL.md` shipped inside the wheel is the LLM's grammar at authoring time. It documents:

- The shipped keyword vocabulary (Given/When/Then with concrete primitives)
- The rule DAG shape (parents, guards, observations, actions, assertions)
- The non-negotiables (no `When I wait`, no invented site-specific verbs, all selectors must come from a live snapshot)
- The patterns library (auth flow, observation gates, dismiss scoping)
- The agent contract (what the LLM is and isn't authorized to do)

Without the skill loaded, the LLM emits prose. With it, the LLM emits valid `.robot` files that the engine can execute.

## Where the LLM is and isn't in the loop

| Phase | LLM? | Why |
|---|---|---|
| Discovery | Yes | Reads source, snapshots app, proposes structure |
| Authoring | Yes | Composes the .robot file from story + snapshot |
| Dryrun | No | Plain `robot --dryrun` — fast, no tokens |
| Execution | No | Plain Robot Framework — deterministic, no tokens |
| Diagnostics on failure | Optional | LLM can read RF log.html and suggest a refine, but the failure detection itself is non-LLM |
| Refinement (loop) | Yes | Failed dryrun or execution feeds back into authoring |

Token cost is bounded: one author call + N refine calls per scenario. Production CI runs `.robot` files plain — no LLM cost on PR gates.
