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
| 4 | Correction capture | Web review UI editing the spec | Corrections are structural diffs, which is what makes §9 possible. |
| 5 | Cursor events | Available from recorder | Auto-zoom and reframing are arithmetic, not inference. See §4.3. |
| 6 | Outputs | 9:16, 16:9, both from one source, stills | Forces the two-layer spec in §4.1. |
| 7 | Language | Python core, Pydantic → JSON Schema → TS | One spec definition serves validation, the UI types, *and* LLM output constraint. |
| 8 | Script source | Supplied or AI-drafted | `narration.script` is optional; absence triggers a draft stage. |
| 9 | Publishing | File on disk | No platform APIs, no OAuth, no scheduling subsystem. |
| 10 | Fixtures | Synthetic first | Ingest is an adapter boundary. See risk R1. |
| 11 | Overlays | Generated from templates per video | SVG re-renders correctly across aspect ratios; a bitmap library would not. See risk R2. |
| 12 | Autonomy | Full auto, review the finished render | Fastest feedback per job; cleanest diffs. Costs compute on bad scripts. |
| 13 | LLM stages | Anthropic API, `claude-opus-5` | See §7. |

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
functions between versions. The golden set (§10) will outlive several schema changes,
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

## 6. Rendering

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

### 6.1 Overlays

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

Replaying the golden set (§10) through the LLM stages is a non-latency-sensitive batch
workload — exactly what the Message Batches API is for, at half the cost.

## 8. Verification

Three layers. The first two exist to keep garbage from reaching a person.

### 8.1 Deterministic

- Render exit clean; duration within tolerance of the spec
- Integrated loudness −14 LUFS, true peak below −1 dBTP, dialogue-to-bed ratio above threshold
- Caption blocks: no mutual overlap, minimum display duration, maximum characters per line,
  fully inside the profile's safe area
- Overlay anchors inside safe area and not occluding a caption box
- **Crop-path continuity** — no crop delta above `max_crop_delta_per_frame`

That last check earns its place: judder is *the* failure mode of automated vertical
reframing, it is invisible in a still frame, and it is catchable with arithmetic.

### 8.2 Objective semantic

Open-transcribe the rendered audio and diff against the script (§5.3). One check that
catches TTS mispronunciation, audio/video desync, a wrong take, truncated narration, and
captions that drifted — all otherwise invisible until a human watches.

### 8.3 Perceptual

A VLM over sampled frames, only for questions text cannot answer: is a caption sitting on
the UI element it describes, is the zoom framing the cursor or empty space.

## 9. Preference learning

Three tiers, none of them fine-tuning. Volume is too low, feedback too slow, and
auditability matters more than marginal accuracy.

**Hard constraints** (`prefs/constraints.yaml`) — hand-written, never auto-modified.
Fonts, forbidden voices, fixed output dimensions.

**Learned numeric defaults** (`prefs/defaults.json`) — per render profile. Rolling median
over accepted specs for every tunable in §4.3 plus caption geometry, music level, and cut
pacing. Plain statistics. Given decision #5, this tier does most of the work.

**Exemplars** (`prefs/exemplars/`) — accepted specs retrieved by similarity and few-shot
into the LLM stages. Only relevant to §7.1's model-using stages.

### 9.1 Update rules

- Never learn from a job that failed verification. A broken render's corrections poison
  the defaults.
- Require a minimum sample count before a default moves. One-off corrections are noise.
- Use a windowed median, so an old preference can be superseded.
- Every default change is written to a changelog with the jobs that caused it. "It got
  worse and nobody can explain why" is the characteristic failure of self-tuning systems.

### 9.2 Bootstrapping

With synthetic fixtures and zero accepted jobs there is nothing to learn from. Ship
hand-written values in `constraints.yaml`; the learner activates after ~10–15 accepted
real jobs. **Built earlier, it is dead code that still has to be debugged** — hence its
position in §11.

## 10. Golden set

Archived fixtures plus their approved `EditSpec`s, in `golden/`.

Renders are slow, so regression compares **specs, not pixels**: replan each fixture and
diff the proposed spec against the approved one field-by-field with per-field tolerances.
Render only two or three for frame hashing.

Any change to prompts, rules, or learned defaults replays the full set before taking
effect. Preference updates are code changes: versioned, diffable, revertable.

At least one fixture is **deliberately bad** — clipped audio, overlapping captions,
juddering crop — so §8's checks are tested against known-bad rather than only known-good.

## 11. Repository layout

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
  runner/       LocalRunner, cache
  docs/
```

## 12. Build sequence

1. **Spec models + synthetic fixture generator.** Pydantic, `spec_version`, migrations,
   a scripted synthetic `FocusTrack`.
2. **FFmpeg compiler + render, both profiles.** Get a watchable video out of a fixture.
   This is the load-bearing milestone — spec → compile → render is the path everything
   else hangs off.
3. **TTS + forced alignment**, wired into the DAG behind the cache.
4. **Verification**, including the deliberately-broken fixture.
5. **Review UI.**
6. **Preference learner** — once real accepted jobs exist.

MLT export and re-ingest slot in whenever Kdenlive is first wanted; nothing depends on them.

## 13. Risks

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

*Mitigation:* the fixed template set in §6.1. If overlay quality is still poor at review
time, the next lever is a gate on `plan_overlays` specifically, not on the whole pipeline.

**R3 — Learner drift.** Addressed by §9.1 and §10; listed here because it is the failure
mode most likely to be noticed late.

## 14. Open questions

- Review UI across profiles: one job with a shared `EditSpec` and per-profile caption
  geometry, or two independent approvals? Current lean is one job, since most corrections
  apply to the shared spec.
- Music beds: templates generate visual overlays, but audio beds still need a source.
- Whether `still_4x5` is worth a distinct profile or whether stills reuse `shorts_9x16`.
