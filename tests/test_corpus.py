"""The preference corpus (architecture.md §10, ahead of phase 10).

The learner is not built — §10.2 says building it before there is anything to
learn from leaves dead code that still has to be debugged. What is built is the
half that cannot wait, because it is the half that has to be *recording* while the
videos are being made: what was accepted, what it was accepted under, and whether
it was real.

So these tests are about the corpus refusing to count things, which is the whole
of §10.1 and §10.2 stated as filters. A corpus that quietly counted a fixture, a
failed render, or a row that lost its profile would produce a learner whose first
proposal nobody could explain — which §10.1 names as the characteristic failure of
self-tuning systems.
"""

from __future__ import annotations

import json

import pytest

from prefs import resolve_profile
from prefs.corpus import ACTIVATION_JOBS, read_corpus, report
from runner import db
from spec import EditDecisions, EditSpec, Segment, Source, Tier
from spec.source import Provenance

PROFILE = "shorts_9x16"


def spec_for(job_id: str, provenance: Provenance) -> EditSpec:
    return EditSpec(
        job_id=job_id,
        source=Source(
            source_id="s",
            provenance=provenance,
            path="source/take.mp4",
            duration=10.0,
            width=1920,
            height=1080,
            fps=30.0,
        ),
        edit=EditDecisions(
            segments=[Segment(t_in=0.0, t_out=10.0, tier=Tier.ESSENTIAL, reason="all of it")]
        ),
    )


def accept(
    database,
    job_id: str,
    *,
    provenance: Provenance = Provenance.RECORDED,
    passed: bool = True,
    budget: float | None = None,
    zoom: float | None = None,
    with_profile: bool = True,
) -> None:
    """One job through to acceptance, written straight to the record.

    Straight to the record rather than through a render: what is under test is
    which rows the corpus counts, and a corpus that needed twelve encodes to test
    would be tested against three.
    """
    profile = resolve_profile(PROFILE)
    updates = {}
    if budget is not None:
        updates["duration_budget"] = budget
    if zoom is not None:
        updates["focus"] = profile.focus.model_copy(update={"zoom_factor": zoom})
    if updates:
        profile = profile.model_copy(update=updates)

    with db.connect(database) as connection:
        db.upsert_job(
            connection, job_id=job_id, job_dir=f"/jobs/{job_id}", spec_version=1, status="rendered"
        )
        db.record_verification(
            connection, job_id=job_id, profile=PROFILE, passed=passed, findings_json="[]"
        )
        spec_json = spec_for(job_id, provenance).model_dump_json()
        if with_profile:
            db.record_accepted_spec(
                connection,
                job_id=job_id,
                profile=PROFILE,
                spec_json=spec_json,
                profile_json=profile.model_dump_json(),
            )
        else:
            # A row as migration 0004 found them. Written by hand rather than by
            # `record_accepted_spec`, which has no way to write one and should not
            # grow one just so a test can ask what happens to the rows it predates.
            connection.execute(
                "INSERT INTO accepted_specs (job_id, profile, spec_json, accepted_at) "
                "VALUES (?, ?, ?, ?)",
                (job_id, PROFILE, spec_json, db.now()),
            )


@pytest.fixture
def database(tmp_path):
    return tmp_path / "screencut.db"


def test_an_empty_record_is_a_corpus_of_nothing_rather_than_an_error(database):
    corpus = read_corpus(PROFILE, database)
    assert corpus.acceptances == []
    assert not corpus.ready
    assert corpus.skipped.total == 0


def test_an_accepted_real_job_is_counted_with_the_profile_it_ran_under(database):
    """The point of migration 0004. Every tunable §10 moves is a `RenderProfile`
    field and none of them is in the `EditSpec`, so a row without the profile
    records what was accepted and not what it was accepted under."""
    accept(database, "take-1", budget=11.0, zoom=1.6)

    corpus = read_corpus(PROFILE, database)
    assert [a.job_id for a in corpus.acceptances] == ["take-1"]
    assert corpus.acceptances[0].profile.duration_budget == 11.0
    assert corpus.acceptances[0].tunable("focus.zoom_factor") == 1.6


def test_the_profile_is_the_one_accepted_and_not_the_one_resolved_later(database):
    """The reason it is a snapshot. `resolve_profile` reads today's defaults, and
    after the learner's first move those are the learner's own output — a corpus
    that re-resolved would read that back as a preference a person expressed."""
    accept(database, "take-1", budget=11.0)
    assert resolve_profile(PROFILE).duration_budget != 11.0
    assert read_corpus(PROFILE, database).acceptances[0].profile.duration_budget == 11.0


def test_a_job_that_failed_verification_contributes_nothing(database):
    """§10.1's first rule: a broken render's corrections poison the defaults."""
    accept(database, "good", budget=11.0)
    accept(database, "broken", budget=4.0, passed=False)

    corpus = read_corpus(PROFILE, database)
    assert [a.job_id for a in corpus.acceptances] == ["good"]
    assert corpus.skipped.unverified == 1


def test_a_synthetic_job_contributes_nothing(database):
    """§10.2 counts real jobs. The fixture arrives with a hand-written spec, so a
    corpus that counted it would learn the fixture's taste and report it as yours."""
    accept(database, "fixture", provenance=Provenance.SYNTHETIC)

    corpus = read_corpus(PROFILE, database)
    assert corpus.acceptances == []
    assert corpus.skipped.synthetic == 1


