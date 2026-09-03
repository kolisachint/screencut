"""Golden-set replay, split by field origin (architecture.md §11, §11.1).

Replaying a fixture and diffing its spec against the approved one was the right
check while planning was deterministic. It stopped being sufficient the moment
model stages started writing spec fields: replay the same fixture twice and the
two specs differ, so prompt-change drift and sampling noise look identical, and a
tolerance wide enough to absorb the second is too wide to catch the first.

So every field carries its producing stage (`spec/origin.py`) and the origin
decides the check:

| Origin | Check |
|---|---|
| Deterministic | Strict per-field tolerance, **one run**. Any difference is a finding. |
| Model | Distributional over **N runs**: retained fraction, counts, boundary drift. |

Keeping the strict half strict is the point. Most of the spec — and most
regressions — are still deterministic, and a change to `plan_focus` should fail
loudly on one run rather than disappear into a distribution.

**Specs, not pixels** (§11). Renders are slow, so replay runs with
`plan_only=True`: the job-level stages, the spec written, nothing encoded. Two or
three cases get frame hashing when there is something worth hashing; a harness
that encoded every fixture would be run once and then never again.

**Parallel across runs, not across stages** (§7.5). The N runs of the model half
are independent subprocesses with no shared state, and the stages that hold local
weights are the ones a replay of a synthetic case does not touch. Threads rather
than processes because every unit of work here is a subprocess wait.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from pydantic import BaseModel, ConfigDict, Field

from runner.pipeline import JobRun, run_job
from spec.edit import Tier
from spec.editspec import EditSpec
from spec.migrations import load_spec_file
from spec.origin import Origin, Stage, field_origins

GOLDEN_DIR = Path(__file__).resolve().parent
MANIFEST_NAME = "manifest.json"
APPROVED_NAME = "spec.approved.json"

NUMERIC_TOLERANCE = 1e-6
"""The strict half's tolerance, and it is strict on purpose.

