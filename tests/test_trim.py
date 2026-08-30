"""`trim` — arithmetic proposes (§4.6, §7.1).

§7.4's floor, so these are the tests that decide what a job renders when the
network is down. Each names a rule that was written because ignoring it produced
a wrong cut.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from plan.trim import (
    TrimTunables,
    detect_silence,
    filler_spans,
    parse_silencedetect,
    trim,
)
from spec.captions import Word
from spec.edit import Removal, RemovalKind, Tier, decisions_from_removals
from spec.origin import Stage

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def words(*spec: tuple[str, float, float]) -> list[Word]:
    return [Word(text=t, t_in=a, t_out=b) for t, a, b in spec]


# --- reading FFmpeg ----------------------------------------------------------


def test_silencedetect_pairs_are_read_as_source_seconds():
    stderr = (
        "[silencedetect @ 0x1] silence_start: 2.02014\n"
        "[silencedetect @ 0x1] silence_end: 4.01705 | silence_duration: 1.99692\n"
    )
    assert parse_silencedetect(stderr) == [(2.02014, 4.01705)]


def test_a_take_that_ends_in_silence_is_closed_at_the_duration():
    """FFmpeg has nothing to close the last run against, and the pause before you
    reach for the stop button is the single most common thing worth cutting."""
    stderr = "[silencedetect @ 0x1] silence_start: 20.5\n"
    assert parse_silencedetect(stderr, duration=24.0) == [(20.5, 24.0)]


def test_a_trailing_silence_with_no_duration_to_close_it_is_dropped():
    assert parse_silencedetect("[silencedetect @ 0x1] silence_start: 20.5\n") == []


@needs_ffmpeg
def test_silence_is_measured_from_the_audio_and_not_inferred_from_the_words(tmp_path):
    """A gap in the transcript is not a gap in the sound. A long "uhhhh" is one and
    not the other, and so is typing."""
    wav = tmp_path / "tone.wav"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=300:duration=6",
         "-af", "volume=0:enable='between(t,2,4)'", "-y", str(wav)],
        check=True,
    )
    spans = detect_silence(wav, silence_db=-35.0, min_silence_ms=400)
    assert len(spans) == 1
    assert spans[0][0] == pytest.approx(2.0, abs=0.1)
    assert spans[0][1] == pytest.approx(4.0, abs=0.1)


# --- the three rules ---------------------------------------------------------


def test_a_range_with_words_in_it_is_not_dead_air():
    """Rule 1. Someone speaking quietly reads as silence at any threshold loose
    enough to catch real dead air, and cutting there deletes a sentence."""
    said = words(("quietly", 2.0, 2.6))
    removals = trim(said, [(1.0, 5.0)], 6.0, TrimTunables(keep_pad_ms=0, min_silence_ms=0))
    assert [(round(r.t_in, 2), round(r.t_out, 2)) for r in removals] == [(1.0, 2.0), (2.6, 5.0)]


def test_keep_pad_shrinks_a_removal_rather_than_growing_it():
    """Rule 2. A cut placed exactly at the level threshold clips the breath before
    the next word, which is audible immediately and reads as a compiler bug."""
    removals = trim([], [(1.0, 3.0)], 6.0, TrimTunables(keep_pad_ms=200, min_silence_ms=0))
    assert (round(removals[0].t_in, 2), round(removals[0].t_out, 2)) == (1.2, 2.8)


def test_a_silence_shorter_than_the_padding_survives_as_no_removal_at_all():
    removals = trim([], [(1.0, 1.3)], 6.0, TrimTunables(keep_pad_ms=200, min_silence_ms=0))
    assert removals == []


def test_a_filler_against_a_silence_becomes_one_removal():
    """Rule 3. Otherwise a 40ms segment survives between them, and §4.4's totality
    makes that a real segment a profile can select and a reviewer must look at."""
    said = words(("um", 3.0, 3.3), ("right", 3.3, 3.8))
    removals = trim(said, [(1.0, 3.0)], 6.0, TrimTunables(keep_pad_ms=0, min_silence_ms=0))
    assert len(removals) == 1
    assert (round(removals[0].t_in, 2), round(removals[0].t_out, 2)) == (1.0, 3.3)


def test_a_merged_removal_keeps_the_kind_of_its_earliest_part():
    """A silence that swallowed a trailing "um" is what a reviewer reads as a
    silence, and only one kind fits on one `Removal`."""
    said = words(("um", 3.0, 3.3))
    removals = trim(said, [(1.0, 3.0)], 6.0, TrimTunables(keep_pad_ms=0, min_silence_ms=0))
    assert removals[0].kind is RemovalKind.SILENCE


# --- the closed list ---------------------------------------------------------


def test_the_filler_list_matches_through_the_punctuation_transcribe_attached():
    """`plan_captions` joins a full stop onto the word before it, so a list matched
    against the raw string quietly stops matching the moment a filler ends a
    clause."""
    assert filler_spans(words(("um,", 1.0, 1.3)), ("um",)) == [(1.0, 1.3)]
    assert filler_spans(words(("Uh.", 1.0, 1.3)), ("uh",)) == [(1.0, 1.3)]


def test_an_ordinary_word_is_not_a_filler():
    assert filler_spans(words(("umbrella", 1.0, 1.6)), ("um",)) == []


def test_the_default_list_holds_only_unambiguous_disfluencies():
    """"so", "like" and "actually" are fillers about half the time and ordinary
    words the other half. A list cannot tell which; judging that is what §7.1 pays
    `plan_edit` for."""
    listed = set(TrimTunables().filler_words)
    assert listed.isdisjoint({"so", "like", "right", "actually", "basically"})


def test_a_short_gap_is_a_beat_and_not_dead_air():
    removals = trim([], [(1.0, 1.3)], 6.0, TrimTunables(min_silence_ms=600, keep_pad_ms=0))
    assert removals == []


# --- what trim hands to §7.4 -------------------------------------------------


def test_every_removal_says_trim_proposed_it():
    """The override rate on the report is a number about `plan_edit` (§9.1), and
    it only means anything if the proposals are attributed at the source."""
    removals = trim(words(("um", 1.0, 1.3)), [(2.0, 4.0)], 6.0, TrimTunables(keep_pad_ms=0))
    assert removals and all(r.proposed_by is Stage.TRIM for r in removals)


def test_trims_removals_alone_make_a_total_edit():
    """§7.4's floor has to be renderable, and a renderable edit is a total one."""
    removals = trim(words(("um", 1.0, 1.3)), [(2.0, 4.0)], 6.0, TrimTunables(keep_pad_ms=0))
    decisions = decisions_from_removals(removals, 6.0)
    assert decisions.covers(6.0)
    assert all(s.tier is Tier.ESSENTIAL for s in decisions.segments)


