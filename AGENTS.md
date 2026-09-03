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
| `ingest/` | Recorder adapters (Cap), synthetic fixture generators — recorded and narrated |
| `plan/` | Planners: `plan_focus`, `plan_captions`, `trim` deterministic; `plan_edit`, `script_draft`, `emphasis`, `plan_overlays` and the sidecar copy through the agent. `context.py` is what they all put in a prompt |
| `synth/` | §5.3's three calls, kept apart: `asr.py` open transcription, `tts.py` synthesis, `align.py` forced alignment |
| `compile/` | Time projection, ASS captions, SVG overlays, the FFmpeg graph |
| `verify/` | §9.1's deterministic checks, §9.2's transcript round-trip, and the report |
| `runner/` | Stage contract, `LocalRunner`, `RemoteRunner`, the agent-CLI adapter, cache keys, SQLite, the pipeline |
| `review/` | The correction loop (§8): the FastAPI app, the service behind it, and the page |
| `prefs/` | `constraints.yaml` — the hand-written tier, layered sparsely over profiles. `corpus.py` — what §10's learner will read, and how far off its gate it is |
| `golden/` | `replay.py` — §11.1's harness — plus the archived cases and the findings the bad fixture must produce |
| `schemas/` | **Generated.** `make generated` rewrites them; `make check-generated` fails if they drift |
| `data/` | Gitignored. Job directories and `screencut.db` |

Top-level package names follow §12's tree literally rather than nesting under a
`screencut/` package. That is the documented layout; don't "fix" it.

## Running it

