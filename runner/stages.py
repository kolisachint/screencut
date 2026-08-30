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
from plan.focus import plan_focus
from prefs import Constraints, load_constraints
from spec.captions import Word
from spec.editspec import EditSpec
from spec.migrations import load_spec_file
from spec.profiles import Encoder, RenderProfile
from synth.asr import Transcript, transcribe

from runner.cache import file_digest
from runner.contract import StageRequest, StageResult


@dataclass(frozen=True)
class StageSpec:
    """One node of the pipeline."""

    name: str
    version: int
    """Bump to invalidate this stage and everything downstream of it, and nothing else."""
    depends_on: tuple[str, ...]
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

    apply: Callable[[EditSpec, Path], EditSpec] | None = None
    """How this stage's artifact becomes part of the spec, if it does.

    Principle 1 is that the spec is the system, so a stage that produces a spec
    *field* — `plan_captions` writes `EditSpec.captions` — has to put it there
    rather than leave a parallel document beside it for `compile` to prefer. Only
    job-level stages do this: rewriting the spec once the per-profile fingerprints
    have been taken would invalidate them behind their own backs."""


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


def _compile_fingerprint(ctx: StageContext) -> dict[str, Any]:
    """The graph depends on the whole edit and the whole profile *except* the
    encoder — which is `render`'s to decide, so changing `crf` re-encodes without
    recompiling."""
    return {
        "spec": ctx.spec.model_dump(mode="json", exclude={"created_at", "narration"}),
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

    return {
        "source_digest": file_digest(ctx.job_dir / ctx.spec.source.path),
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


def _apply_captions(spec: EditSpec, artifact: Path) -> EditSpec:
    from spec.captions import CaptionBlock

    blocks = [CaptionBlock.model_validate(entry) for entry in json.loads(artifact.read_text())]
    # Round-tripped through validation rather than assigned, so a planner that
    # produced overlapping or out-of-bounds blocks fails here, beside the stage
    # that made them — `model_copy` would take them without a word and let
    # `compile` find out two stages later.
    return EditSpec.model_validate(
        {**spec.model_dump(mode="json"), "captions": [b.model_dump(mode="json") for b in blocks]}
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


def _verify_fingerprint(ctx: StageContext) -> dict[str, Any]:
    """Verification reads the whole spec and the whole profile, so it re-runs
    whenever either moves. It is seconds of work over a render that already
    exists; being precise here would buy nothing."""
    return {
        "spec": ctx.spec.model_dump(mode="json", exclude={"created_at"}),
        "profile": ctx.profile.model_dump(mode="json"),
    }


def _run_verify(request: StageRequest) -> StageResult:
    from compile.overlays import OverlayAsset
    from compile.timeline import project
    from plan.focus import CropPathPlan, ZoomPlan
    from verify import verify_render

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
    report = verify_render(
        spec, profile, project(spec, profile), focus, job_dir / request.inputs["render"], assets
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
        ),
        StageSpec(
            name="plan_captions",
            version=1,
            depends_on=("transcribe",),
            fingerprint=_plan_captions_fingerprint,
            run=_run_plan_captions,
            apply=_apply_captions,
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

JOB_ORDER: tuple[str, ...] = ("transcribe", "plan_captions")

#: What each job-level stage calls its upstream artifacts.
JOB_INPUT_NAMES: dict[str, str] = {
    "transcribe": "transcript",
    "plan_captions": "captions",
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
            name="verify",
            version=1,
            depends_on=("plan_focus", "compile", "render"),
            fingerprint=_verify_fingerprint,
            run=_run_verify,
        ),
    )
}

ORDER: tuple[str, ...] = ("plan_focus", "compile", "render", "verify")
"""Topological order. Four stages do not need a sort; thirty will."""

#: What each stage calls its upstream artifacts in `StageRequest.inputs`.
INPUT_NAMES: dict[str, str] = {
    "plan_focus": "focus",
    "compile": "compile",
    "render": "render",
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
