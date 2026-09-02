"""The review loop (phase 7, architecture.md §8).

Phase 7's exit criteria are all about *cost*: a correction has to re-run the
stages it touched and no others, because a review loop that costs a model call
per correction gets abandoned and the corrections that feed §10 stop happening.
So these tests assert which stages ran, not that the endpoint returned 200.

The job under test has planners that are cached — a transcript, a trim proposal
and an edit from the scripted agent (`conftest.py`) — because a correction is
only interesting on a job whose planners would otherwise rewrite the very fields
being corrected.
"""

from __future__ import annotations

import json
import shutil

import pytest

from ingest.cap_fixture import write_bundle
from ingest.fixtures import DEFAULT_BEATS
from prefs import resolve_profile
from review import service
from runner import db
from runner.cli import main as cli_main
from runner.pipeline import run_job
from runner.stages import JOB_ORDER
from spec import Encoder, Tier
from spec.corrections import (
    PROPOSED_NAME,
    Corrections,
    ReinstatedRemoval,
    RetieredSegment,
)
from spec.migrations import load_spec_file
from verify.probe import probe

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")

pytestmark = needs_ffmpeg

PROFILE = "demo_16x9"
"""One profile throughout. What this phase measures is which stages a correction
re-runs, and rendering the second profile doubles the runtime while testing the
per-profile loop a second time."""

PLAN = {
    "removals": [{"t_in": 2.0, "t_out": 3.0, "kind": "filler"}],
    "segments": [
        {"t_in": 0.0, "t_out": 2.0, "tier": "essential", "reason": "opening claim"},
        {"t_in": 3.0, "t_out": 5.0, "tier": "supporting", "reason": "the walkthrough"},
        {"t_in": 5.0, "t_out": 6.0, "tier": "optional", "reason": "sign-off"},
    ],
}
"""One beat of the fixture, six seconds of it. Every test here renders twice — the
job and then the correction — and the render is 1920x1080 whatever the source is,
so the source's length is the one lever on how long this file takes to run."""


@pytest.fixture(scope="module")
def bundle(tmp_path_factory):
    root = tmp_path_factory.mktemp("cap7") / "take.cap"
    return write_bundle(root, beats=DEFAULT_BEATS[:1], width=640, height=360, fps=30.0)


@pytest.fixture
def reviewable(bundle, tmp_path, fake_agent):
    """An ingested job that has been rendered once, with every planner cached."""
    fake_agent.replies({"text": f"```json\n{json.dumps(PLAN)}\n```"})
    job = tmp_path / "take"
    cli_main(["ingest", str(bundle), "--out", str(job)])
    spec = json.loads((job / "spec.json").read_text())
    spec["source"]["has_audio"] = False  # no ASR on this machine; trim proposes nothing
    (job / "spec.json").write_text(json.dumps(spec, indent=2) + "\n")

    database = tmp_path / "screencut.db"
    run = run_job(job, [PROFILE], db_path=database, encoder=Encoder.SOFTWARE)
    assert not run.degradations, run.degradations
    assert fake_agent.calls == 1

    class Job:
        directory = job
        db_path = database
        agent = fake_agent
        job_id = "take"

        def correct(self, corrections: Corrections):
            return service.correct(
                self.job_id, corrections, db_path=self.db_path, encoder=Encoder.SOFTWARE
            )

        @property
        def spec(self):
            return load_spec_file(self.directory / "spec.json")

        def duration(self) -> float:
            return probe(self.directory / "renders" / f"{self.job_id}_{PROFILE}.mp4").duration

    return Job()


def planners_that_ran(run) -> set[str]:
    return {name.split("/")[-1] for name in run.ran()} & {*JOB_ORDER, "plan_focus"}


# --- the exit criteria -------------------------------------------------------


def test_reinstating_a_removal_re_runs_compile_and_render_and_no_planner_at_all(reviewable):
    """§4.5's whole payoff. If this does not hold, every cut correction costs a
    model call and the loop is abandoned."""
    before = reviewable.duration()
    cut = reviewable.spec.edit.removals[0]

    view, run = reviewable.correct(
        Corrections(reinstated=[ReinstatedRemoval(t_in=cut.t_in, t_out=cut.t_out)])
    )

    assert planners_that_ran(run) == set(), run.ran()
    assert {name.split("/")[-1] for name in run.ran()} == {
        "compile",
        "render",
        "verify_transcript",
        "verify",
    }
    assert reviewable.agent.calls == 1, "a reinstated cut must not cost a model call"
    assert view.spec.edit.removals == []
    assert reviewable.duration() > before + 0.5, "the correction has to reach the video"


