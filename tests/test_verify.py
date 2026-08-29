"""Deterministic verification (architecture.md §9.1).

The first two verification layers exist to keep garbage from reaching a person.
Every check has to fire on the broken fixture and stay quiet on the good one —
which is phase 6's exit criterion, and the reason §11 wants a fixture that is
deliberately bad.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from compile.timeline import EditedCaption, EditedTimeline, EditedWord, KeptSpan, project
from ingest.fixtures import break_fixture, build_spec
from plan.focus import CropPathPlan, PathSample, plan_focus
from prefs import resolve_profile
from spec import Tier
from verify.checks import (
    check_budget,
    check_captions,
    check_crop_continuity,
    check_cuts_land_between_words,
    check_dialogue_to_bed,
    check_edit_integrity,
    check_loudness,
    check_render_integrity,
    check_trim_composition,
)
from verify.probe import measure_loudness
from verify.report import Finding, Severity, VerificationReport

GOLDEN = Path(__file__).resolve().parent.parent / "golden" / "broken_v1"
needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


@pytest.fixture(scope="module")
def good():
    return build_spec().spec


@pytest.fixture(scope="module")
def broken():
    return break_fixture(build_spec()).spec


def spec_free_findings(spec, profile_name):
    profile = resolve_profile(profile_name)
    timeline = project(spec, profile)
    return [
        *check_captions(timeline, profile),
        *check_crop_continuity(plan_focus(spec, profile), profile),
        *check_edit_integrity(spec, timeline),
        *check_budget(timeline, profile),
        *check_cuts_land_between_words(spec),
    ]


def failed(findings) -> list[str]:
    return sorted(f.check for f in findings if f.severity is Severity.FAIL)


# --- the good fixture --------------------------------------------------------


@pytest.mark.parametrize("profile", ["shorts_9x16", "demo_16x9"])
def test_nothing_fires_on_the_good_fixture(good, profile):
    assert failed(spec_free_findings(good, profile)) == []


def test_a_cut_boxed_caption_warns_rather_than_failing(good):
    """The fixture's filler cut leaves one block too short to read, and there is
    nowhere to extend it to. That is worth seeing and is not a broken render."""
    findings = spec_free_findings(good, "shorts_9x16")
    short = next(f for f in findings if f.check == "caption_duration")
    assert short.severity is Severity.WARN


# --- the broken one ----------------------------------------------------------


@pytest.mark.parametrize("profile", ["shorts_9x16", "demo_16x9"])
def test_the_broken_fixture_trips_exactly_the_recorded_checks(broken, profile):
    """The golden answer, replayed. §11 wants the checks tested against known-bad,
    and "known" means written down rather than re-derived from the same code."""
    expected = json.loads((GOLDEN / "expected_findings.json").read_text())["spec_free_checks"]
    assert failed(spec_free_findings(broken, profile)) == expected[profile]


def test_a_cut_through_a_word_is_caught(broken):
    """Invisible in a still frame, audible immediately."""
    finding = next(f for f in check_cuts_land_between_words(broken) if f.check == "cut_mid_word")
    assert finding.severity is Severity.FAIL and finding.value == 2


def test_line_length_is_judged_per_profile(broken):
    """A 34-character word overruns a vertical line and fits a widescreen one. A
    check that answered the same for both would not be checking the profile."""
    assert "caption_line_length" in failed(spec_free_findings(broken, "shorts_9x16"))
    assert "caption_line_length" not in failed(spec_free_findings(broken, "demo_16x9"))


# --- checks on things the pipeline cannot produce ----------------------------


def test_overlapping_captions_are_caught_even_though_no_spec_can_carry_them():
    """`EditSpec` refuses overlapping blocks, so this can only be built by hand.
    The check stays because the thing that makes it unrepresentable is code."""
    timeline = EditedTimeline(
        profile="shorts_9x16", threshold=Tier.ESSENTIAL, duration=10.0,
        spans=[KeptSpan(source_in=0, source_out=10, output_in=0)],
        captions=[
            EditedCaption(t_in=0, t_out=4, words=[EditedWord(t_in=0, t_out=4, text="one")]),
            EditedCaption(t_in=3, t_out=7, words=[EditedWord(t_in=3, t_out=7, text="two")]),
        ],
    )
    assert "caption_overlap" in failed(check_captions(timeline, resolve_profile("shorts_9x16")))


def test_a_juddering_crop_is_caught_even_though_plan_focus_cannot_make_one():
    """Judder is *the* failure mode of automated vertical reframing, and
    `plan_focus` rate-limits it by construction — so the check needs a hand-built
    path to have anything to catch."""
    profile = resolve_profile("shorts_9x16")
    jerky = CropPathPlan(
        window_w=0.3, window_h=1.0, fps=30.0,
        samples=[PathSample(t=0.0, cx=0.2, cy=0.5), PathSample(t=1 / 30, cx=0.8, cy=0.5)],
    )
    finding = check_crop_continuity(jerky, profile)[0]
    assert finding.severity is Severity.FAIL and finding.value > profile.focus.max_crop_delta_per_frame


def test_a_budget_overrun_is_reported_with_its_number(good):
    """`essential` alone overrunning is §4.4.1's expected failure, and rendering
    long in silence is the thing this stops."""
    profile = resolve_profile("shorts_9x16").model_copy(update={"duration_budget": 3.0})
    finding = check_budget(project(good, profile), profile)[0]
    assert finding.severity is Severity.FAIL and finding.value > 0


def test_a_bed_that_buries_the_voice_is_caught(good):
    audio = good.audio.model_copy(update={"music_path": "source/bed.wav", "music_gain_db": -2.0, "duck_db": -1.0})
    loud_bed = good.model_copy(update={"audio": audio})
    assert failed(check_dialogue_to_bed(loud_bed)) == ["dialogue_to_bed"]
    assert failed(check_dialogue_to_bed(good)) == [], "no bed, no ratio to check"


def test_a_missing_render_fails_rather_than_raising(good, tmp_path):
    timeline = project(good, resolve_profile("shorts_9x16"))
    assert failed(check_render_integrity(timeline, tmp_path / "absent.mp4")) == ["render"]


# --- measurement -------------------------------------------------------------


@needs_ffmpeg
def test_clipping_audio_is_measured_and_failed(good, tmp_path):
    """Clipped audio is §11's fourth breakage. The pipeline normalizes loudness, so
    no fixture can carry it through a render — it takes a file made to clip."""
    loud = tmp_path / "loud.wav"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
         "-af", "volume=20dB", "-ar", "48000", str(loud)], check=True,
    )
    measured = measure_loudness(loud)
    assert measured is not None and measured.true_peak_dbtp > good.audio.true_peak_ceiling_dbtp
    assert "true_peak" in failed(check_loudness(good, loud))


@needs_ffmpeg
def test_loudness_is_measured_from_the_render_not_asserted_from_the_spec(good, tmp_path):
    quiet = tmp_path / "quiet.wav"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
         "-af", "volume=-40dB", "-ar", "48000", str(quiet)], check=True,
    )
    assert "loudness" in failed(check_loudness(good, quiet))


# --- the report --------------------------------------------------------------


def test_the_report_leads_with_what_went_wrong():
    """Legible enough to act on without reading the code — phase 6's exit criterion."""
    report = VerificationReport(
        job_id="j", profile="shorts_9x16", render="renders/j.mp4",
        findings=[
            Finding(check="ok_one", severity=Severity.PASS, message="fine"),
            Finding(check="budget", severity=Severity.FAIL, message="7.4s over", value=7.4),
            Finding(check="caption_duration", severity=Severity.WARN, message="brief"),
        ],
    )
    lines = report.summary().splitlines()
    assert not report.passed
    assert "FAIL" in lines[0] and "1 failed, 1 warned, 1 passed" in lines[0]
    assert "budget" in lines[1], "failures first"
    assert "ok_one" not in report.summary(), "passes are counted, not listed"


def test_an_info_finding_is_not_a_verdict(good):
    """The trim override rate is a number on the report, not a pass or fail (§9.1)."""
    finding = check_trim_composition(good)[0]
    assert finding.severity is Severity.INFO
    assert 0.0 <= finding.value <= 1.0


def test_the_broken_fixtures_overlay_sits_on_a_caption(broken, good, tmp_path):
    """The third breakage, which needs compiled overlay geometry rather than only
    the spec — and the reason the good fixture's own target had to move: a check
    that fires on every ordinary run gets ignored within a week."""
    from compile.render import prepare
    from verify.checks import check_overlays

    profile = resolve_profile("shorts_9x16")
    for spec, expected in ((broken, ["overlay_occlusion"]), (good, [])):
        plan = prepare(spec, profile, tmp_path, work_dir=Path("work") / spec.job_id)
        findings = check_overlays(
            plan.timeline, plan.focus, profile, spec, plan.assets
        )
        assert failed(findings) == expected, spec.job_id
