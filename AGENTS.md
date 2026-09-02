# Working on screencut

Orientation for an agent picking this up cold. `docs/architecture.md` holds the
design and the reasoning; `docs/implementation-phases.md` holds the order of work.
**Both are the source of truth. This file is how to work on the repo, not what it
is.**

## The one line that settles arguments

**AI-edited, not AI-generated** (`architecture.md` §1.1). Every frame is captured.
The model's job is an editor's — decide what to cut, where to look, what to
emphasize, what to call the thing. There is no image model, no video model and no
B-roll generator in this design, and adding one would replace it rather than
extend it.

If a change would synthesize a frame, it is out of scope no matter how well it
works. The two adjudicated exceptions are written language (`script_draft`, the
metadata sidecar) and your own cloned voice (decision #20) — both bounded by the
schema, not by intent.

## Cite the design, don't restate it

Code comments here reference sections: `§4.5`, `§9.1`, `decision #22`. That is
deliberate — it keeps the reasoning in one place and lets a comment say *why*
without re-deriving it. When you add code that exists because of a design
decision, cite the section. When you make a decision the design does not cover,
put it in the design first.

## Layout

| Path | What lives there |
|---|---|
| `spec/` | The Pydantic models, field-origin metadata, migrations, JSON Schema + TypeScript emit |
| `ingest/` | Recorder adapters (Cap), synthetic fixture generators |
| `plan/` | Planners: `plan_focus`, `plan_captions`, `trim` deterministic; `plan_edit` the one model stage |
| `synth/` | ASR and TTS stages. `asr.py` is open transcription; `align` is phase 8 (§5.3) |
| `compile/` | Time projection, ASS captions, SVG overlays, the FFmpeg graph |
| `verify/` | §9.1's deterministic checks, §9.2's transcript round-trip, and the report |
| `runner/` | Stage contract, `LocalRunner`, the agent-CLI adapter, cache keys, SQLite, the pipeline |
| `review/` | The correction loop (§8): the FastAPI app, the service behind it, and the page |
| `prefs/` | `constraints.yaml` — the hand-written tier, layered sparsely over profiles |
| `golden/` | The deliberately bad fixture and the findings it must produce |
| `schemas/` | **Generated.** `make generated` rewrites them; `make check-generated` fails if they drift |
| `data/` | Gitignored. Job directories and `screencut.db` |

Top-level package names follow §12's tree literally rather than nesting under a
`screencut/` package. That is the documented layout; don't "fix" it.

## Running it

```sh
make install     # package + dev dependencies
make run         # generate the fixture and run the whole pipeline
make take        # a Cap-format take: bundle -> job -> both renders (needs ASR)
make broken      # the deliberately bad fixture, so §9.1's checks are seen firing
make review      # the review UI over the jobs already rendered (§8)
make check       # tests + generated-artifact drift + TypeScript typecheck
```

`make review` serves the correction loop over whatever `make run` and `make take` have
produced. It needs `pip install -e ".[review]"` — the server is optional so a headless
machine installs the pipeline without one — and it wants the same `ENCODER` the jobs were
rendered with, because a render is keyed on it and a mismatch re-encodes everything on the
first correction instead of re-encoding what changed.

`ENCODER=software` is the default and is byte-reproducible. `ENCODER=videotoolbox`
is the target machine's hardware path and **does not exist off macOS** — the
render stage says so clearly rather than letting FFmpeg fail obscurely.

Needs `ffmpeg` with libass and at least one installed font. On macOS that means
`ffmpeg-full`; Homebrew's `ffmpeg` no longer depends on libass and cannot burn a
caption (environment findings §8). Which option reads the filter graph from a file
differs by version and is probed rather than assumed (`compile/ffmpeg.py`), so any
FFmpeg from 6 to 9 works. Node is only for `make typecheck`.

An ingested job additionally wants two things `make run` and `make broken` do not.
`transcribe` needs `whisper-cli` on `PATH` and `ggml-<model>.bin` under
`prefs/constraints.yaml`'s `asr.models_dir`, and without them the job fails with a message
saying exactly that. `plan_edit` needs `hoocode` on `PATH`, and without it the job does
*not* fail — it degrades to `trim`'s edit and says so on the job record (§7.4), which is
the whole point of that row.

## The target machine

A base-model **M1 MacBook Air: 8GB unified memory, fanless, 256GB** (§16). This
is not trivia; it decides things:

- **One model resident at a time.** `LocalRunner` refuses to start a second stage
  holding weights. `transcribe` is the first stage to set that flag, and it alone is
  3 984 MB against a ~5 500 MB working ceiling (environment findings §5).
- Agent-CLI stages hold no local weights, so they are the ones that parallelize.
- Burst speed and sustained speed are different numbers on a fanless machine. Say
  which one a measurement is.
- The cache is not an optimization here. It is what makes the review loop usable.

## Invariants that must not break

Each is enforced somewhere, not merely intended. If you find yourself wanting to
relax one, that is a design change and belongs in `architecture.md` first.

| Invariant | Where it lives |
|---|---|
| No pixels in `EditSpec`; space is normalized source coordinates | `spec/types.py` — `Normalized` |
| No second time base; everything is seconds from source start | §4.5; whole-output overlays carry no anchor at all |
| `removals` + `segments` partition the source exactly | `spec/edit.py` + `EditSpec._edit_is_total` |
| Every spec field records its producing stage | `spec/origin.py`; `tests/test_origin.py` fails the build without one |
| `EditDecisions` is projected at compile, never baked into the spec | `compile/timeline.py` |
| No model emits an FFmpeg argument or a pixel | principle 2; `compile/` is model-free |
| A model stage's cache key includes model id + prompt version | `runner/cache.py` — refuses to key without them |
| One caption list serves every profile, sized to the tightest | `plan/captions.py`; wrapping stays in `compile/captions.py` |
| Job-level stages run before the per-profile ones, never interleaved | `runner/pipeline.py` — they rewrite the spec the others fingerprint |
| No `duration_budget` reaches a model; tiering is aspect-independent | `plan/edit.py`, §4.4.1; the fingerprint omits profiles too |
| A degraded artifact is never cached | `runner/pipeline.py` — caching it makes one lost network permanent |
| A human correction is a layer over the spec, applied after every planner | `spec/corrections.py`, §8.1; a cached `plan_edit` would otherwise overwrite it |
| §9.2 diffs the render against the *expected* transcript, never the raw one | `verify/transcript.py`; the raw diff is a number beside it, not the verdict |
| Generated files match the models | `make check-generated`, `tests/test_generated.py` |

## Conventions

- **Tests are named as claims.** `test_a_cut_inside_a_block_splits_it_and_clips_no_word`,
  not `test_captions_2`. The name is what the test is for; the body is how.
- **Comments say why.** What the code does is visible. Why it is this way, and
  what breaks if it is not, is not.
- **Fixtures are deterministic and byte-stable.** The same command produces the
  same spec and the same source video on any machine. Tests depend on it; golden
  replay will depend on it harder.
- **Commit messages carry the reasoning**, including the bugs found along the way
  and what they cost. Look at `git log` before writing one.
- Run `make check` before committing. The drift check compares against `HEAD`, so
  regenerated schemas must be committed with the change that caused them.

## Traps this codebase has already sprung

Every one of these was a real bug, found late. They rhyme.

**Normalized geometry rounded to pixels in more than one place.** The safe area's
edges are computed by `SafeArea.pixels()` — ceil the low edges, floor the high
ones — and *everything* that sizes or places into the safe area calls it. Two
callers rounding independently disagree by a pixel, and §9.1 then reports a
real-looking failure that is only arithmetic.

**Comparing normalized geometry for containment.** `0.05 + 0.9 > 0.95` in binary.
Compare in pixels, which is the space placement happens in.

**One formula written twice.** The zoom trapezoid exists as an FFmpeg expression
and as Python (for overlay projection). `tests/test_compile_graph.py` evaluates
the generated expression against the Python one. If you duplicate a formula across
a language boundary, check them against each other — do not trust the comment
saying they match. The expected transcript is the second instance:
`EditSpec.transcript_after_edit` computes it from the spec's caption words and
`verify.transcript.expected_transcript` from the ASR transcript through the
projected timeline, because §9.2 needs output timings the spec does not carry.
Same remedy — `tests/test_verify_transcript.py` asserts they agree.

**Cache fingerprints that read too much.** A stage hashes *what it reads*. Hashing
the whole spec is simpler and makes a caption tweak invalidate `plan_focus`, which
is exactly the cost the review loop cannot bear.

**"Dwell" measured between adjacent samples.** A cursor easing across the screen
moves less between two samples than a resting hand does over a second. Measure
over a window.

**A good fixture that is not actually good.** The fixture's own overlay sat on its
own caption, so the occlusion check failed on every ordinary run. Fix the fixture,
not the check: a check that always fires gets ignored within a week.

**FFmpeg mechanism assumptions.** `zoompan` accepts no commands, which is why zoom
is an expression and the crop path is `sendcmd`. `sendcmd` must sit *upstream* of
the filters it targets. Verify a mechanism with a five-second render before
building on it — `git log` has the session where that saved a day.

**A `sendcmd` with nothing to send.** FFmpeg does not ignore it; it refuses the whole
graph with "No commands were specified" and exits. Zoom mode with no overlays emits no
commands at all, which is exactly what an ingested take looks like before `plan_overlays`
exists — and the synthetic fixture, which always has overlays and music, never went near
it. Both `sendcmd` and `asendcmd` are omitted when their script would be empty.

**A check that fires on every correct job in a phase.** §9.1's budget check failed every
phase-4 render, correctly: a raw take overruns a 15s budget, and before `plan_edit` exists
there is nothing to have cut with. Same rule as the fixture that is not actually good —
the check has to know what phase it is in, so it warns while the spec carries no edit
decisions and fails once something has proposed one.

**Trusting a model to produce a valid partition.** `EditDecisions` wants a gapless cover
of exactly `[0, duration]`, and a language model doing that in float arithmetic buys a
retry on nearly every call. The fragment is *intent*; `plan/edit.py` derives the partition.
Where an invariant is arithmetic, make arithmetic hold it and let the model decide only
what arithmetic cannot.

**Letting a model report a number about itself.** `Removal.proposed_by` feeds §9.1's
override rate, which measures how much of `trim` the model rejected. It is derived from
overlap with `trim`'s proposal, never taken from the fragment.

**A layer that the thing underneath rewrites.** Review edits `EditDecisions`; `plan_edit`
writes `EditDecisions`; and `plan_edit`'s fingerprint does not read the fields review
edits, so it stays a cache hit and applies its old fragment straight back over the
correction. The fix is the shape the rest of this design already uses — state the
difference, apply it last, keep what it differs from — and the general rule is that
wherever two writers own one field, the later writer has to be the one that cannot be
skipped.

**Caching a fallback.** §7.4's degradation is what a stage produced *because it could not
run*. Cache it and one lost network is permanent — every later run serves the degraded
edit with the network back up and nothing saying why.

**"Could not run" and "ran and found nothing" are different states.** `verify_transcript`
wrote one flag for both, so a screen capture with the mic off — an ordinary job under §5.3
— carried a warning about a missing checker forever. Same family as the check that fires
on every correct job: wherever a stage can be inapplicable as well as broken, the report
has to be able to say which.

**An FFmpeg option baked into a cached artifact.** `compile` writes the whole command into
its manifest and `render` replays it, so a cached compile plus a toolchain upgrade replays
an option the new binary does not have. Anything that belongs to the *binary* rather than
to the graph is `render`'s to decide and `render`'s to carry in its cache key.

## What is built, and what is blocked

Phases 1–7 are built. Phase 0 has been run: `docs/environment-findings.md` holds the
measured numbers, and everything phase 4 was waiting on is settled — Cap's cursor
format, `whisper.cpp` as the ASR backend, and the memory budget per stage.

**What is still missing is a real recording and a real model call.** Phases 4 and 5 built
the whole path and ran it against `ingest/cap_fixture.py` — a bundle in Cap's own on-disk
format carrying every trap phase 0 measured — and against a scripted agent. Neither is the
same thing as the real one:

- The phase-2 focus tunables have never met real cursor data. Expect to retune them, and
  expect that to be the first honest number `plan_captions`'s `PAUSE_S` gets too.
- Nothing is in `golden/` but the broken fixture. Promoting a real take is phase 4's one
  unfinished build item.
- ASR has run against a test tone, not speech. The invocation, the parse and the stage are
  exercised end to end; recognition accuracy is not. §9.2's round-trip inherits that
  exactly — its happy path runs in CI against a `whisper-cli` stand-in, the same shape
  as phase 5's agent stand-in — but `SEAM_TOLERANCE_S` and `WER_CEILING` are guesses
  until a real recording moves them.
- **No model has run.** `hoocode` is not installed here, so `plan_edit` has only ever
  taken §7.4's degradation path or run against the scripted stand-in in
  `tests/conftest.py`. That stand-in tests our code end to end and tests nothing
  about editorial taste, which is what phase 5's stop-and-reassess gate is for.
- **No take has been corrected.** Phase 7's loop is exercised against the fixture and the
  scripted agent, so what is proved is that a correction costs one compile and one encode.
  Which corrections you actually make most often is what §8 says should decide what the
  overlay preview optimizes for, and what §10 needs ten to fifteen jobs of. Nothing in
  `accepted_specs` came from real footage.

**Do not write parsers for output you cannot run.** Three ASR parsers against JSON shapes
nobody has seen is precisely the failure phase 0 exists to prevent. There is one ASR
backend for that reason, and its parser is written against `output_json` in whisper.cpp's
own `examples/cli/cli.cpp`. The same goes for the agent-CLI invocation: §7.3's contract is
specified and phase 0 confirmed it, including that 11 of 12 replies arrive inside a code
fence that has to be stripped.
