"""The time projection (architecture.md §4.5).

`EditDecisions` is never applied to the spec. Captions, overlays and the
`FocusTrack` all stay in source time, and *this* is where the removal-and-selection
happens: remapping timings, splitting caption blocks at boundaries, and dropping
overlays anchored inside removed ranges.

The payoff is the one §8 depends on. Adjusting a cut in review re-runs `compile`
and `render` and nothing else — no planner, and in particular no model call. If
cuts were baked into the spec, moving one boundary would re-plan captions and
overlays, and `plan_overlays` is a model call. That would put a model call behind
every cut correction, which is precisely the failure that kills a review loop.
"""

from __future__ import annotations

from typing import Iterable

from pydantic import BaseModel, ConfigDict, Field

from spec.captions import CaptionBlock, Word
from spec.edit import Tier, choose_threshold
from spec.editspec import EditSpec
from spec.overlays import OverlayIntent
from spec.profiles import RenderProfile
from spec.types import TIME_EPS


MIN_OVERLAY_PIECE_S = 0.15
"""Shortest overlay fragment worth compositing.

A cut through an overlay's range splits it, and the piece on the far side can be a
frame or two long. Rendering that is a flash — worse than the overlay being absent,
and invisible in every check that looks at a still frame. Same reasoning as a
caption's minimum display duration, and the opposite remedy: a caption gets held,
an overlay gets dropped, because holding one would put it over footage it does not
belong to."""


class TimelineModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class KeptSpan(TimelineModel):
    """One surviving stretch, in both time bases at once.

    Holding both is what makes every other mapping in this module a subtraction
    rather than a search.
    """

    source_in: float
    source_out: float
    output_in: float

    @property
    def duration(self) -> float:
        return self.source_out - self.source_in

    @property
    def output_out(self) -> float:
        return self.output_in + self.duration

    def to_output(self, t: float) -> float:
        return self.output_in + (t - self.source_in)

    def to_source(self, t: float) -> float:
        return self.source_in + (t - self.output_in)

    def holds_source(self, t: float) -> bool:
        return self.source_in - TIME_EPS <= t <= self.source_out + TIME_EPS


class EditedWord(TimelineModel):
    t_in: float
    t_out: float
    text: str
    emphasis: bool = False


class EditedCaption(TimelineModel):
    """A caption block in output time, carrying only the words that survived."""

    t_in: float
    t_out: float
    words: list[EditedWord]

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)


class EditedOverlay(TimelineModel):
    """An overlay in output time. `anchor` stays in normalized *source* space —
    the compiler projects it per frame through whatever the focus plan is doing,
    because the point it labels moves in the output frame when the crop moves."""

    template: str
    text: str
    anchor: tuple[float, float] | None
    t_in: float
    t_out: float
    spans_whole_output: bool = False


class EditedTimeline(TimelineModel):
    """Everything the filter graph needs, with source time already projected out."""

    profile: str
    threshold: Tier
    duration: float
    spans: list[KeptSpan]
    captions: list[EditedCaption] = Field(default_factory=list)
    overlays: list[EditedOverlay] = Field(default_factory=list)
    dropped_overlays: int = 0
    budget_overrun: float = Field(
        default=0.0,
        description=(
            "Seconds this profile runs over its duration_budget. Nonzero only when "
            "`essential` alone does not fit, which §4.4.1 calls the expected failure and "
            "§9.1 reports with a number rather than rendering long in silence."
        ),
    )

    def source_at(self, output_t: float) -> float:
        """Source time for an output time. The graph asks this once per frame."""
        for span in self.spans:
            if span.output_in - TIME_EPS <= output_t < span.output_out - TIME_EPS:
                return span.to_source(output_t)
        return self.spans[-1].source_out if self.spans else 0.0

    def output_at(self, source_t: float) -> float | None:
        """Output time for a source time, or None if it did not survive."""
        for span in self.spans:
            if span.holds_source(source_t):
                return span.to_output(source_t)
        return None

    @property
    def cuts(self) -> list[float]:
        """Output times where the source jumps. Every one is a visible cut."""
        return [span.output_in for span in self.spans[1:]]


def project(spec: EditSpec, profile: RenderProfile) -> EditedTimeline:
    """Apply `EditDecisions` for one profile. The spec is not modified."""
    threshold, selected = _selection(spec, profile)
    spans = _spans(spec, threshold)
    duration = sum(span.duration for span in spans)
    timeline = EditedTimeline(
        profile=profile.name,
        threshold=threshold,
        duration=duration,
        spans=spans,
        budget_overrun=max(0.0, selected - profile.duration_budget),
    )
    timeline.captions = _captions(spec.captions, spans, profile)
    timeline.overlays, timeline.dropped_overlays = _overlays(spec.overlays, spans, duration)
    return timeline