def test_a_job_ingested_before_provenance_was_recorded_contributes_nothing(database):
    """`unknown` is not a third kind of footage; it is the absence of an answer,
    and the honest thing to do with it is not count it."""
    accept(database, "old", provenance=Provenance.UNKNOWN)

    corpus = read_corpus(PROFILE, database)
    assert corpus.acceptances == []
    assert corpus.skipped.unknown_provenance == 1


def test_a_row_from_before_the_profile_column_contributes_nothing(database):
    """Migration 0004 added the column to a table that could already have rows,
    and a row from before it cannot be given a profile that is not a guess."""
    accept(database, "pre-0004", with_profile=False)

    corpus = read_corpus(PROFILE, database)
    assert corpus.acceptances == []
    assert corpus.skipped.no_profile == 1


def test_every_skipped_row_is_counted_under_the_rule_that_dropped_it(database):
    """Counted rather than silently dropped. A gate that reports 1 while discarding
    four is what an afternoon of debugging looks like."""
    accept(database, "good")
    accept(database, "broken", passed=False)
    accept(database, "fixture", provenance=Provenance.SYNTHETIC)
    accept(database, "old", provenance=Provenance.UNKNOWN)
    accept(database, "pre-0004", with_profile=False)

    skipped = read_corpus(PROFILE, database).skipped
    assert (skipped.unverified, skipped.synthetic, skipped.unknown_provenance, skipped.no_profile) == (
        1,
        1,
        1,
        1,
    )
    assert skipped.total == 4


def test_the_gate_opens_only_at_section_10_2s_count(database):
    for index in range(ACTIVATION_JOBS - 1):
        accept(database, f"take-{index}")
    assert not read_corpus(PROFILE, database).ready

    accept(database, "take-last")
    assert read_corpus(PROFILE, database).ready


def test_acceptances_come_back_oldest_first_because_the_window_has_an_order(database):
    """§10.1 learns over a window so an old preference can be superseded, and
    "superseded" is a claim about order."""
    for index, budget in enumerate((20.0, 18.0, 12.0)):
        accept(database, f"take-{index}", budget=budget)

    assert read_corpus(PROFILE, database).spread("duration_budget") == [20.0, 18.0, 12.0]


def test_a_tunable_nobody_moved_has_no_signal_however_many_jobs_there_are(database):
    """Ten acceptances of the number a profile already had are not ten votes for
    it. A median over them proposes the default it started from, which is the
    appearance of learning rather than learning."""
    for index in range(ACTIVATION_JOBS):
        accept(database, f"take-{index}")
    corpus = read_corpus(PROFILE, database)

    assert corpus.ready
    assert corpus.corrected("duration_budget") == 0
    assert corpus.corrected("focus.zoom_factor") == 0


def test_a_tunable_corrected_across_jobs_is_the_signal_phase_10_reads(database):
    """Phase 10's first exit criterion in the corpus's terms: the same tunable
    corrected the same way across several jobs, countable before anything has been
    proposed."""
    for index in range(3):
        accept(database, f"take-{index}", zoom=1.6)
    corpus = read_corpus(PROFILE, database)

    assert corpus.corrected("focus.zoom_factor") == 3
    assert corpus.spread("focus.zoom_factor") == [1.6, 1.6, 1.6]
    assert corpus.corrected("duration_budget") == 0


def test_the_report_says_how_far_off_the_gate_is_rather_than_only_that_it_is_shut(database):
    """§10.2's "go and make videos instead" is the right instruction and a hard one
    to act on without a number. Same shape as R5's meter: say "not yet, by this
    much" rather than printing a zero that reads like a measurement."""
    accept(database, "take-1", zoom=1.6)
    accept(database, "fixture", provenance=Provenance.SYNTHETIC)
    text = report(database)

    assert f"1/{ACTIVATION_JOBS} accepted real jobs" in text
    assert f"{ACTIVATION_JOBS - 1} more needed" in text
    assert "1 synthetic (§10.2)" in text
    assert "focus.zoom_factor (1)" in text
    assert "the learner is not built" in text


def test_the_corpus_reads_a_spec_through_the_migration_chain(database):
    """Specs in this table outlive schema versions exactly as golden specs do, and
    a corpus that validated them bare would go quiet one migration from now."""
    profile = resolve_profile(PROFILE)
    stale = json.loads(spec_for("old", Provenance.RECORDED).model_dump_json())
    stale["spec_version"] = 2
    del stale["source"]["provenance"]

    with db.connect(database) as connection:
        db.upsert_job(
            connection, job_id="old", job_dir="/jobs/old", spec_version=2, status="rendered"
        )
        db.record_verification(
            connection, job_id="old", profile=PROFILE, passed=True, findings_json="[]"
        )
        db.record_accepted_spec(
            connection,
            job_id="old",
            profile=PROFILE,
            spec_json=json.dumps(stale),
            profile_json=profile.model_dump_json(),
        )

    corpus = read_corpus(PROFILE, database)
    assert corpus.acceptances == []
    assert corpus.skipped.unknown_provenance == 1
