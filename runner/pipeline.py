"""Running a job: what is stale, what is cached, what gets published.

This is the part §8 leans on. A correction in review layers over the spec and
re-runs the pipeline; if the cache holds, only the stages the correction actually
touched do any work. Cache correctness is a review-UI requirement, not an
optimization — and the same cache is why the correction has to be a layer rather
than an edit (§8.1): a cached planner would write its answer back over it.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from prefs import Constraints, load_constraints, resolve_profile
from spec.corrections import PROPOSED_NAME, Corrections
from spec.editspec import EditSpec
from spec.migrations import load_spec_file
from spec.profiles import BUILTIN_PROFILES, Encoder, RenderProfile

from runner import agent, db
from runner.cache import cache_key
from runner.contract import Runner, StageRequest
from runner.job import JobConfig
from runner.local import LocalRunner
from runner.stages import (
    INPUT_NAMES,
    JOB_INPUT_NAMES,
    JOB_ORDER,
    JOB_STAGES,
    ORDER,
    STAGES,
    JobContext,
    StageContext,
)
from verify.report import VerificationReport


@dataclass
class StageOutcome:
    stage: str
    profile: str
    cache_key: str
    path: str
    cached: bool
    note: str | None = None
    """What the stage said about what it did — words transcribed, removals
    proposed, seconds of narration and at what fraction of realtime.

    Every stage already computed one and the pipeline threw it away. They are the
    numbers that say whether a stage did something sensible, and the same argument
    as §9.1's report being numbers rather than a verdict applies to them: a
    coverage of 40% on an alignment is not a failure, and it is exactly what you
    want to see before wondering why the captions drift."""

    def __str__(self) -> str:
        state = "cached " if self.cached else "ran    "
        line = f"{state}{self.profile:>12} {self.stage:<17} {self.cache_key[:12]}"
        return f"{line}  {self.note}" if self.note and not self.cached else line


@dataclass
class JobRun:
    job_id: str
    outcomes: list[StageOutcome] = field(default_factory=list)
    profiles: list[RenderProfile] = field(default_factory=list)
    """The profiles as rendered, corrections applied. Review reads the budget it
    actually got back off this rather than re-deriving it (§8)."""
    renders: dict[str, Path] = field(default_factory=dict)
    reports: dict[str, VerificationReport] = field(default_factory=dict)
    degradations: list[str] = field(default_factory=list)
    """Stages that produced their deterministic fallback rather than their real
    answer (§7.4). Carried to the job record because under decision #12 the review
    page is the only place a degraded job announces itself."""

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
    remote_runner: Runner | None = None,
    force: bool = False,
) -> JobRun:
    """`remote_runner`, when given, takes the stages that asked for it.

    Phase 0 made that concrete rather than hypothetical: `tts` is 0.11x realtime
    on this machine (environment findings §4), so it is the one stage whose
    `prefers_remote` is set. Everything else stays where the media is."""
    job_dir = Path(job_dir)
    # Read before anything runs, applied after everything job-level has: a
    # correction is a layer over the planners' spec, not an edit of it
    # (spec/corrections.py). The planners are cached, so a spec they rewrite from
    # cache would otherwise overwrite the correction on the very next run.
    corrections = Corrections.load(job_dir)
    _restore_proposal(job_dir, corrections)

    spec = load_spec_file(job_dir / "spec.json")
    runner = runner or LocalRunner()
    db_path = Path(db_path) if db_path else db.DEFAULT_DB_PATH
    names = profiles if profiles is not None else list(BUILTIN_PROFILES)

    constraints = load_constraints()
    run = JobRun(job_id=spec.job_id)
    with db.connect(db_path) as connection:
        db.upsert_job(
            connection,
            job_id=spec.job_id,
            job_dir=str(job_dir),
            spec_version=spec.spec_version,
            status="running",
        )
        # A name goes through `constraints.yaml`; a profile object arrives already
        # resolved. Either way the correction layer goes on last, so "make the short
        # shorter" reaches a `screencut run` from the shell and not only the page it
        # was typed on.
        resolved = tuple(
            corrections.apply_to_profile(
                name if isinstance(name, RenderProfile) else resolve_profile(name)
            )
            for name in names
        )
        # The job-level stages first and in full, because they rewrite `spec.json`
        # and every per-profile fingerprint is taken from the spec. Interleaving
        # them would have `compile` hash a document that changed after it looked.
        spec, job_paths, job_keys = _run_job_stages(
            run, connection, spec, resolved, job_dir, runner, remote_runner, force,
            constraints, corrections,
        )
        run.profiles = list(resolved)
        for profile in resolved:
            _run_profile(
                run, connection, spec, profile, job_dir, runner, encoder, force,
                constraints, job_paths, job_keys,
            )
        db.upsert_job(
            connection,
            job_id=spec.job_id,
            job_dir=str(job_dir),
            spec_version=spec.spec_version,
            status="rendered" if run.verified else "failed_verification",
            degradations=run.degradations,
        )
    return run


def _run_job_stages(
    run: JobRun,
    connection,
    spec: EditSpec,
    profiles: tuple[RenderProfile, ...],
    job_dir: Path,
    runner: LocalRunner,
    remote_runner: Runner | None,
    force: bool,
    constraints: Constraints,
    corrections: Corrections,
) -> tuple[EditSpec, dict[str, str], dict[str, str]]:
    """Run what `job.json` asks for, once, and fold the results into the spec.

    Returns the spec the per-profile stages will render, plus their artifact paths
    and cache keys, which `verify` needs. A job directory with no `job.json` —
    every fixture in this repository — asks for nothing and still passes through
    the correction layer, because a fixture is as reviewable as a take.
    """
    wanted = set(JobConfig.load(job_dir).stages)
    if not wanted:
        return _write_spec(job_dir, spec, corrections, applied=False), {}, {}

    context = JobContext(
        spec=spec, profiles=profiles, job_dir=job_dir, constraints=constraints
    )
    keys: dict[str, str] = {}
    paths: dict[str, str] = {}
    applied = False

    for name in JOB_ORDER:
        if name not in wanted:
            continue
        stage = JOB_STAGES[name]
        if stage.provision in keys:
            # `transcribe` and `align` both provide `transcript` and a job asks for
            # one of them (§5.3). Asking for both is not a merge to resolve — it is
            # a job.json that says the narration is recorded and synthesized at
            # once, and the second stage would silently win.
            raise ValueError(
                f"{name!r} and an earlier stage both provide {stage.provision!r}; "
                f"a job runs one of them (runner/stages.py's two recipes)"
            )
        # §5.2's one subtlety that will not announce itself: a model stage's key
        # must carry the model id and prompt version, or the cache serves the old
        # answer after exactly the change you were evaluating. `cache_key` refuses
        # to compute one without them; this is where they come from.
        params = agent.cache_params(context.constraints.agent.model) if stage.model_backed else {}
        key = cache_key(
            stage=stage.name,
            stage_version=stage.version,
            inputs={
                "self": stage.fingerprint(context),
                "upstream": {dependency: keys[dependency] for dependency in stage.depends_on},
            },
            params=params,
            model_backed=stage.model_backed,
        )
        keys[stage.provision] = key
        artifact = Path("stages") / f"{key}{stage.suffix}"
        paths[stage.provision] = str(artifact)

        cached = _is_cached(connection, key, job_dir / artifact, stage.directory) and not force
        if not cached:
            request = StageRequest(
                stage=name,
                job_dir=str(job_dir),
                inputs={
                    "spec": "spec.json",
                    **{
                        JOB_INPUT_NAMES[dependency]: paths[dependency]
                        for dependency in stage.depends_on
                    },
                },
                params={
                    "asr": context.constraints.asr.model_dump(mode="json"),
                    "tts": context.constraints.tts.model_dump(mode="json"),
                    "trim": context.constraints.trim.model_dump(mode="json"),
                    "model": context.constraints.agent.model,
                    "profiles": [p.model_dump(mode="json") for p in profiles],
                },
                output=str(artifact),
            )
            result = _runner_for(stage, runner, remote_runner).run(
                request, holds_local_weights=stage.holds_local_weights
            )
            if result.degraded:
                # Deliberately not cached. A degraded artifact is what the stage
                # produced *because it could not run* — no network, no agent, a
                # fragment rejected twice (§7.4) — and caching it would make one
                # transient failure permanent. The file stays so the job finishes;
                # the missing row is what makes the next run try the model again.
                run.degradations.append(f"{name}: {result.note or 'degraded'}")
            else:
                db.record_artifact(
                    connection,
                    cache_key=key,
                    job_id=spec.job_id,
                    stage=name,
                    stage_version=stage.version,
                    profile="-",
                    path=str(artifact),
                )
        run.outcomes.append(
            StageOutcome(
                stage=name, profile="job", cache_key=key, path=str(artifact), cached=cached,
                note=None if cached else result.note,
            )
        )
        if stage.apply is not None:
            spec = stage.apply(spec, job_dir, str(artifact))
            applied = True
            # The next stage's fingerprint has to see the spec as it now stands.
            # `tts` writes `narration.audio_path` and `trim` measures the file it
            # names, so a context built once and kept would fingerprint `trim`
            # against a spec with no narration on the first run and one with it on
            # the second — a cache miss on every re-run of a job that changed
            # nothing, which is the review loop's whole cost model gone.
            context = JobContext(
                spec=spec, profiles=profiles, job_dir=job_dir, constraints=constraints
            )

    return _write_spec(job_dir, spec, corrections, applied), paths, keys


def _runner_for(stage, runner: LocalRunner, remote_runner: Runner | None) -> Runner:
    """Where this stage runs. The `Runner` protocol is the whole reason this is
    one line rather than a branch inside every stage (§5.1)."""
    return remote_runner if (stage.prefers_remote and remote_runner is not None) else runner


def _write_spec(
    job_dir: Path, proposed: EditSpec, corrections: Corrections, applied: bool
) -> EditSpec:
    """Put the correction layer on the planners' spec, and write both documents.

    `spec.json` is what everything downstream reads — principle 1, and the reason
    a caption list that lived only in `stages/` would be invisible to compile,
    verification and golden replay alike. `proposed.json` is the same document
    without the human layer, and it exists only while a correction does: it is the
    left-hand side of the diff §10 learns from, and deriving it afterwards from
    cached artifacts would be an archaeology exercise that a `--force` run breaks.
    """
    if corrections.empty:
        if applied:
            (job_dir / "spec.json").write_text(proposed.model_dump_json(indent=2) + "\n")
        return proposed

    corrected = corrections.apply_to(proposed)
    (job_dir / PROPOSED_NAME).write_text(proposed.model_dump_json(indent=2) + "\n")
    (job_dir / "spec.json").write_text(corrected.model_dump_json(indent=2) + "\n")
    return corrected


def _restore_proposal(job_dir: Path, corrections: Corrections) -> None:
    """With the corrections gone, the proposal is the spec again.

    Withdrawing a correction has to be as complete as making one. A job whose
    stages all rewrite the spec would recover on its own the next time they ran,
    but a fixture's would not, and "delete corrections.json" quietly meaning
    "keep them forever" is the same silence this whole layer exists to avoid.
    """
    previous = job_dir / PROPOSED_NAME
    if corrections.empty and previous.is_file():
        (job_dir / "spec.json").write_text(previous.read_text())
        previous.unlink()


def _run_profile(
    run: JobRun,
    connection,
    spec: EditSpec,
    profile: RenderProfile,
    job_dir: Path,
    runner: LocalRunner,
    encoder: Encoder | None,
    force: bool,
    constraints: Constraints,
    job_paths: dict[str, str] | None = None,
    job_keys: dict[str, str] | None = None,
) -> None:
    job_paths = job_paths or {}
    context = StageContext(
        spec=spec,
        profile=profile,
        job_dir=job_dir,
        encoder=encoder or profile.encode.encoder,
        job_keys=job_keys or {},
    )
    keys: dict[str, str] = {}
    paths: dict[str, str] = {}

    for name in ORDER:
        stage = STAGES[name]
        if any(required not in job_paths for required in stage.requires):
            # Nothing for it to read, so nothing for it to say. `verify_transcript`
            # on a fixture is the case: no `transcribe` ran, so there is no
            # transcript to diff the render against (§9.2). A skipped stage
            # contributes no key and no input to its dependents, which is exactly
            # right — it is not part of what they read.
            continue
        # A stage's inputs include its upstream stages' keys, which is how bumping
        # one stage_version invalidates that stage and its dependents — and nothing
        # else, since a sibling's key is not in anybody's inputs.
        key = cache_key(
            stage=stage.name,
            stage_version=stage.version,
            inputs={
                "self": stage.fingerprint(context),
                "upstream": {
                    dependency: keys[dependency]
                    for dependency in stage.depends_on
                    if dependency in keys
                },
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
                        if dependency in paths
                    },
                    # Job-level artifacts a per-profile stage reads: `trim`'s
                    # proposal for the override rate (§9.1), the source transcript
                    # for the round-trip (§9.2). Their keys are in the
                    # fingerprints, so a changed proposal re-verifies.
                    **{
                        JOB_INPUT_NAMES[wanted]: job_paths[wanted]
                        for wanted in (*stage.job_inputs, *stage.requires)
                        if wanted in job_paths
                    },
                },
                params={
                    "profile": profile.model_dump(mode="json"),
                    "encoder": context.encoder.value,
                    "asr": constraints.asr.model_dump(mode="json"),
                },
                output=str(artifact),
            )
            result = runner.run(request, holds_local_weights=stage.holds_local_weights)
            if result.degraded:
                # Same rule as the job-level stages, for the same reason: a
                # degraded artifact is what the stage produced because it could
                # not run, and caching it makes one missing checker permanent.
                run.degradations.append(f"{profile.name}/{name}: {result.note or 'degraded'}")
            else:
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
            StageOutcome(
                stage=name, profile=profile.name, cache_key=key, path=str(artifact),
                cached=cached, note=None if cached else result.note,
            )
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
