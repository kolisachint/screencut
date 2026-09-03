# screencut — Implementation Phases

Companion to [`architecture.md`](architecture.md), which holds the design and the
reasoning. This document holds only the order of work and what "done" means at each step.

Phase 0 has been run and phases 1 to 9 are built; phase 10 has its corpus and not its
learner, and the first of the later phases — kinetic captions — has landed. Four phases carry a caveat rather than a clean finish: phase 4 has no real take,
phase 5 has never called a model, phase 6's transcript round-trip has never met speech, and
phase 10 is waiting on ten to fifteen accepted real jobs — the first three because of what
is not installed on the machine this was built on, the fourth because the jobs have to be
made rather than installed. Each section says so and marks which of its exit criteria that
leaves open. Its measured results are in [`environment-findings.md`](environment-findings.md), and
phases 4, 5 and 8 should be read alongside it. Phases
are sized to be picked up cold: each states its goal, what gets built, how you know it is
finished, and what is deliberately excluded.

## Ordering principles

1. **Prove the load-bearing path early.** Spec → compile → render is what everything else
   hangs off. It is phase 2, before any model runs.
2. **Reach a genuinely useful tool before the riskiest dependency.** Captions over your
   own recorded voice is a real deliverable and needs no TTS — so it lands before F5-TTS,
   which is the component most likely to fight Apple Silicon (`architecture.md` §16).
3. **Prove the product claim early too.** Under `architecture.md` §1.1 this is an *editing*
   tool, and `plan_edit` is the stage that makes it one. It is phase 5 — the first model
   stage, ahead of the model stages that only decorate — because a tool that cannot cut
   well is a different project, and that is worth finding out in phase 5 rather than
   phase 9. Decision #13 is what makes this affordable: with no SDK client to build first,
   model stages can sit where they belong instead of clumping at the end.
4. **Defer anything that learns until there is something to learn from.** A learner with
   an empty corpus is dead code that still has to be debugged.
5. **Every phase ends with something runnable.** No phase is only scaffolding.

---

## Phase 0 — Environment spike — **run**

**Goal:** replace assumptions with facts before any of them is load-bearing.

**Results: [`environment-findings.md`](environment-findings.md)**, with raw measurements
under `docs/measurements/` and the harness in `tools/`.

Not a coding phase. Half a day of finding out whether the stack works on this machine —
a base-model M1 MacBook Air, 8GB, fanless (`architecture.md` §16). Two things follow for
how the measuring is done: **record peak memory alongside every timing**, because on 8GB
the number that decides a design is resident size rather than seconds; and **say whether a
timing is burst or sustained**, because a fanless machine gives two different answers and
only the sustained one governs a real job.

**Build**

- Record a real take with Cap (and/or Screenize) and try to extract cursor + click events.
  Document the actual format, sample rate, and coordinate space.
- Run each candidate ASR backend on a sample: `mlx-whisper`, `whisper.cpp`, and
  faster-whisper. Time them and record peak RSS. Confirm the CTranslate2/MPS situation
  firsthand. Do it at more than one model size — `large-v3` against `medium` — since on
  8GB the smaller model winning on wall-clock is a live possibility rather than a
  consolation prize.
- Run WhisperX's wav2vec2 alignment model under MPS. Confirm it works and time it.
- Install F5-TTS and synthesize thirty seconds. Note whether MPS works, whether
  `PYTORCH_ENABLE_MPS_FALLBACK=1` is needed, how long it takes, and peak memory. A CPU
  fallback on 8GB is also a second copy of the tensors, so the memory number is as much
  the verdict as the timing is.
- Transcribe and synthesize back to back in one process and watch for swap. This is the
  cheapest possible test of the §16 claim that stages must be serialized, and it is worth
  knowing before `LocalRunner` is written rather than after.
- Confirm `h264_videotoolbox` is available in the installed FFmpeg, and time a one-minute
  1080p encode both ways — hardware and `libx264` — since the software path is what the
  golden set pays for on every replay.
- Install hoocode and confirm the stage invocation from `architecture.md` §7.3 works:
  `-p --mode json`, tools disabled, a JSON Schema in the prompt, and a schema-valid
  fragment parsed back out of stdout. Do it once by hand with a throwaway schema. Note
  the round-trip latency — it sets the floor on every model stage.

**Exit criteria**

`docs/environment-findings.md` recording measured numbers, plus three verdicts:

- **Are cursor events extractable?** This is risk R1. A "no" invalidates the `FocusTrack`
  design and must be known now, not in phase 4.
- **Is local F5-TTS viable?** A "no" does not block anything — it means phase 8 starts by
  building `RemoteRunner`, which the stage-contract seam exists to absorb.
- **Does the agent CLI round-trip a schema reliably?** This is risk R5. A "mostly" is the
  expected answer and is fine — §7.2's validate-retry-degrade handles it. A "no" means
  reopening decision #13 before phase 5 depends on it.
- **What is the memory budget per stage?** The 8GB ceiling decides the ASR model size that
  goes into `constraints.yaml` and confirms whether local stages have to be serialized. A
  measured peak-RSS figure per stage is the deliverable; "it seemed fine" is not, because
  the failure mode is swap rather than a crash and swap looks like slowness.

**Not in this phase:** any pipeline code.

**How it came out.** Three of the four verdicts landed where the design hoped and the
fourth did not, which is roughly the intended yield of a spike.

**The measurement itself was nearly wrong.** Resident set size does not see MLX's
unified-memory allocations: mlx-whisper on `large-v3` polls at 1.3GB RSS against a 5.7GB
`phys_footprint`, a factor of 4.3. Since this phase exists because the failure mode is swap
rather than a crash, a budget built on RSS would have been built on half the truth — and it
would have picked the wrong ASR model while looking careful. The harness now wraps every
child in `/usr/bin/time -l` and reports both numbers. The tell was a nonsense result: the
"both models resident" case appeared to use *less* memory than running them one at a time.

**R1 came back better than assumed.** Cap writes cursor positions already normalized to
0..1 — `FocusTrack`'s own space — so the adapter needs no pixel conversion. But it samples
on movement, not on a clock: a resting cursor emits nothing for up to two seconds. That
turns the known dwell trap into something sharper, since dwell is not mismeasured so much
as invisible. Clicks carry no position at all, and cursor time runs on the recording clock
rather than the video's.

**F5-TTS is the "no".** 0.11x realtime on MPS and only for single-batch text; the chunked
path segfaults. CPU completes but takes ten minutes per thirty seconds of audio. Phase 8
starts with `RemoteRunner`, exactly as this phase's text anticipated.

**The toolchain fought back harder than any of the four questions.** Homebrew's `ffmpeg` no
longer depends on libass, so the repository's own phase-2 render could not run on the
target machine at all until `ffmpeg-full` was installed. That is worth knowing before
someone concludes the compiler is broken.

**And installing it exposed a second break: FFmpeg 9 removed `-filter_complex_script`,**
which `runner/stages.py` uses, so twenty tests fail with `Option not found`. The
replacement `-/filter_complex` is verified working here but is not applied — this phase
excludes pipeline code, and the swap needs a version guard since the new syntax predates
neither FFmpeg 7 nor the old option's removal cleanly. **Phase 4 fixed this first**, as a
capability probe rather than a version comparison (`compile/ffmpeg.py`).
On a clean macOS box today no single FFmpeg has both libass and
`-filter_complex_script`, so this is not optional maintenance.

---

## Phase 1 — Spec and fixtures — **built**

**Goal:** the data model everything else is written against.

**Build**

- Pydantic models: `EditSpec`, `RenderProfile` (with `duration_budget`), `FocusTrack`,
  `EditDecisions` (`removals` and tiered `segments` — `architecture.md` §4.4),
  `CaptionBlock` (carrying per-word timings from the start — §6.2), `OverlayIntent`,
  `AudioTrack`.
- `EditDecisions` validators that make an impossible edit unrepresentable: inside source
  bounds, non-inverted, non-overlapping, and **totality** — removals and segments partition
  the source with no gaps. Cheap here, and it is half of risk R5's mitigation.
