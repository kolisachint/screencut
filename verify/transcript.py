"""The transcript round-trip (architecture.md §9.2).

Open-transcribe the rendered audio and diff it against what the edit says should
be there. One check catching audio/video desync, a wrong take, truncated
narration, a TTS mispronunciation — and the failure §4.4 introduced: a cut
applied at the wrong instant, which clips a word. Every one of those is invisible
until a person watches the video.

**The diff does not run against the raw transcript.** Once `plan_edit` removes
disfluencies and drops segments, the rendered audio is *supposed* to differ from
what was said, and a check that fires on every successful edit gets ignored
within a week. It runs against the **expected transcript**: the source transcript
minus `removals`, minus every segment below this profile's tier threshold. That
is arithmetic over the spec — no model, no guesswork — and it is **per profile**,
since two profiles select different tiers and therefore expect different audio.

§9.2's three classes, and what each one is here:

| Class | Here |
|---|---|
| Matches expected | an alignment match; not reported |
| A range `EditDecisions` accounts for | a difference sitting on a cut seam |
| Anything else | a real failure, and what the WER is computed over |

The middle row needs saying plainly, because "a range `EditDecisions` accounts
for" has a wrong reading that looks right. It cannot mean *removed* material
turning up in the render — if a removed word is audible, the cut did not happen,
which is a real failure and the loudest one this check can find. It means the
**seam**: a splice joins two stretches of audio that were never adjacent, and the
word on either side of it can lose its onset or its tail. Whisper then mishears
it, or hears one word where there were two. That is the edit working, at the only
place an edit can leave a mark, and it is the one difference the arithmetic
predicts rather than merely tolerates.
"""

from __future__ import annotations

import re
from typing import Iterable, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field

from compile.timeline import EditedTimeline
from synth.asr import TranscribedWord

SEAM_TOLERANCE_S = 0.30
"""How near a cut a difference has to sit to be the cut's doing.

Roughly one word's onset. Wider and a genuine misrecognition next to a cut gets
excused; narrower and the word the splice actually clipped is reported as a
failure on every correctly edited job. It has never met real speech — see the
phase 6 note in `implementation-phases.md` — and it is the first number here to
retune once it has."""

WER_CEILING = 0.10
"""Word error rate over real differences alone, above which the render fails.

Not zero, and not a claim about ASR quality: whisper hears "5" for "five" and
loses a word to a music bed, and both are floor rather than fault. Ten percent of
the expected words differing *away from every cut* is not a floor — it is a
different take, a desync, or narration that stopped early.

Like `SEAM_TOLERANCE_S` this has been exercised against constructed transcripts
and not against a real recording."""

_EDGES = re.compile(r"^\W+|\W+$", re.UNICODE)


def normalize(text: str) -> str:
    """Word forms that should compare equal.

    `parse_whisper_cpp` joins a trailing comma or full stop onto the word before
    it (§5.3), so punctuation is a property of nearly every token here and
    comparing it would report the edit's own punctuation drift as a failure.
    """
    return _EDGES.sub("", text).casefold()


class ExpectedWord(BaseModel):
    """A source word that survived, in **output** time.

    Output time, because that is the time base of the thing being measured: the
    render. Everything in `EditSpec` is source time (§4.5) and this is the one
    place the projection has already happened.
    """

    model_config = ConfigDict(extra="forbid")

    t_in: float
    t_out: float
    text: str


class Difference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["substitution", "omission", "insertion"]
    expected: str | None = None
    actual: str | None = None
    at: float = Field(description="Output seconds, so it can be scrubbed to in review (§8).")
    at_seam: bool = Field(
        default=False,
        description="Sits on a cut, so `EditDecisions` accounts for it (§9.2's second class).",
    )


class RoundTrip(BaseModel):
    """What §9.2 measured for one render. Folded into that render's report."""

    model_config = ConfigDict(extra="forbid")

    profile: str
    ran: bool = True
    note: str | None = None
    expected_words: int = 0
    heard_words: int = 0
    differences: list[Difference] = Field(default_factory=list)
    raw_differences: int = Field(
        default=0,
        description=(
            "What diffing against the *raw* transcript would have reported. Not a check — "
            "the check on the check. A correct edit makes this large and `real` zero, and "
            "that gap is the whole argument for §9.2 comparing against the expected "
            "transcript instead."
        ),
    )

    @property
    def real(self) -> list[Difference]:
        return [d for d in self.differences if not d.at_seam]

    @property
    def at_seam(self) -> list[Difference]:
        return [d for d in self.differences if d.at_seam]

    @property
    def wer(self) -> float:
        """Over the third class only, as §9.2 asks. Substitutions, omissions and
        insertions over the expected word count — the ordinary definition, with
        the seam differences taken out of the numerator rather than out of the
        alignment."""
        if not self.expected_words:
            return 0.0
        return len(self.real) / self.expected_words