```sh
make install     # package + dev dependencies
make run         # generate the fixture and run the whole pipeline
make take        # a Cap-format take: bundle -> job -> both renders (needs ASR)
make narrate     # a script read in a cloned voice over a silent capture (needs TTS + ASR)
make broken      # the deliberately bad fixture, so §9.1's checks are seen firing
make review      # the review UI over the jobs already rendered (§8)
make replay      # replay the golden set, report per-field spec drift (§11.1)
make corpus      # what §10's learner would read, and how many jobs it still needs
make check       # tests + drift + TypeScript typecheck + golden replay
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

`make narrate` is the phase-8 recipe and wants a third thing: a Python with `f5_tts`
installed, named by `prefs/constraints.yaml`'s `tts.python`. Phase 0 measured F5-TTS at
0.11x realtime on the target machine and its MPS path aborts on any text long enough to be
chunked, so `tts` is the one stage that asks for a worker (`runner/remote.py`) — and still
runs locally, slowly, when there is none.

An ingested job additionally wants two things `make run` and `make broken` do not.
`transcribe` needs `whisper-cli` on `PATH` and `ggml-<model>.bin` under
`prefs/constraints.yaml`'s `asr.models_dir`, and without them the job fails with a message
saying exactly that. Every model stage needs `hoocode` on `PATH`, and without it the job
does *not* fail — `plan_edit` degrades to `trim`'s edit, `emphasis` to no markers,
`plan_overlays` to no overlays, the sidecar to script-derived copy, and each says so on the
job record (§7.4), which is the whole point of those rows. `script_draft` is the one
exception and §7.4 says so: there is no script to fall back to, so the job halts.

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
| A model stage's cache key includes model id + prompt version | `runner/cache.py` — refuses to key without them; the version is a hash of the prompt (`runner/agent.py`), so it cannot be forgotten |
| A model stage declares the prompt its key is derived from | `runner/stages.py` — `StageSpec` refuses `model_backed` without an instruction |
| A fragment that writes into `EditSpec` is reconciled, never applied raw | `plan/edit.py`, `plan/overlays.py`; the fragment schema cannot see the document it joins |
| One caption list serves every profile, sized to the tightest | `plan/captions.py`; wrapping stays in `compile/captions.py` |
| Job-level stages run before the per-profile ones, never interleaved | `runner/pipeline.py` — they rewrite the spec the others fingerprint |
| No `duration_budget` reaches a *planner*; tiering is aspect-independent | `plan/edit.py`, §4.4.1; the fingerprint omits profiles too. The metadata sidecar is per-profile on purpose and decides nothing about the edit |
| A degraded artifact is never cached | `runner/pipeline.py` — caching it makes one lost network permanent |
| Synthesis is of you: reference audio, its text and a script, or it does not validate | `spec/narration.py`; decision #20, and §1.1 says it is a schema matter rather than an intent one |
| A stage reads the spec as the stages before it left it | `runner/pipeline.py` rebuilds the job context after every `apply`; otherwise a fingerprint keys on a document that has moved |
| A human correction is a layer over the spec, applied after every planner | `spec/corrections.py`, §8.1; a cached `plan_edit` would otherwise overwrite it |
| §9.2 diffs the render against the *expected* transcript, never the raw one | `verify/transcript.py`; the raw diff is a number beside it, not the verdict |
| A spec is accepted *with* the profile it was accepted under, never with its name | `runner/db.py` migration 0004; re-resolving the name returns the learner's own last move |
| What §10 may move is `learnable=True` on the field, read by the correction layer, the diff and the review page alike | `spec/profiles.py`, `spec/origin.py`; `tests/test_profiles.py` pins the set |
| A take records whether it was recorded or generated | `spec/source.py` — `Provenance`; §10.2 counts real jobs and cannot recover this later |
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
edit with the network back up and nothing saying why. The corollary looks like a bug and
is not: with no agent installed, a job never reaches "nothing to do", because every model
stage degrades and re-attempts on every run. That is why the cache tests script an agent
instead of asserting a hit over a pipeline where no model stage could have run.

**A valid fragment that is an invalid document.** `OverlayPlan` validates in isolation, so
an overlay from 55s to 61s is a well-formed `OverlayIntent` — and `EditSpec` rejects it,
because only the spec knows the source is 60s long. The stage *succeeded* and the job died
at apply time, which §7.4 forbids. A fragment's schema cannot see the document it will
join, so it is never the last word on validity: every model stage writing into `EditSpec`
ends in a `reconcile` that clamps, drops and derives.

**One prompt version across five prompts.** A hand-bumped integer works with one prompt and
fails two ways with five — forget it and the cache serves the old answer to the new prompt,
share it and editing the overlay instruction re-runs `script_draft`. It is now a hash of the
instruction text. Same family as the fingerprint that reads too much: a key is a claim about
what a stage depends on, and a claim maintained by hand goes stale.

**A validator that made its own producing stage unreachable.** Decision #20 required a
synthesized narration to carry a script — and a job that needs `script_draft` has none yet,
so the document that stage exists to complete was invalid until after it had run. A
validator has to admit the state *before* each of its producers, not only the state after
all of them. The relaxation then made a second guard necessary: `tts` could be reached with
a null script and would have synthesized the empty string, which renders as a finished
video with no narration.

**Scripting a test double by call order when there is more than one caller.** The phase-5
agent stand-in replied by call index. Phase 9 put five model stages in one job, so a test
scripting one `EditPlan` handed it to `emphasis`, which runs earlier, and then failed on a
degradation it never asked for. It now also scripts by fragment, keyed on the schema title
in the prompt — the only part of a prompt that names its stage.

**"Could not run" and "ran and found nothing" are different states.** `verify_transcript`
wrote one flag for both, so a screen capture with the mic off — an ordinary job under §5.3
— carried a warning about a missing checker forever. Same family as the check that fires
on every correct job: wherever a stage can be inapplicable as well as broken, the report
has to be able to say which.

**A fingerprint reading a spec a stage before it had already rewritten.** The job-level
context was built once, before the loop, so `trim` fingerprinted a spec without
`narration.audio_path` on the first run and one with it on the second — a cache miss on
every re-run of a job nothing had touched, on the one artifact that costs an hour to
remake. Rebuild the context after any stage that applies to the spec.

**An exclusion from a fingerprint that expired.** `compile` excluded `narration` because it
named a script the graph never read. That was right until the graph was built around a
narration input, at which point it became a cached graph pointed at the wrong file.
Everything a fingerprint excludes is a claim about what the stage reads, and the claim goes
stale when the stage changes.

**An FFmpeg option baked into a cached artifact.** `compile` writes the whole command into
its manifest and `render` replays it, so a cached compile plus a toolchain upgrade replays
an option the new binary does not have. Anything that belongs to the *binary* rather than
to the graph is `render`'s to decide and `render`'s to carry in its cache key.

**A record that could not answer the question it existed for.** Three at once, found by
reading §10 against what phase 7 actually wrote down. `accepted_specs` stored the profile's
*name*, and every tunable §10 learns is a `RenderProfile` field — so the row recorded what
was accepted and dropped what it was accepted under, and re-resolving the name later
returns the learner's own last move. `Corrections` could express a budget and nothing else,
so exit criterion 1's signal — a zoom factor corrected the same way across several jobs —
had no way of being produced; nothing failed, reviewers simply never made one. And nothing
said whether a take was recorded or generated, though §10.2 counts real jobs and the
fixture bundle is in Cap's own format. Same family as the fingerprint that read too much
and the exclusion that expired, one layer up: **a record is only as good as the question it
will be asked**, and unlike those two this kind cannot be fixed afterwards — a corpus
recorded wrong is fifteen reviews to do again. Check what a record will be read for before
the recording starts.

## What is built, and what is blocked

Phases 1–9 are built, and phase 10's corpus with them — the learner itself is not, and
`make corpus` says why: it needs ten to fifteen accepted real jobs and there are none.
Phase 0 has been run: `docs/environment-findings.md` holds the
measured numbers, and everything phase 4 was waiting on is settled — Cap's cursor
format, `whisper.cpp` as the ASR backend, and the memory budget per stage.

**What is still missing is a real recording, a real model call and a real voice.** Phases
4, 5, 8 and 9 built the whole path and ran it against `ingest/cap_fixture.py` — a bundle in
Cap's own on-disk format carrying every trap phase 0 measured — against a scripted agent,
and against stand-ins for both speech backends. None is the same thing as the real one:

- The phase-2 focus tunables have never met real cursor data. Expect to retune them, and
  expect that to be the first honest number `plan_captions`'s `PAUSE_S` gets too.
- ASR has run against a test tone, not speech. The invocation, the parse and the stage are
  exercised end to end; recognition accuracy is not. §9.2's round-trip inherits that
  exactly — its happy path runs in CI against a `whisper-cli` stand-in, the same shape
  as phase 5's agent stand-in — but `SEAM_TOLERANCE_S` and `WER_CEILING` are guesses
  until a real recording moves them.
- **No model has run.** `hoocode` is not installed here, so all five model stages —
  `script_draft`, `plan_edit`, `emphasis`, `plan_overlays` and the metadata sidecar — have
  only ever taken §7.4's degradation path or run against the scripted stand-in in
  `tests/conftest.py`. That stand-in tests our code end to end and tests nothing
  about editorial taste, which is what phase 5's stop-and-reassess gate is for. Risk R5
  now has a meter (`StageResult.schema_violations`, reported by `make replay`) and no
  reading, and it says "unmeasured" rather than 0% when nothing asked a model anything.
- **The golden set's model half has a harness and no case.** `golden/demo_v1` is the
  synthetic fixture, which arrives with a complete spec and runs no job-level stage, so
  the strict half of §11.1 is exercised over a whole real `EditSpec` and the
  distributional half is a baseline of zeroes. Every number in `Tolerances` is a guess.
  Promoting a real take is phase 4's one unfinished build item and it fixes this and three
  other things at once.
- **No voice has been synthesized.** F5-TTS is not installed here, and per phase 0 it is
  not something to run on the target machine either — `runner/remote.py` exists for that
  reason. `synth/tts.py` is written against the invocation phase 0 measured, and every
  number in it came from that benchmark rather than from a job. The narrated fixture's
  voice reference is a *tone*: it proves the invocation, the file handling and the schema
  boundary, and nothing whatever about cloning. Same shape of debt as ASR against a test
  tone, one phase later.
- **`align` is not WhisperX**, though §5.3 names it. WhisperX was never run on the target
  machine, and a parser for output nobody has seen is what phase 0 exists to prevent — so
  alignment anchors the script to whisper.cpp's word timings by arithmetic. WhisperX is the
  upgrade, behind the same stage contract, for whoever can write against it.
- **No take has been corrected.** Phase 7's loop is exercised against the fixture and the
  scripted agent, so what is proved is that a correction costs one compile and one encode.
  Which corrections you actually make most often is what §8 says should decide what the
  overlay preview optimizes for, and what §10 needs ten to fifteen jobs of. Nothing in
  `accepted_specs` came from real footage.

- **No job has been accepted.** §10's learner is gated on ten to fifteen, and every filter
  it will read through — verification, provenance, a recorded profile — is built and
  tested against rows written straight to the record. What none of it has seen is a
  preference. `ACTIVATION_JOBS` is §10.2's lower bound taken literally, and the minimum
  sample count that gates one *default* moving is deliberately not decided here: that is a
  number to pick having seen a distribution, not before.

**Do not write parsers for output you cannot run.** Three ASR parsers against JSON shapes
nobody has seen is precisely the failure phase 0 exists to prevent. There is one ASR
backend for that reason, and its parser is written against `output_json` in whisper.cpp's
own `examples/cli/cli.cpp`. The same goes for the agent-CLI invocation: §7.3's contract is
specified and phase 0 confirmed it, including that 11 of 12 replies arrive inside a code
fence that has to be stripped.