- **Field-origin metadata** on every spec field: which stage produces it, and whether that
  stage is deterministic or model-backed (§11.1). No model stage exists until phase 5, but
  retrofitting this once the golden set matters means backfilling it across a schema that
  has already drifted.
- `spec_version` and a migration registry, with one no-op migration to prove the mechanism.
- JSON Schema emit + TypeScript type generation, wired to a `make` target.
- Two profiles: `shorts_9x16`, `demo_16x9`.
- Synthetic fixture generator: scripted mouse paths and click clusters producing a
  `FocusTrack`, plus a generated source video (FFmpeg `testsrc`/`lavfi` with visible
  motion) so renders are visually verifiable.

**Exit criteria**

- A fixture round-trips: construct → serialize → deserialize → equal.
- Generated TypeScript types compile.
- A migration from a hand-written v1 fixture to current loads cleanly.

**Not in this phase:** planners, rendering, anything that reads real media.

---

## Phase 2 — Compiler and render ★ — **built**

**★ The load-bearing milestone.** Everything downstream assumes this works.

**Goal:** a watchable video out of a synthetic fixture, in both aspect ratios.

**Build**

- `plan_focus`: `FocusTrack` → zoom keyframes (16:9) and crop path (9:16), with the
  tunables from `architecture.md` §4.3 read from `constraints.yaml`.
- FFmpeg compiler: `EditSpec` + `RenderProfile` → filter graph. Zoom/crop, plain ASS
  caption blocks, overlay PNG compositing, audio mix with ducking, loudness normalization.
- **The time projection** (`architecture.md` §4.5): apply `removals`, select segments by
  the profile's `duration_budget`, concatenate, remap every timing from source into edited
  time, split caption blocks at boundaries using their per-word timings, drop overlays
  landing inside removed ranges. Driven by **hand-authored** `EditDecisions` on the
  fixtures — no model, no `trim`, just the mechanism.
- SVG overlay templates (callout arrow, highlight box, label chip, progress pill) rendered
  to PNG at target resolution.
- VideoToolbox encode path plus a software-encode path for reproducible golden renders.

**Exit criteria**

- One fixture renders to both profiles and both are watchable.
- Zoom lands on the click clusters; the 9:16 crop follows the focus point without judder.
- Captions burn in, correctly timed, inside the safe area for each profile.
- A fixture with hand-authored removals renders cut, with captions trimmed at the
  boundaries and no clipped words. The same fixture under two `duration_budget` values
  produces two different lengths from one `EditSpec`.
- Software-encoded renders are byte-identical across two runs.

**Not in this phase:** kinetic captions, the cache, any model, real media.

**How it came out.** Two mechanisms rather than one, because the two projections
want different things. A crop path is a *sampled* path, so it is computed per frame
in Python and delivered through `sendcmd` to a `crop` filter whose window never
changes size. A zoom is a handful of eased regions, which is analytic, so it stays
an FFmpeg expression — and it has to, because `zoompan` accepts no commands and is
the only filter that can hold a window whose size varies. The same `sendcmd` stream
carries overlay positions (an overlay follows the point it labels, so it moves when
the crop moves) and the progress pill's fill, which is computed from output duration
exactly as §4.5 says it should be.

The trapezoid that shapes a zoom now exists twice — once as an expression, once in
Python for the overlay projection. `tests/test_compile_graph.py` evaluates the
generated expression against the Python one at quarter-second steps, because two
implementations of one formula is the pair that drifts silently.

**Why the projection is here and not in phase 5.** Cutting is a compiler capability, not a
model capability — the model only decides *what*, and §4.5 makes the compiler responsible
for *how*. Building it here means phase 5 adds a stage against a mechanism that already
demonstrably works, instead of debugging timeline remapping and model output at the same
time. It also means a hand-authored `EditDecisions` is a complete, renderable edit from
phase 2 onward, which is the manual escape hatch for any job the model gets wrong.

---

## Phase 3 — Runner, cache, and persistence — **built**

**Goal:** make re-running cheap, which is what makes the review loop possible at all.

**Build**

- Stage contract: `(inputs, params) -> artifact` as a CLI taking JSON on stdin.
- `LocalRunner` (subprocess). Define the `Runner` interface such that `RemoteRunner` is a
  drop-in; do not build it. Local-inference stages run **one at a time** — 8GB will not
  hold two models, and a runner that discovers this by swapping is a runner that looks
  merely slow (`architecture.md` §16). Stages that hold no local weights, the agent-CLI
  ones included, are exempt.
- Content-addressed cache keyed on `(stage_name, input_hash, params_hash)`, with
  `stage_version` in the key — and, for model stages, the model identifier and prompt
  version folded into `params_hash` (`architecture.md` §5.2). No model stage exists yet;
  build the key correctly anyway, because the bug it prevents is silent.
- SQLite: `jobs`, `stage_cache`, `accepted_specs`, `pref_changes` (`architecture.md` §5.4),
  with schema migrations.
- Job directory layout and a `screencut run <job>` CLI.

**Exit criteria**

- Re-running an unchanged job does no work and says so.
- Changing only caption text re-runs compile and render, and nothing upstream.
- Bumping a `stage_version` invalidates that stage and its dependents, and nothing else.

**Not in this phase:** `RemoteRunner`, distributed anything.

**How it came out.** The load-bearing decision is that **a stage fingerprints what
it reads**, not the whole spec. `plan_focus` hashes the source dimensions, the
focus track and the profile's geometry; `compile` hashes the edit and the profile
*minus* the encoder; `render` hashes the media contents and the encoder alone. So a
caption edit re-runs compile and render, and changing `crf` re-encodes without
recompiling. Hashing the spec wholesale would have been simpler and would have
made §8's argument false.

Invalidation propagates because a stage's inputs include its upstream stages'
cache keys — which is also why bumping one `stage_version` touches that stage and
its dependents and nothing beside them.

Renders are cached under their key like every other artifact and **hard-linked**
into `renders/` under a stable name. The cache stays immutable and content-
addressed, `renders/` is a view onto it, and 256GB (§16) does not stretch to two
copies of everything.

---

## Phase 4 — Real ingest and transcription ★ — **built, less the real take**

**★ First genuinely useful output.** A real recording in, a captioned auto-zoomed 9:16
short and 16:9 demo out, narrated by your own voice — with no TTS anywhere in the path.

**Goal:** stop working against fictions.

**Build**

- Recorder adapter: real cursor/click events → `FocusTrack`, written against what phase 0
  actually found. Keep it a thin adapter; `FocusTrack` stays our format.
- `transcribe` stage: ASR backend chosen in phase 0, producing word-level timings from the
  recording's own audio.
- `plan_captions`: word timings → `CaptionBlock`s, with profile-aware line breaking.
- Promote a real take into `golden/`.

**Exit criteria**

- A real recording produces both renders, unattended, from one command. — **met for a
  Cap-format recording, not a real one.** `screencut ingest <take>.cap --out <job>` then
  `screencut run <job>` (or `make take`) produces both, unattended, from a bundle in Cap's
  own format. No real take could be recorded here; see *How it came out*.
- Zoom behaviour on real cursor data is sane — this is the first test of the phase-2
  tunables against reality, and expect to retune them. — **open.** Sane on the fixture's
  cursor data, which is the fixture's data. The tunables have not met reality.
- The output is something you would actually post. If it is not, stop and fix that before
  continuing; every later phase assumes this baseline is good. — **open**, and for the
  same reason. Nothing has been posted, because nothing real has been recorded.

**Not in this phase:** synthesized voice, cuts, verification, review UI.

