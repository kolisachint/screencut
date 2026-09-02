"""The transcript round-trip (architecture.md §9.2).

The check exists to catch what a still frame cannot show: desync, a wrong take,
truncated narration, and a cut applied at the wrong instant. Its own hard part is
not the diff — it is that after §4.4 the rendered audio is *supposed* to differ
from what was said, so the interesting test is not "a difference is caught" but
"a correct edit reports nothing while differing from the raw transcript by half
its words". That is phase 6's second exit criterion, and it is the check on the
check.

Like the two §11 breakages a spec cannot carry, this runs against constructed
transcripts. It has to: the synthetic fixture's audio is a test tone, so ASR of
its render says nothing whatever the edit did, and the deliberately broken
fixture cannot mispronounce a word it never speaks.
"""

import json
import os
import shutil
import stat
import subprocess

import pytest

from compile.timeline import EditedTimeline, KeptSpan, project
from runner.contract import StageRequest
from runner.stages import STAGES
from prefs import resolve_profile
from spec.audio import AudioTrack
from spec.captions import CaptionBlock, Word
from spec.edit import EditDecisions, Removal, RemovalKind, Segment, Tier
from spec.editspec import EditSpec
from spec.source import Source
from synth.asr import TranscribedWord
from verify.checks import check_transcript
from verify.report import Severity
from verify.transcript import (
    SEAM_TOLERANCE_S,
    WER_CEILING,
    RoundTrip,
    expected_transcript,
    normalize,
    round_trip,
)

WORD_S = 0.4
"""One word per 0.4s, so a source time reads off as `index * 0.4`."""


def words(text: str, start: float = 0.0) -> list[TranscribedWord]:
    """A transcript on a metronome. Timings are what the diff anchors on, so they
    have to be predictable enough to write an expectation about."""
    return [
        TranscribedWord(t_in=start + i * WORD_S, t_out=start + i * WORD_S + WORD_S * 0.8, text=token)
        for i, token in enumerate(text.split())
    ]


def timeline_of(*spans: tuple[float, float]) -> EditedTimeline:
    kept: list[KeptSpan] = []
    offset = 0.0
    for source_in, source_out in spans:
        kept.append(KeptSpan(source_in=source_in, source_out=source_out, output_in=offset))
        offset += source_out - source_in
    return EditedTimeline(profile="test", threshold=Tier.OPTIONAL, duration=offset, spans=kept)


def heard(expected, *, drop=(), substitute=None, extra=None):
    """What ASR made of the render, built by perturbing the expected transcript.

    A render is measured, not asserted, so every "actual" here stands in for an
    ASR pass that cannot run in this repository. Perturbing the expected list is
    what keeps the perturbation the only variable.
    """
    substitute = substitute or {}
    out = [
        TranscribedWord(t_in=w.t_in, t_out=w.t_out, text=substitute.get(i, w.text))
        for i, w in enumerate(expected)
        if i not in drop
    ]
    return sorted([*out, *(extra or [])], key=lambda w: w.t_in)


# --- the expected transcript -------------------------------------------------


def test_the_expected_transcript_is_the_source_minus_removals_and_unselected_tiers():
    source = words("one two three four five six")  # 0.0 .. 2.4s
    # Keep [0, 0.8) and [1.6, 2.4): words one, two, five and six.
    expected = expected_transcript(source, timeline_of((0.0, 0.8), (1.6, 2.4)))
    assert [w.text for w in expected] == ["one", "two", "five", "six"]


def test_a_surviving_word_is_reported_in_output_time_not_source_time():
    source = words("one two three four five six")
    expected = expected_transcript(source, timeline_of((0.0, 0.8), (1.6, 2.4)))
    # "five" is at 1.6s in the source and immediately after "two" in the render.
    assert expected[2].text == "five"
    assert expected[2].t_in == pytest.approx(0.8, abs=1e-6)


