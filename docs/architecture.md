# screencut — Design & Architecture

Status: design, pre-implementation. Nothing in this document is built yet.

## 1. What this is

A pipeline that turns a raw screen recording (or a still image) into publish-ready
video: narrated, captioned, auto-zoomed, reframed for multiple aspect ratios — with a
review loop that captures corrections as structured diffs and feeds them back as
learned preferences.

**In scope:** capture ingest, narration, captioning, auto-zoom/reframe, overlay
generation, rendering, automated verification, human review, preference learning.

**Out of scope:** recording itself (an external recorder produces the input), and
publishing (the pipeline ends at a file plus a metadata sidecar; posting is manual).

Single user, single style. No multi-tenancy, no auth, no billing, no job queue beyond
what one person running one job at a time needs.

## 2. Decisions

Recorded so future-us knows what was chosen deliberately versus what merely happened.

| # | Decision | Choice | Consequence |
|---|---|---|---|
| 1 | Users | Single user, one style | No tenancy/auth/quota layers. Preferences are one directory. |
| 2 | Compute location | Undecided | Every heavy stage is a CLI contract behind a `Runner`. Only `LocalRunner` gets built. |
| 3 | Renderer | FFmpeg primary, MLT export | One renderer, no parity guarantee to maintain. See §6. |
| 4 | Correction capture | Web review UI editing the spec | Corrections are structural diffs, which is what makes §10 possible. |
| 5 | Cursor events | Available from recorder | Auto-zoom and reframing are arithmetic, not inference. See §4.3. |
| 6 | Outputs | 9:16, 16:9, both from one source, stills | Forces the two-layer spec in §4.1. |
| 7 | Language | Python core, Pydantic → JSON Schema → TS | One spec definition serves validation, the UI types, *and* LLM output constraint. |
| 8 | Script source | Supplied or AI-drafted | `narration.script` is optional; absence triggers a draft stage. |
| 9 | Publishing | File on disk | No platform APIs, no OAuth, no scheduling subsystem. |
| 10 | Fixtures | Synthetic first | Ingest is an adapter boundary. See risk R1. |
| 11 | Overlays | Generated from templates per video | SVG re-renders correctly across aspect ratios; a bitmap library would not. See risk R2. |
| 12 | Autonomy | Full auto, review the finished render | Fastest feedback per job; cleanest diffs. Costs compute on bad scripts. |
| 13 | LLM stages | Anthropic API, `claude-opus-5` | See §7. |
| 14 | Captions | Plain blocks first, kinetic later | Spec carries word timings from day one; only the compiler changes later. See §6.2. |
| 15 | Review UX | Form + re-render; overlay preview later | Puts the whole weight on the stage cache. See §8. |
| 16 | State | SQLite + files on disk | Media in per-job directories, records and cache index in SQLite. See §5.4. |
| 17 | Hardware | Apple Silicon | ASR backend must be swappable; cache becomes load-bearing; VideoToolbox for encode. See §16. |

## 3. Principles

1. **The spec is the system.** Planner, renderer, verifier, review UI, and learner all
   read and write one versioned document. Everything else is a detail.
2. **The model proposes; deterministic code disposes.** LLM stages emit typed,
   schema-validated spec fragments. They never emit FFmpeg arguments, never emit
   coordinates in pixels, and never render anything.
3. **Prefer arithmetic to inference.** Where a decision can be computed from data
   (cursor events, audio levels, word timings), compute it. Reserve the model for
   decisions that are genuinely about taste or language.
4. **Every stage is cacheable and replayable.** The review loop is iterative by design.
   If a caption tweak re-synthesizes the voiceover, corrections become expensive and
   the loop dies.

## 4. Core data model

### 4.1 Two layers

Producing 9:16, 16:9 and stills from one source makes a single flat spec impossible.

**`EditSpec`** — aspect-agnostic. Sources, in/out points, `FocusTrack`, narration
segments with word timings, emphasis markers, overlay intents, audio levels. All spatial
values normalized to `0.0–1.0` in source coordinates. All temporal values in seconds
from source start. **No pixels anywhere.** This is what the planner produces, what the
review UI edits, and what the learner diffs.