Fixtures are byte-stable by construction (`ingest/fixtures.py`) and every
deterministic planner is arithmetic over them, so the honest tolerance is float
noise and nothing more. A looser one here would quietly absorb the regressions
this half exists to catch.
"""


class Recipe(BaseModel):
    """How to rebuild a case's job directory.

    A synthetic fixture is archived as a recipe rather than as bytes: the
    generator is deterministic and byte-stable, so committing 115KB of
    regenerable focus points would archive the output of a function next to the
    function (`golden/README.md`). A real take has no recipe and is archived
    whole, which is what `directory` is for.
    """

    model_config = ConfigDict(extra="forbid")

    module: str | None = Field(
        default=None, description="Run as `python -m <module>`, with {out} substituted."
    )
    args: list[str] = Field(default_factory=list)
    directory: str | None = Field(
        default=None, description="Copy this directory instead. For an archived real take."
    )


class Tolerances(BaseModel):
    """The distributional half's bands (§11.1).

    Per case, because what counts as drift depends on the footage: a two-minute
    demo tolerates a segment more than a fifteen-second short does. Every default
    here is a starting number rather than a measurement — none has met a real take,
    which is the same debt `SEAM_TOLERANCE_S` and the focus tunables carry.
    """

    model_config = ConfigDict(extra="forbid")

    retained_fraction: float = 0.15
    """How far the kept share of the source may move, in absolute fraction."""
    segment_count: float = 2.0
    boundary_drift_p90_s: float = 1.0
    """Where a proposed cut may land relative to the approved one it is nearest."""
    overlay_count: float = 2.0
    emphasis_fraction: float = 0.10


class GoldenCase(BaseModel):
    """One archived fixture and the answer it is expected to reproduce."""

    model_config = ConfigDict(extra="forbid")

    name: str
    recipe: Recipe
    profiles: list[str] = Field(default_factory=lambda: ["shorts_9x16", "demo_16x9"])
    runs: int = Field(default=1, ge=1)
    """How many times to replay for the distributional half.

    One is right for a case with no model stage — running a deterministic
    pipeline five times to compute the variance of zero is a slow way to learn
    nothing — and the harness says so rather than pretending otherwise.
    """
    tolerances: Tolerances = Field(default_factory=Tolerances)
    note: str = ""

    @classmethod
    def load(cls, directory: Path | str) -> "GoldenCase":
        directory = Path(directory)
        raw = json.loads((directory / MANIFEST_NAME).read_text())
        return cls.model_validate({"name": directory.name, **raw})

    def approved(self, directory: Path | str) -> EditSpec:
        return load_spec_file(Path(directory) / APPROVED_NAME)


# --- the strict half ---------------------------------------------------------


class FieldDrift(BaseModel):
    """One deterministic field that moved. Any of these is a failure."""

    model_config = ConfigDict(extra="forbid")

    path: str
    stage: Stage
    approved: Any = None
    proposed: Any = None

    def __str__(self) -> str:
        return f"{self.path}: {self.approved!r} -> {self.proposed!r} ({self.stage.value})"


def _origin_by_path(model: type[BaseModel]) -> dict[str, tuple[Stage, Origin]]:
    return {f.path: (f.stage, f.origin) for f in field_origins(model)}


def _leaves(value: Any, path: str) -> Iterable[tuple[str, str, Any]]:
    """Every scalar in a dumped document, as `(addressed_path, origin_path, value)`.

    Two paths, because they are two different things. The addressed path carries
    list indices so a finding names *which* segment moved; the origin path drops
    them so it can be looked up in the schema metadata, which describes a field
    rather than an occurrence of one.
    """
    if isinstance(value, dict):
        for key, nested in value.items():
            child = f"{path}.{key}" if path else key
            yield from _leaves(nested, child)
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            yield from _leaves(nested, f"{path}[{index}]")
    else:
        yield path, _unindexed(path), value


def _unindexed(path: str) -> str:
    out = []
    for part in path.split("."):
        out.append(part.split("[")[0])
    return ".".join(out)


def spec_drift(approved: EditSpec, proposed: EditSpec) -> list[FieldDrift]:
    """Per-field drift over the **deterministic** fields only (§11.1).

    Model-written fields are skipped here rather than compared loosely, because a
    loose comparison is what makes a strict check untrustworthy: a reader who has
    seen one field pass on a tolerance stops believing the ones that did not need
    it. They are checked in `distributions`, over N runs, where the check is true.

    A structural difference — one document having a segment the other does not —
    is reported as a drift with a missing side rather than crashing the walk. It
    is the loudest thing this half can find and it should read like one.
    """
    origins = _origin_by_path(EditSpec)
    skip = {"created_at", "job_id"}  # bookkeeping (§4.2); a replay is a different job

    left = dict(
        (addressed, (origin, value))
        for addressed, origin, value in _leaves(approved.model_dump(mode="json"), "")
    )
    right = dict(
        (addressed, (origin, value))
        for addressed, origin, value in _leaves(proposed.model_dump(mode="json"), "")
    )

    drifts: list[FieldDrift] = []
    for addressed in sorted(set(left) | set(right)):
        origin_path = (left.get(addressed) or right[addressed])[0]
        if origin_path in skip:
            continue
        found = origins.get(origin_path)
        if found is None or found[1] is not Origin.DETERMINISTIC:
            continue
        a = left.get(addressed, (None, _MISSING))[1]
        b = right.get(addressed, (None, _MISSING))[1]
        if not _same(a, b):
            drifts.append(
                FieldDrift(
                    path=addressed,
                    stage=found[0],
                    approved=None if a is _MISSING else a,
                    proposed=None if b is _MISSING else b,
                )
            )
    return drifts


class _Missing:
    def __repr__(self) -> str:
        return "(absent)"


_MISSING = _Missing()


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(a - b) <= NUMERIC_TOLERANCE
    return a == b


# --- the distributional half -------------------------------------------------


class Distribution(BaseModel):
    """One summary statistic over the model-written fields, across N runs."""

    model_config = ConfigDict(extra="forbid")

    name: str
    approved: float
    runs: list[float]
    tolerance: float

    @property
    def median(self) -> float:
        ordered = sorted(self.runs)
        if not ordered:
            return self.approved
        middle = len(ordered) // 2
        if len(ordered) % 2:
            return ordered[middle]
        return (ordered[middle - 1] + ordered[middle]) / 2.0

    @property
    def spread(self) -> float:
        return max(self.runs) - min(self.runs) if self.runs else 0.0

    @property
    def within(self) -> bool:
        """The **median** against the approved value, not every run.

        A distribution is the check here, so one sample outside the band is what a
        distribution looks like rather than a regression. What a regression looks
        like is the middle of the distribution having moved.
        """
        return abs(self.median - self.approved) <= self.tolerance + NUMERIC_TOLERANCE

    def __str__(self) -> str:
        mark = "ok  " if self.within else "DRIFT"
        return (
            f"{mark} {self.name:<20} approved {self.approved:.3f}  "
            f"median {self.median:.3f}  spread {self.spread:.3f}  tol {self.tolerance:g}"
        )


def summarize(spec: EditSpec) -> dict[str, float]:
    """The model half of a spec, as numbers §11.1 can compare across runs.

    Not the fields themselves. `tier` on segment 7 is not comparable between two
    runs that cut the take into different numbers of segments, and pretending
    otherwise produces a diff whose length is the regression signal. What is
    comparable is the shape of the decision: how much survived, in how many
    pieces, how close the seams landed, how much furniture went on top.
    """
    duration = spec.source.duration or 1.0
    words = [w for block in spec.captions for w in block.words]
    return {
        "retained_fraction": (
            sum(s.duration for s in spec.edit.segments) / duration if spec.edit.segments else 1.0
        ),
        "essential_fraction": (
            spec.edit.selected_duration(Tier.ESSENTIAL) / duration if spec.edit.segments else 1.0
        ),
        "segment_count": float(len(spec.edit.segments)),
        "removal_count": float(len(spec.edit.removals)),
        "overlay_count": float(len(spec.overlays)),
        "emphasis_fraction": (
            sum(1 for w in words if w.emphasis) / len(words) if words else 0.0
        ),
    }


def boundary_drift(approved: EditSpec, proposed: EditSpec) -> list[float]:
    """For every approved cut, how far the nearest proposed cut landed from it.

    Cuts rather than segments, because a segment split in two is not a moved
    boundary and should not read as one — the seams are what a viewer hears
    (§9.2) and what a reviewer corrects.
    """
    reference = _cuts(approved)
    candidates = _cuts(proposed)
    if not reference:
        return []
    if not candidates:
        return [float(approved.source.duration)] * len(reference)
    return [min(abs(t - other) for other in candidates) for t in reference]


def _cuts(spec: EditSpec) -> list[float]:
    edges = {r.t_in for r in spec.edit.removals} | {r.t_out for r in spec.edit.removals}
    return sorted(t for t in edges if t > NUMERIC_TOLERANCE)


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(int(round(fraction * (len(ordered) - 1))), len(ordered) - 1)
    return ordered[index]


def distributions(
    approved: EditSpec, proposals: list[EditSpec], tolerances: Tolerances
) -> list[Distribution]:
    """§11.1's distributional check, one entry per statistic."""
    reference = summarize(approved)
    per_run = [summarize(spec) for spec in proposals]
    banded = {
        "retained_fraction": tolerances.retained_fraction,
        "essential_fraction": tolerances.retained_fraction,
        "segment_count": tolerances.segment_count,
        "removal_count": tolerances.segment_count,
        "overlay_count": tolerances.overlay_count,
        "emphasis_fraction": tolerances.emphasis_fraction,
    }
    out = [
        Distribution(
            name=name,
            approved=reference[name],
            runs=[run[name] for run in per_run],
            tolerance=tolerance,
        )
        for name, tolerance in banded.items()
    ]
    out.append(
        Distribution(
            name="boundary_drift_p90",
            approved=0.0,
            runs=[percentile(boundary_drift(approved, spec), 0.9) for spec in proposals],
            tolerance=tolerances.boundary_drift_p90_s,
        )
    )
    return out


