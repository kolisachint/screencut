"""Running a render, and the CLI that does it.

Kept apart from `compile.graph` so the graph can be built, inspected and tested
without FFmpeg present — which is most of what the tests want, and all of what a
machine without the codecs can do.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
from pathlib import Path

from plan.focus import plan_focus
from prefs import resolve_profile
from spec.editspec import EditSpec
from spec.migrations import load_spec_file
from spec.profiles import Encoder, RenderProfile

from compile.captions import render_ass
from compile.graph import RenderPlan, build_audio_commands, build_commands, build_graph, encode_args, view_rects
from compile.overlays import render_asset
from compile.timeline import project

GRAPH_NAME = "graph.txt"
COMMANDS_NAME = "commands.txt"
AUDIO_COMMANDS_NAME = "audio_commands.txt"
ASS_NAME = "captions.ass"


class FfmpegMissing(RuntimeError):
    pass


class EncoderUnavailable(RuntimeError):
    pass


def _encoder_available(name: str) -> bool:
    listing = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-encoders"],
        capture_output=True, text=True, check=True,
    ).stdout
    return any(line.split()[1:2] == [name] for line in listing.splitlines())


def prepare(
    spec: EditSpec,
    profile: RenderProfile,
    job_dir: Path,
    *,
    encoder: Encoder | None = None,
    work_dir: Path | None = None,
    out_path: Path | None = None,
    focus=None,
) -> RenderPlan:
    """Build the graph and write everything it references into the job directory.

    All paths are relative to the job directory and FFmpeg is run with that as its
    working directory. That is not tidiness: filtergraph syntax gives `:` and `\\`
    their own meanings, and the cheapest way never to escape a path is never to
    write one.
    """
    job_dir = Path(job_dir)
    timeline = project(spec, profile)
    focus = focus if focus is not None else plan_focus(spec, profile)
    encoder = encoder or profile.encode.encoder

    work_dir = work_dir or Path("renders") / profile.name
    (job_dir / work_dir).mkdir(parents=True, exist_ok=True)

    assets = [
        render_asset(overlay.template, overlay.text, profile, job_dir / work_dir, index)
        for index, overlay in enumerate(timeline.overlays)
    ]
    rects = view_rects(timeline, focus, profile)
    commands = build_commands(timeline, rects, profile, spec, dict(enumerate(assets)))
    audio_commands = build_audio_commands(timeline, spec)
    ass = render_ass(timeline.captions, profile)

    music_input = 1 if spec.audio.music_path else None
    graph = build_graph(
        spec,
        profile,
        timeline,
        focus,
        assets,
        ass_name=str(work_dir / ASS_NAME),
        commands_name=str(work_dir / COMMANDS_NAME),
        audio_commands_name=str(work_dir / AUDIO_COMMANDS_NAME),
        music_input=music_input,
    )

    (job_dir / work_dir / GRAPH_NAME).write_text(graph)
    (job_dir / work_dir / COMMANDS_NAME).write_text(commands)
    (job_dir / work_dir / AUDIO_COMMANDS_NAME).write_text(audio_commands)
    (job_dir / work_dir / ASS_NAME).write_text(ass)

    out_path = out_path or Path("renders") / f"{spec.job_id}_{profile.name}.mp4"
    inputs: list[str] = ["-i", spec.source.path]
    if spec.audio.music_path:
        inputs += ["-stream_loop", "-1", "-i", spec.audio.music_path]
    for asset in assets:
        inputs += ["-i", str(asset.path.relative_to(job_dir))]

    # Split deliberately: everything up to the maps is what `compile` decided, and
    # the encoder and the filename are what `render` decides. Keeping them apart is
    # what lets a change of encoder re-run the render and not the compile (§5.2).
    graph_args = [
        "ffmpeg", "-y", "-hide_banner", "-nostdin", "-loglevel", "error",
        *inputs,
        "-filter_complex_script", str(work_dir / GRAPH_NAME),
        "-map", "[vout]",
        "-map", "[aout]",
    ]
    return RenderPlan(
        profile=profile,
        timeline=timeline,
        focus=focus,
        encoder=encoder,
        work_dir=work_dir,
        out_path=out_path,
        graph=graph,
        commands=commands,
        audio_commands=audio_commands,
        ass=ass,
        assets=assets,
        graph_args=graph_args,
        ffmpeg_args=[*graph_args, *encode_args(profile, encoder), str(out_path)],
    )


def run(plan: RenderPlan, job_dir: Path) -> Path:
    if shutil.which("ffmpeg") is None:
        raise FfmpegMissing("ffmpeg is not on PATH")
    codec = plan.ffmpeg_args[plan.ffmpeg_args.index("-c:v") + 1]
    if not _encoder_available(codec):
        raise EncoderUnavailable(
            f"this FFmpeg has no {codec!r} encoder. VideoToolbox is macOS-only (§16); "
            f"re-run with --encoder software, which is also what golden renders use."
        )
    subprocess.run(plan.ffmpeg_args, cwd=job_dir, check=True)
    return Path(job_dir) / plan.out_path


def render_job(
    job_dir: Path | str, profile_name: str, *, encoder: Encoder | None = None, dry_run: bool = False
) -> RenderPlan:
    job_dir = Path(job_dir)
    spec = load_spec_file(job_dir / "spec.json")
    plan = prepare(spec, resolve_profile(profile_name), job_dir, encoder=encoder)
    if not dry_run:
        run(plan, job_dir)
    return plan


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render a job for one profile.")
    parser.add_argument("--job", default="data/fixtures/demo01")
    parser.add_argument("--profile", default="shorts_9x16")
    parser.add_argument("--encoder", choices=[e.value for e in Encoder], default=None,
                        help="Override the profile's encoder. `software` is the reproducible one.")
    parser.add_argument("--dry-run", action="store_true", help="Write the graph and assets, run nothing.")
    args = parser.parse_args(argv)

    plan = render_job(
        args.job,
        args.profile,
        encoder=Encoder(args.encoder) if args.encoder else None,
        dry_run=args.dry_run,
    )
    timeline = plan.timeline
    print(
        f"{args.profile}: {timeline.duration:.2f}s  tier={timeline.threshold.value}  "
        f"spans={len(timeline.spans)}  captions={len(timeline.captions)}  "
        f"overlays={len(timeline.overlays)} (+{timeline.dropped_overlays} dropped)  "
        f"encoder={plan.encoder.value}"
    )
    if timeline.budget_overrun:
        print(f"  budget overrun: {timeline.budget_overrun:.2f}s over {plan.profile.duration_budget:g}s")
    print(f"  {'planned' if args.dry_run else 'wrote'} {plan.out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
