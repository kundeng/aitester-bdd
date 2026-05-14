# engine/

Plan-then-Execute rule DAG runtime, ported from WISE RPA BDD.

## Heritage

`_wise_source.py` is a verbatim copy of `WiseRpaBDD.py` from the wise-rpa-bdd skill — kept here as the reference until we've broken out the testable subset.

## What carries over from WISE (domain-neutral)

- Rule DAG model: `RuleNode`, `Action`, `StateCheck`, `Expansion`, `FieldSpec`
- Plan-then-Execute walker (`finalize_deployment` style)
- Browser adapter wrapping `robotframework-browser`
- Aspect registry for cross-cutting concerns (timing, screenshots, slow-mo, checkpoint)
- Persistent checkpoint/resume (`PersistentArtifactStore`)
- Position-determined state checks (guard before action, observation gate after)
- Parent-child rule composition

## What gets rewritten for testing

WISE's vocabulary is for **W**eb **I**nformation **S**tructured **E**xtraction — its keywords (artifact registration, emit, merge, quality gates, extract fields) collect data into artifacts. Testing has a different ontology: assertions on state, no data collection.

The keyword library in `../keywords/` is being authored from scratch for verification, NOT renamed from WISE's extraction vocabulary. See the spec in `../skill/SKILL.md` for the new vocabulary design.

## Migration plan

1. **Phase 1:** import only from `_wise_source.py` (keep WISE as-is, treat as a vendored dep)
2. **Phase 2:** extract the engine primitives into typed modules: `rules.py`, `walk.py`, `browser.py`, `aspects.py`, `checkpoint.py`
3. **Phase 3:** delete `_wise_source.py` once nothing imports it