**`RenderProfile`** — `shorts_9x16`, `demo_16x9`, `still_4x5`. Resolution, framerate,
safe-area insets, caption box geometry and type scale, encode settings, and the
projection rule that turns a `FocusTrack` into either zoom keyframes or a crop path.

One `EditSpec` × N `RenderProfile` = N renders.

Preferences are learned **per profile**. Caption Y in vertical is a different number from
caption Y in widescreen; a single global default averages them into a value wrong for
both.

### 4.2 Versioning

`EditSpec` carries `spec_version` from the first commit, with explicit migration
functions between versions. The golden set (§11) will outlive several schema changes,
and v1 golden specs that no longer load are a golden set silently lost.

### 4.3 FocusTrack

A time series of `(t, x, y, weight, kind)` in normalized source coordinates, where
`kind` distinguishes movement, click, dwell, and manual annotation.

This single structure serves all three output types:

- **16:9 demo** — cluster clicks in time and space; emit zoom keyframes over dwell regions
- **9:16 short** — the same track becomes a crop path; vertical reframing of a widescreen
  recording is largely solved once attention position is known
- **Stills** — no cursor, so a saliency pass (or a manual point) yields a track of one or
  two entries, and Ken Burns is a pan along it

Modelling this as `FocusTrack` rather than "cursor zoom" is what makes the photo path
the degenerate case of the video path rather than a parallel implementation.

Planning from a `FocusTrack` is deterministic, governed by a handful of tunables:

| Tunable | Role |
|---|---|
| `zoom_factor` | Magnification at a dwell region |
| `min_dwell_ms` | Ignore transient cursor passes |
| `min_gap_ms` | Suppress rapid zoom oscillation |
| `ease_ms` | In/out transition duration |
| `crop_lag` | How far the 9:16 crop trails the focus point |
| `max_crop_delta_per_frame` | Ceiling that prevents judder |

Every one is a scalar the preference store can learn by median. No model participates in
the highest-impact decision in the pipeline.

## 5. Pipeline

```mermaid
flowchart TD
    ingest[ingest] --> focus[plan_focus]
    ingest --> draft["script_draft (conditional)"]
    draft --> tts[tts]
    tts --> align[align]
    align --> caps[plan_captions]
    focus --> caps
    align --> stick[plan_overlays]
    focus --> stick
    caps --> compile[compile per profile]
    stick --> compile
    focus --> compile
    compile --> render[render]
    render --> verify[verify]
    verify --> review[review UI]
```

`plan_focus` has no audio dependency and runs parallel to TTS. Everything else waits on
`align`, because narration timing drives caption timing and edit pacing.

### 5.1 Stage contract

Each stage is a pure function `(inputs, params) -> artifact`, exposed as a CLI taking
JSON on stdin and file paths as arguments. This is the seam that defers decision #2:
`LocalRunner` shells out to a subprocess; a future `RemoteRunner` ships inputs to a GPU
worker and retrieves outputs. Pipeline code is identical under both.

Build only `LocalRunner`.

### 5.2 Caching

Content-addressed, keyed on `(stage_name, input_hash, params_hash)`. Non-negotiable — see
principle 4.

### 5.3 The two ASR calls are different

Conflating them is a real bug waiting to happen.

**`align`** runs WhisperX in **forced alignment** mode against the known script text. F5-TTS
does not return reliable word timestamps, so alignment is required even when the script
was supplied. Because the text is ground truth, this is substantially more accurate than
open transcription.

**`verify`** runs **open transcription** on the *final rendered audio* and diffs against the
script. Same library, opposite purpose: one produces timings, the other independently
checks that the render did not lie.

### 5.4 Persistence

Media and artifacts are files, in per-job directories. Records are rows, in SQLite.

```
data/
  jobs/<job_id>/
    source/        raw take, sidecar events
    stages/        per-stage outputs, named by cache key
    renders/       final files + metadata sidecars
    spec.json      current EditSpec
    spec.proposed.json
  screencut.db
```

SQLite holds four things, none of them large:

| Table | Purpose |
|---|---|
| `jobs` | Job record, status, spec version, degradations recorded by §7.4 |
| `stage_cache` | Cache key → artifact path, so lookup is a query and not a directory walk |
| `accepted_specs` | The learning corpus: accepted `EditSpec`s with the profile they were accepted under |
| `pref_changes` | Changelog of every learned-default move and the jobs that caused it (§10.1) |

