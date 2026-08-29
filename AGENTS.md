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
| `ingest/` | Recorder-event adapter, synthetic fixture generator |
| `plan/` | Deterministic planners (`plan_focus`) |
| `compile/` | Time projection, ASS captions, SVG overlays, the FFmpeg graph |
| `verify/` | §9.1's deterministic checks and the report |
| `runner/` | Stage contract, `LocalRunner`, cache keys, SQLite, the pipeline |
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
make broken      # the deliberately bad fixture, so §9.1's checks are seen firing
make check       # tests + generated-artifact drift + TypeScript typecheck
```

`ENCODER=software` is the default and is byte-reproducible. `ENCODER=videotoolbox`
is the target machine's hardware path and **does not exist off macOS** — the
render stage says so clearly rather than letting FFmpeg fail obscurely.

Needs `ffmpeg` with libass and at least one installed font. Node is only for
`make typecheck`.

## The target machine

A base-model **M1 MacBook Air: 8GB unified memory, fanless, 256GB** (§16). This
is not trivia; it decides things:

- **One model resident at a time.** `LocalRunner` refuses to start a second stage
  holding weights. Nothing sets that flag yet — `transcribe` will be the first.
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
saying they match.

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

## What is built, and what is blocked

Phases 1–3 are built, plus §9.1's deterministic checks pulled forward from phase 6.
Phase 0 — the environment spike — **has not been run**, and phase 4 is blocked on
it:

- No recorder output, so the adapter has nothing real to be written against
  (risk R1 is still open).
- No ASR backend chosen or benchmarked.
- `hoocode` not installed, so no model stage can run (phase 5 onward).

**Do not write ASR output parsers you cannot run.** Three parsers against JSON
shapes nobody has seen is precisely the failure phase 0 exists to prevent. The
same goes for the agent-CLI invocation: §7.3's contract is specified, and phase 0
is where it gets confirmed.
