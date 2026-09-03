"""Golden-set replay, split by field origin (architecture.md §11, §11.1).

The split is the whole subject. One tolerance cannot serve both halves: wide
enough to absorb model variance is too wide to catch a `plan_focus` regression,
and strict enough to catch that fires on every replay of a model stage. So these
tests are mostly one claim stated twice — that a moved deterministic field is a
finding, and that a moved model-written field is not, and that the second is
visible somewhere else.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from golden.replay import (
    GOLDEN_DIR,
    Distribution,
    GoldenCase,
    Tolerances,
    boundary_drift,
    cases,
    distributions,
    percentile,
    replay,
    spec_drift,
    summarize,
)
from spec.migrations import load_spec

APPROVED = GOLDEN_DIR / "demo_v1" / "spec.approved.json"


@pytest.fixture(scope="module")
def approved_doc() -> dict:
    return json.loads(APPROVED.read_text())


def moved(doc: dict, **_) -> dict:
    return copy.deepcopy(doc)


# --- the strict half ---------------------------------------------------------


def test_a_deterministic_field_that_moves_is_a_finding_that_names_its_stage(approved_doc):
    """Most of the spec — and most regressions — are still deterministic, and a
    change to `plan_focus` should fail loudly on one run rather than disappear
    into a distribution."""
    changed = moved(approved_doc)
    changed["focus"]["points"][10]["x"] += 0.01
    changed["audio"]["target_lufs"] = -12.0

    found = {d.path: d for d in spec_drift(load_spec(approved_doc), load_spec(changed))}
    assert set(found) == {"focus.points[10].x", "audio.target_lufs"}
    assert found["focus.points[10].x"].stage.value == "ingest"
    assert found["audio.target_lufs"].approved == -14.0


def test_a_model_written_field_is_not_checked_strictly(approved_doc):
    """Replaying the same fixture twice produces different specs, so prompt-change
    drift and sampling noise would look identical here. A weaker check on these
    fields is the true one; pretending otherwise makes the whole set untrustworthy
    rather than just the uncertain part of it."""
    changed = moved(approved_doc)
    changed["edit"]["segments"][0]["tier"] = "optional"
    changed["overlays"] = []

    assert spec_drift(load_spec(approved_doc), load_spec(changed)) == []


def test_the_model_half_is_visible_in_the_distribution_instead(approved_doc):
    """The other half of the claim above: skipped strictly is not ignored."""
    changed = moved(approved_doc)
    changed["overlays"] = []

    checks = {
        d.name: d
        for d in distributions(load_spec(approved_doc), [load_spec(changed)], Tolerances())
    }
    assert not checks["overlay_count"].within
    assert checks["overlay_count"].approved == 4.0 and checks["overlay_count"].median == 0.0


def test_bookkeeping_is_not_drift(approved_doc):
    """A replay is a different job at a different time (§4.2). If `created_at`
    counted, every replay would fail and the check would be ignored in a week."""
    changed = moved(approved_doc)
    changed["created_at"] = "2030-01-01T00:00:00Z"
    changed["job_id"] = "replayed"
    assert spec_drift(load_spec(approved_doc), load_spec(changed)) == []


def test_a_missing_value_is_the_loudest_finding_rather_than_a_crash(approved_doc):
    """One document having a caption the other does not is a structural
    difference, and it should read like one."""
    changed = moved(approved_doc)
    changed["captions"] = changed["captions"][:-1]

    drift = spec_drift(load_spec(approved_doc), load_spec(changed))
    assert drift, "dropping a caption block must be found"
    assert all(d.proposed is None for d in drift)


# --- the distributional half -------------------------------------------------


def test_the_median_is_what_is_checked_not_every_run():
    """A distribution is the check, so one sample outside the band is what a
    distribution looks like. What a regression looks like is the middle moving."""
    scattered = Distribution(name="x", approved=1.0, runs=[1.0, 1.0, 1.0, 9.0], tolerance=0.5)
    assert scattered.within and scattered.spread == 8.0

    shifted = Distribution(name="x", approved=1.0, runs=[3.0, 3.1, 2.9], tolerance=0.5)
    assert not shifted.within


def test_a_summary_compares_the_shape_of_the_decision_not_field_by_field(approved_doc):
    """`tier` on segment 7 is not comparable between two runs that cut the take
    into different numbers of segments."""
    stats = summarize(load_spec(approved_doc))
    assert 0.0 < stats["retained_fraction"] <= 1.0
    assert stats["segment_count"] == 5.0
    assert stats["overlay_count"] == 4.0


def test_boundary_drift_measures_the_nearest_cut(approved_doc):
    """Cuts rather than segments: a segment split in two is not a moved boundary
    and must not read as one."""
    approved = load_spec(approved_doc)
    assert boundary_drift(approved, approved) == [0.0] * len(boundary_drift(approved, approved))

    # Every interior boundary moved by the same 0.2. Adjacent spans share one
    # value, so shifting both sides keeps §4.4's partition total — a "nudged
    # edit" that broke totality would be testing the validator, not the drift.
    nudged = moved(approved_doc)
    end = nudged["source"]["duration"]
    for span in [*nudged["edit"]["removals"], *nudged["edit"]["segments"]]:
        span["t_in"] = span["t_in"] + 0.2 if span["t_in"] > 0.0 else 0.0
        span["t_out"] = span["t_out"] + 0.2 if span["t_out"] < end else end
    drift = boundary_drift(approved, load_spec(nudged))
    assert drift and max(drift) == pytest.approx(0.2, abs=1e-6)


def test_a_replay_that_proposed_no_cuts_at_all_drifts_by_the_whole_take(approved_doc):
    """Silence is not agreement. A proposal with no seams is maximally far from
    one with seams, and reporting zero drift would say the opposite."""
    approved = load_spec(approved_doc)
    empty = moved(approved_doc)
    empty["edit"] = {"removals": [], "segments": []}
    drift = boundary_drift(approved, load_spec(empty))
    assert drift and all(d == approved.source.duration for d in drift)


def test_percentiles_of_nothing_are_zero_rather_than_an_error():
    assert percentile([], 0.9) == 0.0
    assert percentile([0.1, 0.2, 0.9], 0.9) == 0.9


# --- the harness -------------------------------------------------------------


def test_the_committed_case_replays_clean():
    """§11's actual claim, run. `ingest/fixtures.py` is deterministic and
    byte-stable, and every test in this repository leans on that — so the golden
    set checking it is the check with the widest blast radius."""
    report = replay(GOLDEN_DIR / "demo_v1")
    assert report.passed, "\n".join(report.lines())
    assert report.drift == []


def test_the_violation_rate_says_unmeasured_rather_than_zero_when_no_model_ran():
    """"Could not run" and "ran and found nothing" are different states — the
    same lesson §9.2's checker flag learned one phase earlier. A rate of 0.00 over
    no calls reads as reassurance when it is the absence of a measurement."""
    report = replay(GOLDEN_DIR / "demo_v1")
    assert report.agent_calls == 0
    assert report.violation_rate is None
    assert "R5 unmeasured" in "\n".join(report.lines())


def test_every_committed_case_has_a_manifest_that_loads():
    found = cases()
    assert [p.name for p in found] == ["demo_v1"], (
        "a real take promoted here is phase 4's one unfinished build item"
    )
    for directory in found:
        case = GoldenCase.load(directory)
        assert case.profiles and case.runs >= 1
        assert case.approved(directory).spec_version >= 1


def test_a_case_replays_each_run_in_a_directory_of_its_own(tmp_path):
    """N runs sharing one job directory would be N runs of which N-1 were cache
    hits, which is the one thing a distributional check cannot be measuring."""
    report = replay(GOLDEN_DIR / "demo_v1", runs=3)
    assert report.runs == 3
    assert all(len(d.runs) == 3 for d in report.distributions)
    assert report.passed


# --- a case that actually asks a model something -----------------------------


@pytest.fixture
def modelled_case(tmp_path, fake_agent):
    """A golden case built here rather than committed, and the reason is phase 4's.

    Nothing in `golden/` calls a model yet: the one committed case is the
    synthetic fixture, which arrives with a complete spec and runs no job-level
    stage. Promoting a real take is phase 4's unfinished build item, and until it
    happens the distributional half has no *archived* fixture to run against — so
    it is exercised here, against the same scripted subprocess phase 5 used, which
    proves the harness and proves nothing about editorial taste.
    """
    from ingest.cap_fixture import write_bundle
    from ingest.fixtures import DEFAULT_BEATS
    from runner.cli import main as cli_main

    bundle = write_bundle(tmp_path / "take.cap", beats=DEFAULT_BEATS[:1],
                          width=320, height=180, fps=30.0)
    archived = tmp_path / "archived"
    cli_main(["ingest", str(bundle), "--out", str(archived)])
    doc = json.loads((archived / "spec.json").read_text())
    doc["source"]["has_audio"] = False
    (archived / "spec.json").write_text(json.dumps(doc, indent=2))

    case = tmp_path / "take_v1"
    case.mkdir()
    (case / "manifest.json").write_text(json.dumps({
        "recipe": {"directory": "archived"},
        "profiles": ["demo_16x9"],
        "runs": 3,
    }))
    # The approved answer: this job, planned once with the agent saying nothing.
    from runner.pipeline import run_job

    run_job(archived, ["demo_16x9"], db_path=tmp_path / "seed.db", plan_only=True)
    (case / "spec.approved.json").write_text((archived / "spec.json").read_text())
    (tmp_path / "archived").rename(case / "archived")
    return case


def test_a_replay_records_the_schema_violation_rate_across_its_runs(modelled_case, fake_agent):
    """Phase 9's third exit criterion, and the first real measurement of risk R5.

    One reply in three is rejected and retried, so four `OverlayPlan` calls buy
    three fragments — which is exactly the shape §7.2 predicted the honest cost of
    decision #13 would take: one extra round trip, landing somewhere the design
    already handles."""
    fake_agent.fragments(OverlayPlan=[
        {"text": json.dumps({"overlays": []})},
        {"text": json.dumps({"overlays": [{"template": "not_a_template"}]})},
        {"text": "```json\n{\"overlays\": []}\n```"},
        {"text": json.dumps({"overlays": []})},
    ])
    report = replay(modelled_case, workers=1)

    assert report.runs == 3
    assert report.agent_calls > 0, "the point of this case is that a model was asked"
    assert report.schema_violations == 1
    assert report.violation_rate == pytest.approx(1 / report.agent_calls)
    assert "R5 unmeasured" not in "\n".join(report.lines())
    assert not report.degradations, "one retry was enough (§7.2)"


def test_replaying_a_planned_case_leaves_the_deterministic_half_untouched(modelled_case):
    """The split, over a case where the model half is live: `trim`'s removals and
    `plan_captions`'s blocks are arithmetic and must reproduce exactly, whatever
    the model did beside them."""
    report = replay(modelled_case, workers=1)
    assert report.drift == [], "\n".join(report.lines())
