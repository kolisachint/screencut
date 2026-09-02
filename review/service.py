"""What the review page does, with no HTTP in it (architecture.md §8).

Kept apart from `app.py` so the loop this phase exists to prove — read a job,
correct it, re-render, accept — is testable as three function calls. The exit
criterion is about *which stages ran*, and that is a fact about the pipeline, not
about a status code.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from prefs import resolve_profile
from spec.corrections import CorrectionDiff, Corrections, PROPOSED_NAME, diff_specs
from spec.editspec import EditSpec
from spec.migrations import load_spec_file
from spec.profiles import BUILTIN_PROFILES, Encoder, RenderProfile

from runner import db
from runner.pipeline import JobRun, run_job
from verify.report import VerificationReport

ACCEPTED = "accepted"
REJECTED = "rejected"
DECISIONS = (ACCEPTED, REJECTED)


class UnknownJob(KeyError):
    """A job id with no row. Its own type so the app can answer 404 without
    guessing which KeyError it caught."""


@dataclass(frozen=True)
class JobSummary:
    """A row of the index."""

    job_id: str
    status: str
    degradations: list[str]
    updated_at: str
    decision: str | None

    @property
    def degraded(self) -> bool:
        return bool(self.degradations)


@dataclass(frozen=True)
class JobView:
    """Everything one review page shows.

    Both specs, because the page argues from the difference: the proposal is what
    the planners said and the spec is what is rendering, and a reviewer who cannot
    see which is which cannot tell a correction that took from one that did not.
    """

    job_id: str
    job_dir: Path
    status: str
    degradations: list[str]
    spec: EditSpec
    proposed: EditSpec
    profiles: list[RenderProfile]
    proposed_profiles: list[RenderProfile]
    corrections: Corrections
    diff: CorrectionDiff
    reports: dict[str, VerificationReport]
    renders: dict[str, Path]
    decision: str | None

    def payload(self) -> dict:
        """The JSON the page reads. Spec-shaped fields keep their generated types."""
        return {
            "job_id": self.job_id,
            "status": self.status,
            "degradations": self.degradations,
            "spec": self.spec.model_dump(mode="json"),
            "proposed": self.proposed.model_dump(mode="json"),
            "profiles": [p.model_dump(mode="json") for p in self.profiles],
            "proposed_profiles": [p.model_dump(mode="json") for p in self.proposed_profiles],
            "corrections": self.corrections.model_dump(mode="json"),
            "diff": self.diff.model_dump(mode="json"),
            "reports": {name: r.model_dump(mode="json") for name, r in self.reports.items()},
            "renders": sorted(self.renders),
            "decision": self.decision,
        }


def list_jobs(db_path: Path | str | None = None) -> list[JobSummary]:
    with db.connect(db_path or db.DEFAULT_DB_PATH) as connection:
        rows = db.list_jobs(connection)
        return [
            JobSummary(
                job_id=row["job_id"],
                status=row["status"],
                degradations=json.loads(row["degradations"]),
                updated_at=row["updated_at"],
                decision=_decision(connection, row["job_id"]),
            )
            for row in rows
        ]


def load_job(job_id: str, db_path: Path | str | None = None) -> JobView:
    with db.connect(db_path or db.DEFAULT_DB_PATH) as connection:
        row = db.get_job(connection, job_id)
        if row is None:
            raise UnknownJob(job_id)
        job_dir = Path(row["job_dir"])
        corrections = Corrections.load(job_dir)
        spec = load_spec_file(job_dir / "spec.json")
        # Without a correction there is nothing for the proposal to differ from,
        # so the pipeline does not write one and the spec *is* the proposal
        # (spec/corrections.py). Two documents where one would do is how they
        # drift.
        proposal = job_dir / PROPOSED_NAME
        proposed = load_spec_file(proposal) if proposal.is_file() else spec

        names = db.rendered_profiles(connection, job_id) or list(BUILTIN_PROFILES)
        proposed_profiles = [resolve_profile(name) for name in names if name in BUILTIN_PROFILES]
        profiles = [corrections.apply_to_profile(p) for p in proposed_profiles]

        reports: dict[str, VerificationReport] = {}
        for profile in profiles:
            record = db.latest_verification(connection, job_id, profile.name)
            if record is not None:
                reports[profile.name] = VerificationReport.model_validate_json(
                    record["findings_json"]
                )

        return JobView(
            job_id=job_id,
            job_dir=job_dir,
            status=row["status"],
            degradations=json.loads(row["degradations"]),
            spec=spec,
            proposed=proposed,
            profiles=profiles,
            proposed_profiles=proposed_profiles,
            corrections=corrections,
            diff=diff_specs(proposed, spec, proposed_profiles, profiles),
            reports=reports,
            renders=_renders(job_dir, spec.job_id, profiles),
            decision=_decision(connection, job_id),
        )


def correct(
    job_id: str,
    corrections: Corrections,
    *,
    db_path: Path | str | None = None,
    encoder: Encoder | None = None,
) -> tuple[JobView, JobRun]:
    """Write the correction layer and re-render through the ordinary pipeline.

    Through `run_job` rather than around it, on purpose. The whole claim of §8 is
    that a correction costs the stages it actually touched, and a review-only
    render path would be a second pipeline where that claim was never tested.
    """
    view = load_job(job_id, db_path)
    corrections.write(view.job_dir)
    run = run_job(
        view.job_dir,
        [p.name for p in view.proposed_profiles],
        encoder=encoder,
        db_path=db_path,
    )
    return load_job(job_id, db_path), run


def decide(
    job_id: str, decision: str, *, db_path: Path | str | None = None
) -> JobView:
    """Accept or reject, and record what was decided about which difference (§8).

    An accepted spec is stored per profile because preferences are learned per
    profile (§5.4) — the document is the same, the budget it was accepted under is
    not, and that budget is exactly what §10 reads back.
    """
    if decision not in DECISIONS:
        raise ValueError(f"decision must be one of {DECISIONS}, not {decision!r}")
    view = load_job(job_id, db_path)
    with db.connect(db_path or db.DEFAULT_DB_PATH) as connection:
        db.record_review(
            connection,
            job_id=job_id,
            decision=decision,
            diff_json=view.diff.model_dump_json(),
        )
        if decision == ACCEPTED:
            for profile in view.profiles:
                db.record_accepted_spec(
                    connection,
                    job_id=job_id,
                    profile=profile.name,
                    spec_json=view.spec.model_dump_json(),
                )
    return load_job(job_id, db_path)


def _renders(job_dir: Path, job_id: str, profiles: list[RenderProfile]) -> dict[str, Path]:
    found = {}
    for profile in profiles:
        path = job_dir / "renders" / f"{job_id}_{profile.name}.mp4"
        if path.is_file():
            found[profile.name] = path
    return found


def _decision(connection, job_id: str) -> str | None:
    row = db.latest_review(connection, job_id)
    return row["decision"] if row else None