**How it came out.** Everything phase 0 unblocked got built, and the one thing it could
not unblock is the one thing still open: **no real take was promoted into `golden/`,**
because recording one needs a screen, a microphone and Cap on the machine doing the work.
Read the exit criteria against that. What runs instead is `ingest/cap_fixture.py`, a
bundle written in Cap's own on-disk format carrying every trap phase 0 measured at or
beyond the measured severity — a 3.6 s rest gap against the real take's 1.98 s worst case,
clicks landing inside those gaps with no position of their own, the 0.194 s clock offset,
and a sidecar claiming an fps the stream does not have. A fixture whose traps are gentler
than reality passes while the adapter is broken, so these are not.

**The first thing was the FFmpeg option, exactly as phase 0 said.** It came out as a
**capability probe rather than a version comparison**, and that was the decision inside
the ten lines. Version strings are a proxy for the thing that changed — distribution
builds carry suffixes, git builds report `N-109848-g0d1c2c9c1a` and no number at all —
while `-h full` either lists `-filter_complex_script` or does not, which is the question.
Asking the binary cannot guess wrong. The second half is less obvious: `compile` writes
the whole FFmpeg command into its manifest and `render` replays it, so a cached compile
plus a toolchain upgrade replays an option that no longer exists. The option belongs to
the binary, so `render` rewrites it and `render`'s cache key carries it.

**Reading absence as dwell turned out to need no new dwell rule.** Phase 0 sharpened the
known trap into "dwell is invisible, not mismeasured", which sounds like it wants a
special case. It does not: resampling onto a fixed grid and *holding* position across a
gap — rather than interpolating a glide that never happened — turns absence into a run of
identical samples, which is what the existing classifier already reads as dwell. One rule,
reached two ways. The four rest gaps in the fixture produce exactly four zoom regions,
each starting the moment the cursor stops.

**One caption list, sized against the tightest profile.** §4.1 has one `EditSpec` serving
N profiles and `EditSpec.captions` is part of that document, so "profile-aware line
breaking" cannot mean one list per profile. Wrapping is already per profile in
`compile/captions.py`; what `plan_captions` decides is where a block ends, and sizing that
against the narrowest box is the only answer that is one list. Blocks break on sentence
endings first, pauses second, capacity last — capacity being the fallback is the point,
because a block split only on capacity reads as cut mid-thought.

**`transcribe` and `plan_captions` are the first stages that are not per profile.** What
was said does not depend on the shape it will be rendered into, and running ASR twice for
two profiles is 23 seconds thrown away per run on the target machine. They run once per
job, ahead of the per-profile group and not interleaved with it, because they rewrite
`spec.json` and every per-profile fingerprint is taken from the spec. `transcribe` is also
the first stage to set `holds_local_weights`, which `runner/stages.py` predicted it would
be.

**Whether a job wants those stages is not in the spec, and it cannot be.** `captions: []`
means "not planned yet" in an ingested job and "this take is silent" in the next one, and
nothing in the document distinguishes them — a fixture with hand-authored captions would
be transcribed over. So `job.json` says: pipeline configuration for one run, beside
`spec.json` rather than inside it. A job directory without one is a job whose spec is
complete as given, which is every fixture in this repository, which is why adding a whole
stage group broke none of them.

**Two bugs, both found by running the first job that was not the synthetic fixture.**

An ingested take has no overlays yet, and in zoom mode that leaves the `sendcmd` script
empty — FFmpeg does not ignore a `sendcmd` with nothing to send, it refuses the graph with
"No commands were specified" and exits. The synthetic fixture always has overlays and
music, so the path had never run. Both `sendcmd` and `asendcmd` are now omitted when there
is nothing to send.

And §9.1's budget check failed every phase-4 job, correctly and uselessly: a 24 s take
against a 15 s budget overruns, and before `plan_edit` exists there is nothing to have cut
with. A check that fails on every correct job in a phase gets ignored within a week, which
is this repository's own standing warning turned on itself. It reports the overrun as a
warning while the spec carries no edit decisions at all, and as a failure once something
has proposed one.

**What could not be run here.** ASR against real speech. The stage runs end to end — audio
extraction, the phase 0 invocation, the parse, the artifact — and the parser is written
against `output_json` in whisper.cpp's own `examples/cli/cli.cpp` rather than against a
JSON shape somebody imagined. But no machine available for this work could reach the
weights, so what has been transcribed is a test tone, correctly, to no words. The
end-to-end ASR test is written against whatever `constraints.yaml` names and skips when
those weights are absent, so it runs on the target machine at `large-v3` rather than
quietly testing something smaller.

**What phase 5 inherits.** A real take, still. Both remaining exit criteria — "zoom
behaviour on real cursor data is sane" and "the output is something you would actually
post" — are judgements about footage, and the phase-2 tunables have not met reality yet.
Expect to retune them, and expect the first real recording to be where `plan_captions`'s
`PAUSE_S` and the focus weights get their first honest number.

---

## Phase 5 — Editorial pass ★ — **built, less the judgement**

**★ The product claim.** Phase 4 produces a captioned, auto-zoomed version of everything
you recorded. This is the phase where it becomes *edited* — where the dead air, the
fumbled restart and the "um" come out.

**Goal:** the first model stage, and the one the whole philosophy rests on
(`architecture.md` §1.1).

**Build**

- **`trim`** (deterministic, no model): silence and dead air from audio levels, filler
  words from a closed list against the transcript, with the §4.6 tunables in
  `constraints.yaml`. Emits proposed `removals`. **Build and evaluate this alone first** —
  it is most of the value, and knowing how much it gets right is what tells you what the
  model actually needs to do.
- Agent-CLI adapter in `runner/`: the §7.3 invocation (print mode, JSON events, tools
  disabled, fixed cwd), schema into the prompt, fragment validated back out, retry once on
  a validation error, degrade per §7.4. One adapter, reused by every later model stage —
  this is the phase that pays for phase 9.
- **`plan_edit`** (model): transcript, word timings, `trim`'s proposal and a `FocusTrack`
  summary in; final `removals` plus tiered `segments` out. It may reject any proposed
  removal, add false starts and restarts of its own, and rank what remains.
- §9.1's edit-integrity, totality and budget checks, ahead of the rest of verification,
  because they bound what a bad fragment can do.

**Exit criteria**

- `trim` alone produces a watchable video from a real take: no dead air, no fillers, no
  clipped words at the boundaries. This is the §7.4 floor, and it has to be genuinely
  acceptable on its own. — **met on the fixture, not on a real take.** `silencedetect`
  finds all four of the synthetic fixture's dead-air gaps and the closed list finds its
  one "um"; 24 s becomes 17.7 s and §9.1's `cut_mid_word` reports nothing. Whether it is
  *watchable* is a judgement about footage, and there is still no footage.
- `plan_edit` on top of it produces cuts you would have made, and its overrides of `trim`
  are defensible — check the override rate on the report (§9.1). — **open.** The stage,
  the adapter and the override rate are built and exercised against a scripted agent;
  `hoocode` is not installed on the machine this was built on, so no model has run.
- One `EditSpec` renders at two different lengths under two `duration_budget` values, and
  both are coherent — the short is not just the demo with its ending missing. — **met**,
  and with no second model call, which is the §4.4.1 half of the claim.
- Killing the network mid-job still produces a render: the `trim`-only one, all segments
  `essential`. — **met.**
- Re-running with an unchanged transcript hits the cache and calls no model. If it does not,
  phase 3's key is wrong (§5.2), and everything after this gets slow and expensive. —
  **met**, and a *degraded* run deliberately does not cache, so one lost network does not
  become permanent.

**This is a stop-and-reassess gate,** and decision #19 gives it a much better shape than a
single pass/fail: `trim` and `plan_edit` can be judged separately. If `trim` is good and
`plan_edit` adds nothing, ship `trim` and leave tiering manual — that is a real product.
If `trim` itself is wrong, that is tunables in `constraints.yaml`, not an architecture
problem. Work R4's levers in order and do not carry a bad `plan_edit` forward on the
assumption that later phases improve it. They do not; they decorate it.

**Not in this phase:** emphasis, overlays, metadata — those are phase 9, and they are
decoration on top of this.