# --- running one --------------------------------------------------------------


@dataclass
class RunOutcome:
    spec: EditSpec
    agent_calls: int
    schema_violations: int
    degradations: list[str]


class ReplayReport(BaseModel):
    """What one case's replay found. Numbers first, verdict after."""

    model_config = ConfigDict(extra="forbid")

    case: str
    runs: int
    drift: list[FieldDrift] = Field(default_factory=list)
    distributions: list[Distribution] = Field(default_factory=list)
    agent_calls: int = 0
    schema_violations: int = 0
    degradations: list[str] = Field(default_factory=list)

    @property
    def violation_rate(self) -> float | None:
        """Risk R5, measured (§7.2). `None` when nothing asked a model anything.

        None rather than zero, on the same rule phase 6 learned the hard way:
        "could not run" and "ran and found nothing" are different states, and a
        rate of 0.00 over no calls reads as a reassuring measurement when it is
        the absence of one.
        """
        if self.agent_calls == 0:
            return None
        return self.schema_violations / self.agent_calls

    @property
    def passed(self) -> bool:
        return not self.drift and all(d.within for d in self.distributions)

    def lines(self) -> list[str]:
        out = [f"{self.case}: {'PASS' if self.passed else 'FAIL'} over {self.runs} run(s)"]
        for drift in self.drift[:20]:
            out.append(f"  DRIFT {drift}")
        if len(self.drift) > 20:
            out.append(f"  ... and {len(self.drift) - 20} more deterministic fields")
        for distribution in self.distributions:
            out.append(f"  {distribution}")
        rate = self.violation_rate
        out.append(
            f"  schema violations: {self.schema_violations} of {self.agent_calls} replies"
            + (f" ({rate:.1%})" if rate is not None else " — no model stage ran (R5 unmeasured)")
        )
        for note in self.degradations:
            out.append(f"  degraded: {note}")
        return out


