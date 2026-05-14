# aitester-bdd

**LLM-driven BDD test authoring for Robot Framework.** Give it an intention and a live web app; it explores, drafts a `.robot` suite with snapshot-grounded selectors, validates via `robot --dryrun`, and refines on failure.

## What it is

A Robot Framework library that fills a gap in the RF ecosystem: turning a plain-English story into a deterministic, executable `.robot` test suite. Built on top of `robotframework-browser` (Playwright) and `robotframework-aiagent` (LLM client) — adds the discovery, authoring, and rule-DAG layer.

## What's novel

| Existing in RF ecosystem | What aitester-bdd adds |
|---|---|
| `robotframework-browser` — drive a browser | — |
| `robotframework-aiagent` — call an LLM from a test | — |
| `robotframework-ai` / `robotframework-roboai` — AI helpers inside tests | — |
| _no canonical package_ | **Intention → `.robot` suite generation with snapshot-grounded selectors** |
| _no canonical package_ | **Rule DAG with parent-child composition (BDD with embedded assertions)** |
| _no canonical package_ | **Skill file as LLM grammar — constrains output to valid suites** |
| _no canonical package_ | **Black-box + white-box discovery modes** |

## Status

**Alpha — v0.1.** Borrowed architecture (Plan-then-Execute rule DAG, observation gates, aspect registry) from the [WISE RPA BDD](https://github.com/kundeng/wise-rpa-bdd) skill, retargeted from web extraction to web verification.

## Quick start

```bash
pip install aitester-bdd
aitester author --story "log in as admin and approve case MAIN-0001" --base-url http://localhost:5173
```

Output: `suite.robot` you can run via `robot suite.robot`.

## Architecture (one paragraph)

The LLM is the author, not the runtime. At authoring time, the agent reads a SKILL.md grammar, takes accessibility snapshots of the live app, identifies elements visible in the snapshot, and emits a `.robot` file using only shipped keywords with selectors grounded in observed DOM. At runtime, plain Robot Framework executes the suite — no LLM in the loop, deterministic, fast, reproducible. Failures are diagnosed by the LLM with full RF log access; successful runs are the codified test artifact.

## License

MIT
