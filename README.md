# screencut

A pipeline that turns a raw screen recording into publish-ready video: narrated,
captioned, auto-zoomed, reframed for multiple aspect ratios, with a review loop
that captures corrections as structured diffs.

**AI-edited, not AI-generated.** Every frame is captured. The model's job is the
job of an editor — decide what to cut, where to look, what to emphasize, what to
call the thing. There is no image model, no video model and no B-roll generator
anywhere in this design, and adding one would replace it rather than extend it.

- [`docs/architecture.md`](docs/architecture.md) — the design and the reasoning.
- [`docs/implementation-phases.md`](docs/implementation-phases.md) — the order of
  work and what "done" means at each step.

## Status

**Phase 1 (spec and fixtures) is built.** The data model everything else is
written against, its invariants, its versioning, its generated schemas and types,
and a synthetic fixture to write the compiler against.

Phase 0 is an environment spike on the target machine (Apple Silicon) and has not
been run; it is the phase that answers whether cursor events are extractable
(risk R1) and whether F5-TTS is viable locally. Nothing in phase 1 depends on
those answers, which is why it could go first.

Next is phase 2 — `plan_focus`, the FFmpeg compiler and the time projection.

## Quickstart

```sh
make install     # the package and its dev dependencies
make fixture     # a synthetic job in data/fixtures/demo01 (needs ffmpeg)
make check       # tests, generated-artifact drift, TypeScript typecheck
```

```python
from spec import load_spec_file, choose_threshold, SHORTS_9X16, DEMO_16X9

spec = load_spec_file("data/fixtures/demo01/spec.json")
for profile in (SHORTS_9X16, DEMO_16X9):
    tier, seconds = choose_threshold(spec.edit, profile.duration_budget)
    print(f"{profile.name}: keeps {tier.value} and above, {seconds:.1f}s")
```

```
shorts_9x16: keeps supporting and above, 12.4s
demo_16x9: keeps optional and above, 16.7s
```

One `EditSpec`, two `RenderProfile`s, two different edits — with no second cut
list anywhere. That is [§4.4.1](docs/architecture.md) working.

## What phase 1 built

| Path | What is in it |
|---|---|
| `spec/` | Pydantic models, field-origin metadata, migrations, JSON Schema and TypeScript emit |
| `ingest/` | The recorder-event adapter and the synthetic fixture generator |
| `schemas/` | Generated — four JSON Schemas and the TypeScript types. Regenerate with `make generated` |
| `tests/` | The invariants, the round-trip, the migration, and the generated-artifact drift check |

Four things in the spec are load-bearing and easy to miss:

- **Everything is in source coordinates.** Space is normalized `0.0–1.0`, time is
  seconds from source start, and there is no second time base. Cuts are projected
  at compile, never baked into the spec (§4.5) — which is what makes a cut
  correction cost a re-render instead of a re-plan.
- **`removals` and `segments` partition the source exactly.** Gapless,
  non-overlapping, ending at the source duration. Enforced by the schema, so an
  impossible edit is unrepresentable rather than merely detectable.
- **Segments are tiered, not cut.** How good a bit is gets decided once; how much
  fits is arithmetic against each profile's `duration_budget` (§4.4.1).
- **Every field records the stage that produced it**, and whether that stage is
  deterministic or model-backed. Golden replay (§11.1) checks the two halves
  differently, and backfilling that metadata later means backfilling it across a
  schema that has already drifted.

## Requirements

Python 3.11+, `ffmpeg` for fixture media, Node only for `make typecheck`.