def test_two_profiles_expect_different_audio():
    """§9.2 is per profile because §4.4.1 makes tier selection per profile. A
    15-second short and a three-minute demo do not expect to hear the same words,
    and one expected transcript for both would fail whichever it was not written
    for."""
    spec = spec_with_tiers()
    source = words("aa bb cc dd ee ff gg hh ii jj kk ll mm nn oo pp qq rr ss tt uu vv ww xx")
    # A budget between the two segments, which is the whole of §4.4.1's rule: the
    # tiers are one aspect-independent decision and the budget is what selects.
    tight = resolve_profile("shorts_9x16").model_copy(update={"duration_budget": 6.0})
    short = expected_transcript(source, project(spec, tight))
    demo = expected_transcript(source, project(spec, resolve_profile("demo_16x9")))
    assert len(short) < len(demo), "the tighter budget drops the supporting segment"
    assert [w.text for w in demo][: len(short)] == [w.text for w in short]


def spec_with_tiers() -> EditSpec:
    """Ten seconds: an essential half and a supporting half, and a budget between
    them, so `shorts_9x16` selects one and `demo_16x9` selects both."""
    return EditSpec(
        job_id="rt",
        source=Source(source_id="s", path="source.mp4", duration=10.0, width=1920, height=1080, fps=30.0),
        edit=EditDecisions(
            segments=[
                Segment(t_in=0.0, t_out=5.0, tier=Tier.ESSENTIAL, reason="the point"),
                Segment(t_in=5.0, t_out=10.0, tier=Tier.SUPPORTING, reason="the aside"),
            ]
        ),
        audio=AudioTrack(),
    )


def test_the_timeline_and_the_spec_agree_about_what_survives():
    """Two implementations of one formula, which is the trap this codebase has
    sprung before. `EditSpec.transcript_after_edit` computes the expected
    transcript from the spec's own caption words; `expected_transcript` computes
    it from the ASR transcript through the projected timeline, because it needs
    output timings to anchor a difference. They must not disagree."""
    spec = spec_with_captions()
    profile = resolve_profile("demo_16x9")
    timeline = project(spec, profile)
    source = [
        TranscribedWord(t_in=w.t_in, t_out=w.t_out, text=w.text)
        for block in spec.captions
        for w in block.words
    ]
    projected = " ".join(w.text for w in expected_transcript(source, timeline))
    assert projected == spec.transcript_after_edit(timeline.threshold)


def spec_with_captions() -> EditSpec:
    source = words("alpha bravo charlie delta echo foxtrot golf hotel")  # 0.0 .. 3.2s
    return EditSpec(
        job_id="rt",
        source=Source(source_id="s", path="source.mp4", duration=4.0, width=1920, height=1080, fps=30.0),
        edit=EditDecisions(
            removals=[Removal(t_in=1.2, t_out=2.0, kind=RemovalKind.FILLER)],
            segments=[
                Segment(t_in=0.0, t_out=1.2, tier=Tier.ESSENTIAL, reason="opening"),
                Segment(t_in=2.0, t_out=4.0, tier=Tier.ESSENTIAL, reason="closing"),
            ],
        ),
        captions=[
            CaptionBlock(
                t_in=source[0].t_in,
                t_out=source[-1].t_out,
                words=[Word(t_in=w.t_in, t_out=w.t_out, text=w.text) for w in source],
            )
        ],
    )


# --- the check on the check --------------------------------------------------


def test_a_correct_render_reports_zero_real_differences_though_the_raw_transcript_differs():
    """Phase 6's second exit criterion, and the one that decides whether §9.2
    stays useful once phase 5 exists."""
    source = words("this is the part that matters um and this is the bit i fluffed")
    timeline = timeline_of((0.0, 2.4))  # only the first six words survive
    expected = expected_transcript(source, timeline)
    result = round_trip("demo_16x9", source, heard(expected), timeline)

    assert result.real == []
    assert result.wer == 0.0
    assert result.raw_differences == len(source) - len(expected) > 0, (
        "against the raw transcript this render is missing half its words, which is "
        "the edit working — the whole reason §9.2 does not diff against it"
    )


def test_a_word_dropped_from_the_middle_of_a_span_is_a_real_failure():
    source = words("the cursor moves to the settings panel and stops")
    timeline = timeline_of((0.0, 3.6))
    expected = expected_transcript(source, timeline)
    result = round_trip("demo_16x9", source, heard(expected, drop={4}), timeline)

    assert [d.kind for d in result.real] == ["omission"]
    assert result.real[0].expected == "the"


