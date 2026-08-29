"""Running a job: what is stale, what is cached, what gets published.

This is the part §8 leans on. A correction in review rewrites the spec and re-runs
the pipeline; if the cache holds, only the stages the correction actually touched
do any work. Cache correctness is a review-UI requirement, not an optimization.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from prefs import resolve_profile
from spec.editspec import EditSpec
from spec.migrations import load_spec_file
from spec.profiles import BUILTIN_PROFILES, Encoder, RenderProfile

from runner import db
from runner.cache import cache_key
from runner.contract import StageRequest
from runner.local import LocalRunner
from runner.stages import INPUT_NAMES, ORDER, STAGES, StageContext
from verify.report import VerificationReport


@dataclass
class StageOutcome:
    stage: str
    profile: str
    cache_key: str
    path: str
    cached: bool

    def __str__(self) -> str:
        state = "cached " if self.cached else "ran    "
        return f"{state}{self.profile:>12} {self.stage:<11} {self.cache_key[:12]}"


@dataclass
class JobRun:
    job_id: str
    outcomes: list[StageOutcome] = field(default_factory=list)
    renders: dict[str, Path] = field(default_factory=dict)
    reports: dict[str, VerificationReport] = field(default_factory=dict)

    @property
    def verified(self) -> bool:
        return all(report.passed for report in self.reports.values())

    @property
    def did_no_work(self) -> bool:
        return all(outcome.cached for outcome in self.outcomes)

    def ran(self) -> list[str]:
        return [f"{o.profile}/{o.stage}" for o in self.outcomes if not o.cached]


def run_job(
    job_dir: Path | str,
    profiles: list[str | RenderProfile] | None = None,
    *,
    encoder: Encoder | None = None,
    db_path: Path | str | None = None,
    runner: LocalRunner | None = None,
    force: bool = False,
) -> JobRun:
    job_dir = Path(job_dir)
    spec = load_spec_file(job_dir / "spec.json")
    runner = runner or LocalRunner()
    db_path = Path(db_path) if db_path else db.DEFAULT_DB_PATH
    names = profiles if profiles is not None else list(BUILTIN_PROFILES)

    run = JobRun(job_id=spec.job_id)
    with db.connect(db_path) as connection:
        db.upsert_job(
            connection,
            job_id=spec.job_id,
            job_dir=str(job_dir),
            spec_version=spec.spec_version,
            status="running",
        )
        for name in names:
            # A name goes through `constraints.yaml`; a profile object arrives already
            # resolved. The review UI will pass the second kind, since "make the short
            # shorter" is one edited field on a profile rather than a new named one.
            profile = name if isinstance(name, RenderProfile) else resolve_profile(name)
            _run_profile(run, connection, spec, profile, job_dir, runner, encoder, force)
        db.upsert_job(
            connection,
            job_id=spec.job_id,
            job_dir=str(job_dir),
            spec_version=spec.spec_version,
            status="rendered" if run.verified else "failed_verification",
        )
    return run


def _run_profile(
    run: JobRun,
    connection,
    spec: EditSpec,
    profile: RenderProfile,
    job_dir: Path,
    runner: LocalRunner,
    encoder: Encoder | None,
    force: bool,
) -> None:
    context = StageContext(
        spec=spec, profile=profile, job_dir=job_dir, encoder=encoder or profile.encode.encoder
    )
    keys: dict[str, str] = {}
    paths: dict[str, str] = {}

    for name in ORDER:
        stage = STAGES[name]
        # A stage's inputs include its upstream stages' keys, which is how bumping
        # one stage_version invalidates that stage and its dependents — and nothing
        # else, since a sibling's key is not in anybody's inputs.
        key = cache_key(
            stage=stage.name,
            stage_version=stage.version,
            inputs={
                "self": stage.fingerprint(context),
                "upstream": {dependency: keys[dependency] for dependency in stage.depends_on},
            },
            params={},
            model_backed=stage.model_backed,
        )
        keys[name] = key
        artifact = Path("stages") / f"{key}{stage.suffix}"
        paths[name] = str(artifact)

        cached = _is_cached(connection, key, job_dir / artifact, stage.directory) and not force
        if not cached:
            request = StageRequest(
                stage=name,
                job_dir=str(job_dir),
                inputs={
                    "spec": "spec.json",
                    **{
                        INPUT_NAMES[dependency]: paths[dependency]
                        for dependency in stage.depends_on
                    },
                },
                params={"profile": profile.model_dump(mode="json"), "encoder": context.encoder.value},
                output=str(artifact),
            )
            runner.run(request, holds_local_weights=stage.holds_local_weights)
            db.record_artifact(
                connection,
                cache_key=key,
                job_id=spec.job_id,
                stage=name,
                stage_version=stage.version,
                profile=profile.name,
                path=str(artifact),
            )
        run.outcomes.append(
            StageOutcome(stage=name, profile=profile.name, cache_key=key, path=str(artifact), cached=cached)
        )

    run.renders[profile.name] = _publish(job_dir, Path(paths["render"]), spec.job_id, profile.name)
    report = VerificationReport.model_validate_json((job_dir / paths["verify"]).read_text())
    run.reports[profile.name] = report
    db.record_verification(
        connection,
        job_id=spec.job_id,
        profile=profile.name,
        passed=report.passed,
        findings_json=report.model_dump_json(),
    )


def _is_cached(connection, key: str, path: Path, directory: bool) -> bool:
    """A row is not enough. The file has to be there.

    Artifacts are files and rows are rows (§5.4), so they can disagree — a job
    directory copied without `stages/`, a cache swept for disk (§5.2). Trusting the
    row alone turns that into a render that never happens.
    """
    if db.lookup_artifact(connection, key) is None:
        return False
    if not (path.is_dir() if directory else path.is_file()):
        db.forget_artifact(connection, key)
        return False
    return True


def _publish(job_dir: Path, artifact: Path, job_id: str, profile: str) -> Path:
    """Give the cached render a stable human-facing name.

    A hard link rather than a copy: the cache is content-addressed and immutable,
    `renders/` is a view onto it, and 256GB (§16) is not enough to keep two of
    every render.
    """
    destination = job_dir / "renders" / f"{job_id}_{profile}.mp4"
    destination.parent.mkdir(parents=True, exist_ok=True)
    source = job_dir / artifact
    if destination.exists():
        if destination.samefile(source):
            return destination
        destination.unlink()
    try:
        os.link(source, destination)
    except OSError:
        shutil.copy2(source, destination)
    return destination
