# Quickstart — `wiki_smoke.robot`

A real passing example: authored end-to-end from the story below in **~70 seconds** by `aitester author`.

## Story (the input)

> Open en.wikipedia.org. Verify the main page renders: it has the Wikipedia logo (link to /wiki/Main_Page), a 'Search Wikipedia' input, and at least 5 'In the news' / 'On this day' / 'Did you know' sidebar links. Then use the search input: type 'Behavior-driven development' and submit. Verify the resulting article page shows an h1 'Behavior-driven development', a paragraph containing the word 'BDD', and a 'References' section heading further down the page.

## Reproduce the authoring

```bash
# Requires: agent-browser CLI on PATH, an LLM endpoint configured via
# AITESTER_LLM_MODEL / OPENAI_BASE_URL / OPENAI_API_KEY.
aitester author \
  --story "$(cat story.txt)" \
  --base-url https://en.wikipedia.org \
  --out wiki_smoke.robot \
  --debug                                    # optional: stream agent steps
```

The authored suite (`wiki_smoke.robot`) is the file committed alongside this README.

## Run the authored suite

```bash
aitester run wiki_smoke.robot
```

Output:
```
Wiki Smoke :: Wikipedia smoke: main page renders + search for 'Beh... | PASS |
1 test, 1 passed, 0 failed
```

## What the suite looks like

Two rules with a parent/child relationship, position-determined state checks,
no explicit waits, no `set retry`. The framework's auto-wait covers async
content — `set rule timeout` would extend the per-check timeout if a SUT
were known-slow (not needed here).
