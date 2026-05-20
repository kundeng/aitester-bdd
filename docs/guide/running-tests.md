# Running Tests

## Basic run

```bash
aitester run suite.robot
```

This:
1. Reads `${ENGINE}` from the suite to pick the browser backend
2. Invokes Robot Framework with the suite
3. The keyword library builds the rule DAG, walks it, and reports pass/fail

## Headed mode (watch it run)

```bash
aitester run suite.robot --headed
```

Opens a visible browser window. Combine with step delay for slow-motion:

```bash
aitester run suite.robot --headed --step-delay 500
```

Pauses 500ms after each action so you can follow along visually.

## Output files

After a run, you'll find in the output directory:

| File | Content |
|------|---------|
| `output.xml` | Robot Framework's standard output |
| `log.html` | Detailed HTML log (clickable keywords) |
| `report.html` | Summary report |
| `walk_log.jsonl` | Every MDP transition (action, state check, timing) |
| `failures.jsonl` | Failure context + AI diagnosis per failed rule |
| `emit.jsonl` | Explicit emit captures (if any) |
| `fail_*.png` | Screenshots captured on failure |

## Choosing a backend

```bash
# Default: playwright (consistent with explore, reliable get_text, native waits)
aitester run suite.robot

# Agent-browser (zero-install, same driver as authoring)
AITESTER_BROWSER=agent-browser aitester run suite.robot

# Nodriver (bot-detection-resistant)
AITESTER_BROWSER=nodriver aitester run suite.robot
```

First run with playwright requires `aitester init-browser` (or `rfbrowser init`) to download browser binaries. The Browser library is auto-imported — suites don't need `Library Browser`.

Or declare in the suite itself:
```robot
*** Variables ***
${ENGINE}    playwright
```

## CI integration

```yaml
# GitHub Actions example
- name: Run E2E tests
  run: |
    pip install aitester-bdd
    npm i -g agent-browser
    aitester run tests/smoke.robot --output-dir results/
- uses: actions/upload-artifact@v4
  if: always()
  with:
    name: test-results
    path: results/
```

No LLM configuration needed for running authored suites. Zero tokens consumed.

## Debugging failures

### 1. Check the verdict output

Robot Framework's console output shows which rules passed/failed:

```
Login Flow :: Verify login and dashboard
    Rule login: PASS (1.2s)
    Rule dashboard_widgets: FAIL (30.1s)
        observation_or_assertion: post-action state check failed
        expected: count >= 5
        observed: 2
```

### 2. Read the AI diagnosis

If the `diagnose` aspect is enabled (default), `failures.jsonl` contains an LLM-written explanation:

```json
{
  "rule": "dashboard_widgets",
  "ai_diagnosis": "The page loaded but only 2 widgets rendered. The API response for /api/widgets returned a 500 error (visible in the network tab), suggesting a backend issue rather than a test problem."
}
```

### 3. Check the trajectory

`walk_log.jsonl` has the full MDP trace — every action, state check, and timing:

```json
{"kind": "before_action", "rule": "dashboard_widgets", "action": "click", "target": ".refresh"}
{"kind": "after_action", "rule": "dashboard_widgets", "action": "click", "dt_ms": 87, "raised": false}
{"kind": "state_check", "rule": "dashboard_widgets", "check": "count_at_least", "ok": false, "expected": "count >= 5", "observed": "2", "position": "observation"}
```

### 4. Re-run headed

```bash
aitester run suite.robot --headed --step-delay 1000
```

Watch exactly what happens. The step delay gives you time to see each action's effect.

### 5. Disable diagnosis (faster iteration)

```bash
AITESTER_DISABLE_ASPECTS=diagnose aitester run suite.robot
```

Skips the LLM call on failure — useful when iterating quickly on a known issue.

## Timeouts

| Scope | Default | Override |
|-------|---------|---------|
| Global run | 300s | `AITESTER_RUN_TIMEOUT=600` |
| Observation (after action) | 30s | `set rule timeout 60000` |
| Guard (before action) | 10s | `set rule timeout 15000` (guards inherit) |

## Running with Robot Framework directly

Since aitester-bdd is a standard RF library, you can also run suites directly:

```bash
robot --outputdir results/ suite.robot
```

This works but skips the `aitester run` backend-selection logic. Set `AITESTER_BROWSER` manually if not using the default.