Never put media in the database. The reason for having one at all is that both the
learner and the review UI want queries — "median `zoom_factor` over the last 20 accepted
jobs in `shorts_9x16`" is a query, and retrofitting a database once the golden set matters
is worse than starting with one.

## 6. Rendering

### 6.1 One renderer

**FFmpeg is the only renderer.** `compile` turns `EditSpec + RenderProfile` into a filter
graph. Zoom and crop become `zoompan`/`crop` expressions driven by the projected
`FocusTrack`; captions burn in from generated ASS; overlays composite from
template-rendered PNGs; audio ducking and loudness normalization run in the same graph.

**MLT XML is a one-way export, not a second renderer.** When a job needs something the
spec cannot express, export to MLT, hand-edit in Kdenlive, and re-ingest the modified XML
back into an `EditSpec`. There is deliberately **no parity guarantee** between the MLT
export and the FFmpeg render — declaring one would mean maintaining two renderers and
debugging their divergence forever. The review UI is the normal correction path;
Kdenlive is the escape hatch.

### 6.2 Captions

`plan_captions` emits `CaptionBlock`s that **always carry per-word timings**, because
`align` produces them anyway and the cost of carrying them is zero.

The first compiler renders plain timed ASS blocks and ignores the word array. Kinetic
word-highlight rendering is a later, purely-compiler-side phase: per-word ASS override
tags with active-word colouring. Because the spec already carries the data, that phase
changes no schema, invalidates no golden specs, and needs no migration.

This is the general shape to aim for — **model the end state, render the simple case** —
and it is why the load-bearing milestone in the phase plan does not have hand-authored per-word ASS
timing in its path.

### 6.3 Overlays

Overlays are **parameterized templates**, not free-form generation. A small fixed set —
callout arrow, highlight box, label chip, progress pill — rendered deterministically from
SVG to PNG at the target profile's resolution.