def test_a_word_the_splice_clipped_is_the_edits_doing_rather_than_a_failure():
    """A cut joins two stretches that were never adjacent, and the word on either
    side of the seam can lose its onset. §9.2's second class is exactly that."""
    source = words("one two three four five six")
    timeline = timeline_of((0.0, 0.8), (1.6, 2.4))
    expected = expected_transcript(source, timeline)
    # "five" is the first word after the cut at 0.8s.
    result = round_trip("demo_16x9", source, heard(expected, substitute={2: "hive"}), timeline)

    assert result.real == []
    assert [d.actual for d in result.at_seam] == ["hive"]
    assert result.wer == 0.0


def test_a_misheard_word_away_from_every_cut_is_not_excused_by_the_edit():
    source = words("one two three four five six")
    timeline = timeline_of((0.0, 0.8), (1.6, 2.4))
    expected = expected_transcript(source, timeline)
    result = round_trip("demo_16x9", source, heard(expected, substitute={0: "wun"}), timeline)

    assert [d.expected for d in result.real] == ["one"]


def test_the_seam_tolerance_is_a_window_and_not_the_whole_render():
    """A tolerance wide enough to excuse everything is not a tolerance. The word
    after the seam is forgiven; the one after that is not."""
    source = words("one two three four five six seven eight")
    timeline = timeline_of((0.0, 0.8), (1.6, 3.2))
    expected = expected_transcript(source, timeline)
    beyond = next(i for i, w in enumerate(expected) if w.t_in > 0.8 + SEAM_TOLERANCE_S + WORD_S)
    result = round_trip("demo_16x9", source, heard(expected, substitute={beyond: "nope"}), timeline)

    assert [d.actual for d in result.real] == ["nope"]


def test_truncated_narration_is_a_real_failure_and_not_a_seam():
    """Narration that stops early is one of the five failures §9.2 names, and it
    is why the end of the timeline is not treated as a cut."""
    source = words("the whole point of this check is that it fails when the audio stops")
    timeline = timeline_of((0.0, 5.6))
    expected = expected_transcript(source, timeline)
    result = round_trip("demo_16x9", source, heard(expected)[:6], timeline)

    assert len(result.real) == len(expected) - 6
    assert all(d.kind == "omission" for d in result.real)
    assert result.wer > WER_CEILING


def test_a_removed_word_that_survived_the_render_is_a_real_failure():
    """The cut that did not happen. It cannot be excused as "a range the edit
    accounts for" — the edit accounting for it is precisely what did not occur."""
    source = words("keep this um drop that keep this")
    timeline = timeline_of((0.0, 0.8), (1.2, 2.8))  # "um" at 0.8 is removed
    expected = expected_transcript(source, timeline)
    survived = TranscribedWord(t_in=0.79, t_out=0.85, text="um")
    result = round_trip("demo_16x9", source, heard(expected, extra=[survived]), timeline)

    assert any(d.kind == "insertion" and d.actual == "um" for d in result.differences)


def test_punctuation_and_case_are_not_differences():
    """`parse_whisper_cpp` joins a trailing comma onto the word before it, so
    punctuation is a property of most tokens here. Comparing it would report the
    render's own punctuation as a fault on every job."""
    assert normalize("Settings,") == normalize("settings")
    source = words("open the settings panel")
    timeline = timeline_of((0.0, 1.6))
    expected = expected_transcript(source, timeline)
    spoken = heard(expected, substitute={2: "Settings,"})
    assert round_trip("demo_16x9", source, spoken, timeline).differences == []


def test_no_speech_round_trips_to_nothing_rather_than_failing():
    """A screen capture with the mic off is an ordinary job (§5.3)."""
    result = round_trip("demo_16x9", [], [], timeline_of((0.0, 4.0)))
    assert result.expected_words == 0 and result.wer == 0.0


# --- what lands on the report ------------------------------------------------


def test_a_clean_round_trip_says_how_much_of_the_render_the_edit_explains():
    """"Zero real differences" is indistinguishable from a check that ran against
    nothing. The number beside it is what makes the report legible (§9.1's third
    exit criterion)."""
    source = words("this is the part that matters and this is the bit i fluffed")
    timeline = timeline_of((0.0, 2.4))
    expected = expected_transcript(source, timeline)
    findings = check_transcript(round_trip("demo_16x9", source, heard(expected), timeline))

    by_check = {f.check: f for f in findings}
    assert by_check["transcript_round_trip"].severity is Severity.PASS
    assert by_check["transcript_vs_raw"].value == len(source) - len(expected)
    assert by_check["transcript_vs_raw"].severity is Severity.INFO, "a number, not a verdict"