def expected_transcript(
    words: Sequence[TranscribedWord], timeline: EditedTimeline
) -> list[ExpectedWord]:
    """The source transcript projected through this profile's edit (§9.2).

    A word is kept when its **midpoint** survives, which is the same rule
    `CaptionBlock.words_in` applies when `compile` trims a caption block to a span
    (§4.5). The two have to agree: if the caption keeps a word this drops, every
    correctly rendered job reports a difference for it, and the check is worthless
    within a week.

    `EditSpec.transcript_after_edit` is the same selection over the spec's own
    caption words. This one exists beside it because a difference has to be
    locatable — "at 41.2s" is what makes a finding actionable in review (§8) — and
    that needs output timings the spec deliberately does not carry (§4.5). One
    formula written twice is a trap this codebase has sprung before, so
    `tests/test_verify_transcript.py` checks them against each other.
    """
    kept: list[ExpectedWord] = []
    for word in words:
        middle = (word.t_in + word.t_out) / 2
        span = next((s for s in timeline.spans if s.holds_source(middle)), None)
        if span is None:
            continue  # removed, or in a segment this profile's threshold did not select
        # Clamped to the span before projecting, the same way `compile` projects a
        # caption's words (`timeline._edited_word`). A word straddling a cut keeps
        # only the part the viewer hears, and the two have to place it identically
        # or the caption and the expected transcript disagree about where it is.
        kept.append(
            ExpectedWord(
                t_in=span.to_output(max(word.t_in, span.source_in)),
                t_out=span.to_output(min(word.t_out, span.source_out)),
                text=word.text,
            )
        )
    kept.sort(key=lambda w: w.t_in)
    return kept


def round_trip(
    profile: str,
    source_words: Sequence[TranscribedWord],
    heard: Sequence[TranscribedWord],
    timeline: EditedTimeline,
) -> RoundTrip:
    """Diff the rendered audio against the expected transcript and classify.

    `source_words` is the raw transcript rather than the expected one because both
    are wanted: the expected transcript is what the check compares against, and
    the raw one is what a naive check would have compared against. Reporting the
    second number beside the first is what makes a passing report legible — zero
    real differences is unconvincing until you can see how many the edit moved.
    """
    expected = expected_transcript(source_words, timeline)
    differences = _classify(_diff(expected, heard), timeline.cuts)
    return RoundTrip(
        profile=profile,
        expected_words=len(expected),
        heard_words=len(heard),
        differences=differences,
        raw_differences=sum(
            1
            for operation in _diff(
                [ExpectedWord(t_in=w.t_in, t_out=w.t_out, text=w.text) for w in source_words], heard
            )
            if operation[0] != "match"
        ),
    )


# --- alignment ---------------------------------------------------------------

_Operation = tuple[str, ExpectedWord | None, TranscribedWord | None]


def _diff(
    expected: Sequence[ExpectedWord], heard: Sequence[TranscribedWord]
) -> list[_Operation]:
    """Levenshtein alignment over normalized word forms.

    The full O(n*m) table rather than a band: a truncated render is exactly the
    failure this exists to catch, and it is the case a banded aligner gives up on.
    A ten-minute take is about 1 500 words, so the table is a couple of million
    cells and runs in seconds — next to a render, and next to the ASR pass that
    produced its input, it is free.
    """
    left = [normalize(w.text) for w in expected]
    right = [normalize(w.text) for w in heard]

    rows, columns = len(left), len(right)
    # cost[i][j] = edit distance between left[:i] and right[:j].
    cost = [[0] * (columns + 1) for _ in range(rows + 1)]
    for i in range(1, rows + 1):
        cost[i][0] = i
    for j in range(1, columns + 1):
        cost[0][j] = j
    for i in range(1, rows + 1):
        row, previous = cost[i], cost[i - 1]
        token = left[i - 1]
        for j in range(1, columns + 1):
            row[j] = min(
                previous[j - 1] + (token != right[j - 1]),
                previous[j] + 1,
                row[j - 1] + 1,
            )

    operations: list[_Operation] = []
    i, j = rows, columns
    while i > 0 or j > 0:
        if i > 0 and j > 0 and cost[i][j] == cost[i - 1][j - 1] + (left[i - 1] != right[j - 1]):
            kind = "match" if left[i - 1] == right[j - 1] else "substitution"
            operations.append((kind, expected[i - 1], heard[j - 1]))
            i, j = i - 1, j - 1
        elif i > 0 and cost[i][j] == cost[i - 1][j] + 1:
            operations.append(("omission", expected[i - 1], None))
            i -= 1
        else:
            operations.append(("insertion", None, heard[j - 1]))
            j -= 1
    operations.reverse()
    return operations


def _classify(operations: Iterable[_Operation], cuts: Sequence[float]) -> list[Difference]:
    """Every non-match, with the seam ones marked (§9.2's second class).

    A difference is the seam's doing when the word it concerns *straddles* a cut,
    within one word-onset of tolerance on each side. Measured against the word's
    span rather than its start, because a splice clips the tail of the word before
    it as readily as the head of the word after."""
    differences: list[Difference] = []
    for kind, expected, heard in operations:
        if kind == "match":
            continue
        span = (
            (expected.t_in, expected.t_out) if expected is not None else (heard.t_in, heard.t_out)  # type: ignore[union-attr]
        )
        differences.append(
            Difference(
                kind=kind,  # type: ignore[arg-type]
                expected=expected.text if expected is not None else None,
                actual=heard.text if heard is not None else None,
                at=span[0],
                at_seam=any(
                    span[0] - SEAM_TOLERANCE_S <= cut <= span[1] + SEAM_TOLERANCE_S for cut in cuts
                ),
            )
        )
    return differences
