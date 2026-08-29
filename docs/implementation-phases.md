# screencut — Implementation Phases

Companion to [`architecture.md`](architecture.md), which holds the design and the
reasoning. This document holds only the order of work and what "done" means at each step.

Nothing here is built yet. Phases are sized to be picked up cold: each states its goal,
what gets built, how you know it is finished, and what is deliberately excluded.

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

Not a coding phase. Half a day of finding out whether the stack works on this machine.

**Build**

- Record a real take with Cap (and/or Screenize) and try to extract cursor + click events.
  Document the actual format, sample rate, and coordinate space.
- Run each candidate ASR backend on a sample: `mlx-whisper`, `whisper.cpp`, and
  faster-whisper. Time them. Confirm the CTranslate2/MPS situation firsthand.
- Run WhisperX's wav2vec2 alignment model under MPS. Confirm it works and time it.
- Install F5-TTS and synthesize thirty seconds. Note whether MPS works, whether
  `PYTORCH_ENABLE_MPS_FALLBACK=1` is needed, and how long it takes.
- Confirm `h264_videotoolbox` is available in the installed FFmpeg.
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

**Not in this phase:** any pipeline code.

---

## Phase 1 — Spec and fixtures

**Goal:** the data model everything else is written against.

**Build**

- Pydantic models: `EditSpec`, `RenderProfile`, `FocusTrack`, `EditDecisions` (`cuts` and
  `transcript_edits` — `architecture.md` §4.4), `CaptionBlock` (carrying per-word timings
  from the start — §6.2), `OverlayIntent`, `AudioTrack`.
- `EditDecisions` validators that make an impossible cut unrepresentable: segments inside
  source bounds, non-overlapping, non-inverted. Cheap here, and it is half of risk R5's
  mitigation.
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

## Phase 2 — Compiler and render ★

**★ The load-bearing milestone.** Everything downstream assumes this works.

**Goal:** a watchable video out of a synthetic fixture, in both aspect ratios.

**Build**

- `plan_focus`: `FocusTrack` → zoom keyframes (16:9) and crop path (9:16), with the
  tunables from `architecture.md` §4.3 read from `constraints.yaml`.
- FFmpeg compiler: `EditSpec` + `RenderProfile` → filter graph. Zoom/crop, plain ASS
  caption blocks, overlay PNG compositing, audio mix with ducking, loudness normalization.
- SVG overlay templates (callout arrow, highlight box, label chip, progress pill) rendered
  to PNG at target resolution.
- VideoToolbox encode path plus a software-encode path for reproducible golden renders.

**Exit criteria**

- One fixture renders to both profiles and both are watchable.
- Zoom lands on the click clusters; the 9:16 crop follows the focus point without judder.
- Captions burn in, correctly timed, inside the safe area for each profile.
- Software-encoded renders are byte-identical across two runs.

**Not in this phase:** kinetic captions, the cache, any model, real media.

---

## Phase 3 — Runner, cache, and persistence

**Goal:** make re-running cheap, which is what makes the review loop possible at all.

**Build**

- Stage contract: `(inputs, params) -> artifact` as a CLI taking JSON on stdin.
- `LocalRunner` (subprocess). Define the `Runner` interface such that `RemoteRunner` is a
  drop-in; do not build it.
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

- Agent-CLI adapter in `runner/`: the §7.3 invocation (print mode, JSON events, tools
  disabled, fixed cwd), schema into the prompt, fragment validated back out, retry once on
  a validation error, degrade per §7.4. One adapter, reused by every later model stage —
  this is the phase that pays for phases 9 and later.
- `plan_edit` — **cuts**: transcript plus word timings plus a `FocusTrack` summary in,
  `EditDecisions.cuts` out. Dead air, restarts, and dwell on nothing.
- `plan_edit` — **transcript cleanup**: disfluencies as timing ranges, never as rewritten
  text (§4.4).
- Compiler support for a cut timeline: segments concatenated, `FocusTrack` and caption
  timings remapped into edited time.
- Cut integrity checks from §9.1 — the arithmetic ones, ahead of the rest of verification,
  because they bound what a bad fragment can do.

**Exit criteria**

- A real take comes out shorter than it went in, and the cuts are ones you would have made.
- Removing a disfluency removes it from *both* the audio and the caption, at the same
  instants, with no clipped adjacent word.
- Killing the network mid-job still produces a render — the uncut, verbatim one from §7.4.
- Re-running with an unchanged transcript hits the cache and calls no model. If it does not,
  phase 3's key is wrong (§5.2), and everything after this gets slow and expensive.

**This is a stop-and-reassess gate.** If the cuts are not good, risk R4 has landed: work
the levers in R4's order — exemplars, then a stage gate, then narrowing `plan_edit` to
silence-trimming with substantive cuts left manual. Do not carry a bad `plan_edit` forward
on the assumption that later phases improve it. They do not; they decorate it.

**Not in this phase:** emphasis, overlays, metadata — those are phase 9, and they are
decoration on top of this.

---

## Phase 6 — Verification

**Goal:** stop garbage reaching a person.

**Build**

- Deterministic checks (`architecture.md` §9.1): render integrity, duration, loudness and
  true peak, caption overlap / duration / line length / safe area, overlay occlusion,
  crop-path continuity. Cut integrity already exists from phase 5; fold it into the same
  report.
- Transcript round-trip: open-transcribe the rendered audio, diff against the
  **post-`plan_edit` expected transcript**, classify each difference per §9.2's three
  classes, report WER over the third class only. Diffing against the raw transcript would
  flag every successful phase-5 edit as a failure.
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
- Cut review specifically: the `EditDecisions` segments with their `reason` (§4.4), so a
  cut can be reinstated or extended without leaving the form. Corrections to cuts are the
  ones most worth capturing — they are the phase-5 feedback signal and the input to §10's
  cut-pacing defaults.
- Any §7.4 degradation shown prominently. Under decision #12 this page is the only place a
  degraded job announces itself.
- Accept / reject, writing accepted specs and the proposed→corrected diff to SQLite.
- Generated TypeScript types from phase 1 wired into the frontend.

**Exit criteria**

- A correction round trip — edit, re-render, accept — completes in seconds, not minutes,
  because the cache holds.
- Adjusting a cut re-runs compile and render and **does not re-run `plan_edit`**. If it
  does, the review loop costs a model call per correction and will be abandoned.
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
  is where `RemoteRunner` gets built instead.
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
- Golden-set replay across the model stages, parallelized per §7.5.

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
  including cut pacing, which phase 5 made a real quantity (`architecture.md` §10).
- Exemplar retrieval feeding the phase-5 and phase-9 stages. `plan_edit` benefits most:
  accepted cut decisions are the closest thing this system has to a record of your taste,
  and they are the first lever in risk R4.
- Learner emits a **proposed** diff to `defaults.json` for approval; never auto-applies.
- Every accepted change written to `pref_changes` with its causing jobs.
- Golden-set replay gating: a preference change that moves golden specs beyond tolerance
  is surfaced before it can be accepted.

**Exit criteria**

- Correcting zoom factor the same way across several jobs produces a proposal to move the
  default, and the changelog explains which jobs caused it.
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
