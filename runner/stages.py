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
from plan.focus import plan_focus
from spec.editspec import EditSpec
from spec.migrations import load_spec_file
from spec.profiles import Encoder, RenderProfile

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

    8GB will not hold two (§16), so the runner refuses to start a second one. No
    stage sets this yet — `transcribe` in phase 4 is the first — and declaring it
    now is what stops the first parallel scheduler from discovering the limit by
    swapping."""
    model_backed: bool = False
    """Whether an LLM writes this artifact. Forces model id and prompt version into
    the cache key (§5.2). Phase 5 sets the first one."""


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


def _compile_fingerprint(ctx: StageContext) -> dict[str, Any]:
    """The graph depends on the whole edit and the whole profile *except* the
    encoder — which is `render`'s to decide, so changing `crf` re-encodes without
    recompiling."""
    return {
        "spec": ctx.spec.model_dump(mode="json", exclude={"created_at", "narration"}),
        "profile": ctx.profile.model_dump(mode="json", exclude={"encode"}),
    }


def _render_fingerprint(ctx: StageContext) -> dict[str, Any]:
    """The only stage that reads the media, so the only one that hashes it."""
    return {
        "source_digest": file_digest(ctx.job_dir / ctx.spec.source.path),
        "music_digest": (
            file_digest(ctx.job_dir / ctx.spec.audio.music_path) if ctx.spec.audio.music_path else None
        ),
        "encode": ctx.profile.encode.model_dump(mode="json"),
        "encoder": ctx.encoder.value,
    }


# --- implementations ---------------------------------------------------------


def _context(request: StageRequest) -> tuple[EditSpec, RenderProfile, Path]:
    job_dir = Path(request.job_dir)
    spec = load_spec_file(job_dir / request.inputs["spec"])
    profile = RenderProfile.model_validate(request.params["profile"])
    return spec, profile, job_dir


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
            },
            indent=2,
        )
    )
    return StageResult(stage=request.stage, output=request.output)


def _run_render(request: StageRequest) -> StageResult:
    from compile.graph import encode_args
    from compile.render import _encoder_available

    _, profile, job_dir = _context(request)
    manifest = json.loads((job_dir / request.inputs["compile"] / "manifest.json").read_text())
    encoder = Encoder(request.params["encoder"])
    args = [*manifest["graph_args"], *encode_args(profile, encoder), request.output]
    codec = args[args.index("-c:v") + 1]
    if not _encoder_available(codec):
        raise RuntimeError(
            f"this FFmpeg has no {codec!r} encoder. VideoToolbox is macOS-only (§16); "
            f"re-run with --encoder software, which is also what golden renders use."
        )
    (job_dir / request.output).parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(args, cwd=job_dir, check=True)
    return StageResult(stage=request.stage, output=request.output)


# --- the graph ---------------------------------------------------------------

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
    )
}

ORDER: tuple[str, ...] = ("plan_focus", "compile", "render")
"""Topological order. Three stages do not need a sort; thirty will."""

#: What each stage calls its upstream artifacts in `StageRequest.inputs`.
INPUT_NAMES: dict[str, str] = {"plan_focus": "focus", "compile": "compile", "render": "render"}


def main(argv: list[str] | None = None) -> int:
    """The §5.1 CLI: JSON in on stdin, JSON out on stdout, nothing else on stdout."""
    argv = argv if argv is not None else sys.argv[1:]
    if len(argv) != 1 or argv[0] not in STAGES:
        print(f"usage: python -m runner.stages <{'|'.join(STAGES)}>", file=sys.stderr)
        return 2
    request = StageRequest.model_validate_json(sys.stdin.read())
    result = STAGES[argv[0]].run(request)
    sys.stdout.write(result.model_dump_json())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
