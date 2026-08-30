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
- [`AGENTS.md`](AGENTS.md) — how to work on the repo: layout, invariants,
  conventions, and the traps this codebase has already sprung.

## Status

**Phases 1 to 5 are built, plus §9.1's deterministic checks.** The data model
everything else is written against; the compiler that turns it into video —
`plan_focus`, the time projection, ASS captions, SVG overlays, an FFmpeg graph
rendering one spec to two aspect ratios at two different lengths; the runner that
makes re-running cheap; and verification that reads the render back and reports
what is wrong with it.

**Phase 5 makes it an editing tool.** `trim` finds dead air by measuring the
audio and fillers by a closed list, and `plan_edit` — the first model stage —
reviews that proposal, adds false starts of its own and ranks what survives into
tiers. When the model cannot be reached the job still renders: `trim`'s cuts, every
segment `essential` (§7.4), which is a real edit rather than the unedited take.

**Two things have not happened yet, and both are about what is installed rather
than what is written.** No *real* recording has been ingested — that needs a
screen, a microphone and Cap on the machine doing the work — so the phase-2 focus
tunables and `trim`'s thresholds have never met real footage. And no model has
run: `plan_edit` is exercised against a scripted stand-in, so whether its cuts are
ones you would have made is still an open question.

**Phase 0 has been run** on the target machine — a base-model M1 MacBook Air,
8GB, fanless. Its four verdicts are in
[`docs/environment-findings.md`](docs/environment-findings.md), with the raw
numbers under `docs/measurements/` and the harness in `tools/`:

- **Cursor events are extractable** (risk R1 closed), and Cap already writes them
  in normalized coordinates — `FocusTrack`'s own space.
- **ASR: `whisper.cpp`,** `large-v3` at 1.95x realtime and 3 984 MB, `medium` as
  the fallback. All three candidates emit the word-level timings §6.2 needs.
- **F5-TTS is not viable locally** — 0.11x realtime at best. Phase 8 starts with
  `RemoteRunner`.
- **The agent CLI round-trips a schema** — 12/12 valid, though 11/12 arrive
  wrapped in a code fence, and latency runs 6–66 s per call.

Next is a real take and a real model call — record one, run it, look at the cuts,
and retune — which is phase 5's stop-and-reassess gate. Then phase 6's remaining
layers and phase 7's review UI.

## Quickstart

```sh
make install     # the package and its dev dependencies
make run         # a synthetic job through the whole pipeline (needs ffmpeg)
make take        # a Cap-format take: generate it, ingest it, render it
                 # (needs whisper-cli and its weights; the others do not)
make broken      # the deliberately bad fixture, so the checks are seen firing
make check       # tests, generated-artifact drift, TypeScript typecheck
```

On the target machine, `make run ENCODER=videotoolbox` uses the hardware encoder.
The default is `software`, which is slower and byte-reproducible — what §11 hashes.

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

## What is built

| Path | What is in it |
|---|---|
| `spec/` | Pydantic models, field-origin metadata, migrations, JSON Schema and TypeScript emit |
| `ingest/` | The recorder-event adapter and the synthetic fixture generator |
| `plan/` | `plan_focus` — the crop path and the zoom regions, both deterministic |
| `compile/` | The time projection, ASS captions, SVG overlay templates, the FFmpeg graph |
| `prefs/` | `constraints.yaml`: the hand-written tier, layered sparsely over the profiles |
| `runner/` | The stage contract, `LocalRunner`, the content-addressed cache, SQLite |
| `verify/` | §9.1's deterministic checks, and the report they produce |
| `golden/` | The deliberately bad fixture, and the findings it must produce |
| `schemas/` | Generated — four JSON Schemas and the TypeScript types. Regenerate with `make generated` |
| `tests/` | The invariants, the round-trip, the migration, the graph, and real renders |

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

Python 3.11+, `ffmpeg` **with libass** and a font installed for anything that
renders, and Node only for `make typecheck`.

On macOS, `brew install ffmpeg` is no longer enough: the formula dropped its
libass dependency, so it has no `ass` filter and cannot burn captions. Use
`brew install ffmpeg-full` and put `/opt/homebrew/opt/ffmpeg-full/bin` first on
`PATH` — it is keg-only. `cairosvg` also needs
`DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib` to find Homebrew's libcairo.
Both are covered in the environment findings.

The target machine is a base-model M1 MacBook Air (8GB, fanless). That is not a
build requirement — phase 1 is portable Python — but it is what the encode
defaults, the ASR backend choice and the "one model resident at a time" rule in
[§16](docs/architecture.md) are written for.