def test_re_tiering_a_segment_re_runs_no_planner_either(reviewable):
    view, run = reviewable.correct(
        Corrections(retiered=[RetieredSegment(t_in=5.0, tier=Tier.ESSENTIAL)])
    )
    assert planners_that_ran(run) == set(), run.ran()
    assert next(s for s in view.spec.edit.segments if s.t_in == 5.0).tier is Tier.ESSENTIAL


def test_a_tighter_budget_re_runs_no_planner_and_makes_a_shorter_video(reviewable):
    """"Make the short shorter" is one field, and §4.4.1 makes it arithmetic:
    tiering was decided once, and the budget only says how much of it survives."""
    before = reviewable.duration()

    view, run = reviewable.correct(Corrections(budgets={PROFILE: 2.5}))

    assert planners_that_ran(run) == set(), run.ran()
    assert reviewable.agent.calls == 1
    assert view.profiles[0].duration_budget == 2.5
    assert reviewable.duration() < before - 1.0


def test_a_correction_survives_the_planners_running_again_from_their_cache(reviewable):
    """The failure this layer exists to prevent. `plan_edit`'s fingerprint does not
    read the tier, so its cached fragment is still valid — and applying it over the
    correction would undo a person's decision with nothing said."""
    reviewable.correct(Corrections(retiered=[RetieredSegment(t_in=5.0, tier=Tier.ESSENTIAL)]))

    again = run_job(
        reviewable.directory, [PROFILE], db_path=reviewable.db_path, encoder=Encoder.SOFTWARE
    )

    assert again.did_no_work, again.ran()
    assert next(s for s in reviewable.spec.edit.segments if s.t_in == 5.0).tier is Tier.ESSENTIAL
    assert load_spec_file(reviewable.directory / PROPOSED_NAME).edit.segments[2].tier is Tier.OPTIONAL


def test_withdrawing_the_corrections_restores_the_plan_that_was_proposed(reviewable):
    """Taking a correction back has to be as complete as making one."""
    proposed = reviewable.spec.model_dump(mode="json")
    reviewable.correct(Corrections(reinstated=[ReinstatedRemoval(t_in=2.0, t_out=3.0)]))
    assert reviewable.spec.edit.removals == []

    view, _ = reviewable.correct(Corrections())

    assert view.spec.model_dump(mode="json") == proposed
    assert not (reviewable.directory / PROPOSED_NAME).exists()


def test_accepting_records_the_spec_per_profile_and_the_diff_that_produced_it(reviewable):
    """§5.4's two records answer different questions: what was accepted, and what
    the reviewer changed to get there."""
    reviewable.correct(
        Corrections(
            reinstated=[ReinstatedRemoval(t_in=2.0, t_out=3.0)],
            budgets={PROFILE: 30.0},
        )
    )

    view = service.decide(reviewable.job_id, service.ACCEPTED, db_path=reviewable.db_path)

    assert view.decision == service.ACCEPTED
    with db.connect(reviewable.db_path) as connection:
        accepted = connection.execute("SELECT * FROM accepted_specs").fetchall()
        review = dict(db.latest_review(connection, reviewable.job_id))
    assert [row["profile"] for row in accepted] == [PROFILE]
    assert json.loads(accepted[0]["spec_json"])["edit"]["removals"] == []

    paths = {change["path"] for change in json.loads(review["diff_json"])["changes"]}
    assert paths == {"edit.removals[2.000-3.000]", f"profiles.{PROFILE}.duration_budget"}


def test_rejecting_records_the_decision_and_no_accepted_spec(reviewable):
    """A rejection changes no spec and still says something — which is why the
    review record is its own table."""
    view = service.decide(reviewable.job_id, service.REJECTED, db_path=reviewable.db_path)

    assert view.decision == service.REJECTED
    with db.connect(reviewable.db_path) as connection:
        assert connection.execute("SELECT COUNT(*) c FROM accepted_specs").fetchone()["c"] == 0
        assert db.latest_review(connection, reviewable.job_id)["decision"] == service.REJECTED


def test_an_unrecognised_decision_is_refused(reviewable):
    with pytest.raises(ValueError, match="decision must be one of"):
        service.decide(reviewable.job_id, "maybe", db_path=reviewable.db_path)