def _selection(spec: EditSpec, profile: RenderProfile) -> tuple[Tier, float]:
    if not spec.edit.segments:
        # Nothing has been decided yet — the whole take survives. Phase 4 renders
        # here, before `plan_edit` exists, and it must not render an empty video.
        return Tier.OPTIONAL, spec.source.duration
    return choose_threshold(spec.edit, profile.duration_budget)


def _spans(spec: EditSpec, threshold: Tier) -> list[KeptSpan]:
    """Selected segments, merged where they touch, with output offsets accumulated.

    Merging matters: two adjacent segments of different tiers that both survive are
    one continuous stretch of footage, and emitting them as two spans would put a
    concat boundary — and its frame-accurate seam — where there is no cut.
    """
    if not spec.edit.segments:
        ranges: list[tuple[float, float]] = [(0.0, spec.source.duration)]
    else:
        ranges = [(s.t_in, s.t_out) for s in spec.edit.selected(threshold)]

    merged: list[list[float]] = []
    for start, end in ranges:
        if merged and start <= merged[-1][1] + TIME_EPS:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])

    spans: list[KeptSpan] = []
    offset = 0.0
    for start, end in merged:
        spans.append(KeptSpan(source_in=start, source_out=end, output_in=offset))
        offset += end - start
    return spans


def _captions(
    blocks: Iterable[CaptionBlock], spans: list[KeptSpan], profile: RenderProfile
) -> list[EditedCaption]:
    """Split blocks at span boundaries, keeping whole words.

    §6.2 carries per-word timings for kinetic captions later; they are what makes
    this exact now. A block straddling a cut becomes two blocks, each holding the
    words whose audio actually survived — so no caption shows a word the viewer
    never hears, and no word is shown half-spoken.
    """
    edited: list[EditedCaption] = []
    for span in spans:
        for block in blocks:
            if block.t_out <= span.source_in + TIME_EPS or block.t_in >= span.source_out - TIME_EPS:
                continue
            words = block.words_in(span.source_in, span.source_out)
            if not words:
                continue
            edited.append(
                EditedCaption(
                    t_in=span.to_output(max(block.t_in, span.source_in, words[0].t_in)),
                    t_out=span.to_output(min(block.t_out, span.source_out, words[-1].t_out)),
                    words=[_edited_word(w, span) for w in words],
                )
            )
    edited.sort(key=lambda c: c.t_in)
    return _hold_minimum(edited, profile.captions.min_display_s, spans)


def _edited_word(word: Word, span: KeptSpan) -> EditedWord:
    return EditedWord(
        t_in=span.to_output(max(word.t_in, span.source_in)),
        t_out=span.to_output(min(word.t_out, span.source_out)),
        text=word.text,
        emphasis=word.emphasis,
    )


def _hold_minimum(
    captions: list[EditedCaption], minimum: float, spans: list[KeptSpan]
) -> list[EditedCaption]:
    """Extend a block that a cut left too short to read, up to the next one.

    §9.1 checks minimum display duration, and a trimmed block is exactly where that
    check would otherwise start firing on correct behaviour.
    """
    end = spans[-1].output_out if spans else 0.0
    for index, caption in enumerate(captions):
        if caption.t_out - caption.t_in >= minimum:
            continue
        ceiling = captions[index + 1].t_in if index + 1 < len(captions) else end
        caption.t_out = min(max(caption.t_out, caption.t_in + minimum), ceiling)
    return captions


def _overlays(
    intents: Iterable[OverlayIntent], spans: list[KeptSpan], duration: float
) -> tuple[list[EditedOverlay], int]:
    """Project overlays, dropping the ones that landed in removed material.

    `plan_overlays` runs against the full source timeline and may spend an overlay
    on footage that is later cut (§4.5). Dropping it here is deterministic, and a
    wasted overlay is worth a stage dependency removed.
    """
    edited: list[EditedOverlay] = []
    dropped = 0
    for intent in intents:
        if intent.spans_whole_output:
            edited.append(
                EditedOverlay(
                    template=intent.template.value,
                    text=intent.text,
                    anchor=None,
                    t_in=0.0,
                    t_out=duration,
                    spans_whole_output=True,
                )
            )
            continue
        pieces = 0
        for span in spans:
            start = max(intent.t_in, span.source_in)
            end = min(intent.t_out, span.source_out)
            if end - start < MIN_OVERLAY_PIECE_S:
                continue
            pieces += 1
            edited.append(
                EditedOverlay(
                    template=intent.template.value,
                    text=intent.text,
                    anchor=(intent.anchor.x, intent.anchor.y) if intent.anchor else None,
                    t_in=span.to_output(start),
                    t_out=span.to_output(end),
                )
            )
        if not pieces:
            dropped += 1
    edited.sort(key=lambda o: (o.t_in, o.template))
    return edited, dropped
