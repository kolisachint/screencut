"""The whisper.cpp transcript parser (§5.3, phase 4).

Fixtures here are whisper.cpp's own output shape, taken from `output_json` in its
`examples/cli/cli.cpp` and from the invocation phase 0 ran on the target machine
(environment findings §3) — not from a plausible-looking JSON somebody imagined.
`offsets` are milliseconds because `cli.cpp` writes `t0 * 10` over whisper's
centisecond timestamps.
"""

from __future__ import annotations

import pytest

from synth.asr import (
    WHISPER_SAMPLE_RATE,
    Transcript,
    audio_command,
    model_path,
    parse_whisper_cpp,
    whisper_command,
)


def segment(text: str, from_ms: int, to_ms: int) -> dict:
    return {
        "timestamps": {"from": "00:00:00,000", "to": "00:00:00,000"},
        "offsets": {"from": from_ms, "to": to_ms},
        "text": text,
    }


def test_offsets_are_read_as_milliseconds():
    """`timestamps` beside them is the same number formatted for a human, and
    parsing a rendered string back into a number when the number is one field
    along is how a rounding bug gets in for free."""
    words = parse_whisper_cpp({"transcription": [segment(" Here", 0, 320)]})
    assert (words[0].t_in, words[0].t_out) == (0.0, 0.32)


def test_whispers_leading_space_is_not_part_of_the_word():
    words = parse_whisper_cpp({"transcription": [segment(" dashboard", 100, 400)]})
    assert words[0].text == "dashboard"


def test_a_non_speech_annotation_is_not_a_word():
    """Left in, `[BLANK_AUDIO]` is burned into the video — obvious in a render and
    invisible in a transcript."""
    data = {"transcription": [segment(" [BLANK_AUDIO]", 0, 900), segment(" (upbeat music)", 900, 1800)]}
    assert parse_whisper_cpp(data) == []


def test_a_punctuation_only_segment_joins_the_word_before_it():
    """At `--max-len 1` a full stop is its own segment, and as a word it becomes a
    caption block containing one full stop."""
    data = {"transcription": [segment(" today", 0, 300), segment(".", 300, 320)]}
    words = parse_whisper_cpp(data)
    assert [w.text for w in words] == ["today."]
    assert words[0].t_out == 0.32


def test_leading_punctuation_with_nothing_before_it_is_dropped_not_crashed_on():
    assert parse_whisper_cpp({"transcription": [segment(",", 0, 20)]}) == []


def test_an_empty_transcription_array_is_a_transcript_with_no_words():
    """A screen capture with the mic off is an ordinary job. This is the exact
    top-level shape whisper.cpp writes when it finds no speech."""
    data = {
        "systeminfo": "WHISPER : COREML = 0 | ...",
        "model": {"type": "tiny", "multilingual": False},
        "params": {"model": "ggml-large-v3.bin", "language": "en", "translate": False},
        "result": {"language": "en"},
        "transcription": [],
    }
    assert parse_whisper_cpp(data) == []
    assert Transcript(words=[]).text == ""


def test_an_inverted_segment_does_not_produce_an_inverted_word():
    """`CaptionBlock` rejects inverted words, and it would reject them a stage
    later where the cause is no longer in view."""
    words = parse_whisper_cpp({"transcription": [segment(" ok", 500, 400)]})
    assert words[0].t_out >= words[0].t_in


def test_the_invocation_is_the_one_phase_0_confirmed():
    """`--max-len 1` with `-ml 1` is what makes segments words: whisper.cpp has no
    word-timestamp flag, and §6.2's per-word timings are not optional."""
    command = whisper_command(
        "whisper-cli", model_path("/w", "large-v3"), "a.wav", "out", language="en", threads=4
    )
    assert command[0] == "whisper-cli"
    assert "-oj" in command
    assert command[command.index("--max-len") + 1] == "1"
    assert command[command.index("-ml") + 1] == "1"
    assert command[command.index("-m") + 1] == "/w/ggml-large-v3.bin"
    assert command[command.index("-t") + 1] == "4"


def test_audio_is_extracted_as_mono_16k_because_that_is_what_whisper_wants():
    """It also has to happen at all: whisper.cpp reads wav, mp3, flac and ogg —
    not the mp4 a screen recorder writes."""
    command = audio_command("take.mp4", "audio.wav")
    assert command[command.index("-ar") + 1] == str(WHISPER_SAMPLE_RATE)
    assert command[command.index("-ac") + 1] == "1"
    assert "-vn" in command
