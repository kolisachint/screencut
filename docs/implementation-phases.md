# screencut — Implementation Phases

Companion to [`architecture.md`](architecture.md), which holds the design and the
reasoning. This document holds only the order of work and what "done" means at each step.

Phases 1 to 3 are built; phase 0 is a spike on the target machine and has not been run. Phases
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

## Phase 0 — Environment spike

**Goal:** replace assumptions with facts before any of them is load-bearing.

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

## Phase 4 — Real ingest and transcription ★

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

- A real recording produces both renders, unattended, from one command.
- Zoom behaviour on real cursor data is sane — this is the first test of the phase-2
  tunables against reality, and expect to retune them.
- The output is something you would actually post. If it is not, stop and fix that before
  continuing; every later phase assumes this baseline is good.

**Not in this phase:** synthesized voice, cuts, verification, review UI.

---

## Phase 5 — Editorial pass ★

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
  acceptable on its own.
- `plan_edit` on top of it produces cuts you would have made, and its overrides of `trim`
  are defensible — check the override rate on the report (§9.1).
- One `EditSpec` renders at two different lengths under two `duration_budget` values, and
  both are coherent — the short is not just the demo with its ending missing.
- Killing the network mid-job still produces a render: the `trim`-only one, all segments
  `essential`.
- Re-running with an unchanged transcript hits the cache and calls no model. If it does not,
  phase 3's key is wrong (§5.2), and everything after this gets slow and expensive.

**This is a stop-and-reassess gate,** and decision #19 gives it a much better shape than a
single pass/fail: `trim` and `plan_edit` can be judged separately. If `trim` is good and
`plan_edit` adds nothing, ship `trim` and leave tiering manual — that is a real product.
If `trim` itself is wrong, that is tunables in `constraints.yaml`, not an architecture
problem. Work R4's levers in order and do not carry a bad `plan_edit` forward on the
assumption that later phases improve it. They do not; they decorate it.

**Not in this phase:** emphasis, overlays, metadata — those are phase 9, and they are
decoration on top of this.

---

## Phase 6 — Verification

**Goal:** stop garbage reaching a person.

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

- Every check fires on the broken fixture and none fires on the good one.
- A correctly edited job reports zero real differences despite the rendered audio
  differing from the raw transcript. This is the check on the check, and it is the one
  that decides whether §9.2 stays useful once phase 5 exists.
- The report is legible enough to act on without reading the code.

**Not in this phase:** the VLM perceptual layer. Add it once you know which real failures
the deterministic checks miss.

---

## Phase 7 — Review UI

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
  because the cache holds.
- Adjusting a removal, a tier, or a budget re-runs compile and render and **re-runs no
  planner at all** — not `plan_edit`, not `plan_captions`, not `plan_overlays`. This is
  §4.5's whole payoff, and if it does not hold, the review loop costs a model call per
  correction and will be abandoned.
- `accepted_specs` and the diff record populate correctly.

**Not in this phase:** overlay preview, live playback.

---

## Phase 8 — Voice synthesis

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

- Script in, narrated and captioned video out.
- Phase 6's transcript round-trip passes on synthesized narration — this is the check that
  catches TTS mispronunciation, and it is the reason verification comes first.
- `plan_edit`'s cleanup half no-ops on synthesized narration, which has no disfluencies to
  remove. If it starts cutting clean speech, that is a phase-5 prompt problem surfacing on
  new input, and it is worth knowing before the learner starts averaging over it.

---

## Phase 9 — Remaining model stages

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
- Golden-set replay runs and reports per-field spec drift.
- Schema-violation rate across a replay is recorded. This is the first real measurement of
  risk R5, and it is the number that says whether decision #13 was right.

---

## Phase 10 — Preference learner

**Goal:** close the loop.

Requires ~10–15 accepted real jobs in `accepted_specs`. If they do not exist yet, this
phase is not ready — go and make videos instead.

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
- A job that failed verification contributes nothing.
- Reverting a preference change restores prior planning behaviour exactly.

---

## Later phases

Unordered. Pull them in when the need is felt, not on a schedule.

| Phase | Trigger |
|---|---|
| **Kinetic captions** | When plain blocks look too plain for shorts. Purely a compiler change — the spec already carries word timings, so no migration and no golden-spec churn. |
| **Overlay preview in review UI** | When you know which corrections you make most often and can optimize for them. |
| **VLM perceptual verification** | When real failures are slipping past the deterministic checks. Goes through the phase-5 adapter — the agent CLI takes image paths in print mode, so there is no new mechanism to build. |
| **MLT export and re-ingest** | The first time you want Kdenlive for something the spec cannot express. |
| **`still_4x5` profile** | When photo posts are actually being made; may turn out that `shorts_9x16` suffices. |
| **`RemoteRunner`** | When local inference becomes the bottleneck, or phase 0/8 forces it earlier. |
| **Multi-take assembly** | When re-recording a section and stitching it in is something you actually want (decision #24). A `source_id` on `removals` and `segments`, plus a compiler that concatenates across takes — a schema migration and a compiler change, not a redesign. |

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
