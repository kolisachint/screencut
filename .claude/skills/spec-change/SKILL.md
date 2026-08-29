---
name: spec-change
description: Change screencut's EditSpec or RenderProfile safely — field origins, validators, the spec_version migration, regenerating schemas and TypeScript, and the golden set. Use when adding, removing or reshaping a field in spec/.
---

# Changing the spec

The spec is the system (principle 1). One definition does four jobs: it constrains
what an LLM prompt asks for, validates what comes back, generates the review UI's
types, and defines what the learner diffs. A change touches all four.

## Every new field needs

- **`spec_field(produced_by=...)`.** Not `Field`. `tests/test_origin.py` fails the
  build without it. Pick the stage from §7.1's table; golden replay checks a field
  strictly or distributionally depending on it (§11.1). A value object nested
  under a field inherits that field's origin — do not annotate geometry primitives.
- **Normalized or source-time units.** No pixels, no frame numbers, no
  output-relative times. If a value seems to need output time, re-read §4.5: it
  almost certainly needs no anchor at all, or a source anchor the compiler maps.
- **A validator, if the field can be wrong.** Prefer making a bad value
  unrepresentable over detecting it later. This is half of risk R5's mitigation:
  most wrong answers from a model should be *invalid* answers.

## If the change is not backward compatible

1. Bump `CURRENT_SPEC_VERSION` in `spec/version.py`.
2. Add a migration in `spec/migrations.py` with `@migration(n, n+1)`. One version
   step per function, so adding the next one never touches this one.
3. `tests/data/spec_v1.json` is an **artifact, not a fixture**. Never regenerate
   it — it is the only thing proving old documents still load. The test asserting
   it is still v1 exists to stop exactly that.

## Then, always

```sh
make generated   # rewrites schemas/*.json and schemas/screencut.ts
make check       # tests, drift check, TypeScript typecheck
```

The drift check compares against `HEAD`, so regenerated files must be committed
with the change that caused them. `tests/test_generated.py` will fail first and
tell you.

## Things that look like spec changes and are not

- **Profile tunables** are `Stage.CONFIG` and live on `RenderProfile`. To retune
  one without touching code, use `prefs/constraints.yaml` — sparse overrides,
  deep-merged, re-validated after merging.
- **Anything the compiler derives** — a crop rectangle, an output timestamp, a
  wrapped caption line — belongs in `compile/`, not in the spec. If the compiler
  can compute it, the spec should not carry it.