The LLM chooses *template + anchor + text*. It does not invent layouts. Free-form
generation would be unpredictable, untestable, and unlearnable, and under full autonomy
(#12) every instance would be discovered at review time.

## 7. LLM stages

Provider: **Anthropic API**, model `claude-opus-5`, via the official `anthropic` Python SDK.

### 7.1 Which stages use a model

| Stage | Model? | Rationale |
|---|---|---|
| `plan_focus` | No | Arithmetic over `FocusTrack` |
| `plan_captions` (timing, layout) | No | Derived from `align` output and profile geometry |
| Audio levels, ducking | No | Loudness measurement |
| `script_draft` | Yes | Language |
| Emphasis word selection | Yes | Taste |
| `plan_overlays` (template + anchor + text) | Yes | Taste, bounded by the template set |
| Metadata sidecar (title, description, tags) | Yes | Language |

### 7.2 Structured output is the whole trick

Because the spec is already Pydantic, LLM stages return validated spec fragments
directly:

```python
response = client.messages.parse(
    model="claude-opus-5",
    max_tokens=16000,
    thinking={"type": "adaptive"},
    messages=[...],
    output_format=OverlayPlan,   # a Pydantic model from spec/
)
plan = response.parsed_output    # a validated OverlayPlan
```

One definition constrains the model's output, validates it, generates the review UI's
TypeScript types, and defines what the learner diffs. A model that cannot emit an invalid
overlay anchor is worth more than any amount of prompt engineering telling it not to.

Adaptive thinking is the default; `output_config.effort` is tuned per stage — higher for
script drafting, lower for overlay placement. Stages with long output stream and use
`.get_final_message()`.

### 7.3 Prompt caching

The stable prefix is: system prompt, `constraints.yaml`, and the retrieved exemplar set.
Per-job content (the transcript, the `FocusTrack` summary) goes after the last cache
breakpoint. Confirm it is working by asserting `usage.cache_read_input_tokens` is nonzero
across repeated runs — a silent invalidator (a timestamp, unsorted JSON, a varying tool
list) shows up as a cost regression and nothing else.

### 7.4 Failure handling

An LLM stage failure must not fail the job. Each degrades to a deterministic default —
no overlays, no emphasis, script-derived metadata — and records the degradation in the
job record so review shows it. Verification still runs.

Check `stop_reason` before reading content; a refusal is an HTTP 200, not an exception.
Enable server-side fallbacks (`fallbacks: "default"` with beta
`server-side-fallback-2026-07-01`) so a classifier decline routes rather than drops the
stage. Catch typed exceptions most-specific-first (`RateLimitError` before `APIStatusError`
before `APIConnectionError`) rather than one broad class.

### 7.5 Golden-set replay

Replaying the golden set (§11) through the LLM stages is a non-latency-sensitive batch
workload — exactly what the Message Batches API is for, at half the cost.

## 8. Review UI

FastAPI serving a single page per job: the rendered video for each profile, the proposed
`EditSpec` as an editable form, and a re-render button.

Corrections are field edits. Committing writes a corrected spec, re-runs only the
invalidated stages, and re-renders. **This design puts the entire burden on the stage
cache** — if a caption-position tweak re-synthesizes narration, the loop is unusable and
the corrections that feed §10 stop happening. Cache correctness is a review-UI
requirement, not an optimization.

A later phase adds static-frame overlay preview: scrub to a frame, see captions and
overlays composited on canvas, drag to reposition, re-render only for motion and audio.
Deferred deliberately — which corrections are made most often is not yet known, and
building the preview before knowing that optimizes the wrong interaction.

Full live preview is explicitly not a goal. That is a browser NLE, which is more work
than the rest of this pipeline combined.

## 9. Verification

Three layers. The first two exist to keep garbage from reaching a person.

### 9.1 Deterministic

- Render exit clean; duration within tolerance of the spec
- Integrated loudness −14 LUFS, true peak below −1 dBTP, dialogue-to-bed ratio above threshold
- Caption blocks: no mutual overlap, minimum display duration, maximum characters per line,
  fully inside the profile's safe area
- Overlay anchors inside safe area and not occluding a caption box
- **Crop-path continuity** — no crop delta above `max_crop_delta_per_frame`

That last check earns its place: judder is *the* failure mode of automated vertical
reframing, it is invisible in a still frame, and it is catchable with arithmetic.

### 9.2 Objective semantic

Open-transcribe the rendered audio and diff against the script (§5.3). One check that
catches TTS mispronunciation, audio/video desync, a wrong take, truncated narration, and
captions that drifted — all otherwise invisible until a human watches.

### 9.3 Perceptual

A VLM over sampled frames, only for questions text cannot answer: is a caption sitting on
the UI element it describes, is the zoom framing the cursor or empty space.

## 10. Preference learning

Three tiers, none of them fine-tuning. Volume is too low, feedback too slow, and
auditability matters more than marginal accuracy.

**Hard constraints** (`prefs/constraints.yaml`) — hand-written, never auto-modified.
Fonts, forbidden voices, fixed output dimensions.

**Learned numeric defaults** (`prefs/defaults.json`) — per render profile. Rolling median
over accepted specs for every tunable in §4.3 plus caption geometry, music level, and cut
pacing. Plain statistics. Given decision #5, this tier does most of the work.

**Exemplars** (`prefs/exemplars/`) — accepted specs retrieved by similarity and few-shot
into the LLM stages. Only relevant to §7.1's model-using stages.

### 10.1 Update rules

- Never learn from a job that failed verification. A broken render's corrections poison
  the defaults.
- Require a minimum sample count before a default moves. One-off corrections are noise.
- Use a windowed median, so an old preference can be superseded.
- Every default change is written to a changelog with the jobs that caused it. "It got
  worse and nobody can explain why" is the characteristic failure of self-tuning systems.

### 10.2 Bootstrapping

With synthetic fixtures and zero accepted jobs there is nothing to learn from. Ship
hand-written values in `constraints.yaml`; the learner activates after ~10–15 accepted
real jobs. **Built earlier, it is dead code that still has to be debugged** — hence its
position late in the phase plan.

## 11. Golden set

Archived fixtures plus their approved `EditSpec`s, in `golden/`.

Renders are slow, so regression compares **specs, not pixels**: replan each fixture and
diff the proposed spec against the approved one field-by-field with per-field tolerances.
Render only two or three for frame hashing.

Any change to prompts, rules, or learned defaults replays the full set before taking
effect. Preference updates are code changes: versioned, diffable, revertable.

At least one fixture is **deliberately bad** — clipped audio, overlapping captions,
juddering crop — so §9's checks are tested against known-bad rather than only known-good.

## 12. Repository layout

```
screencut/
  spec/         Pydantic models, JSON Schema emit, migrations
  ingest/       Recorder adapters -> RawTake; synthetic fixture generator
  plan/         Deterministic planners; LLM planners
  synth/        TTS and ASR stages (CLI contracts)
  compile/      EditSpec + RenderProfile -> FFmpeg graph; MLT export; MLT re-ingest
  verify/       Deterministic checks, transcript round-trip, frame sampling
  prefs/        constraints.yaml, defaults.json, exemplars/
  review/       FastAPI + web UI
  golden/       Fixtures, approved specs, replay harness
  runner/       LocalRunner, cache index, SQLite schema + migrations
  docs/
data/           gitignored — per-job directories and screencut.db (§5.4)
```

## 13. Build sequence

See [`implementation-phases.md`](implementation-phases.md).

## 14. Risks

**R1 — FocusTrack rests on an unverified assumption.** The recorder is believed to export
cursor and click events, but no recording has been obtained yet, and the whole
zoom/reframe/Ken-Burns design depends on it.

*Mitigation:* `FocusTrack` is our format, not the recorder's; ingest is an adapter from
day one. Synthetic fixtures assume only sampled `(t, x, y)` plus click timestamps — the
floor any recorder could plausibly provide. Deliberately do **not** design around window
bounds, keystroke events, or scroll deltas even if the recorder turns out to expose them.
Building on the richest plausible data and receiving the poorest means rewriting the
planner; building on the floor and receiving more means extending it.

**R2 — Generated overlays under full autonomy.** The most open-ended component combined
with no stage gate.

*Mitigation:* the fixed template set in §6.3. If overlay quality is still poor at review
time, the next lever is a gate on `plan_overlays` specifically, not on the whole pipeline.

**R3 — Learner drift.** Addressed by §10.1 and §11; listed here because it is the failure
mode most likely to be noticed late.

## 15. Open questions

- Review UI across profiles: one job with a shared `EditSpec` and per-profile caption
  geometry, or two independent approvals? Current lean is one job, since most corrections
  apply to the shared spec.
- Music beds: templates generate visual overlays, but audio beds still need a source.
- Whether `still_4x5` is worth a distinct profile or whether stills reuse `shorts_9x16`.

## 16. Platform notes — Apple Silicon

The target machine is Apple Silicon. Three consequences, all of which want verifying in
phase 0 rather than trusting this document.

**The transcription backend must be swappable.** WhisperX's default backend is
faster-whisper, which is built on CTranslate2 — and CTranslate2 has no Metal/MPS backend,
so on Apple Silicon it runs on CPU. `mlx-whisper` and `whisper.cpp` both have Metal
acceleration and are the likely choices here. This is exactly the payoff of the §5.1 CLI
seam: the ASR backend is a swap of one executable, not a refactor.

Note that WhisperX is two things — a transcription backend and a wav2vec2 forced-alignment
model. The alignment half is ordinary PyTorch and can use MPS; only the transcription half
has the CTranslate2 problem. They can be sourced independently.

**F5-TTS on MPS is the open question.** It is PyTorch, so it should run, but op coverage
under MPS varies and unsupported ops either fail or fall back to CPU
(`PYTORCH_ENABLE_MPS_FALLBACK=1`) at a speed cost. This is the single most likely reason
to end up wanting a remote GPU — which is what §5.1's `Runner` exists to absorb. Phase 0
answers it.

**Use VideoToolbox for encode.** FFmpeg's `h264_videotoolbox` / `hevc_videotoolbox`
hardware encoders are a straightforward render-time win on macOS. Keep a software-encode
path for the golden set, though: hardware encoders are not bit-reproducible across
machines or OS versions, and §11's frame hashing needs determinism.

**The cache stops being an optimization.** Slower local inference means the difference
between a cached and uncached correction cycle is the difference between a usable review
loop and an abandoned one.
