"""`trim` — arithmetic proposes what to cut (architecture.md §4.6, §7.1).

Two mechanisms, neither of them a model, and §7.1 commits to that in writing:
finding a silence is a threshold comparison and finding an "um" is a lookup.

- **Dead air** from `ffmpeg -af silencedetect`, which measures the audio rather
  than inferring silence from gaps in the transcript. A gap in the words is not
  the same thing as a gap in the sound — a long "uhhhh" is a gap in one and not
  in the other, and so is typing.
- **Filler words** from a closed list against the transcript's word timings.

**This is §7.4's floor, not a first draft.** When `plan_edit` fails — no network,
no agent, an invalid fragment twice — the job renders `trim`'s removals with every
segment `essential`. That is a silence-trimmed, filler-stripped video rather than
the unedited take this project exists to avoid, which is the whole reason §7.1
splits the two stages instead of handing the model the raw timeline.

Three rules that are not obvious and that each caused a wrong cut before they
were written down:

1. **A range with words in it is not dead air**, whatever the meter says. Someone
   speaking quietly reads as silence at any threshold loose enough to catch real
   dead air, and cutting there deletes a sentence. ASR wins; the silence is
   clipped around the words.
2. **`keep_pad_ms` shrinks a removal, it does not grow it.** A cut placed exactly
   at the level threshold clips the breath before the next word, which is audible
   immediately and looks like a bug in the compiler.
3. **Removals merge.** A filler landing against a silence would otherwise leave a
   40 ms segment between them, and §4.4's totality makes that a real segment that
   a profile can select and a reviewer has to look at.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from spec.captions import Word
from spec.edit import Removal, RemovalKind
from spec.origin import Stage
from spec.types import TIME_EPS

Span = tuple[float, float]

_SILENCE_START = re.compile(r"silence_start:\s*(-?[\d.]+)")
_SILENCE_END = re.compile(r"silence_end:\s*(-?[\d.]+)")


@dataclass(frozen=True)
class TrimTunables:
    """§4.6's scalars. Every one is learnable by median under §10, and every one
    is a number you can also just set by hand when a job needs it."""

    silence_db: float = -35.0
    min_silence_ms: int = 600
    keep_pad_ms: int = 120
    filler_words: tuple[str, ...] = ("um", "uh", "erm", "uhm", "mmm", "hmm")


def detect_silence(
    audio: Path | str, *, silence_db: float, min_silence_ms: int
) -> list[Span]:
    """Measured silence, from FFmpeg rather than from the transcript.

    `silencedetect` writes to stderr and reports nothing on stdout, which is why
    this reads the former. It is a measurement of the source, so it is `trim`'s
    only reason to touch media at all.
    """
    completed = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-nostats", "-nostdin",
            "-i", str(audio),
            "-af", f"silencedetect=n={silence_db}dB:d={min_silence_ms / 1000.0}",
            "-f", "null", "-",
        ],
        capture_output=True, text=True,
    )
    return parse_silencedetect(completed.stderr)


def parse_silencedetect(stderr: str, duration: float | None = None) -> list[Span]:
    """`silence_start`/`silence_end` pairs, in source seconds.

    A take that ends in silence gets a `silence_start` with no `silence_end` —
    FFmpeg has nothing to close it against. That trailing run is the single most
    common thing worth cutting from a screen recording (the pause before you
    reach for the stop button), so it is closed at the source duration rather
    than dropped.
    """
    spans: list[Span] = []
    start: float | None = None
    for line in stderr.splitlines():
        opened = _SILENCE_START.search(line)
        if opened:
            start = max(float(opened.group(1)), 0.0)
            continue
        closed = _SILENCE_END.search(line)
        if closed and start is not None:
            spans.append((start, float(closed.group(1))))
            start = None
    if start is not None and duration is not None and duration > start:
        spans.append((start, duration))
    return spans


def filler_spans(words: list[Word], filler_words: tuple[str, ...]) -> list[Span]:
    """Where the closed list matched. A list, not a model (§7.1)."""
    listed = {w.lower() for w in filler_words}
    return [(w.t_in, w.t_out) for w in words if _bare(w.text) in listed]


def _bare(text: str) -> str:
    """A word without the punctuation `transcribe` attached to it.

    `plan_captions` joins a full stop onto the word before it, so the transcript
    carries "um," rather than "um" — and a closed list matched against the raw
    string quietly stops matching the moment a filler ends a clause.
    """
    return re.sub(r"[^\w']", "", text).lower()


def trim(
    words: list[Word],
    silences: list[Span],
    duration: float,
    tunables: TrimTunables | None = None,
) -> list[Removal]:
    """Proposed removals, in source time, ordered and non-overlapping.

    Proposed is the operative word: `plan_edit` may reject any of these, and
    `Removal.proposed_by` records which stage each one came from so the override
    rate lands on the verification report (§9.1).
    """
    tunables = tunables or TrimTunables()
    pad = tunables.keep_pad_ms / 1000.0
    minimum = tunables.min_silence_ms / 1000.0

    spoken = sorted((w.t_in, w.t_out) for w in words)
    kept: list[tuple[Span, RemovalKind]] = []

    for span in silences:
        for piece in _subtract(span, spoken):
            if piece[1] - piece[0] < minimum - TIME_EPS:
                continue  # a beat, not dead air (§4.6)
            padded = (piece[0] + pad, piece[1] - pad)
            if padded[1] - padded[0] > TIME_EPS:
                kept.append((padded, RemovalKind.SILENCE))

    for span in filler_spans(words, tunables.filler_words):
        kept.append((span, RemovalKind.FILLER))

    return [
        Removal(
            t_in=max(t_in, 0.0),
            t_out=min(t_out, duration),
            kind=kind,
            proposed_by=Stage.TRIM,
        )
        for (t_in, t_out), kind in _merge(kept)
        if min(t_out, duration) - max(t_in, 0.0) > TIME_EPS
    ]


def _subtract(span: Span, spoken: list[Span]) -> list[Span]:
    """`span` minus every range a word occupies — rule 1.

    Returns the pieces that survive, which is usually the whole span and
    occasionally nothing at all."""
    pieces = [span]
    for word in spoken:
        nxt: list[Span] = []
        for piece in pieces:
            if word[1] <= piece[0] + TIME_EPS or word[0] >= piece[1] - TIME_EPS:
                nxt.append(piece)
                continue
            if word[0] > piece[0] + TIME_EPS:
                nxt.append((piece[0], word[0]))
            if word[1] < piece[1] - TIME_EPS:
                nxt.append((word[1], piece[1]))
        pieces = nxt
    return pieces


def _merge(spans: list[tuple[Span, RemovalKind]]) -> list[tuple[Span, RemovalKind]]:
    """Overlapping or touching removals become one — rule 3.

    A merged range keeps the kind of its earliest part. Two kinds cannot both be
    recorded on one `Removal`, and the leading one is what a reviewer reads the
    cut as: a silence that swallowed a trailing "um" is still a silence.
    """
    if not spans:
        return []
    ordered = sorted(spans, key=lambda entry: entry[0][0])
    merged = [ordered[0]]
    for (t_in, t_out), kind in ordered[1:]:
        (last_in, last_out), last_kind = merged[-1]
        if t_in <= last_out + TIME_EPS:
            merged[-1] = ((last_in, max(last_out, t_out)), last_kind)
        else:
            merged.append(((t_in, t_out), kind))
    return merged