def test_a_render_over_the_word_error_ceiling_fails_and_says_where():
    source = words("alpha bravo charlie delta echo foxtrot golf hotel india juliett")
    timeline = timeline_of((0.0, 4.0))
    expected = expected_transcript(source, timeline)
    bad = heard(expected, substitute={i: "noise" for i in range(5)})
    finding = next(
        f for f in check_transcript(round_trip("demo_16x9", source, bad, timeline))
        if f.check == "transcript_round_trip"
    )
    assert finding.severity is Severity.FAIL
    assert finding.value > WER_CEILING and finding.limit == WER_CEILING
    assert "alpha" in finding.message, "a failure nobody can locate is a failure nobody fixes"


def test_a_round_trip_that_could_not_run_warns_rather_than_passing_quietly():
    """A missing checker is not a passing render. Omitting the finding would make
    the two read the same on the report."""
    findings = check_transcript(
        RoundTrip(profile="demo_16x9", ran=False, note="'whisper-cli' is not on PATH")
    )
    assert [f.severity for f in findings] == [Severity.WARN]
    assert "whisper-cli" in findings[0].message


def test_no_round_trip_at_all_adds_nothing_to_the_report():
    """A fixture job has no transcript to diff against, and a report full of
    findings about a check that could not apply is a report nobody reads."""
    assert check_transcript(None) == []


# --- the stage (§5.1's contract, §9.2's work) --------------------------------


def stage_request(job_dir, transcript, *, binary="definitely-not-whisper") -> StageRequest:
    spec = spec_with_captions()
    (job_dir / "spec.json").write_text(spec.model_dump_json(indent=2))
    (job_dir / "transcript.json").write_text(
        json.dumps({"words": [w.model_dump() for w in transcript]})
    )
    return StageRequest(
        stage="verify_transcript",
        job_dir=str(job_dir),
        inputs={"spec": "spec.json", "transcript": "transcript.json", "render": "render.mp4"},
        params={
            "profile": resolve_profile("demo_16x9").model_dump(mode="json"),
            "encoder": "software",
            "asr": {
                "binary": binary,
                "models_dir": str(job_dir),
                "model": "large-v3",
                "language": "en",
            },
        },
        output="round_trip.json",
    )


def test_a_silent_source_does_not_transcribe_the_render_to_learn_it_is_silent(tmp_path):
    """The most expensive stage there is, run to confirm what the source
    transcript already said (§5.3). It does not run: no ASR binary is on PATH in
    this test and the stage still succeeds."""
    result = STAGES["verify_transcript"].run(stage_request(tmp_path, []))

    assert not result.degraded
    outcome = RoundTrip.model_validate_json((tmp_path / "round_trip.json").read_text())
    # It *ran* — it found nothing to hear, which is not a check that could not run.
    assert outcome.ran and outcome.expected_words == 0
    assert [f.severity for f in check_transcript(outcome)] == [Severity.INFO]


def test_a_missing_asr_backend_degrades_the_check_rather_than_failing_the_job(tmp_path):
    """§7.4's shape, applied to a checker. The render is fine; the check could not
    run, and a job that refused to finish because a *verifier* was missing would be
    a worse tool than one that says so on the report.

    Degraded, so `runner/pipeline.py` does not cache it — caching a fallback makes
    one missing binary permanent."""
    request = stage_request(tmp_path, words("alpha bravo charlie delta"))
    result = STAGES["verify_transcript"].run(request)

    assert result.degraded, "an uncached artifact is the point; a cached one is a lie"
    outcome = RoundTrip.model_validate_json((tmp_path / "round_trip.json").read_text())
    assert outcome.ran is False
    assert [f.severity for f in check_transcript(outcome)] == [Severity.WARN]


def test_the_stage_that_holds_weights_is_the_one_that_declares_it():
    """8GB will not hold two models (§16). `verify_transcript` runs ASR and
    `verify` does not, which is why they are two stages: one flag on `verify`
    would have every report claim 4GB it never used."""
    assert STAGES["verify_transcript"].holds_local_weights
    assert not STAGES["verify"].holds_local_weights


# --- the ASR path, against a stand-in ----------------------------------------

