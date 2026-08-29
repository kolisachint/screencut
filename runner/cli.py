"""`screencut run <job>` (architecture.md phase 3)."""

from __future__ import annotations

import argparse
from pathlib import Path

from spec.profiles import BUILTIN_PROFILES, Encoder

from runner.pipeline import run_job


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

    args = parser.parse_args(argv)
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