def materialize(case: GoldenCase, directory: Path, out: Path) -> Path:
    """Rebuild the case's job directory at `out`."""
    if case.recipe.directory:
        shutil.copytree(directory / case.recipe.directory, out)
        return out
    if not case.recipe.module:
        raise ValueError(f"golden case {case.name!r} has neither a module nor a directory")
    args = [arg.format(out=str(out)) for arg in case.recipe.args]
    subprocess.run(
        [sys.executable, "-m", case.recipe.module, *args], check=True, capture_output=True
    )
    return out


def _one_run(case: GoldenCase, directory: Path, workspace: Path, index: int) -> RunOutcome:
    """One independent replay, in a job directory of its own.

    Of its own because a replay writes `spec.json` and the stage cache alongside
    it, and N runs sharing one directory would be N runs of which N-1 were cache
    hits — which is the one thing a distributional check cannot be measuring.
    """
    job_dir = workspace / f"run{index}"
    materialize(case, directory, job_dir)
    run: JobRun = run_job(
        job_dir,
        case.profiles,
        db_path=workspace / f"run{index}.sqlite",
        plan_only=True,
        force=True,
    )
    return RunOutcome(
        spec=load_spec_file(job_dir / "spec.json"),
        agent_calls=run.agent_calls,
        schema_violations=run.schema_violations,
        degradations=list(run.degradations),
    )


def replay(directory: Path | str, *, runs: int | None = None, workers: int = 4) -> ReplayReport:
    """Replay one case and report what moved.

    The strict half reads the **first** run and only the first: a deterministic
    field that differs between two runs of the same fixture is a bug in the
    fixture, and averaging it away would hide exactly that.
    """
    directory = Path(directory)
    case = GoldenCase.load(directory)
    total = runs if runs is not None else case.runs
    approved = case.approved(directory)

    with tempfile.TemporaryDirectory(prefix=f"replay-{case.name}-") as raw:
        workspace = Path(raw)
        # §7.5: independent subprocesses, no shared state, so fan out. Threads
        # because each unit of work is a subprocess wait, and the stages that
        # hold local weights are serialized by `LocalRunner` regardless (§16).
        with ThreadPoolExecutor(max_workers=max(1, min(workers, total))) as pool:
            outcomes = list(
                pool.map(lambda i: _one_run(case, directory, workspace, i), range(total))
            )

    return ReplayReport(
        case=case.name,
        runs=total,
        drift=spec_drift(approved, outcomes[0].spec),
        distributions=distributions(
            approved, [o.spec for o in outcomes], case.tolerances
        ),
        agent_calls=sum(o.agent_calls for o in outcomes),
        schema_violations=sum(o.schema_violations for o in outcomes),
        degradations=sorted({note for o in outcomes for note in o.degradations}),
    )


def cases(root: Path | str = GOLDEN_DIR) -> list[Path]:
    return sorted(p.parent for p in Path(root).glob(f"*/{MANIFEST_NAME}"))


def main(argv: list[str] | None = None) -> int:
    """`python -m golden.replay [case ...]` — replay the set and report drift."""
    argv = argv if argv is not None else sys.argv[1:]
    wanted = [GOLDEN_DIR / name for name in argv] if argv else cases()
    if not wanted:
        print("no golden cases found", file=sys.stderr)
        return 2

    failed = False
    for directory in wanted:
        report = replay(directory)
        for line in report.lines():
            print(line)
        failed = failed or not report.passed
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
