"""The stage graph, and the CLI every stage is run as.

One file, so the whole pipeline is readable in one place: what each stage reads,
what invalidates it, and what it produces. `runner/pipeline.py` walks this; it does
not know what any particular stage does.

**What a stage reads is what invalidates it.** Each stage declares a fingerprint —
the exact subset of the spec and profile it depends on — and that is what gets
hashed. Hashing the whole spec would be simpler and wrong: a caption tweak would
invalidate `plan_focus`, and §8's whole argument is that a correction re-runs the
few stages it actually touched.

Run as `python -m runner.stages <name>` with a `StageRequest` on stdin and a
`StageResult` on stdout — the §5.1 contract, and the reason a remote worker needs
no new mechanism.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from compile.render import prepare
from plan.captions import PAUSE_S, plan_captions, tightest
from plan.edit import EditPlan, INSTRUCTION, build_content, reconcile
from plan.focus import plan_focus
from plan.trim import TrimTunables, detect_silence, trim
from prefs import Constraints, load_constraints
from spec.captions import Word
from spec.edit import EditDecisions, Removal, decisions_from_removals
from spec.editspec import EditSpec
from spec.migrations import load_spec_file
from spec.narration import NarrationSource
from spec.profiles import Encoder, RenderProfile
from spec.types import TIME_EPS
from synth.align import align
from synth.asr import Transcript, transcribe
from synth.tts import audio_duration, synthesize

from runner import agent
from runner.cache import file_digest
from runner.contract import StageRequest, StageResult


@dataclass(frozen=True)
class StageSpec:
    """One node of the pipeline."""

    name: str
    version: int
    """Bump to invalidate this stage and everything downstream of it, and nothing else."""
    depends_on: tuple[str, ...]
    """What this stage reads, named by *provision* rather than by stage name."""
    fingerprint: Callable[["StageContext"], dict[str, Any]]
    run: Callable[[StageRequest], StageResult]
    suffix: str = ".json"
    directory: bool = False
    holds_local_weights: bool = False
    """Whether running this stage puts a model in memory.

    8GB will not hold two (§16), so the runner refuses to start a second one.
    `transcribe` is the first to set it, and it alone is 3 984 MB against a
    ~5 500 MB working ceiling (environment findings §5) — the flag was declared a
    phase before anything needed it so that the first parallel scheduler would not
    discover the limit by swapping."""
    model_backed: bool = False
    """Whether an LLM writes this artifact. Forces model id and prompt version into
    the cache key (§5.2). Phase 5 sets the first one."""

    prefers_remote: bool = False
    """Run this stage on a worker when one is configured (§5.1, decision #2).

    One stage sets it, and phase 0 is why: F5-TTS on the target machine is 0.11x
    realtime and its MPS path crashes on any text long enough to be chunked
    (environment findings §4). Everything else is either fast enough here or
    reads media that lives here, and shipping a 2 GB recording to save 20 seconds
    of ASR is a worse trade than the one it undoes.

    A flag rather than a policy: `runner/pipeline.py` still runs it locally when
    no remote runner is given, which is what makes a machine with no worker able
    to run the whole pipeline slowly rather than not at all."""

    provides: str | None = None
    """What this stage produces, for a dependent that does not care which stage
    made it. Defaults to the stage's own name, which is the ordinary case.

    Phase 8 is why it exists. Word timings reach `plan_captions`, `trim` and
    `plan_edit` from `transcribe` on a recorded take and from `align` on a
    synthesized one, and those two are alternatives within one job rather than
    steps in a sequence (§5.3). Naming the dependency `transcript` says the true
    thing — these stages want words with timings — where naming `transcribe`
    would put an `if` about how the narration was made into every stage
    downstream of it."""

    job_inputs: tuple[str, ...] = ()
    """Job-level artifacts this per-profile stage reads, if they exist.

    `verify` reads `trim`'s proposal for the override rate and works without it,
    which is what "if they exist" means: a job that ran no job-level stages is
    every fixture in this repository, and none of them has a trim proposal."""

    requires: tuple[str, ...] = ()
    """Job-level artifacts without which this stage has **nothing to do**.

    Skipped rather than failed, and the difference matters. `verify_transcript`
    diffs the render against the source transcript (§9.2); a job with no
    transcript has no expectation to compare against, and inventing one from the
    hand-authored captions of a fixture would be checking the spec against
    itself."""

    apply: Callable[[EditSpec, Path, str], EditSpec] | None = None
    """How this stage's artifact becomes part of the spec, if it does.

    Called with the job directory and the artifact's path *relative* to it: a
    spec recording an absolute path stops being portable the moment the job
    directory moves, which `runner/remote.py` does on every remote run.

    Principle 1 is that the spec is the system, so a stage that produces a spec
    *field* — `plan_captions` writes `EditSpec.captions` — has to put it there
    rather than leave a parallel document beside it for `compile` to prefer. Only
    job-level stages do this: rewriting the spec once the per-profile fingerprints
    have been taken would invalidate them behind their own backs."""

    @property
    def provision(self) -> str:
        return self.provides or self.name


@dataclass
class JobContext:
    """What a job-level fingerprint may look at.

    Separate from `StageContext` because these stages run **once per job**, not
    once per profile, and a context carrying a single profile would let one of
    them quietly read it. `plan_captions` is profile-*aware* — it sizes blocks
    against the tightest box — and it gets the whole set, which is the §4.1 shape:
    one spec, N profiles.
    """

    spec: EditSpec
    profiles: tuple[RenderProfile, ...]
    job_dir: Path
    constraints: Constraints


@dataclass
class StageContext:
    """Everything a fingerprint may look at."""

    spec: EditSpec
    profile: RenderProfile
    job_dir: Path
    encoder: Encoder
    job_keys: dict[str, str] = field(default_factory=dict)
    """Cache keys of the job-level stages this run produced.

    A per-profile stage that reads a job-level artifact has to be invalidated when
    that artifact changes. Two do: `verify` reads `trim`'s proposal for the
    override rate (§9.1), and `verify_transcript` reads the source transcript to
    compute what this profile expects to hear (§9.2). Keys rather than contents,
    for the same reason a stage's upstream keys are what appear in its own: the
    key already stands for everything the artifact depends on."""


# --- fingerprints ------------------------------------------------------------


def _focus_fingerprint(ctx: StageContext) -> dict[str, Any]:
    """Where we look depends on the track and the profile's geometry — and on
    nothing a caption, a cut or an overlay can change."""
    return {
        "source": {
            "width": ctx.spec.source.width,
            "height": ctx.spec.source.height,
            "fps": ctx.spec.source.fps,
            "duration": ctx.spec.source.duration,
        },
        "focus": ctx.spec.focus.model_dump(mode="json"),
        "profile": {
            "width": ctx.profile.width,
            "height": ctx.profile.height,
            "fps": ctx.profile.fps,
            "focus": ctx.profile.focus.model_dump(mode="json"),
        },
    }


def _transcribe_fingerprint(ctx: JobContext) -> dict[str, Any]:
    """The recording's bytes and the ASR settings, and nothing else.

    No profile appears here on purpose: what was said does not depend on the shape
    it will be rendered into, so both profiles of a job share one transcript and
    the second one is a cache hit. On the target machine that hit is worth 23
    seconds per profile per run (environment findings §3), which is most of the
    reason the review loop is usable at all.
    """
    return {
        "source_digest": file_digest(ctx.job_dir / ctx.spec.source.path),
        "has_audio": ctx.spec.source.has_audio,
        "asr": ctx.constraints.asr.model_dump(mode="json"),
    }


def _tts_fingerprint(ctx: JobContext) -> dict[str, Any]:
    """The script, the voice it is read in, and how it is synthesized.

    Not the recording: `tts` never opens it. That is the whole discipline of a
    fingerprint here — re-recording the screen must not re-synthesize an hour of
    narration (environment findings §4), and it does not, because nothing about
    the video is in this key.

    The reference audio is hashed rather than named. Decision #20 permits
    synthesis of you and nobody else, and a key over the *path* would serve the
    old narration after the file behind that path was replaced — which is the one
    substitution this system must never make silently.
    """
    narration = ctx.spec.narration
    return {
        "script": narration.script,
        "voice_reference": file_digest(ctx.job_dir / narration.voice_reference_path),
        "voice_reference_text": narration.voice_reference_text,
        "tts": ctx.constraints.tts.model_dump(mode="json"),
    }


def _align_fingerprint(ctx: JobContext) -> dict[str, Any]:
    """The script it aligns and the ASR that measures the timings.

    The narration audio arrives through `tts`'s upstream key rather than being
    hashed again — the key already stands for everything that produced the file.
    The script is here in its own right even so: it is *both* an input to `tts`
    and the ground truth this stage anchors to, and a spec whose script was
    corrected in review must re-align even if the audio is unchanged.
    """
    return {
        "script": ctx.spec.narration.script,
        "asr": ctx.constraints.asr.model_dump(mode="json"),
    }


def _plan_captions_fingerprint(ctx: JobContext) -> dict[str, Any]:
    """Only the capacity the blocks are sized against, not the profiles themselves.

    Every other field of a profile — the caption box, the type scale, the encoder —
    can move without changing where one block ends and the next begins. A
    fingerprint that hashed the profiles wholesale would re-plan captions on a
    crop-lag tweak, which is the cost §8's review loop cannot bear.
    """
    return {
        "capacity": tightest(list(ctx.profiles)),
        "pause_s": PAUSE_S,
    }


def _trim_fingerprint(ctx: JobContext) -> dict[str, Any]:
    """The media it measures and the §4.6 scalars it measures with.

    The transcript arrives through the upstream key rather than being hashed
    again here, which is the difference between a fingerprint and a copy of the
    inputs.

    The media is whichever file the narration is in (`EditSpec.narration_path`),
    because that is the one `detect_silence` opens. Hashing the recording on a
    synthesized job would key this stage on a video whose audio it never
    measures — and leave it *unkeyed* on the narration it does."""
    narration = ctx.spec.narration_path
    return {
        "narration_digest": file_digest(ctx.job_dir / narration) if narration else None,
        "trim": ctx.constraints.trim.model_dump(mode="json"),
    }


def _plan_edit_fingerprint(ctx: JobContext) -> dict[str, Any]:
    """The focus track, and nothing else of its own.

    Not the profiles, and specifically not `duration_budget`: §4.4.1 makes tiering
    aspect-independent, so a shorter short must not cost a model call. Not the
    transcript or the trim proposal either — both reach the key through the
    upstream stages they came from. The model id and prompt version go in through
    `params`, where §5.2 requires them.
    """
    return {"focus": ctx.spec.focus.model_dump(mode="json"), "duration": ctx.spec.source.duration}


def _compile_fingerprint(ctx: StageContext) -> dict[str, Any]:
    """The graph depends on the whole edit and the whole profile *except* the
    encoder — which is `render`'s to decide, so changing `crf` re-encodes without
    recompiling.

    `narration` was excluded here until phase 8, correctly: it named a script the
    graph never read. Now it names the audio input the graph is built around
    (`compile/graph.py`), and an exclusion that was bookkeeping would have become
    a cached graph pointed at the wrong file."""
    return {
        "spec": ctx.spec.model_dump(mode="json", exclude={"created_at"}),
        "profile": ctx.profile.model_dump(mode="json", exclude={"encode"}),
    }


def _render_fingerprint(ctx: StageContext) -> dict[str, Any]:
    """The only stage that reads the media, so the only one that hashes it.

    The graph option is in here because it is a property of the FFmpeg binary
    rather than of the graph (compile/ffmpeg.py): a cached render replayed against
    an upgraded FFmpeg has to re-run, and a key that omitted this would serve the
    old artifact and hide that the command changed.
    """
    from compile.ffmpeg import graph_option_or_legacy

    narration = ctx.spec.narration_path
    return {
        "source_digest": file_digest(ctx.job_dir / ctx.spec.source.path),
        # The synthesized narration is a second media input, and a re-synthesis
        # produces different audio under the same spec. `compile` hashes the spec
        # and would not notice; this stage reads the file.
        "narration_digest": (
            file_digest(ctx.job_dir / narration) if narration and narration != ctx.spec.source.path else None
        ),
        "music_digest": (
            file_digest(ctx.job_dir / ctx.spec.audio.music_path) if ctx.spec.audio.music_path else None
        ),
        "encode": ctx.profile.encode.model_dump(mode="json"),
        "encoder": ctx.encoder.value,
        "graph_option": graph_option_or_legacy(),
    }


# --- implementations ---------------------------------------------------------


def _context(request: StageRequest) -> tuple[EditSpec, RenderProfile, Path]:
    job_dir = Path(request.job_dir)
    spec = load_spec_file(job_dir / request.inputs["spec"])
    profile = RenderProfile.model_validate(request.params["profile"])
    return spec, profile, job_dir


def _job_context(request: StageRequest) -> tuple[EditSpec, Path]:
    job_dir = Path(request.job_dir)
    return load_spec_file(job_dir / request.inputs["spec"]), job_dir


def _run_transcribe(request: StageRequest) -> StageResult:
    """Open transcription of the recording's own audio (§5.3).

    A source with no audio track transcribes to no words rather than failing: a
    screen capture with the mic off is an ordinary job and should render
    captionless.
    """
    spec, job_dir = _job_context(request)
    asr = request.params["asr"]
    out = job_dir / request.output
    out.parent.mkdir(parents=True, exist_ok=True)

    if not spec.source.has_audio:
        out.write_text(Transcript(model=asr["model"]).model_dump_json(indent=2))
        return StageResult(stage=request.stage, output=request.output, note="source has no audio")

    result = transcribe(
        job_dir / spec.source.path,
        binary=asr["binary"],
        models_dir=asr["models_dir"],
        model=asr["model"],
        language=asr["language"],
    )
    out.write_text(result.model_dump_json(indent=2))
    return StageResult(
        stage=request.stage, output=request.output, note=f"{len(result.words)} words"
    )


def _run_tts(request: StageRequest) -> StageResult:
    """The one synthesis this design permits (decision #20, §1.1).

    No degradation path, and §7.4's table says so in the same row-shape as
    `script_draft`: a job whose narration is synthesized has no audio to fall
    back to. Rendering it silent would be the worst of the available answers,
    because a silent video looks finished.
    """
    spec, job_dir = _job_context(request)
    narration = spec.narration
    if narration.source is not NarrationSource.SYNTHESIZED:
        raise RuntimeError(
            "tts ran on a job whose narration is recorded. The take's own audio is "
            "its narration (§5.3); remove the stage from job.json rather than "
            "synthesizing over a voice that is already there."
        )

    settings = request.params["tts"]
    out = job_dir / request.output
    result = synthesize(
        narration.script or "",
        reference=job_dir / narration.voice_reference_path,
        reference_text=narration.voice_reference_text or "",
        out_wav=out,
        python=settings["python"],
        device=settings["device"],
        library_path=settings.get("library_path"),
        reference_seconds=settings["reference_seconds"],
    )
    note = f"{result.duration:.2f}s of narration on {result.device}"
    if result.infer_seconds:
        note += f", {result.duration / result.infer_seconds:.2f}x realtime"
    if not result.clean_exit:
        # Phase 0's teardown crash (environment findings §4). Worth saying out
        # loud on the job record: the audio is good, and the next PyTorch may
        # make this a real failure rather than a cosmetic one.
        note += "; the backend exited nonzero after writing good audio"
    return StageResult(stage=request.stage, output=request.output, note=note)


def _apply_narration(spec: EditSpec, job_dir: Path, artifact: str) -> EditSpec:
    """The synthesized narration becomes the spec's audio spine.

    Principle 1: a wav sitting in `stages/` that only the pipeline knew about
    would be invisible to `compile`, to verification and to golden replay alike.
    """
    return EditSpec.model_validate(
        {
            **spec.model_dump(mode="json"),
            "narration": {**spec.narration.model_dump(mode="json"), "audio_path": artifact},
        }
    )


def _run_align(request: StageRequest) -> StageResult:
    """Forced alignment of the script against the narration that reads it (§5.3).

    Open transcription of the synthesized audio, then the script anchored to what
    came back — see `synth/align.py` for why that rather than WhisperX. The
    artifact is a `Transcript`, the same document `transcribe` writes, because
    everything downstream wants words with timings and should not care which of
    §5.3's calls produced them.
    """
    spec, job_dir = _job_context(request)
    asr = request.params["asr"]
    audio = job_dir / request.inputs["narration"]

    heard = transcribe(
        audio,
        binary=asr["binary"],
        models_dir=asr["models_dir"],
        model=asr["model"],
        language=asr["language"],
    )
    spoken = audio_duration(audio)
    if spoken > spec.source.duration + TIME_EPS:
        # Fail here, where both numbers are in hand and the remedy is obvious.
        # Left alone it surfaces two stages later as a caption block past the end
        # of the source (`EditSpec._within_source`), which is true and useless.
        # Holding the last frame to cover the overrun would be a design decision
        # about synthesizing video, and this project does not make those in a
        # stage (§1.1).
        raise RuntimeError(
            f"the narration is {spoken:.2f}s and the recording is {spec.source.duration:.2f}s. "
            f"Shorten the script or record more screen — there is nothing to show under "
            f"the last {spoken - spec.source.duration:.2f}s."
        )

    alignment = align(spec.narration.script or "", heard.words, spoken)
    out = job_dir / request.output
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        Transcript(
            words=alignment.words,
            language=asr["language"],
            backend="align",
            model=asr["model"],
        ).model_dump_json(indent=2)
    )
    return StageResult(
        stage=request.stage,
        output=request.output,
        note=(
            f"{len(alignment.words)} script words, "
            f"{alignment.anchored} anchored ({alignment.coverage:.0%})"
        ),
    )


def _run_plan_captions(request: StageRequest) -> StageResult:
    spec, job_dir = _job_context(request)
    transcript = Transcript.model_validate_json(
        (job_dir / request.inputs["transcript"]).read_text()
    )
    profiles = [RenderProfile.model_validate(p) for p in request.params["profiles"]]
    # `Word.emphasis` defaults false and is the one model-written field in the
    # caption subtree (§7.1). It arrives in phase 9; nothing here decides it.
    words = [Word(t_in=w.t_in, t_out=w.t_out, text=w.text) for w in transcript.words]
    blocks = plan_captions(words, profiles)
    out = job_dir / request.output
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps([block.model_dump(mode="json") for block in blocks], indent=2)
    )
    return StageResult(stage=request.stage, output=request.output, note=f"{len(blocks)} blocks")


def _apply_captions(spec: EditSpec, job_dir: Path, artifact: str) -> EditSpec:
    from spec.captions import CaptionBlock

    blocks = [
        CaptionBlock.model_validate(entry)
        for entry in json.loads((job_dir / artifact).read_text())
    ]
    # Round-tripped through validation rather than assigned, so a planner that
    # produced overlapping or out-of-bounds blocks fails here, beside the stage
    # that made them — `model_copy` would take them without a word and let
    # `compile` find out two stages later.
    return EditSpec.model_validate(
        {**spec.model_dump(mode="json"), "captions": [b.model_dump(mode="json") for b in blocks]}
    )


def _run_trim(request: StageRequest) -> StageResult:
    """Proposed removals, by arithmetic (§4.6). No model, and §7.1 says so."""
    spec, job_dir = _job_context(request)
    transcript = Transcript.model_validate_json(
        (job_dir / request.inputs["transcript"]).read_text()
    )
    words = [Word(t_in=w.t_in, t_out=w.t_out, text=w.text) for w in transcript.words]
    tunables = TrimTunables(**request.params["trim"])

    # Whichever file the narration is in (§5.3): the take's own track, or the
    # wav `tts` wrote. Measuring the recording on a synthesized job would look
    # for dead air in an audio track the render does not use.
    narration = spec.narration_path
    silences = (
        detect_silence(
            job_dir / narration,
            silence_db=tunables.silence_db,
            min_silence_ms=tunables.min_silence_ms,
        )
        if narration
        else []
    )
    removals = trim(words, silences, spec.source.duration, tunables)

    out = job_dir / request.output
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps([r.model_dump(mode="json") for r in removals], indent=2)
    )
    cut = sum(r.duration for r in removals)
    return StageResult(
        stage=request.stage,
        output=request.output,
        note=f"{len(removals)} removals, {cut:.2f}s of {spec.source.duration:.2f}s",
    )


def _run_plan_edit(request: StageRequest) -> StageResult:
    """The first model stage (§7.1), and the first that is allowed to fail (§7.4).

    A failure here degrades to `trim`'s removals with every segment `essential` —
    a silence-trimmed, filler-stripped video rather than the unedited take. That
    row of §7.4's table is why the `trim` split earns its place, and it is the one
    path in this pipeline that has to work when nothing else does.
    """
    spec, job_dir = _job_context(request)
    transcript = Transcript.model_validate_json(
        (job_dir / request.inputs["transcript"]).read_text()
    )
    proposals = [
        Removal.model_validate(entry)
        for entry in json.loads((job_dir / request.inputs["trim"]).read_text())
    ]
    words = [Word(t_in=w.t_in, t_out=w.t_out, text=w.text) for w in transcript.words]

    outcome = agent.run_stage(
        agent.build_prompt(
            INSTRUCTION,
            EditPlan,
            build_content(words, proposals, spec.focus, spec.source.duration),
        ),
        EditPlan,
        job_dir=job_dir,
        model=request.params["model"],
    )
    if outcome.fragment is not None:
        decisions = reconcile(outcome.fragment, proposals, spec.source.duration)
    else:
        decisions = decisions_from_removals(proposals, spec.source.duration)

    out = job_dir / request.output
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(decisions.model_dump_json(indent=2))
    return StageResult(
        stage=request.stage,
        output=request.output,
        degraded=outcome.degraded,
        note=outcome.note,
    )


def _apply_edit(spec: EditSpec, job_dir: Path, artifact: str) -> EditSpec:
    decisions = EditDecisions.model_validate_json((job_dir / artifact).read_text())
    return EditSpec.model_validate(
        {**spec.model_dump(mode="json"), "edit": decisions.model_dump(mode="json")}
    )


def _run_plan_focus(request: StageRequest) -> StageResult:
    spec, profile, job_dir = _context(request)
    plan = plan_focus(spec, profile)
    out = job_dir / request.output
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(plan.model_dump_json(indent=2))
    return StageResult(stage=request.stage, output=request.output)


def _run_compile(request: StageRequest) -> StageResult:
    from plan.focus import CropPathPlan, ZoomPlan

    spec, profile, job_dir = _context(request)
    raw = json.loads((job_dir / request.inputs["focus"]).read_text())
    focus = (CropPathPlan if raw["mode"] == "crop_path" else ZoomPlan).model_validate(raw)

    work_dir = Path(request.output)
    plan = prepare(spec, profile, job_dir, work_dir=work_dir, focus=focus)
    (job_dir / work_dir / "manifest.json").write_text(
        json.dumps(
            {
                "graph_args": plan.graph_args,
                "profile": profile.name,
                "duration": plan.timeline.duration,
                "threshold": plan.timeline.threshold.value,
                "budget_overrun": plan.timeline.budget_overrun,
                "captions": len(plan.timeline.captions),
                "overlays": len(plan.timeline.overlays),
                "dropped_overlays": plan.timeline.dropped_overlays,
                "assets": [
                    {
                        "template": asset.template,
                        "path": str(asset.path.relative_to(job_dir)),
                        "width": asset.width,
                        "height": asset.height,
                        "dx": asset.dx,
                        "dy": asset.dy,
                        "fill_rect": list(asset.fill_rect) if asset.fill_rect else None,
                    }
                    for asset in plan.assets
                ],
            },
            indent=2,
        )
    )
    return StageResult(stage=request.stage, output=request.output)


def _run_render(request: StageRequest) -> StageResult:
    from compile.ffmpeg import graph_option, with_graph_option
    from compile.graph import encode_args
    from compile.render import _encoder_available

    _, profile, job_dir = _context(request)
    manifest = json.loads((job_dir / request.inputs["compile"] / "manifest.json").read_text())
    encoder = Encoder(request.params["encoder"])
    # `compile` may have been cached under a different FFmpeg than the one about
    # to run. The stage that invokes the binary is the one that names its options.
    graph_args = with_graph_option(manifest["graph_args"], graph_option())
    args = [*graph_args, *encode_args(profile, encoder), request.output]
    codec = args[args.index("-c:v") + 1]
    if not _encoder_available(codec):
        raise RuntimeError(
            f"this FFmpeg has no {codec!r} encoder. VideoToolbox is macOS-only (§16); "
            f"re-run with --encoder software, which is also what golden renders use."
        )
    (job_dir / request.output).parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(args, cwd=job_dir, check=True)
    return StageResult(stage=request.stage, output=request.output)


def _verify_transcript_fingerprint(ctx: StageContext) -> dict[str, Any]:
    """What this profile expects to hear, and the transcript it expects it from.

    The render arrives through the upstream key, and it is the *audio* this stage
    measures — so a caption tweak, which changes the render's pixels and nothing
    else, re-runs ASR for no gain. That is the one place §9.2 costs the review
    loop something real (§8), and it is accepted rather than fixed: a key over the
    rendered audio alone would have to be computed from a render that does not
    exist yet.

    The ASR settings are not here because they do not need to be. They are in
    `transcribe`'s fingerprint, whose key is, and a stage that hashed them again
    would be keeping a second copy of a number that already decides this key.
    """
    return {
        "transcript": ctx.job_keys.get("transcript"),
        "edit": ctx.spec.edit.model_dump(mode="json"),
        "duration_budget": ctx.profile.duration_budget,
    }


def _verify_fingerprint(ctx: StageContext) -> dict[str, Any]:
    """Verification reads the whole spec and the whole profile, so it re-runs
    whenever either moves. It is seconds of work over a render that already
    exists; being precise here would buy nothing.

    `trim`'s key is here because the override rate compares the surviving removals
    against what `trim` proposed, and a changed proposal changes that number
    without changing the spec at all. `verify_transcript`'s key arrives through
    `depends_on` like any other upstream — and is absent from the key entirely on
    a job that has no transcript to round-trip, which is correct: a stage that did
    not run is not part of what this one read."""
    return {
        "spec": ctx.spec.model_dump(mode="json", exclude={"created_at"}),
        "profile": ctx.profile.model_dump(mode="json"),
        "trim": ctx.job_keys.get("trim"),
    }


def _run_verify_transcript(request: StageRequest) -> StageResult:
    """§9.2: open-transcribe the render and diff it against what the edit expects.

    The second of §5.3's two ASR calls, and the same one — open transcription,
    pointed at the render instead of the recording. It holds weights, so it is its
    own stage rather than part of `verify`: declaring the flag on `verify` would
    have every report claim 4GB it does not use, and folding the two together
    would re-run ASR whenever a §9.1 tunable moved.
    """
    from compile.timeline import project
    from synth.asr import AsrUnavailable
    from verify.transcript import RoundTrip, expected_transcript, round_trip

    spec, profile, job_dir = _context(request)
    source = Transcript.model_validate_json(
        (job_dir / request.inputs["transcript"]).read_text()
    )
    out = job_dir / request.output
    out.parent.mkdir(parents=True, exist_ok=True)

    timeline = project(spec, profile)
    if not expected_transcript(source.words, timeline):
        # A silent screen capture is an ordinary job (§5.3). Transcribing the
        # render to confirm it says nothing would cost the most expensive stage
        # there is to learn what the source transcript already said.
        # `ran` stays true: the check ran and found nothing to hear, which is not
        # the same as a check that could not run. Conflating them puts a warning
        # on every silent job forever, and a warning that fires on correct
        # behaviour gets ignored within a week.
        result = RoundTrip(profile=profile.name, note="no speech in the source to round-trip")
        out.write_text(result.model_dump_json(indent=2))
        return StageResult(stage=request.stage, output=request.output, note="no speech")

    asr = request.params["asr"]
    try:
        heard = transcribe(
            job_dir / request.inputs["render"],
            binary=asr["binary"],
            models_dir=asr["models_dir"],
            model=asr["model"],
            language=asr["language"],
        )
    except AsrUnavailable as unavailable:
        # §7.4's shape rather than a failure. The render is fine; the *check* could
        # not run, and a job that refused to finish because a checker was missing
        # would be a worse tool than one that says so on the report. Degraded, so
        # the pipeline does not cache it — see `_run_job_stages`.
        result = RoundTrip(profile=profile.name, ran=False, note=str(unavailable))
        out.write_text(result.model_dump_json(indent=2))
        return StageResult(
            stage=request.stage, output=request.output, degraded=True,
            note=f"round-trip not run: {unavailable}",
        )

    result = round_trip(profile.name, source.words, heard.words, timeline)
    out.write_text(result.model_dump_json(indent=2))
    return StageResult(
        stage=request.stage,
        output=request.output,
        note=f"{len(result.real)} real differences in {result.expected_words} expected words",
    )


def _run_verify(request: StageRequest) -> StageResult:
    from compile.overlays import OverlayAsset
    from compile.timeline import project
    from plan.focus import CropPathPlan, ZoomPlan
    from verify import verify_render
    from verify.transcript import RoundTrip

    spec, profile, job_dir = _context(request)
    raw = json.loads((job_dir / request.inputs["focus"]).read_text())
    focus = (CropPathPlan if raw["mode"] == "crop_path" else ZoomPlan).model_validate(raw)
    manifest = json.loads((job_dir / request.inputs["compile"] / "manifest.json").read_text())
    assets = [
        OverlayAsset(
            template=entry["template"],
            path=job_dir / entry["path"],
            width=entry["width"],
            height=entry["height"],
            dx=entry["dx"],
            dy=entry["dy"],
            fill_rect=tuple(entry["fill_rect"]) if entry["fill_rect"] else None,
        )
        for entry in manifest.get("assets", [])
    ]
    proposal_path = request.inputs.get("trim")
    proposals = (
        [Removal.model_validate(entry) for entry in json.loads((job_dir / proposal_path).read_text())]
        if proposal_path and (job_dir / proposal_path).is_file()
        else None
    )
    round_trip_path = request.inputs.get("round_trip")
    round_trip = (
        RoundTrip.model_validate_json((job_dir / round_trip_path).read_text())
        if round_trip_path and (job_dir / round_trip_path).is_file()
        else None
    )
    report = verify_render(
        spec, profile, project(spec, profile), focus, job_dir / request.inputs["render"], assets,
        trim_proposals=proposals,
        round_trip=round_trip,
    )
    out = job_dir / request.output
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report.model_dump_json(indent=2))
    return StageResult(
        stage=request.stage,
        output=request.output,
        note=None if report.passed else f"{len(report.failures)} checks failed",
    )


# --- the graph ---------------------------------------------------------------

JOB_STAGES: dict[str, StageSpec] = {
    stage.name: stage
    for stage in (
        StageSpec(
            name="transcribe",
            version=1,
            depends_on=(),
            fingerprint=_transcribe_fingerprint,
            run=_run_transcribe,
            holds_local_weights=True,
            provides="transcript",
        ),
        StageSpec(
            name="tts",
            version=1,
            depends_on=(),
            fingerprint=_tts_fingerprint,
            run=_run_tts,
            suffix=".wav",
            apply=_apply_narration,
            prefers_remote=True,
            # True on this machine, which is the reason above: F5-TTS is ~2 500 MB
            # on CPU (environment findings §5), so run locally it is one of the
            # stages §16 serializes. Run it on a worker and the flag costs
            # nothing, which is most of the point of routing it there.
            holds_local_weights=True,
            provides="narration",
        ),
        StageSpec(
            name="align",
            version=1,
            depends_on=("narration",),
            fingerprint=_align_fingerprint,
            run=_run_align,
            holds_local_weights=True,
            provides="transcript",
        ),
        StageSpec(
            name="plan_captions",
            version=1,
            depends_on=("transcript",),
            fingerprint=_plan_captions_fingerprint,
            run=_run_plan_captions,
            apply=_apply_captions,
        ),
        StageSpec(
            name="trim",
            version=1,
            depends_on=("transcript",),
            fingerprint=_trim_fingerprint,
            run=_run_trim,
        ),
        StageSpec(
            name="plan_edit",
            version=1,
            depends_on=("transcript", "trim"),
            fingerprint=_plan_edit_fingerprint,
            run=_run_plan_edit,
            apply=_apply_edit,
            model_backed=True,
        ),
    )
}
"""Stages that run **once per job**, ahead of the per-profile ones.

They are here rather than in `STAGES` because of what they read and what they
write. `transcribe` reads the recording, which no profile changes, so running it
per profile would transcribe the same audio twice — and on the target machine
that is the most expensive deterministic stage there is. `plan_captions` writes a
spec *field*, and §4.1 has one `EditSpec` serving N profiles, so there is one
caption list or the document is not one document.

Ordering matters for a duller reason too: they rewrite `spec.json`, and the
per-profile fingerprints are taken from the spec. Interleaving the two groups
would have `compile` hash a spec that changed after it looked."""

JOB_ORDER: tuple[str, ...] = (
    "tts", "align", "transcribe", "plan_captions", "trim", "plan_edit",
)
"""`plan_captions` sits ahead of `trim` because §4.5 lets it: captions are planned
against the full source timeline and `compile` applies the edit, so the two do not
depend on each other in either direction. The order here is the order they run in,
and only `plan_edit`'s dependence on `trim` is load-bearing.

`tts` and `align` lead because everything about a synthesized job waits on the
narration existing, which is §5's flowchart exactly: `plan_focus` is the only
stage with no audio dependency, and it is per-profile. `align` and `transcribe`
are alternatives rather than neighbours — both provide `transcript`, and a job
asks for one of them — so their order relative to each other never comes up."""

#: The two job recipes, which are two ways of getting words with timings (§5.3).
#: Named here rather than assembled by whoever writes a `job.json`, because the
#: difference between them is a design fact — a take is narrated by you or by
#: your voice reading a script, and nothing else about the pipeline changes.
RECORDED_STAGES: tuple[str, ...] = ("transcribe", "plan_captions", "trim", "plan_edit")
SYNTHESIZED_STAGES: tuple[str, ...] = ("tts", "align", "plan_captions", "trim", "plan_edit")

#: What each job-level **provision** is called in a dependent's `inputs`.
#: Keyed by provision rather than by stage: `transcribe` and `align` are two ways
#: to get one thing, and a dependent that had to know which it got would carry an
#: `if` about how the narration was made.
JOB_INPUT_NAMES: dict[str, str] = {
    "transcript": "transcript",
    "narration": "narration",
    "captions": "captions",
    "trim": "trim",
    "edit": "edit",
}


STAGES: dict[str, StageSpec] = {
    stage.name: stage
    for stage in (
        StageSpec(
            name="plan_focus",
            version=1,
            depends_on=(),
            fingerprint=_focus_fingerprint,
            run=_run_plan_focus,
        ),
        StageSpec(
            name="compile",
            version=1,
            depends_on=("plan_focus",),
            fingerprint=_compile_fingerprint,
            run=_run_compile,
            directory=True,
            suffix="",
        ),
        StageSpec(
            name="render",
            version=1,
            depends_on=("compile",),
            fingerprint=_render_fingerprint,
            run=_run_render,
            suffix=".mp4",
        ),
        StageSpec(
            name="verify_transcript",
            version=1,
            depends_on=("render",),
            fingerprint=_verify_transcript_fingerprint,
            run=_run_verify_transcript,
            requires=("transcript",),
            holds_local_weights=True,
        ),
        StageSpec(
            name="verify",
            version=1,
            depends_on=("plan_focus", "compile", "render", "verify_transcript"),
            fingerprint=_verify_fingerprint,
            run=_run_verify,
            job_inputs=("trim",),
        ),
    )
}

ORDER: tuple[str, ...] = ("plan_focus", "compile", "render", "verify_transcript", "verify")
"""Topological order. Five stages do not need a sort; thirty will."""

#: What each stage calls its upstream artifacts in `StageRequest.inputs`.
INPUT_NAMES: dict[str, str] = {
    "plan_focus": "focus",
    "compile": "compile",
    "render": "render",
    "verify_transcript": "round_trip",
    "verify": "verify",
}


def main(argv: list[str] | None = None) -> int:
    """The §5.1 CLI: JSON in on stdin, JSON out on stdout, nothing else on stdout."""
    argv = argv if argv is not None else sys.argv[1:]
    known = {**JOB_STAGES, **STAGES}
    if len(argv) != 1 or argv[0] not in known:
        print(f"usage: python -m runner.stages <{'|'.join(known)}>", file=sys.stderr)
        return 2
    request = StageRequest.model_validate_json(sys.stdin.read())
    result = known[argv[0]].run(request)
    sys.stdout.write(result.model_dump_json())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