# --- the page ----------------------------------------------------------------


@pytest.fixture
def client(reviewable):
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from review.app import create_app

    assert fastapi  # the import is the check
    return TestClient(create_app(reviewable.db_path, Encoder.SOFTWARE))


def test_the_index_lists_the_job_and_the_page_serves_its_render(client, reviewable):
    listed = client.get("/api/jobs").json()["jobs"]
    assert [row["job_id"] for row in listed] == [reviewable.job_id]

    assert client.get(f"/jobs/{reviewable.job_id}").text.startswith("<!doctype html>")
    video = client.get(f"/api/jobs/{reviewable.job_id}/render/{PROFILE}")
    assert video.status_code == 200
    assert video.headers["content-type"] == "video/mp4"


def test_the_page_gets_both_documents_so_it_can_show_which_is_which(client, reviewable):
    payload = client.get(f"/api/jobs/{reviewable.job_id}").json()
    assert payload["spec"] == payload["proposed"], "nothing corrected yet"
    assert [p["name"] for p in payload["profiles"]] == [PROFILE]
    assert payload["reports"][PROFILE]["profile"] == PROFILE
    assert payload["decision"] is None


def test_the_correction_endpoint_says_what_ran_and_what_was_cached(client, reviewable):
    """The reviewer should be able to watch §8's claim hold rather than take it
    on faith, so the response carries it."""
    response = client.post(
        f"/api/jobs/{reviewable.job_id}/corrections",
        json={"reinstated": [{"t_in": 2.0, "t_out": 3.0}], "retiered": [], "budgets": {}},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert {name.split("/")[-1] for name in body["ran"]} == {
        "compile",
        "render",
        "verify_transcript",
        "verify",
    }
    assert set(JOB_ORDER) <= {name.split("/")[-1] for name in body["cached"]}
    assert body["spec"]["edit"]["removals"] == []
    assert body["diff"]["changes"][0]["path"] == "edit.removals[2.000-3.000]"


def test_a_correction_the_plan_no_longer_contains_is_refused_rather_than_dropped(client, reviewable):
    response = client.post(
        f"/api/jobs/{reviewable.job_id}/corrections",
        json={"reinstated": [{"t_in": 99.0, "t_out": 99.5}], "retiered": [], "budgets": {}},
    )
    assert response.status_code == 409
    assert "not in the current plan" in response.json()["detail"]


def test_a_correction_that_breaks_the_schema_never_reaches_the_pipeline(client, reviewable):
    response = client.post(
        f"/api/jobs/{reviewable.job_id}/corrections",
        json={"reinstated": [], "retiered": [{"t_in": 5.0, "tier": "vital"}], "budgets": {}},
    )
    assert response.status_code == 422


def test_an_unknown_job_is_a_404(client):
    assert client.get("/api/jobs/nothing-here").status_code == 404


def test_a_degraded_job_announces_itself_on_its_page(bundle, tmp_path, monkeypatch):
    """Decision #12 reviews the finished render, which makes this page the only
    place a §7.4 degradation is ever seen."""
    from runner import agent

    monkeypatch.setattr(agent, "available", lambda: False)
    job = tmp_path / "degraded"
    cli_main(["ingest", str(bundle), "--out", str(job)])
    spec = json.loads((job / "spec.json").read_text())
    spec["source"]["has_audio"] = False
    (job / "spec.json").write_text(json.dumps(spec, indent=2) + "\n")
    database = tmp_path / "screencut.db"
    run_job(job, [PROFILE], db_path=database, encoder=Encoder.SOFTWARE)

    view = service.load_job("degraded", database)
    assert any("plan_edit" in note for note in view.degradations)
    assert view.payload()["degradations"] == view.degradations


def test_a_budget_correction_reaches_a_run_from_the_shell_too(reviewable):
    """The correction layer is the job's, not the page's. A `screencut run` that
    ignored it would render something the review page does not show."""
    Corrections(budgets={PROFILE: 4.0}).write(reviewable.directory)

    run = run_job(
        reviewable.directory, [PROFILE], db_path=reviewable.db_path, encoder=Encoder.SOFTWARE
    )

    assert run.profiles[0].duration_budget == 4.0
    assert planners_that_ran(run) == set(), run.ran()
    assert resolve_profile(PROFILE).duration_budget != 4.0, "the built-in profile is untouched"
