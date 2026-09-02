"""The runner, the cache and the job record (architecture.md §5.1, §5.2, §5.4).

Phase 3 exists to make re-running cheap, which is what makes the review loop
possible at all (§8). These tests are that claim, stated as behaviour.
"""

import dataclasses
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from ingest.fixtures import DEFAULT_BEATS, build_spec, write_fixture
from prefs import resolve_profile
from runner import db
from runner.cache import MissingModelParams, cache_key, digest, file_digest, require_model_params
from runner.contract import StageFailed, StageRequest
from runner.local import LocalRunner
from runner.pipeline import run_job
from runner.stages import ORDER, STAGES
from spec import Encoder

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


# --- cache keys --------------------------------------------------------------


def test_a_key_does_not_depend_on_dict_ordering():
    assert digest({"a": 1, "b": 2}) == digest({"b": 2, "a": 1})


def test_bumping_a_stage_version_changes_the_key():
    common = dict(stage="plan_focus", inputs={"a": 1}, params={})
    assert cache_key(stage_version=1, **common) != cache_key(stage_version=2, **common)


def test_a_model_stage_may_not_be_keyed_without_its_model_and_prompt():
    """The one cache subtlety that will not announce itself (§5.2): it looks like
    the prompt edit had no effect."""
    with pytest.raises(MissingModelParams, match="prompt_version"):
        cache_key(stage="plan_edit", stage_version=1, inputs={}, params={"model": "x"}, model_backed=True)
    require_model_params("plan_edit", {"model": "x", "prompt_version": "3"})


def test_the_model_and_the_prompt_each_change_the_key():
    base = dict(stage="plan_edit", stage_version=1, inputs={}, model_backed=True)
    a = cache_key(params={"model": "one", "prompt_version": "1"}, **base)
    b = cache_key(params={"model": "two", "prompt_version": "1"}, **base)
    c = cache_key(params={"model": "one", "prompt_version": "2"}, **base)
    assert len({a, b, c}) == 3


def test_media_is_keyed_by_content_not_by_timestamp(tmp_path):
    """A re-encoded take of the same length must not serve the old render."""
    first, second = tmp_path / "a.bin", tmp_path / "b.bin"
    first.write_bytes(b"x" * 4096)
    second.write_bytes(b"x" * 4095 + b"y")
    assert file_digest(first) != file_digest(second)
    assert file_digest(first) == file_digest(first)


# --- the database ------------------------------------------------------------


def test_migrations_apply_once_and_only_once(tmp_path):
    path = tmp_path / "screencut.db"
    with db.connect(path) as connection:
        assert db.migrate(connection) == [], "connect() already applied them"
        names = {row["name"] for row in connection.execute("SELECT name FROM schema_migrations")}
    assert names == {name for name, _ in db.MIGRATIONS}
    with db.connect(path) as connection:
        assert db.migrate(connection) == []


