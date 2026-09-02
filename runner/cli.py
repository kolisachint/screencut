"""`screencut ingest <recording>`, `screencut narrate <job>` and `screencut run <job>`.

Three commands because they answer to different things. `ingest` reads a recorder
bundle once and turns it into a job directory; `narrate` attaches a script and a
voice to one; `run` renders whatever is in one, as many times as a correction
cycle needs. Keeping them apart is what lets a job be re-rendered without the
recorder's files still being on the machine.

`narrate` is separate from `ingest` for a reason beyond tidiness. Decision #20
permits synthesis of you and nobody else, and §1.1 says that boundary is a
schema-and-config matter rather than a matter of intent — so it is a command a
person runs deliberately, naming their own reference recording and writing a
consent note, rather than a flag that could default its way into a job.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from spec.migrations import load_spec_file
from spec.profiles import BUILTIN_PROFILES, Encoder

from runner.pipeline import run_job
from runner.stages import RECORDED_STAGES


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="screencut")
    subcommands = parser.add_subparsers(dest="command", required=True)

    run = subcommands.add_parser("run", help="Render a job, skipping whatever the cache already holds.")
    run.add_argument("job", help="Job directory.")
    run.add_argument("--profile", action="append", choices=sorted(BUILTIN_PROFILES),
                     help="Repeatable. Defaults to every profile.")
    run.add_argument("--encoder", choices=[e.value for e in Encoder], default=None)
    run.add_argument("--db", default=None, help="SQLite path. Defaults to data/screencut.db.")
    run.add_argument("--force", action="store_true", help="Ignore the cache and re-run every stage.")

    ingest = subcommands.add_parser("ingest", help="Turn a recorder bundle into a job directory.")
    ingest.add_argument("recording", help="A Cap `.cap` bundle.")
    ingest.add_argument("--out", required=True, help="Job directory to create.")
    ingest.add_argument("--job-id", default=None, help="Defaults to the job directory's name.")
    ingest.add_argument("--segment", type=int, default=0, help="Which recorded segment to take.")

    narrate = subcommands.add_parser(
        "narrate", help="Attach a script and a voice reference to a job, for synthesis (decision #20)."
    )
    narrate.add_argument("job", help="Job directory, from `ingest` or a fixture generator.")
    narrate.add_argument("--script", required=True, help="Text file holding the narration script.")
    narrate.add_argument("--voice", required=True, help="Your reference recording. Copied into the job.")
    narrate.add_argument("--voice-text", required=True, help="What the reference recording says.")
    narrate.add_argument(
        "--consent", required=True,
        help="Recorded on the job so decision #20's boundary is auditable rather than assumed.",
    )

    args = parser.parse_args(argv)
    if args.command == "ingest":
        return _ingest(args)
    if args.command == "narrate":
        return _narrate(args)
    result = run_job(
        Path(args.job),
        args.profile,
        encoder=Encoder(args.encoder) if args.encoder else None,
        db_path=args.db,
        force=args.force,
    )
    for outcome in result.outcomes:
        print(f"  {outcome}")
    if result.did_no_work:
        print(f"{result.job_id}: nothing to do — every stage was cached")
    else:
        print(f"{result.job_id}: ran {', '.join(result.ran())}")
    for profile, path in result.renders.items():
        print(f"  {profile} -> {path}")
    for report in result.reports.values():
        print(report.summary())
    return 0 if result.verified else 1


def _ingest(args) -> int:
    """Cap bundle in, a job directory the pipeline can run out.

    The spec written here carries only what ingest owns — the source and the
    focus track. Captions arrive from the job-level stages `job.json` asks for,
    and `EditDecisions` stays empty until phase 5, which the compiler already
    reads as "the whole take survives" (compile/timeline.py).
    """
    from ingest.cap import ingest as read_cap
    from runner.job import JobConfig
    from spec.editspec import EditSpec

    job_dir = Path(args.out)
    job_dir.mkdir(parents=True, exist_ok=True)
    source, focus = read_cap(args.recording, job_dir, segment=args.segment)
    spec = EditSpec(job_id=args.job_id or job_dir.name, source=source, focus=focus)
    (job_dir / "spec.json").write_text(spec.model_dump_json(indent=2) + "\n")
    JobConfig(
        stages=list(RECORDED_STAGES), recorder="cap", recording=str(args.recording)
    ).write(job_dir)

    print(f"{spec.job_id}: {source.width}x{source.height} @ {source.fps:g}fps, {source.duration:.2f}s")
    print(f"  focus track: {len(focus.points)} points, {len(focus.clicks())} on clicks")
    print(f"  wrote {job_dir / 'spec.json'} and {job_dir / 'job.json'}")
    return 0


def _narrate(args) -> int:
    """Turn a recorded job into a narrated one: script in, voice reference in.

    The reference is *copied into the job directory* rather than pointed at. Every
    path in a spec is relative to the job (§5.1), which is what lets a stage run on
    a worker at all — and a voice reference that lived outside would be the one
    input that silently did not travel.
    """
    import shutil

    from runner.job import JobConfig
    from runner.stages import SYNTHESIZED_STAGES
    from spec.narration import Narration, NarrationSource

    job_dir = Path(args.job)
    spec = load_spec_file(job_dir / "spec.json")
    script = Path(args.script).read_text().strip()

    reference = Path("source") / f"voice{Path(args.voice).suffix or '.wav'}"
    (job_dir / "source").mkdir(parents=True, exist_ok=True)
    shutil.copy2(args.voice, job_dir / reference)

    narrated = spec.model_copy(
        update={
            "narration": Narration(
                source=NarrationSource.SYNTHESIZED,
                script=script,
                voice_reference_path=str(reference),
                voice_reference_text=args.voice_text,
                voice_consent_note=args.consent,
            )
        }
    )
    (job_dir / "spec.json").write_text(narrated.model_dump_json(indent=2) + "\n")
    previous = JobConfig.load(job_dir)
    JobConfig(
        stages=list(SYNTHESIZED_STAGES), recorder=previous.recorder, recording=previous.recording
    ).write(job_dir)

    print(f"{narrated.job_id}: {len(script.split())} words of script, voice from {reference}")
    print(f"  stages: {' -> '.join(SYNTHESIZED_STAGES)}")
    print(f"  consent: {args.consent}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
