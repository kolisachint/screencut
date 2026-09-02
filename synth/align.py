"""Forced alignment: a known script against the audio that reads it (§5.3).

The second of §5.3's two different ASR calls, and the one phase 8 needs. F5-TTS
returns no reliable word timestamps, so a synthesized narration arrives as a wav
and a script with nothing joining them. `align` joins them: the script is ground
truth for *what* is said, the audio decides *when*.

**Why this is not WhisperX.** §5.3 named WhisperX, and it remains the upgrade
behind this stage's contract. It is not what runs here, because phase 0 ran three
ASR backends on the target machine and WhisperX was not among them (environment
findings §3) — writing a parser for output nobody has seen is the exact failure
phase 0 exists to prevent, and `AGENTS.md` says so in as many words. What this
repository *has* run is whisper.cpp, whose word timings phase 4's captions are
already built on.

So alignment is done the way the rest of this system does things that arithmetic
can do (principle 3): open-transcribe the narration with the backend that works,
then anchor the script to what came back. Every script word that whisper
recognized takes that word's measured timing; the runs between anchors are
interpolated across the gap, weighted by word length. The script text is what
survives — never whisper's — because the script is the ground truth here and
whisper's mishearing must not be laundered into a caption.

That last point is what keeps §9.2 honest. Captions carry the script; `verify`
open-transcribes the *render* and diffs it against them (§9.2), so a TTS
mispronunciation is a difference between what the script says and what the
render says. Align the other way — take whisper's words — and the round-trip
would be comparing the render against a transcript of itself, agreeing perfectly
about a word the narration got wrong.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from synth.asr import TranscribedWord

MIN_WORD_S = 0.04
"""Floor on an interpolated word's width. Two words at the same timestamp are a
zero-length caption and a division by zero in `plan_captions`; a 40 ms floor is
below anything a person says and above anything that breaks arithmetic."""

_WORD = re.compile(r"\S+")
_NORMALIZE = re.compile(r"[^\w']+", re.UNICODE)


@dataclass(frozen=True)
class Alignment:
    """The aligned words, plus how much of the script was actually heard."""

    words: list[TranscribedWord]
    anchored: int
    """Script words that took a measured timing rather than an interpolated one."""

    @property
    def coverage(self) -> float:
        """0.0 when nothing was recognized, 1.0 when every script word was.

        Not a verdict on its own. Low coverage on synthesized narration means the
        audio and the script have drifted apart, which §9.2 reports properly by
        listening to the render — this is the cheap number that says where to look.
        """
        return self.anchored / len(self.words) if self.words else 0.0


def script_words(script: str) -> list[str]:
    """The script as it will be captioned: whitespace-separated, punctuation kept.

    Punctuation stays because it is burned into a frame. Matching strips it
    (`_key`), so "corner." still anchors to whisper's "corner"."""
    return _WORD.findall(script)


def _key(word: str) -> str:
    return _NORMALIZE.sub("", word).lower()


def align(script: str, heard: list[TranscribedWord], duration: float) -> Alignment:
    """Anchor `script` to the timings in `heard`, filling the gaps by arithmetic.

    `duration` bounds the last word, so a script whose tail was never recognized
    still lands inside the audio rather than trailing past the end of it.
    """
    words = script_words(script)
    if not words:
        return Alignment(words=[], anchored=0)

    anchors = _anchors(words, heard)
    spans: list[tuple[float, float]] = [(0.0, 0.0)] * len(words)
    for index, word in anchors.items():
        spans[index] = (word.t_in, word.t_out)

    ordered = sorted(anchors)
    _fill(words, spans, ordered, duration)
    return Alignment(
        words=[
            TranscribedWord(t_in=t_in, t_out=t_out, text=text)
            for text, (t_in, t_out) in zip(words, _monotonic(spans, duration))
        ],
        anchored=len(anchors),
    )


def _anchors(words: list[str], heard: list[TranscribedWord]) -> dict[int, TranscribedWord]:
    """Script index -> the recognized word it matches.

    `SequenceMatcher` over normalized tokens rather than a hand-rolled dynamic
    program: the two sequences are nearly identical by construction — the audio
    is a reading of this script — so the interesting cases are short runs of
    insertion and deletion, which is what its opcodes describe directly.
    """
    matcher = SequenceMatcher(
        a=[_key(w) for w in words], b=[_key(w.text) for w in heard], autojunk=False
    )
    matched: dict[int, TranscribedWord] = {}
    for tag, i1, i2, j1, _ in matcher.get_opcodes():
        if tag != "equal":
            continue
        for offset in range(i2 - i1):
            matched[i1 + offset] = heard[j1 + offset]
    return matched


def _fill(
    words: list[str], spans: list[tuple[float, float]], anchored: list[int], duration: float
) -> None:
    """Interpolate every run of unanchored words between its bracketing anchors.

    Weighted by word length, which is a better proxy for how long a word takes to
    say than an equal split and costs three lines. The head runs from 0, the tail
    to `duration`; with no anchors at all the whole script is spread across the
    audio, which is the honest answer when nothing was recognized.
    """
    boundaries = [-1, *anchored, len(words)]
    for left, right in zip(boundaries, boundaries[1:]):
        gap = range(left + 1, right)
        if not gap:
            continue
        start = spans[left][1] if left >= 0 else 0.0
        end = spans[right][0] if right < len(words) else duration
        end = max(end, start)
        weights = [max(len(_key(words[index])), 1) for index in gap]
        total = sum(weights)
        cursor = start
        for index, weight in zip(gap, weights):
            width = (end - start) * weight / total
            spans[index] = (cursor, cursor + width)
            cursor += width


def _monotonic(spans: list[tuple[float, float]], duration: float) -> list[tuple[float, float]]:
    """Ascending, non-degenerate, and inside the audio.

    Whisper's own timings can overlap by a few milliseconds at a segment boundary,
    and an interpolated run across a zero-width gap is zero-width too. Both make a
    `CaptionBlock` that fails validation two stages later, where the cause is no
    longer visible.

    The backward pass is the one that matters: applying a floor word by word can
    push the tail past the end of the audio, and a caption after the last frame
    fails `EditSpec._within_source`. It walks back from `duration` so the crowded
    tail gives way rather than every measured timing being scaled to make room.
    """
    forward: list[tuple[float, float]] = []
    cursor = 0.0
    for t_in, t_out in spans:
        t_in = max(t_in, cursor)
        t_out = max(t_out, t_in + MIN_WORD_S)
        forward.append((t_in, t_out))
        cursor = t_out

    if duration <= 0 or not forward or forward[-1][1] <= duration:
        return forward

    limit = duration
    out = list(forward)
    for index in reversed(range(len(out))):
        t_in, t_out = out[index]
        if t_out <= limit:
            break
        t_out = limit
        t_in = max(min(t_in, t_out - MIN_WORD_S), 0.0)
        out[index] = (t_in, t_out)
        limit = t_in
    return out