def test_the_four_tables_of_section_5_4_exist(tmp_path):
    with db.connect(tmp_path / "x.db") as connection:
        tables = {
            row["name"]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    assert {"jobs", "stage_cache", "accepted_specs", "pref_changes"} <= tables


def test_the_learning_corpus_records_the_profile_it_was_accepted_under(tmp_path):
    """Preferences are learned per profile (§4.1), so the corpus has to say which."""
    with db.connect(tmp_path / "x.db") as connection:
        db.record_accepted_spec(connection, job_id="j", profile="shorts_9x16", spec_json="{}")
        db.record_pref_change(
            connection, profile="shorts_9x16", key="zoom_factor",
            old_value=1.4, new_value=1.6, caused_by=["j1", "j2"],
        )
        accepted = connection.execute("SELECT * FROM accepted_specs").fetchone()
        change = connection.execute("SELECT * FROM pref_changes").fetchone()
    assert accepted["profile"] == "shorts_9x16"
    assert json.loads(change["caused_by"]) == ["j1", "j2"], "the jobs that caused it (§10.1)"


# --- the local runner --------------------------------------------------------


def test_a_stage_that_fails_reports_its_stderr():
    runner = LocalRunner()
    request = StageRequest(stage="plan_focus", job_dir="/nonexistent", output="out.json")
    with pytest.raises(StageFailed, match="plan_focus exited"):
        runner.run(request)


def test_two_stages_may_not_hold_model_weights_at_once():
    """8GB will not hold two (§16). Nothing sets the flag yet; the guard is what
    stops the first parallel scheduler from discovering the limit by swapping."""
    runner = LocalRunner()
    runner._weights_in_flight = "transcribe"
    with pytest.raises(StageFailed, match="8GB will not hold two"):
        runner.run(StageRequest(stage="render", job_dir=".", output="x"), holds_local_weights=True)


def test_the_stage_cli_is_json_in_json_out(tmp_path):
    """§5.1's contract, exercised as a subprocess rather than as a function call —
    it is the seam a remote worker would use, so it has to work as one."""
    fixture = build_spec("cli", width=640, height=360, beats=DEFAULT_BEATS[:2], slot_s=2.0)
    write_fixture(tmp_path, fixture, with_video=False)
    request = StageRequest(
        stage="plan_focus",
        job_dir=str(tmp_path),
        inputs={"spec": "spec.json"},
        params={"profile": resolve_profile("shorts_9x16").model_dump(mode="json")},
        output="stages/focus.json",
    )
    completed = subprocess.run(
        [sys.executable, "-m", "runner.stages", "plan_focus"],
        input=request.model_dump_json(), capture_output=True, text=True,
        cwd=Path(__file__).resolve().parent.parent, check=True,
    )
    assert json.loads(completed.stdout)["output"] == "stages/focus.json"
    assert json.loads((tmp_path / "stages" / "focus.json").read_text())["mode"] == "crop_path"


# --- the pipeline ------------------------------------------------------------


def small(name: str):
    profile = resolve_profile(name)
    size = {"shorts_9x16": {"width": 270, "height": 480}, "demo_16x9": {"width": 640, "height": 360}}[name]
    return profile.model_copy(update={**size, "encode": profile.encode.model_copy(
        update={"encoder": Encoder.SOFTWARE})})


@pytest.fixture(scope="module")
def job(tmp_path_factory) -> Path:
    directory = tmp_path_factory.mktemp("job")
    write_fixture(directory, build_spec("run", width=640, height=360, beats=DEFAULT_BEATS[:2], slot_s=2.0),
                  with_video=True)
    return directory


@pytest.fixture(scope="module")
def database(tmp_path_factory) -> Path:
    return tmp_path_factory.mktemp("db") / "screencut.db"


def go(job: Path, database: Path, **kwargs):
    return run_job(job, [small("shorts_9x16")], encoder=Encoder.SOFTWARE, db_path=database, **kwargs)


@needs_ffmpeg
def test_the_first_run_does_the_work_and_the_second_does_none(job, database):
    first = go(job, database)
    assert not first.did_no_work
    assert [o.stage for o in first.outcomes] == ["plan_focus", "compile", "render", "verify"]
    assert go(job, database).did_no_work, "and it says so, which is the exit criterion"


@needs_ffmpeg
def test_the_render_is_published_under_a_stable_name(job, database):
    result = go(job, database)
    published = result.renders["shorts_9x16"]
    assert published.exists() and published.name.endswith("_shorts_9x16.mp4")
    artifact = job / next(o.path for o in result.outcomes if o.stage == "render")
    assert published.samefile(artifact), "a view onto the cache, not a second copy (§16)"


@needs_ffmpeg
def test_changing_only_caption_text_reruns_compile_and_render_and_nothing_upstream(job, database):
    """The exit criterion, and the reason each stage fingerprints what it reads
    rather than the whole spec."""
    go(job, database)
    spec_path = job / "spec.json"
    original = spec_path.read_text()
    doc = json.loads(original)
    doc["captions"][0]["words"][0]["text"] = "rewritten"
    spec_path.write_text(json.dumps(doc, indent=2, sort_keys=True))
    try:
        result = go(job, database)
        assert result.ran() == [
            "shorts_9x16/compile", "shorts_9x16/render", "shorts_9x16/verify"
        ]
    finally:
        spec_path.write_text(original)


@needs_ffmpeg
@pytest.mark.parametrize(
    "bumped, expected",
    [
        ("plan_focus", ["plan_focus", "compile", "render", "verify"]),
        ("compile", ["compile", "render", "verify"]),
        ("render", ["render", "verify"]),
        ("verify", ["verify"]),
    ],
)
def test_bumping_a_stage_version_invalidates_it_and_its_dependents_and_nothing_else(
    job, database, monkeypatch, bumped, expected
):
    go(job, database)
    stage = STAGES[bumped]
    monkeypatch.setitem(STAGES, bumped, dataclasses.replace(stage, version=stage.version + 100))
    result = go(job, database)
    assert result.ran() == [f"shorts_9x16/{name}" for name in expected]


@needs_ffmpeg
def test_a_missing_artifact_is_a_miss_even_with_a_row_in_the_database(job, database):
    """Artifacts are files and rows are rows, so they can disagree. Trusting the row
    turns a swept cache into a render that never happens."""
    result = go(job, database)
    render = next(o for o in result.outcomes if o.stage == "render")
    (job / render.path).unlink()
    (job / "renders" / f"run_shorts_9x16.mp4").unlink()
    # Only the render: it comes back byte-identical, so its key is unchanged and
    # the verification report it produced is still about this exact file.
    assert go(job, database).ran() == ["shorts_9x16/render"]


@needs_ffmpeg
def test_force_reruns_every_stage_this_job_has(job, database):
    """Every stage the job runs, which is not every stage there is.

    A fixture arrives with a complete spec and runs no job-level stages, so it has
    no transcript and `verify_transcript` has nothing to diff the render against
    (§9.2). `--force` re-runs work; it does not invent it."""
    go(job, database)
    ran = go(job, database, force=True).ran()
    assert [name.split("/")[1] for name in ran] == [s for s in ORDER if s != "verify_transcript"]


@needs_ffmpeg
def test_the_job_record_carries_what_review_will_need(job, database):
    result = go(job, database)
    assert result.verified, "the good fixture must pass its own checks"
    with db.connect(database) as connection:
        row = db.get_job(connection, "run")
        cached = connection.execute(
            "SELECT stage, profile FROM stage_cache WHERE job_id = ?", ("run",)
        ).fetchall()
    assert row["status"] == "rendered" and row["spec_version"] >= 1
    assert json.loads(row["degradations"]) == [], "no stage degrades until phase 5 (§7.4)"
    assert {r["stage"] for r in cached} == {s for s in ORDER if s != "verify_transcript"}
    assert {r["profile"] for r in cached} == {"shorts_9x16"}


@needs_ffmpeg
def test_a_verification_report_lands_on_the_job_record(job, database):
    """§10.1's first rule — never learn from a job that failed verification — is a
    query, which is why the report is a row rather than only a file."""
    go(job, database)
    with db.connect(database) as connection:
        latest = db.latest_verification(connection, "run", "shorts_9x16")
        corpus = db.verified_jobs(connection, "shorts_9x16")
    assert latest is not None and latest["passed"] == 1
    assert json.loads(latest["findings_json"])["profile"] == "shorts_9x16"
    assert "run" in corpus


@needs_ffmpeg
def test_a_job_that_fails_verification_says_so_on_its_record(tmp_path_factory, database):
    from ingest.fixtures import break_fixture

    directory = tmp_path_factory.mktemp("broken")
    write_fixture(
        directory,
        break_fixture(build_spec("broken", width=640, height=360, beats=DEFAULT_BEATS[:2], slot_s=2.0)),
        with_video=True,
    )
    result = run_job(directory, [small("shorts_9x16")], encoder=Encoder.SOFTWARE, db_path=database)
    assert not result.verified
    with db.connect(database) as connection:
        assert db.get_job(connection, "broken-broken")["status"] == "failed_verification"
        assert "broken-broken" not in db.verified_jobs(connection, "shorts_9x16")