**How it came out.** The stop-and-reassess gate cannot be walked through here, and that is
the honest headline: `hoocode` is not installed on the machine this was built on, so
`plan_edit` has never called a model. Everything around it is built and exercised — the
adapter, the reconciliation, the cache key, the override rate, and §7.4's degradation —
against a script on `PATH` that emits the event stream hoocode emits, fence and all. That
tests our code end to end and tests nothing about editorial taste, which is the half the
gate is actually for.

**`trim` is better than the plan gave it credit for, and the reason is `silencedetect`.**
Measuring the audio rather than inferring silence from gaps in the transcript is the
difference between a mechanism and a heuristic: a gap in the words is not a gap in the
sound, and a long "uhhhh" is one and not the other. On the synthetic fixture it finds all
four dead-air gaps to within 20 ms of where the generator put them.

**Three rules in `trim`, each written after it cut something it should not have.** A range
with words in it is not dead air whatever the meter says — someone speaking quietly reads
as silence at any threshold loose enough to catch real dead air, so ASR wins and the
silence is clipped around the words. `keep_pad_ms` *shrinks* a removal rather than growing
it, because a cut at the level threshold clips the breath before the next word. And
removals merge, because a filler landing against a silence would otherwise leave a 40 ms
segment that §4.4's totality makes real, selectable and reviewable.

**The filler list is deliberately short.** "so", "like", "right" and "actually" are fillers
about half the time and ordinary words the other half. A list cannot tell which, and
stripping every one of them mangles speech — judging that is exactly what §7.1 pays
`plan_edit` for, so the list stays unambiguous and the model gets the ambiguous half.

**The model returns intent; the partition is derived.** This is the decision the plan did
not contain. `EditDecisions` demands a gapless, non-overlapping cover of exactly
`[0, duration]`, and asking a language model to land that in float arithmetic buys a retry
on nearly every call for no editorial gain. So the fragment carries removals and tiered
segments as ranges the model cares about, and `reconcile` derives the total partition:
removals win, segments are clipped to what is left, and anything nobody tiered stays
`essential`. §4.4's totality still holds by construction — it is arithmetic that holds it,
which is §4.5's discipline applied one level up.

**`proposed_by` is derived rather than reported,** for the same reason. A removal
overlapping one of `trim`'s proposals came from `trim` whatever the model says, and the
§9.1 override rate is a number about the model that the model therefore cannot write about
itself.

**No `duration_budget` reaches the prompt, and that is §4.4.1 doing real work.** Putting
budgets in front of the model would make the ranking depend on the profile — and then one
`EditSpec` could not render at two lengths, a shorter short would cost a model call, and
the cache key of the most expensive stage in the pipeline would move whenever somebody
adjusted a number the compiler owns.

**A degraded artifact is not cached.** §7.4 says a failed LLM stage degrades and the job
records it; it does not say what the cache should do, and the obvious answer is wrong.
Caching the fallback makes one lost network permanent — the next run, with the network
back, serves the degraded artifact forever. The file is still written so the job finishes;
the missing cache row is what makes the next run try the model again.

**What `trim` alone gets wrong, and why it is left wrong.** Cutting "um" out of the middle
of "so um clicking through…" leaves "so" as a 0.54 s caption block of its own, which §9.1
duly warns about. The deterministic fix — extend a removal to swallow a fragment too short
to display — depends on `min_display_s`, which is per profile, and tiering is supposed to
be aspect-independent. So it is left for `plan_edit`, which under §7.1 is allowed to widen
a proposed removal and is the stage that should be making that call.

**What phase 6 and 7 inherit.** The judgement half of this gate, still: a real take, a
model that has actually run on it, and someone deciding whether the cuts are ones they
would have made. Until that happens, `trim` is the product and `plan_edit` is untested
polish on top of it — which, per decision #19, is a real shipping position rather than a
failure.

---

## Phase 6 — Verification — **built, less the real speech**

**Goal:** stop garbage reaching a person.

**Built in two pieces, and deliberately.** §9.1's checks need a spec, a profile and
a render, and nothing else — not real media, not a model. They were pulled forward
into phase 5 because they are what will catch problems on the *first* real
recording, which is when nobody yet knows what to look for. §9.2's transcript
round-trip waited for phase 4 to choose an ASR backend, and lands here. §9.3's
perceptual layer stays where it is: add it once you know which real failures the
first two miss.

**Build**

- Deterministic checks (`architecture.md` §9.1): render integrity, duration, loudness and
  true peak, caption overlap / duration / line length / safe area, overlay occlusion,
  crop-path continuity. Edit integrity, totality and budget already exist from phase 5;
  fold them into the same report.
