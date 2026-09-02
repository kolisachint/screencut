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

**Phases 1 to 8 are built.** The data model everything else is written against;
the compiler that turns it into video — `plan_focus`, the time projection, ASS
captions, SVG overlays, an FFmpeg graph rendering one spec to two aspect ratios at
two different lengths; the runner that makes re-running cheap; and verification
that reads the render back and reports what is wrong with it.

**Phase 5 makes it an editing tool.** `trim` finds dead air by measuring the
audio and fillers by a closed list, and `plan_edit` — the first model stage —
reviews that proposal, adds false starts of its own and ranks what survives into
tiers. When the model cannot be reached the job still renders: `trim`'s cuts, every
segment `essential` (§7.4), which is a real edit rather than the unedited take.

**Phase 6 stops garbage reaching a person.** Every render is measured rather than
asserted — duration, loudness, true peak, caption geometry, overlay occlusion,
crop judder, and whether the cuts add up. Then the rendered audio is transcribed
back and diffed against what the edit says should be there: not against the raw
transcript, which a successful edit is *supposed* to differ from, but against the
source transcript minus the removals and minus the tiers this profile did not
select. Differences on a cut are the edit working; differences anywhere else are
desync, a wrong take, truncated narration, or a cut that landed inside a word.

**Phase 7 closes the loop back to you.** `screencut-review` serves one page per job:
the render for each profile, the verification report, the cuts grouped by why they were
made, the segments with their tier and the reason for it, and each profile's duration
budget as a number you can type over. Correcting any of them re-runs `compile` and
`render` and **no planner at all** — the page says which stages ran, so the claim is
watchable rather than asserted. A correction is a layer beside the spec rather than an
edit of it, because the planners are cached and would otherwise write their answer back
over yours. Accepting records the spec per profile and the proposed→corrected diff, which
is what the phase-10 learner reads.

**Phase 8 lets the narration come from a script.** `screencut narrate` attaches a
script and your own reference recording to a job; `tts` reads the script in that
voice, `align` puts the script's words on the audio's timings, and the rest of the
pipeline does not change — a synthesized narration is laid down from source t=0 and
the edit cuts it like any other audio. Decision #20 is the boundary and the schema
holds it: no reference recording, no synthesis. Phase 0 measured F5-TTS at 0.11x
realtime here, so `tts` is also the first stage that can run on another machine.

**Four things have not happened yet, and all are about what is installed rather
than what is written.** No *real* recording has been ingested — that needs a
screen, a microphone and Cap on the machine doing the work — so the phase-2 focus
tunables and `trim`'s thresholds have never met real footage. No model has run:
`plan_edit` is exercised against a scripted stand-in, so whether its cuts are ones
you would have made is still an open question. The transcript round-trip has
never met speech: the mechanism runs end to end, but its two tolerances are
guesses until a real recording moves them. And no voice has been synthesized — the
narrated path runs against a stand-in for F5-TTS, which proves the plumbing and
nothing about whether the result sounds like you.

**Phase 0 has been run** on the target machine — a base-model M1 MacBook Air,
8GB, fanless. Its four verdicts are in
[`docs/environment-findings.md`](docs/environment-findings.md), with the raw
numbers under `docs/measurements/` and the harness in `tools/`:

- **Cursor events are extractable** (risk R1 closed), and Cap already writes them
  in normalized coordinates — `FocusTrack`'s own space.
- **ASR: `whisper.cpp`,** `large-v3` at 1.95x realtime and 3 984 MB, `medium` as
  the fallback. All three candidates emit the word-level timings §6.2 needs.
- **F5-TTS is not viable locally** — 0.11x realtime at best, and its MPS path
  crashes on any text long enough to be chunked. Phase 8 built `RemoteRunner`
  because of it, and `tts` is the one stage that asks for a worker.
- **The agent CLI round-trips a schema** — 12/12 valid, though 11/12 arrive
  wrapped in a code fence, and latency runs 6–66 s per call.

Next is a real take and a real model call — record one, run it, look at the cuts in
review, and retune — which is phase 5's stop-and-reassess gate, and now the loop for
doing it exists.

## Quickstart

```sh
make install     # the package and its dev dependencies
make run         # a synthetic job through the whole pipeline (needs ffmpeg)
make take        # a Cap-format take: generate it, ingest it, render it
                 # (needs whisper-cli and its weights; the others do not)
make narrate     # a script read in a cloned voice over a silent capture (§8)
                 # (needs F5-TTS as well as whisper-cli)
make broken      # the deliberately bad fixture, so the checks are seen firing
make review      # the review UI on http://127.0.0.1:8000 (§8)
make check       # tests, generated-artifact drift, TypeScript typecheck
```

`make review` needs `pip install -e ".[review]"` — the server is optional, so a headless
machine installs the pipeline without one. Pass the encoder the jobs were rendered with
(`make review ENCODER=videotoolbox`), or the first correction re-encodes from scratch
rather than re-encoding what changed.

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
| `ingest/` | The recorder-event adapter and the synthetic fixture generators, narrated and not |
| `plan/` | `plan_focus` — the crop path and the zoom regions, both deterministic |
| `synth/` | Open transcription, TTS, and forced alignment — §5.3's three ASR-shaped calls, kept apart |
| `compile/` | The time projection, ASS captions, SVG overlay templates, the FFmpeg graph |
| `prefs/` | `constraints.yaml`: the hand-written tier, layered sparsely over the profiles |
| `runner/` | The stage contract, `LocalRunner` and `RemoteRunner`, the content-addressed cache, SQLite |
| `verify/` | §9.1's deterministic checks, §9.2's transcript round-trip, and the report |
| `review/` | The correction loop (§8): the FastAPI app, the service behind it, and the page |
| `golden/` | The deliberately bad fixture, and the findings it must produce |
| `schemas/` | Generated — six JSON Schemas and the TypeScript types. Regenerate with `make generated` |
| `tests/` | The invariants, the round-trip, the migration, the graph, and real renders |

Five things in the spec are load-bearing and easy to miss:

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
- **A human correction is a sparse layer over the spec, not an edit of it** (§8.1).
  The planners are cached on what they read, so a cached `plan_edit` would write its
  answer back over a re-tiering. The layer goes on last, and the proposal it differs
  from is kept so the difference can be learned from.

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
