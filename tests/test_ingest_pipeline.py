"""A recorder bundle to two renders, in one command (phase 4).

Phase 4's exit criterion is a real recording rendering unattended. A real
recording needs a screen, a microphone and Cap, so what runs here is the Cap-format
fixture — a bundle carrying every trap phase 0 measured, at or beyond the measured
severity (`ingest/cap_fixture.py`). What it cannot stand in for is ASR against real
speech, and the tests that would need it say so by skipping.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from ingest.cap_fixture import META_FPS, START_TIME_S, write_bundle
from ingest.fixtures import DEFAULT_BEATS
from prefs import load_constraints
from runner.cli import main as cli_main
from runner.job import JobConfig
from runner.pipeline import run_job
from runner.stages import JOB_ORDER
from spec import Encoder
from spec.focus import FocusKind
from spec.migrations import load_spec_file
from synth.asr import model_path

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")

TWO_BEATS = DEFAULT_BEATS[:2]


def _asr_installed() -> bool:
    asr = load_constraints().asr
    return shutil.which(asr.binary) is not None and model_path(asr.models_dir, asr.model).is_file()


needs_asr = pytest.mark.skipif(
    not _asr_installed(), reason="the configured ASR backend or its weights are not installed"
)


@pytest.fixture(scope="module")
def bundle(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("cap") / "take.cap"
    return write_bundle(root, beats=TWO_BEATS, width=640, height=360, fps=30.0)


@needs_ffmpeg
def test_ingest_writes_a_job_the_pipeline_can_run(bundle, tmp_path):
    job = tmp_path / "job01"
    assert cli_main(["ingest", str(bundle), "--out", str(job)]) == 0

    spec = load_spec_file(job / "spec.json")
    assert spec.job_id == "job01"
    assert spec.focus.points, "the whole point of ingest is the focus track"
    assert (job / spec.source.path).is_file(), "the take is copied in; a job must survive being moved"
    assert JobConfig.load(job).stages == ["transcribe", "plan_captions", "trim", "plan_edit"]


@needs_ffmpeg
def test_media_facts_come_from_ffprobe_and_not_from_the_sidecar(bundle, tmp_path):
    """`recording-meta.json` claimed 25 fps over a 59 fps stream in the real take.
    The file is the fact; the sidecar is a claim about it."""
    job = tmp_path / "job02"
    cli_main(["ingest", str(bundle), "--out", str(job)])
    spec = load_spec_file(job / "spec.json")
    assert spec.source.fps == 30.0
    assert spec.source.fps != META_FPS


@needs_ffmpeg
def test_the_focus_track_starts_at_the_video_and_not_at_the_recording_clock(bundle, tmp_path):
    """The measured offset is 0.194 s. Ignoring it puts every zoom that much early,
    which reads as a planner bug rather than a clock bug."""
    job = tmp_path / "job03"
    cli_main(["ingest", str(bundle), "--out", str(job)])
    spec = load_spec_file(job / "spec.json")
    assert spec.focus.points[0].t == pytest.approx(0.0, abs=0.05)
    assert spec.focus.points[-1].t <= spec.source.duration + 1e-6
    assert START_TIME_S > 0.1, "a zero offset would make this test prove nothing"


@needs_ffmpeg
def test_the_gap_where_the_cursor_rested_becomes_dwell(bundle, tmp_path):
    """Cap emits on movement, so the clearest attention in a take is the part with
    no samples in it at all."""
    job = tmp_path / "job04"
    cli_main(["ingest", str(bundle), "--out", str(job)])
    spec = load_spec_file(job / "spec.json")
    kinds = {p.kind for p in spec.focus.points}
    assert FocusKind.DWELL in kinds
    assert FocusKind.CLICK in kinds


@needs_ffmpeg
def test_a_silent_recording_renders_both_profiles_and_says_it_found_no_words(bundle, tmp_path):
    """A screen capture with the mic off is an ordinary job, not a failure — and
    it is the one shape of the whole path that needs no ASR installed to run."""
    job = tmp_path / "job05"
    cli_main(["ingest", str(bundle), "--out", str(job)])
    _silence(job)

    result = run_job(job, db_path=tmp_path / "screencut.db", encoder=Encoder.SOFTWARE)
    assert set(result.renders) == {"shorts_9x16", "demo_16x9"}
    assert all(path.is_file() for path in result.renders.values())
    assert result.verified, [f for r in result.reports.values() for f in r.failures]

    assert load_spec_file(job / "spec.json").captions == []
    assert [o.stage for o in result.outcomes if o.profile == "job"] == list(JOB_ORDER)


@needs_ffmpeg
def test_the_job_stages_run_once_for_the_whole_job_not_once_per_profile(bundle, tmp_path):
    """What was said does not depend on the shape it is rendered into. Running
    `transcribe` per profile would transcribe the same audio twice, and on the
    target machine that is 23 seconds each time (environment findings §3)."""
    job = tmp_path / "job06"
    cli_main(["ingest", str(bundle), "--out", str(job)])
    _silence(job)

    result = run_job(job, db_path=tmp_path / "screencut.db", encoder=Encoder.SOFTWARE)
    transcribes = [o for o in result.outcomes if o.stage == "transcribe"]
    assert len(transcribes) == 1


@needs_ffmpeg
def test_re_running_an_ingested_job_repeats_only_what_degraded(bundle, tmp_path):
    """Every stage that produced a real answer is cached — including the job-level
    ones, which rewrite `spec.json`, so a rewrite that was not byte-stable would
    invalidate every per-profile stage on every run.

    `plan_edit` is the exception here because the agent is not installed, so it
    degraded, and a degraded artifact is deliberately not cached (§7.4): caching
    it would make one lost network permanent. `tests/test_plan_edit.py` runs the
    same job with an agent on PATH and gets a clean cache hit."""
    job = tmp_path / "job07"
    cli_main(["ingest", str(bundle), "--out", str(job)])
    _silence(job)
    db = tmp_path / "screencut.db"

    run_job(job, db_path=db, encoder=Encoder.SOFTWARE)
    again = run_job(job, db_path=db, encoder=Encoder.SOFTWARE)
    assert again.ran() == ["job/plan_edit"], again.ran()


@needs_ffmpeg
def test_a_job_without_a_manifest_keeps_the_spec_it_was_given(bundle, tmp_path):
    """Every fixture in this repository is one of these, and adding the job-level
    group must not have started transcribing them."""
    job = tmp_path / "job08"
    cli_main(["ingest", str(bundle), "--out", str(job)])
    (job / "job.json").unlink()

    result = run_job(job, db_path=tmp_path / "screencut.db", encoder=Encoder.SOFTWARE)
    assert [o.stage for o in result.outcomes if o.profile == "job"] == []


@needs_ffmpeg
@needs_asr
def test_the_configured_asr_backend_transcribes_the_take(bundle, tmp_path):
    """The only test here that needs weights on the machine. It is written against
    whatever `constraints.yaml` names, so on the target machine it runs `large-v3`
    and elsewhere it skips rather than quietly testing something smaller."""
    job = tmp_path / "job09"
    cli_main(["ingest", str(bundle), "--out", str(job)])
    result = run_job(job, db_path=tmp_path / "screencut.db", encoder=Encoder.SOFTWARE)

    assert set(result.renders) == {"shorts_9x16", "demo_16x9"}
    transcript = json.loads(
        (job / next(o.path for o in result.outcomes if o.stage == "transcribe")).read_text()
    )
    assert transcript["model"] == load_constraints().asr.model


def _silence(job: Path) -> None:
    """Mark the take's audio absent, which is what a capture with the mic off is.

    Done on the spec rather than by generating a second silent video: `has_audio`
    is what the stage branches on, and a fixture variant would be a second thing
    to keep in step with the first.
    """
    spec = json.loads((job / "spec.json").read_text())
    spec["source"]["has_audio"] = False
    (job / "spec.json").write_text(json.dumps(spec, indent=2) + "\n")