- Transcript round-trip: open-transcribe the rendered audio, diff against the
  **expected transcript** computed per profile from the spec (source minus removals, minus
  segments below this profile's threshold — §9.2), classify each difference into the three
  classes, report WER over the third only. Diffing against the raw transcript would flag
  every successful phase-5 edit as a failure.
- A verification report per render, stored on the job record.
- At least one **deliberately broken** fixture — clipped audio, overlapping captions,
  juddering crop, and a cut landing mid-word — added to `golden/`.

**Exit criteria**

- Every check fires on the broken fixture and none fires on the good one. — **met** for
  §9.1, with the two §11 breakages a spec cannot express exercised against hand-built
  inputs instead (`golden/README.md` says which and why). §9.2 joins them: the synthetic
  fixture's audio is a test tone, so no fixture can mispronounce a word it never speaks,
  and the round-trip is exercised against constructed transcripts.
- A correctly edited job reports zero real differences despite the rendered audio
  differing from the raw transcript. This is the check on the check, and it is the one
  that decides whether §9.2 stays useful once phase 5 exists. — **met against a
  constructed transcript**, and it is the first test in
  `tests/test_verify_transcript.py`: an edit that drops half the words reports zero real
  differences and puts the other number — what the raw transcript would have shown — on
  the report beside it. Met against *speech*: open, and it stays open until there is a
  real recording, along with `SEAM_TOLERANCE_S` and `WER_CEILING`.
- The report is legible enough to act on without reading the code. — **met.** A failing
  round-trip names the first real difference and where in the render it is; a passing one
  says how much of the render the edit is responsible for, because "0 real differences"
  alone reads the same as a check that ran against nothing.

**Not in this phase:** the VLM perceptual layer. Add it once you know which real failures
the deterministic checks miss.

**How it came out.** `verify` is a pipeline stage like any other, so a report is
cached, keyed and invalidated with everything else, and `screencut run` prints it.
The report is a list of findings rather than a verdict, because §9.1's most useful
outputs are numbers — the budget overrun in seconds, the trim composition — and a
check that can only say no cannot say "7.4 seconds over".

Two of §11's four breakages turned out to be **unrepresentable**: `EditSpec`
refuses overlapping caption blocks and `plan_focus` rate-limits the crop by
construction. Their checks stay, exercised against hand-built inputs, because the
thing that makes them impossible today is code that can change. The broken fixture
carries the two a spec can still express, plus an overlay that occludes a caption.

The checks paid for themselves before they were finished. Running them on the good
fixture failed twice for real reasons: overlays placed a pixel outside the safe
area because a normalized inset was rounded to pixels in two places that disagreed,
and the fixture's own highlight box sat on top of the caption. The first is fixed by
one shared rounding helper; the second by moving the fixture's target, because a
good fixture has to actually be good or the check fires every run and gets ignored
within a week.

### The round-trip, and what it cost

**§9.2 is a second stage, not a second check.** It transcribes the *render*, so it
holds 4GB of weights (environment findings §5) and `verify` does not. One stage
with the flag set would have every report claim memory it never used, and would
re-run ASR whenever a §9.1 tunable moved. So `verify_transcript` runs between
`render` and `verify`, and `verify` folds its result into the same report.

**It is skipped rather than failed on a job that has no transcript,** which is
every fixture in this repository. There is nothing dishonest available to compare
against: a fixture's captions are hand-authored, so diffing the render against
them would be checking the spec against itself and passing every time. Skipping a
stage turned out to need one change in the pipeline — a skipped stage contributes
no key and no input to its dependents — and that is the correct reading rather
than a workaround, since a stage that did not run is not part of what the next one
read.

**The second class of difference needed pinning down before it could be coded.**
"A range `EditDecisions` accounts for" has a wrong reading that looks right: that
removed material turning up in the render is expected. It is the opposite — audible
removed material means the cut did not happen, which is the loudest failure this
check can find. What the edit actually accounts for is the **seam**: a splice joins
two stretches that were never adjacent, and the word either side of it can lose its
onset or its tail. Differences within `SEAM_TOLERANCE_S` of a cut are the edit
working; everything else is the third class.

**The end of the timeline is not a seam,** and that asymmetry is load-bearing.
Truncated narration is one of the five failures §9.2 names, and excusing
differences at the last boundary would excuse exactly it.

**One formula ended up written twice.** `EditSpec.transcript_after_edit` already
computed the expected transcript from the spec's caption words;
`verify.transcript.expected_transcript` computes the same selection from the ASR
transcript through the projected timeline, because a finding has to say *where* it
is and the spec deliberately carries no output time (§4.5). Rather than delete
either, `tests/test_verify_transcript.py` checks them against each other — the
remedy `AGENTS.md` already prescribes for this exact trap.

**A silent job briefly warned on every run.** The first cut wrote `ran=False` for
both "the check could not run" and "the check ran and found nothing to hear", so a
screen capture with the mic off — an ordinary job under §5.3 — carried a warning
forever. They are different states and the report says so: the first is a WARN
about a missing checker, the second an INFO about a silent take.

**What the round-trip has not met is speech.** `SEAM_TOLERANCE_S` and
`WER_CEILING` are the two numbers here that a real recording will move, and
neither has seen one — the same standing debt as `plan_captions`'s `PAUSE_S` and
the phase-2 focus tunables. The mechanism is exercised end to end: audio
extraction, the phase-0 invocation, the parse, the diff and the artifact all run
in CI against a `whisper-cli` stand-in on `PATH`, in the same shape phase 5 uses
for the agent, alongside the degradation path and the silent-take path. That
tests our code and tests nothing about recognition, which is the half the
remaining exit criterion is for. The one cost already visible without speech is that the
round-trip is keyed on the render, so a caption tweak re-runs ASR even though it
changed no audio. A key over the rendered audio alone would fix it and cannot be
computed, because the key is needed before the render exists.

---

## Phase 7 — Review UI — **built**

**Goal:** capture corrections as structural diffs.

**Build**

- FastAPI app, one page per job: rendered video per profile, verification report, the
  proposed `EditSpec` as an editable form, re-render button.
- Edit review specifically: `removals` grouped by `kind` and `segments` with their tier and
  `reason` (§4.4), so a removal can be reinstated or a segment re-tiered without leaving
  the form — plus the profile's `duration_budget` as a directly editable number, since
  "make the short shorter" is one field rather than a dozen re-tierings. These corrections
  are the phase-5 feedback signal and the input to §10's budget defaults.
- Any §7.4 degradation shown prominently. Under decision #12 this page is the only place a
  degraded job announces itself.
- Accept / reject, writing accepted specs and the proposed→corrected diff to SQLite.
- Generated TypeScript types from phase 1 wired into the frontend.

**Exit criteria**

- A correction round trip — edit, re-render, accept — completes in seconds, not minutes,
  because the cache holds. — **met, with the render as the floor.** Everything upstream of
  `compile` is a cache hit, so the cost of a correction is one compile and one encode; on
  the fixture that is seconds, and on a long take it is whatever encoding that take costs.
- Adjusting a removal, a tier, or a budget re-runs compile and render and **re-runs no
  planner at all** — not `plan_edit`, not `plan_captions`, not `plan_overlays`. This is
  §4.5's whole payoff, and if it does not hold, the review loop costs a model call per
  correction and will be abandoned. — **met**, and asserted three times over in
  `tests/test_review.py`: each correction re-runs exactly `compile`, `render`,
  `verify_transcript` and `verify`, the scripted agent is called once for the life of the
  job, and `plan_focus` does not re-run either.
- `accepted_specs` and the diff record populate correctly. — **met.** Accepting writes one
  `accepted_specs` row per profile and one `reviews` row carrying the proposed→corrected
  diff; rejecting writes the second without the first.

**Not in this phase:** overlay preview, live playback.

**How it came out.** The build was small and the design problem underneath it was not.
Everything here follows from one thing the plan did not say out loud: **the planners
rewrite the fields review edits, and the cache is what makes that dangerous.**

**A correction cannot be an edit of `spec.json`.** `plan_edit`'s fingerprint reads the
focus track and the source duration, so re-tiering a segment does not invalidate it — and
the next run applies the *cached* fragment straight back over the correction. The
reviewer's decision would survive exactly until the next render, and disappear silently.
So a correction is a sparse layer beside the spec, `corrections.json`, applied after the
job-level stages have folded their artifacts in. It is the same shape as `constraints.yaml`
over the built-in profiles (§4.1) and `EditDecisions` over the timeline (§4.5): state the
difference, apply it last, keep the thing it differs from intact.

**Keeping the proposal intact is what makes the diff possible.** With a correction on the
job, the pipeline writes both documents — `spec.json` as rendered, `proposed.json` as the
planners left it — and the §10 record is the difference between two documents rather than a
restatement of the form that produced them. So a correction that changes nothing produces
no change: setting a tier to the one it already had, or typing the budget the profile
already has, is not a row in the learner's corpus. Recovering the proposal afterwards from
cached artifacts would have been an archaeology exercise that a `--force` run breaks.

**Withdrawing a correction had to be as complete as making one.** Deleting
`corrections.json` restores `spec.json` from `proposed.json` and removes it. A job whose
stages all rewrite the spec would have recovered on its own next run; a fixture's would
not, and "delete the file" quietly meaning "keep them forever" is the same silence the
layer exists to prevent.

**A stale correction is refused, not skipped.** A correction addresses content — a removal
by its span, a segment by where it starts — never an index, because an index into a list
the model rewrote means something else afterwards and silently means it. When the plan
really has moved underneath a correction, applying it raises rather than dropping it: the
API answers 409 and says so. Dropping it quietly is the same class of failure as
overwriting it, and both end with a reviewer watching a video that ignores them.

**Reinstating a removal is arithmetic, and it had two answers that both looked right.** The
span comes back as its own segment rather than as an extension of a neighbour, because
merging would fold two arguments about two passages into one. Its tier is the higher of
whatever it touches, and `essential` when it touches nothing — never the lowest, because a
reviewer who puts a passage back and then watches a tight budget drop it again has been
told the correction worked when it did not.

**The correction layer belongs to the job, not to the page.** `run_job` reads it, so
`screencut run` from the shell renders exactly what review shows. A budget override that
lived only in the web process would be a second pipeline, and the exit criterion above
would have been proved about the wrong one.

**Typed against the generated types with no build step.** The page is plain ES modules;
`tsc` checks it with `checkJs` against `schemas/screencut.ts`, so a spec change the page
has not caught up with fails `make typecheck` rather than rendering an empty column, and
the browser runs exactly what is committed. `Corrections` and the diff are Pydantic models
in `spec/`, which puts them in the same generated file — the page that posts a correction
and the pipeline that reads it cannot disagree about its shape. Two shapes on the page are
not generated: the verification report, which is a pipeline record rather than a spec
document, and the response envelope.

**The page says which stages ran.** That is this phase's exit criterion, and a reviewer
should be able to watch it hold rather than infer it from a stopwatch — the same argument
as §9.1's report being numbers rather than a verdict.

**What this phase does not have is a corrected take.** The loop is exercised against the
Cap-format fixture and the scripted agent, so what is proved is that a correction costs one
compile and one encode. Which corrections get made most often — the thing §8 says should
decide what the overlay preview optimizes for, and the thing §10 needs ten to fifteen jobs
of — needs real footage and a real model, which is the same standing debt phases 4, 5 and 6
each left.

---

## Phase 8 — Voice synthesis — **built, less the real voice**

**Goal:** narration from a script rather than from your microphone.

Deliberately after phase 4: everything before this is useful without it, and phase 0 has
already established whether this runs locally.

This is the one synthesis the design permits, and decision #20 is what permits it — your
voice, your script, your reference audio. That boundary is a schema-and-config matter, not
a matter of intent, so make it one: the voice reference is a required, per-job, explicitly
recorded input, not a default that can be quietly pointed at someone else.

**Build**

- `tts` stage: F5-TTS behind the CLI contract. If phase 0 said local is impractical, this
  is where `RemoteRunner` gets built instead — and on a base-model M1 Air that is a real
  branch rather than a formality, so read phase 0's memory and sustained-speed numbers
  before starting rather than after.
- `align` stage: WhisperX **forced alignment** against the known script text — a different
  mode from phase 4's open transcription (`architecture.md` §5.3).
- Script as an optional job input; music bed mixing and ducking against synthesized narration.
- Voice-reference handling, with an explicit consent note in the job record.

**Exit criteria**

- Script in, narrated and captioned video out. — **met**, against stand-ins for both
  backends. `make narrate` generates a silent capture, a script and a voice reference and
  runs the whole recipe; `tests/test_narration.py` runs the same path in CI and asserts the
  captions are the script word for word and the render came out with an audio track on it.
- Phase 6's transcript round-trip passes on synthesized narration — this is the check that
  catches TTS mispronunciation, and it is the reason verification comes first. — **met**,
  and driven rather than merely observed: one test has the narration say "fitters" for
  "filters" and asserts §9.2 reports it as a real difference rather than a seam.
- `plan_edit`'s cleanup half no-ops on synthesized narration, which has no disfluencies to
  remove. If it starts cutting clean speech, that is a phase-5 prompt problem surfacing on
  new input, and it is worth knowing before the learner starts averaging over it. — **met**,
  in the form arithmetic can state: §4.6's proposal on a read script is empty, so there is
  nothing for the model to review. The test asserts the empty proposal rather than the
  model's restraint, because the model's restraint is not a thing a stand-in can show.

**Not in this phase:** `script_draft` (phase 9 — the script is supplied here), kinetic
captions, a network transport for `RemoteRunner`.

**How it came out.** Three of the four build items came out close to the plan. The one that
did not is `align`, and the reason is the standing rule about code written against output
nobody has run.

**`align` is not WhisperX, and that is deliberate.** §5.3 named it; phase 0 benchmarked
three ASR backends and WhisperX was not among them. Writing a parser for its output would
be the exact failure phase 0 exists to prevent, so alignment is done with the backend this
repository has actually run: open-transcribe the narration with whisper.cpp, then anchor
the script to what came back, interpolating the runs between anchors by word length. It is
principle 3 applied to a stage the design had assumed needed a library — the script and
the audio are nearly identical sequences by construction, so the alignment is arithmetic
over an edit distance rather than an acoustic model. WhisperX remains the upgrade, behind
the same stage contract, for whoever installs it and can therefore write against it.

**The script wins the word; the audio wins the timing.** Aligning the other way — keeping
whisper's words — would have been easier and would have quietly disarmed §9.2. Captions
carry the script, `verify` open-transcribes the *render* and diffs the two, so a
mispronunciation is a difference between what the script says and what came out. Take
whisper's words instead and the round-trip compares the render against a transcript of
itself, agreeing perfectly about a word the narration got wrong.

**`transcribe` and `align` are alternatives, not neighbours, and the graph had to be able
to say so.** They are §5.3's two calls, and a job runs one of them. Stages now declare what
they **provide** rather than only what they are called: both provide `transcript`, and
`plan_captions`, `trim` and `plan_edit` depend on `transcript` rather than on either stage.
The alternative was an `if` about how the narration was made in every stage downstream of
it, which is the same shape of mistake as `compile` asking how a caption was written.

**A fingerprint read a spec that a stage before it had already changed.** `tts` writes
`narration.audio_path`; `trim` measures whichever file the narration is in. The job-level
context was built once, before the loop, so `trim` fingerprinted a spec with no narration
on the first run and one with it on the second — a cache miss on every re-run of a job
nothing had touched. The narration is the most expensive artifact this pipeline makes, so
this was the review loop's cost model gone for exactly the jobs that can least afford it.
The context is now rebuilt after any stage that rewrites the spec.

**`compile`'s fingerprint excluded `narration`, correctly, until it did not.** The
exclusion was right while `narration` named a script the graph never read. The moment the
graph was built around a narration input, an exclusion that had been bookkeeping became a
cached graph pointed at the wrong file. Anything a fingerprint excludes is a claim about
what the stage reads, and it expires when the stage changes.

**A narration longer than its recording fails by name.** Left alone it surfaces two stages
later as a caption block past the end of the source — true, and useless. Holding the last
frame to cover the overrun would be a decision about synthesizing video, and §1.1 does not
let a stage make one of those.

**`RemoteRunner` exists and one transport is written.** Phase 0's verdict was unambiguous —
0.11x realtime, and the chunked path crashes on MPS — so `tts` is the one stage that asks
for a worker, and it still runs locally, slowly, when there is none. What the transport
here proves is the property `StageRequest` has claimed since phase 3 and nothing tested: a
stage sees only its job directory, and only the parts of it that were sent. Superseded
cache artifacts stay home, which is what separates a usable remote from a theoretical one
on a job with a few correction cycles in it. A network transport is three methods and is
deliberately unwritten until there is a worker to write it against.

**Every stage already had something to say and the pipeline was throwing it away.** A run
now prints what each stage did — seconds of narration and at what fraction of realtime,
how much of the script the alignment anchored, how much `trim` proposed. It costs one
field on `StageOutcome`, and it is the same argument as §9.1's report being numbers rather
than a verdict: an alignment that anchored 40% of the script is not a failure, and it is
exactly what you want to have seen before wondering why the captions drift.

**What this phase does not have is a voice.** F5-TTS is not installed here, and per phase 0
it is not something to run on the target machine either — so every number in `synth/tts.py`
came from phase 0's benchmark rather than from a job. The fixture's voice reference is a
tone, which proves the invocation, the file handling and the schema boundary and proves
nothing about cloning. Two of phase 0's hazards are handled in code on its say-so: the
teardown crash that takes the exit code with it after writing good audio, and the FFmpeg
library collision. Both are written down where they will be found when they next fire.

---

---

## Phase 9 — Remaining model stages — **built, less the judgement and the take**

**Goal:** the taste and language decisions, bounded by schema.

The adapter, the retry path, and the degradation path all exist from phase 5. This phase
is four stages against a seam that already works, which is the whole reason it is small.

**Build**

- `script_draft`, emphasis selection, `plan_overlays`, metadata sidecar — each returning a
  Pydantic-validated spec fragment through the phase-5 adapter (`architecture.md` §7.2).
- Per-stage prompt and model configuration: these stages differ in how much thinking they
  deserve, and model choice is a flag (decision #13). Overlay placement is not script
  drafting.
- Degradation paths per §7.4's table, extended to cover the four new stages.
- Golden-set replay split by field origin (§11.1): strict single-run tolerances on
  deterministic fields, distributional over N runs on model-written ones. Parallelized per
  §7.5. The origin metadata came from phase 1, so this is a harness rather than a
  schema change.

**Exit criteria**

- Every model stage returns a validated fragment or degrades cleanly; killing the network
  mid-job still produces a render — now a cut, cleaned, captioned one rather than a raw take.
  — **met**. `tests/test_model_stages.py` runs an ingested job with the agent unreachable
  and asserts four degradations, a total edit, no overlays, no emphasis markers and a
  sidecar carrying script-derived copy — with both renders on disk. The same file runs it
  with the agent answering and asserts each fragment reached `spec.json`.
- Golden-set replay runs and reports per-field spec drift. — **met**. `make replay` replays
  `golden/demo_v1` and prints drift per field with the producing stage named, plus the
  distributional half. It is part of `make check`.
- Schema-violation rate across a replay is recorded. This is the first real measurement of
  risk R5, and it is the number that says whether decision #13 was right. — **met as a
  measurement, unmet as a number.** The rate is computed, carried on every `StageResult`
  and printed by the replay; what has produced it so far is a scripted subprocess, so it
  says whether the meter works and nothing about the models. It reports "R5 unmeasured"
  rather than 0% when nothing asked a model anything.

**Not in this phase:** kinetic captions (which is what would *render* emphasis), the
perceptual checks of §9.3, and the learner that reads all of this (phase 10).

**How it came out.** The four stages were the small half. What the phase actually taught is
in the seams between them.

**A model stage can fail a job without failing.** `OverlayPlan` validates in isolation, so
an overlay running from 55s to 61s is a well-formed fragment — and an invalid `EditSpec`,
because only the spec knows the source is 60 seconds long. The stage therefore *succeeded*
and the job died at apply time, which §7.4 forbids in so many words. The fix is the shape
`plan_edit` already had for a different reason: the fragment is intent, and `reconcile`
makes it a valid document. The general rule is worth more than the fix — **a fragment
schema cannot see the document it will join, so it is never the last word on validity**,
and every model stage writing into `EditSpec` needs the same step.

**One prompt version stopped working the moment there were five prompts.** Phase 5 spelled
it as an integer to bump by hand, which is fine with one. With five it fails in two
directions at once: forget the bump and the cache serves last week's answer to this week's
prompt, share the integer and editing the overlay instruction re-runs `script_draft`. The
version is now derived from the instruction text itself, which is the same remedy this
codebase reaches for everywhere else — where an invariant is arithmetic, let arithmetic
hold it. There is nothing to remember, so there is nothing to forget.

**The sidecar is the one per-profile model stage, and it changes what a correction costs.**
Everything else a model decides here is aspect-independent on purpose (§4.4.1). Copy is the
honest exception: a 20-second vertical short and a 90-second widescreen demo are two posts,
they say different amounts, and one description would describe neither. The consequence is
that phase 7's cost model needed restating rather than defending — **no planner re-runs, so
the edit is not re-decided; the copy about the result is rewritten, because it is about the
result.** A correction that changes what a viewer hears now costs one model call per
profile. Serving the old sidecar would be §5.2's silent bug in the one place a reader would
actually see it.

**A stage that can never succeed can never be cached, and that is correct.** With no agent
on the machine `metadata` degrades on every run, and §7.4 says a degraded artifact is not
cached — install the agent and the next run must try again. So a fixture job stopped
reporting `did_no_work` on its second run, and six phase-3 cache tests failed. They were
right to: they had been passing because *no model stage ran at all*. Giving them a scripted
agent made them tests of the cache again rather than tests of the absence of one.

**"The first reply" stopped naming a stage.** The phase-5 test stand-in scripted replies by
call order. With five model stages in one job, a test scripting one `EditPlan` silently
handed it to `emphasis`, which runs earlier, and then failed on a degradation it never
asked for. The stand-in now also scripts by *fragment*, keyed on the schema title in the
prompt — which is the only part of a prompt that names its stage.

**`script_draft` could not exist inside its own document's validator.** Decision #20 made a
synthesized narration require a script, and a job that needs `script_draft` has none yet —
so the document the stage was supposed to complete was invalid until after it had run.
The boundary decision #20 actually draws is that the words are yours, and a brief is yours:
`narration.brief` is `Stage.HUMAN`, the validator accepts a script *or* a brief, and
`script_draft` refuses to run without one rather than choosing a subject from a duration and
a cursor track. §1.1 puts written language in scope precisely because it passes through you,
and the brief is where you are.

**A guard the relaxation made necessary.** With a brief-only job now valid, `tts` could be
reached with `narration.script` still null and would happily synthesize the empty string —
a silent wav, rendered as a finished video. That is §7.4's worst failure shape, a job that
looks done, so `tts` names it instead.

**Emphasis returns indices, not words.** A fragment carrying word objects can come back with
the text changed, a timing nudged, or one word quietly missing, and each of those is a
caption that no longer matches the audio — which §9.2 would then report as a real failure.
Indices into a list the prompt numbered cannot express any of it. The hazard traded for is
that both sides must number identically, and that is contained by both sides calling one
function.

**Nothing renders emphasis yet, and that is §6.2's plan rather than an omission.** The first
compiler draws plain timed blocks and ignores the word array; kinetic captions are a later,
purely compiler-side phase. The field is populated now so that phase changes no schema and
invalidates no golden spec — model the end state, render the simple case.

**The golden set has a harness and half a corpus.** The split by origin is real and tested
in both directions: a moved `focus.points[10].x` is a finding that names `ingest`, and a
re-tiered segment is not a finding at all but moves the distribution. What the committed set
cannot do yet is exercise the model half, because the one archived case is the synthetic
fixture and it arrives with a complete spec — promoting a real take is still phase 4's
unfinished build item. Until it lands, the distributional half runs against the scripted
agent in `tests/test_golden_replay.py`, which is the same honest placement §9.2's
round-trip already has one phase earlier.

**Every tolerance in `Tolerances` is a guess.** They are stated in one place with their
provenance said out loud rather than scattered as literals, so that the first real take
moves numbers rather than code.

---

## Phase 10 — Preference learner — **corpus built, learner blocked on a corpus**

**Goal:** close the loop.

Requires ~10–15 accepted real jobs in `accepted_specs`. If they do not exist yet, this
phase is not ready — go and make videos instead.

There are none, so the learner is not built. What has been built is the half that
**cannot wait for them**, because it is the half that has to be recording while the
videos are being made: what was accepted, what it was accepted *under*, and whether it
was real. `make corpus` reports how far off the gate is. See *The corpus, ahead of the
learner* below.

**Build**

- Numeric defaults: windowed median per tunable per profile, with a minimum sample count —
  the §4.3 focus tunables, the §4.6 trim tunables, and each profile's `duration_budget`,
  which is what "cut pacing" concretely means (`architecture.md` §10).
- Exemplar retrieval feeding the phase-5 and phase-9 stages. `plan_edit` benefits most:
  accepted tier assignments are the closest thing this system has to a record of your
  taste, and they are the first lever in risk R4.
- Learner emits a **proposed** diff to `defaults.json` for approval; never auto-applies.
- Every accepted change written to `pref_changes` with its causing jobs.
- Golden-set replay gating: a preference change that moves golden specs beyond tolerance
  is surfaced before it can be accepted.

**Exit criteria**

- Correcting zoom factor the same way across several jobs produces a proposal to move the
  default, and the changelog explains which jobs caused it.
- Shortening `shorts_9x16` by hand across several jobs proposes a lower `duration_budget`
  for that profile and leaves `demo_16x9` untouched. Per-profile learning is the reason
  §4.1 has two layers, and the budget is where it pays off most visibly.
- A job that failed verification contributes nothing. — **met, in the corpus rather than
  in the learner.** `prefs/corpus.py` applies §10.1's rule at the read, and
  `tests/test_corpus.py` asserts a failed job, a synthetic one and a row with no profile
  each contribute nothing and each say so.
- Reverting a preference change restores prior planning behaviour exactly. — **open.**
  Nothing writes a preference change yet.

### The corpus, ahead of the learner

§10.2 is right that a learner built before there is anything to learn from is dead code
that still has to be debugged, and that is why the statistics, `defaults.json` and
exemplar retrieval are absent. But reading phase 10 against what phase 7 actually records
found that **the corpus it will read could not answer either of its first two exit
criteria**, for three independent reasons — and every one of them is a *recording* fault,
which is the kind that cannot be fixed afterwards. "Go and make videos instead" is a
one-way door: fifteen jobs reviewed under the old schema would have produced a corpus
nothing could repair, and the only remedy then is to review all fifteen again.

So this much is built, and it is deliberately not the learner:

- **`accepted_specs` now records the whole profile, not its name** (migration 0004). Every
  tunable §10 moves — `duration_budget`, the §4.3 focus numbers, caption geometry — is a
  `RenderProfile` field and none of them is in the `EditSpec`, so the row recorded what was
  accepted and dropped what it was accepted under. Re-resolving the name later is worse
  than not having it: after the learner's first move `resolve_profile` returns the
  learner's own output, and the corpus would read that back as a preference a person
  expressed. That is §10.1's changelog failure and a feedback loop in one.
- **A correction can address any learnable tunable**, not only the budget
  (`spec/corrections.py`). Exit criterion 1 is a zoom factor corrected the same way across
  several jobs, and `Corrections` had no way to express one — so no amount of real
  reviewing would ever have produced that signal. Which tunables those are is
  `learnable=True` on the field (`spec/profiles.py`), read off the schema by the layer, by
  the diff and by the review page alike, because a list of learnable tunables kept beside
  the code is the copy that goes stale.
- **A take records whether it was recorded or generated** (`source.provenance`, spec v3).
  §10.2 counts *real* jobs, and `ingest/cap_fixture.py` writes a bundle in Cap's own format
  that the real adapter reads — by `accepted_specs` the two are the same document. A corpus
  that counted fixtures would learn the fixture's taste and report it as yours.

`prefs/corpus.py` is where the three meet: it applies §10.1's and §10.2's rules as filters,
counts what each one dropped, and `make corpus` prints how many jobs are still needed. Same
shape as risk R5's meter one phase earlier — say "not yet, by this much" rather than print a
zero that reads like a measurement.

**How it came out.** Three findings, one shape. Each was a place where a document recorded
the *decision* and not the *conditions*, and each was invisible until something downstream
tried to read it back — which is the same failure as the fingerprint that read too much and
the exclusion that expired, one layer up. The general rule the phase leaves behind is that
**a record is only as good as the question it will be asked**, and the time to check that
is before the recording starts, not when the reading does.

The second finding is the one that would have hurt most. It is easy to see that
`accepted_specs` was missing a column; it is much harder to notice that a *signal* has no
way of being expressed, because nothing fails — reviewers simply never produce it, and the
learner arrives to find a corpus in which zoom factor was never corrected and concludes,
correctly and uselessly, that nobody minds.

There was a test for the first finding, and it had the right name.
`test_the_learning_corpus_records_the_profile_it_was_accepted_under` asserted that the
profile's *name* was stored, and went green for three phases. Naming tests as claims is
this repository's convention and it earns its keep — but only where the body checks the
claim the name makes, and a test that asserts less than its name fails silently by
passing. It is the same shape as the finding it failed to catch: something recorded the
decision and not the conditions.

The golden set caught the spec change on the first replay and named its stage
(`source.provenance: 'unknown' -> 'synthetic' (ingest)`), which is exactly the strict half
of §11.1 doing its job. `golden/demo_v1` is a recipe, so re-approving it was re-running the
recipe rather than hand-editing an artifact.

---

## Later phases

Unordered. Pull them in when the need is felt, not on a schedule.

| Phase | Trigger |
|---|---|
| **Kinetic captions** — **built** | Trigger fired: plain blocks look plain at a short's pace. It was purely a compiler change, exactly as predicted — see below. |
| **Overlay preview in review UI** | When you know which corrections you make most often and can optimize for them. |
| **VLM perceptual verification** | When real failures are slipping past the deterministic checks. Goes through the phase-5 adapter — the agent CLI takes image paths in print mode, so there is no new mechanism to build. |
| **MLT export and re-ingest** | The first time you want Kdenlive for something the spec cannot express. |
| **`still_4x5` profile** | When photo posts are actually being made; may turn out that `shorts_9x16` suffices. |
| **`RemoteRunner`** | When local inference becomes the bottleneck, or phase 0/8 forces it earlier. |
| **Multi-take assembly** | When re-recording a section and stitching it in is something you actually want (decision #24). A `source_id` on `removals` and `segments`, plus a compiler that concatenates across takes — a schema migration and a compiler change, not a redesign. |

### Kinetic captions — how it came out

The one phase in this table whose trigger had fired, and the cheapest thing left that is
not gated on a real recording. §6.2 predicted in phase 1 that it would change no schema,
invalidate no golden spec and need no migration, and that is what happened: `golden replay`
came back with zero drift on every field, because a `RenderProfile` is not an `EditSpec`.
`CaptionBlock` has carried per-word timings since phase 1 for exactly this.

`compile/captions.py` grew a second renderer over the same block: one ASS event per
*active-word window* instead of one per block, with the spoken word in amber. Both
renderers go through one wrap — `wrap_indices` returns the word indices per line and `wrap`
is that joined back up — because the kinetic path has to decorate one word and leave every
other character where the plain path put it. Two wrappers would have been "one formula
written twice", and §9.1's `caption_line_length` and `caption_lines` read the same call the
renderer does, so a caption cannot pass the check and render over its box.

**Colour is the only channel, and that is arithmetic rather than taste.** A highlight that
changed weight or scale changes glyph advance, the line re-wraps under it, and the whole
caption jitters once per word. The mechanism check `next-phase` asks for is what settled
it: a five-second `lavfi` render of one line under three mid-line `\1c` overrides came back
with an identical ink bounding box in all three frames, which is the property the design
needs and the one a comment claiming it would not have proved.

Two things came out of building it that the plan did not know:

- **A window can be real in the projection and empty in the file.** ASS times are
  centiseconds, so two word starts 3ms apart print identically and the event between them
  has `start == end` — which libass draws as a one-frame flicker or not at all. The guard
  compares the *rendered timestamps* rather than the floats, which is the same remedy as
  comparing the safe area in pixels: decide in the units the thing actually happens in. The
  spans are contiguous, so dropping one leaves its neighbours meeting at the timestamp it
  would have printed, and the caption has no hole — it just never lights that word.
- **`Word.emphasis` had never reached a pixel.** `emphasis` has written it since phase 9
  and `compile` has projected it into `EditedWord` since phase 2, and nothing rendered it:
  a model stage whose entire output was invisible, so phase 5's stop-and-reassess gate
  could never have been applied to it. It is now a second hue, in both renderers — a
  different one from the active word, because the two are on screen together and say
  different things. Same family as the check that never fires and the record that cannot
  answer its question: **something a stage produces that nothing reads is not a feature
  waiting for a consumer, it is an unmeasured stage.**

A word stays lit until the *next* one begins rather than going dark when it ends. The gaps
between spoken words are tens of milliseconds and de-highlighting across each one strobes;
it also means the last word holds through whatever `_hold_minimum` added to a block a cut
left too short to read, instead of the block hanging there with nothing lit.

`demo_16x9` did not opt in, and its ASS file is byte-identical before and after — the
check that says this change is per profile rather than global. Its `compile` cache key
moved anyway, because the fingerprint hashes the whole profile and the profile grew a
field. That is the fingerprint being right at the cost of one re-encode, not a bug.

---

## Dependency graph

```mermaid
flowchart LR
    P0[0 Spike] --> P1[1 Spec]
    P1 --> P2[2 Compile + render]
    P2 --> P3[3 Runner + cache]
    P0 --> P4
    P3 --> P4[4 Real ingest]
    P4 --> P5["5 Editorial ★"]
    P5 --> P6[6 Verify]
    P6 --> P7[7 Review UI]
    P6 --> P8[8 Voice]
    P7 --> P9[9 Model stages]
    P8 --> P9
    P7 --> P10[10 Learner]
    P9 --> P10
```

Phases 7 and 8 are independent of each other and can be done in either order. Everything
else is a hard dependency.

Three ★ milestones, and they answer three different questions. Phase 2 asks *can it
render*. Phase 4 asks *is it useful*. Phase 5 asks *is it the thing we said it was* — and
that one is the only one whose answer can send you back to the drawing board rather than
back to the code.
