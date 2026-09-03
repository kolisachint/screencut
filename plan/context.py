"""What a model stage is told, shared by the stages that tell it (§7.2, §7.3).

With tools off there is no second way to get anything to a model stage (§7.3), so
the prompt *is* the input. Phase 5 had one stage and put its content builder
beside it. Phase 9 has five, four of which want the same two things — what was
said, and where attention was — and a summary written twice drifts the same way
any other duplicated formula does.

Nothing here decides anything. These are projections of artifacts the
deterministic stages already produced, compressed to what fits in a prompt and
says something a model can use.
"""

from __future__ import annotations

from spec.focus import FocusKind, FocusTrack
from spec.types import TIME_EPS

WORDS_PER_SECOND = 2.5
"""Narration pace, ~150 words per minute.

Used only to turn a recording's length into a word budget for `script_draft`.
It is a starting number, not a measurement: the first real narration read at a
real pace is what should move it, the same debt `PAUSE_S` and the focus tunables
are carrying.
"""


def focus_summary(track: FocusTrack, *, bucket_s: float = 2.0) -> str:
    """Where attention was, compressed enough to sit in a prompt.

    The whole track is thousands of points and says nothing a model can use. What
    it needs is the one thing `FocusTrack` knows that the transcript does not:
    which stretches were a demonstration and which were a cursor drifting while
    somebody talked. Clicks and dwell are that signal, so those are what is
    summarized and movement is dropped.
    """
    if not track.points:
        return "cursor: no track"
    end = track.points[-1].t
    lines: list[str] = []
    start = 0.0
    while start < end - TIME_EPS:
        stop = min(start + bucket_s, end)
        inside = [p for p in track.points if start <= p.t < stop]
        clicks = sum(1 for p in inside if p.kind is FocusKind.CLICK)
        dwell = sum(1 for p in inside if p.kind is FocusKind.DWELL)
        if clicks or dwell:
            share = dwell / len(inside) if inside else 0.0
            lines.append(f"[{start:.1f}-{stop:.1f}] {clicks} clicks, {share:.0%} dwell")
        start = stop
    return "cursor activity by 2s bucket:\n" + ("\n".join(lines) or "  (no clicks or dwell)")


def focus_regions(track: FocusTrack, *, bucket_s: float = 2.0) -> str:
    """The same buckets, plus *where on screen* the activity was.

    `plan_overlays` needs the position and `plan_edit` does not, which is the
    whole difference between the two summaries: an editorial decision is about
    when, and an overlay is about when and where. Coordinates are normalized
    source space (§4.1) because that is the space an anchor is in — handing a
    model pixels here would be the one place in this codebase they appeared.
    """
    if not track.points:
        return "cursor: no track"
    end = track.points[-1].t
    lines: list[str] = []
    start = 0.0
    while start < end - TIME_EPS:
        stop = min(start + bucket_s, end)
        inside = [p for p in track.points if start <= p.t < stop]
        notable = [p for p in inside if p.kind in (FocusKind.CLICK, FocusKind.DWELL)]
        if notable:
            x = sum(p.x for p in notable) / len(notable)
            y = sum(p.y for p in notable) / len(notable)
            clicks = sum(1 for p in notable if p.kind is FocusKind.CLICK)
            lines.append(
                f"[{start:.1f}-{stop:.1f}] at ({x:.2f}, {y:.2f}), "
                f"{clicks} clicks, {len(notable) - clicks} dwell"
            )
        start = stop
    return "cursor attention by 2s bucket, normalized source coordinates:\n" + (
        "\n".join(lines) or "  (no clicks or dwell)"
    )


def transcript_lines(words: list) -> str:
    """The transcript with per-word timings, which is what every timed decision
    is made against — a boundary that lands in a gap rather than through a word
    is only checkable if the model can see where the gaps are."""
    return " ".join(f"[{w.t_in:.2f}-{w.t_out:.2f}] {w.text}" for w in words) or "  (no speech)"


def word_budget(duration: float) -> int:
    """How many words fit in a recording of this length, at `WORDS_PER_SECOND`.

    Arithmetic, handed to the model as a constraint rather than checked after the
    fact: a script too long for its screen recording cannot be trimmed by code
    without mangling language, and `align` already refuses the job when the
    narration overruns the source (`runner/stages.py`). Better to say the number
    up front than to fail two stages later with it.
    """
    return max(int(duration * WORDS_PER_SECOND), 1)