FAKE_WHISPER = """#!/usr/bin/env python3
# A stand-in for `whisper-cli`, in the shape of the stand-in `tests/test_plan_edit.py`
# uses for the agent. It writes what whisper.cpp's `-oj` writes — the shape
# `synth/asr.py`'s parser is written against — reading the words from a file the
# test put beside it. It proves the stage runs end to end and proves nothing
# whatever about recognition.
import json, sys
from pathlib import Path

argv = sys.argv[1:]
prefix = Path(argv[argv.index("-of") + 1])
words = json.loads((Path(__file__).parent / "heard.json").read_text())
prefix.with_suffix(".json").write_text(json.dumps({
    "result": {"language": "en"},
    "transcription": [
        {"offsets": {"from": round(w["t_in"] * 1000), "to": round(w["t_out"] * 1000)},
         "text": " " + w["text"]}
        for w in words
    ],
}))
"""

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


@pytest.fixture
def whisper_stand_in(tmp_path, monkeypatch):
    """Install a fake `whisper-cli` on PATH and return a handle to script it."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "whisper-cli"
    script.write_text(FAKE_WHISPER)
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    # `transcribe` refuses without weights as well as without a binary, and it is
    # right to: "installed but unusable" is the state that otherwise fails deep
    # inside whisper.cpp with an unreadable message.
    (tmp_path / "ggml-large-v3.bin").write_bytes(b"")

    def hears(spoken):
        (bin_dir / "heard.json").write_text(
            json.dumps([{"t_in": w.t_in, "t_out": w.t_out, "text": w.text} for w in spoken])
        )

    return hears


@needs_ffmpeg
def test_the_stage_transcribes_the_render_and_reports_what_the_edit_expects(
    tmp_path, whisper_stand_in
):
    """The one path the unit tests above cannot reach: extraction, invocation,
    parse, diff and artifact, in the stage the pipeline actually runs.

    Like the agent stand-in in `tests/test_plan_edit.py`, this tests our code end
    to end and tests nothing about recognition — which is the half phase 6's
    remaining exit criterion is for.
    """
    spec = spec_with_captions()
    render = tmp_path / "render.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", "color=c=black:s=320x180:d=3.2:r=30",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=3.2",
         "-c:v", "libx264", "-c:a", "aac", "-shortest", str(render)],
        check=True,
    )

    source = [
        TranscribedWord(t_in=w.t_in, t_out=w.t_out, text=w.text)
        for block in spec.captions
        for w in block.words
    ]
    timeline = project(spec, resolve_profile("demo_16x9"))
    whisper_stand_in(expected_transcript(source, timeline))

    request = stage_request(tmp_path, source, binary="whisper-cli")
    result = STAGES["verify_transcript"].run(request)

    assert not result.degraded
    outcome = RoundTrip.model_validate_json((tmp_path / "round_trip.json").read_text())
    assert outcome.real == []
    assert outcome.expected_words == 6, "the filler removal drops two of the eight words"
    assert outcome.raw_differences == 2, "the raw transcript still has them, which is the edit"
    assert [f.severity for f in check_transcript(outcome)][0] is Severity.PASS


@needs_ffmpeg
def test_the_stage_catches_a_word_the_render_lost(tmp_path, whisper_stand_in):
    """The failure the whole stage exists for, through the stage rather than the
    function: narration that stops early."""
    spec = spec_with_captions()
    render = tmp_path / "render.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", "color=c=black:s=320x180:d=3.2:r=30",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=3.2",
         "-c:v", "libx264", "-c:a", "aac", "-shortest", str(render)],
        check=True,
    )
    source = [
        TranscribedWord(t_in=w.t_in, t_out=w.t_out, text=w.text)
        for block in spec.captions
        for w in block.words
    ]
    timeline = project(spec, resolve_profile("demo_16x9"))
    # Stopping after the cut, so nothing here can be blamed on the seam.
    whisper_stand_in(expected_transcript(source, timeline)[:4])

    STAGES["verify_transcript"].run(stage_request(tmp_path, source, binary="whisper-cli"))
    outcome = RoundTrip.model_validate_json((tmp_path / "round_trip.json").read_text())

    assert [d.expected for d in outcome.real] == ["golf", "hotel"]
    assert outcome.at_seam == [], "the edit does not get the blame for this one"
    assert outcome.wer > WER_CEILING
    assert check_transcript(outcome)[0].severity is Severity.FAIL
