# aitester-bdd

**LLM-driven BDD test authoring for Robot Framework.** Give it a story and a live web app; an agent explores the target via the `agent-browser` CLI, then writes a deterministic `.robot` suite with selectors grounded in the actual DOM it observed — or files a bug report when the system is broken in a way that prevents authoring.

## What it is

A Robot Framework library that turns a plain-English intention into a deterministic, executable `.robot` test suite. **Run-time has no LLM in the loop** — the authored suite is plain RF code that runs reproducibly, no tokens consumed on PR gates.

## What's novel

| | aitester-bdd |
|---|---|
| **Intention → `.robot` suite** | An agent loop drives the live target via shell-out to `agent-browser` (Playwright under the hood), writes a Robot Framework suite with selectors grounded in the snapshots it actually took. |
| **Bug-report exit channel** | When the SUT is broken in a way that prevents authoring (missing UI, broken auth flow, untestable terminal state), the agent writes `triage/<story>.md` rather than inventing selectors. |
| **Three pluggable runtime backends** | `agent-browser` (default, zero-install) / `playwright` (in-process speed) / `nodriver` (bot-detection-resistant). Same `.robot` runs on any. |
| **AOP failure aspect** | Each failed rule ships with an AI-written natural-language diagnosis (SUT-vs-test classification) plus a full MDP trajectory in `walk_log.jsonl`. |
| **Rule DAG with parent-child composition** | Ported from WISE RPA BDD. Position-determined state checks (guard vs observation), retry-with-redo, scope inheritance — all expressed via Given/When/Then. |

## Status

**Alpha.** Authoring verified end-to-end on public sites (example.com, en.wikipedia.org, the-internet.herokuapp.com) and on a real internal SPA (login + chat + tool-rendering verification).

## How fast is it?

Authoring is **headless DeepAgents on Claude Opus 4.7** shelling out to the `agent-browser` CLI. Typical wall-time for a single suite:

| Site / scope | Steps | Wall time |
|---|---:|---:|
| example.com smoke (heading + link) | 9 | ~27s |
| en.wikipedia.org search + article check (5 assertions) | 27 | ~70s |
| Real SPA login + chat + multi-rule verification | 50-80 | 2-3 min |

The agent batches multiple `agent-browser` subcommands per shell call (`open && snapshot && get count ...`) so ~1 LLM round-trip handles 2-4 browser ops. Most remaining wall-time is **SUT-bound** (waiting for the app's own LLM to stream a response) — not authoring overhead.

## Quick start

```bash
# 1. Install
pip install aitester-bdd
npm i -g agent-browser

# 2. Point at an LLM endpoint. Defaults assume claude-code-proxy:
export AITESTER_LLM_MODEL=cc/claude-opus-4-7
export OPENAI_BASE_URL=http://localhost:20128/v1
export OPENAI_API_KEY=placeholder

# 3. Author a suite from a story
aitester author \
  --story "Open the homepage, search for 'BDD', verify the article heading and a paragraph containing 'BDD'." \
  --base-url https://en.wikipedia.org \
  --out wiki_smoke.robot

# 4. Run it (no LLM at run time)
aitester run wiki_smoke.robot
```

For visibility into the agent's exploration, add `--debug` to `aitester author`. Every LLM turn and shell call streams to stderr with timestamps.

Output sidecar files at `<output_dir>/`:
- `walk_log.jsonl` — every MDP transition (rule_enter / before_action / after_action / state_check / dismiss / emit / rule_exit)
- `failures.jsonl` — failure context + AI diagnosis for every failed rule
- `emit.jsonl` — explicit `And I emit "..."` captures (intention-driven; only when the story is a diagnostic probe)

## Three runtime backends, one authored suite

`AITESTER_BROWSER=` picks the driver at run-time:

| Backend | Default? | Setup | Best for |
|---------|----------|-------|----------|
| `playwright` | ✓ | `aitester init-browser` once | consistent engine for pinned + fluid tests, reliable `get_text`, native Playwright waits |
| `agent-browser` | | `npm i -g agent-browser` | zero install friction, same CLI as authoring phase |
| `nodriver` | | `pip install aitester-bdd[stealth]` + Edge/Chrome | bot-detected sites (DataDome / Cloudflare BM / etc.) |

Same `.robot` runs on any of the three. The Browser library is auto-imported when the `playwright` backend is selected — suites don't need `Library Browser` in their Settings.

### Mixed suites: pinned + fluid on one browser

When the `playwright` backend is active, `I explore` rules share the **same RF Browser session** as pinned rules. The explore agent gets typed Python tools (`browser_click`, `browser_get_text`, `browser_snapshot`, etc.) that call the live Playwright instance — no subprocess, no session handoff. A pinned login rule can run first, then `I explore` picks up the authenticated session.

## Architecture (one paragraph)

The LLM is the author, not the runtime. At authoring time, a DeepAgents/LangGraph agent reads `SKILL.md` as its system prompt, drives the live target by shelling out to the `agent-browser` CLI (via DeepAgents' `LocalShellBackend.execute` tool), and emits a `.robot` file with selectors grounded in real snapshots — or writes a bug report when the system is broken in a way that prevents authoring. At run time, plain Robot Framework executes the suite via the Playwright backend (default); the walker and `I explore` agent share the same RF Browser instance. Pinned rules (deterministic, no LLM) and fluid explore rules (LLM-driven) operate on the same page, same cookies, same DOM. Failures fire an AOP `diagnose` aspect that hands the LLM the MDP trajectory plus snapshot and asks "why?" — short natural-language diagnoses land on `RuleResult.ai_diagnosis` and `failures.jsonl`. The walker, gotcha-fixes, and AspectRegistry are ported from the WISE RPA BDD skill.

## License

MIT