def test_two_removals_that_touch_leave_no_zero_length_segment_between_them():
    decisions = decisions_from_removals(
        [
            Removal(t_in=0.0, t_out=1.0, kind=RemovalKind.SILENCE, proposed_by=Stage.TRIM),
            Removal(t_in=1.0, t_out=2.0, kind=RemovalKind.FILLER, proposed_by=Stage.TRIM),
        ],
        6.0,
    )
    assert [(s.t_in, s.t_out) for s in decisions.segments] == [(2.0, 6.0)]


def test_a_take_that_is_all_silence_still_produces_a_valid_edit():
    decisions = decisions_from_removals(
        [Removal(t_in=0.0, t_out=6.0, kind=RemovalKind.SILENCE, proposed_by=Stage.TRIM)], 6.0
    )
    assert decisions.covers(6.0)
    assert decisions.segments == []


# --- trim alone, against real audio ------------------------------------------


@needs_ffmpeg
def test_trim_alone_cuts_the_fixtures_dead_air_and_filler_without_clipping_a_word(tmp_path):
    """Phase 5's first exit criterion, on the only take this machine has.

    The synthetic fixture is speech for 72% of each beat and silence for the rest
    (`ingest/fixtures.py`), with one deliberate "um" — so `silencedetect` has real
    dead air to find in real audio and the closed list has a real filler to find
    in real word timings. What is *not* proven here is taste: whether the result
    is watchable is a judgement in front of a person, and phase 5's gate says so.
    """
    from ingest.fixtures import build_spec, write_fixture
    from spec.editspec import EditSpec
    from verify.checks import check_cuts_land_between_words

    job = tmp_path / "demo"
    fixture = build_spec("trim-demo", width=320, height=180)
    write_fixture(job, fixture)

    spoken = [w for block in fixture.spec.captions for w in block.words]
    tunables = TrimTunables()
    silences = detect_silence(
        job / fixture.spec.source.path,
        silence_db=tunables.silence_db,
        min_silence_ms=tunables.min_silence_ms,
    )
    removals = trim(spoken, silences, fixture.spec.source.duration, tunables)

    assert silences, "the fixture is supposed to contain dead air"
    assert any(r.kind is RemovalKind.SILENCE for r in removals)
    assert any(r.kind is RemovalKind.FILLER for r in removals), "the fixture says 'um'"

    trimmed = EditSpec.model_validate({
        **fixture.spec.model_dump(mode="json"),
        "edit": decisions_from_removals(removals, fixture.spec.source.duration).model_dump(mode="json"),
    })
    assert trimmed.edit.covers(trimmed.source.duration)
    assert trimmed.edit.removed_duration() > 1.0, "trim found nothing worth cutting"

    # §9.1's own check, which is the one that catches a boundary through a word —
    # audible immediately, invisible in a still frame.
    failures = [f for f in check_cuts_land_between_words(trimmed) if f.severity.value == "fail"]
    assert not failures, [f.message for f in failures]
