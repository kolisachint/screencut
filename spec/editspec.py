"""EditSpec — the aspect-agnostic document the whole system reads and writes.

Principle 1: the spec is the system. Planner, compiler, verifier, review UI and
learner all read and write this one versioned document; everything else is a
detail.

Two rules hold without exception (§4.1, §4.5):

- every spatial value is normalized in *source* coordinates — no pixels,
- every temporal value is seconds from *source* start — no second time base.

`RenderProfile` is what projects both into an actual render. It is not part of
this document: one `EditSpec` x N `RenderProfile` = N renders.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from pydantic import Field, model_validator

from spec.audio import AudioTrack
from spec.captions import CaptionBlock
from spec.edit import EditDecisions, Tier
from spec.focus import FocusTrack
from spec.narration import Narration
from spec.origin import Stage, spec_field
from spec.overlays import OverlayIntent
from spec.source import Source
from spec.types import TIME_EPS, SpecModel
from spec.version import CURRENT_SPEC_VERSION


def _now() -> datetime:
    return datetime.now(timezone.utc)


class EditSpec(SpecModel):
    spec_version: int = spec_field(
        default=CURRENT_SPEC_VERSION,
        produced_by=Stage.SYSTEM,
        description="Carried from the first commit (§4.2). Load through `spec.migrations.load_spec`, never bare.",
    )
    job_id: Annotated[str, Field(min_length=1)] = spec_field(produced_by=Stage.SYSTEM)
    created_at: datetime = spec_field(default_factory=_now, produced_by=Stage.SYSTEM)

    source: Source = spec_field(produced_by=Stage.INGEST)
    narration: Narration = spec_field(default_factory=Narration, produced_by=Stage.CONFIG)
    focus: FocusTrack = spec_field(default_factory=FocusTrack, produced_by=Stage.INGEST)
    edit: EditDecisions = spec_field(default_factory=EditDecisions, produced_by=Stage.PLAN_EDIT)
    captions: list[CaptionBlock] = spec_field(default_factory=list, produced_by=Stage.PLAN_CAPTIONS)
    overlays: list[OverlayIntent] = spec_field(default_factory=list, produced_by=Stage.PLAN_OVERLAYS)
    audio: AudioTrack = spec_field(default_factory=AudioTrack, produced_by=Stage.AUDIO)

    # --- invariants that need more than one field to state -------------------

    @model_validator(mode="after")
    def _within_source(self) -> "EditSpec":
        end = self.source.duration
        for point in self.focus.points:
            if point.t > end + TIME_EPS:
                raise ValueError(f"focus point at {point.t}s is past the source end ({end}s)")
        for name, spans in (("removal", self.edit.removals), ("segment", self.edit.segments)):
            for span in spans:
                if span.t_out > end + TIME_EPS:
                    raise ValueError(f"{name} [{span.t_in}, {span.t_out}] is past the source end ({end}s)")
        for block in self.captions:
            if block.t_out > end + TIME_EPS:
                raise ValueError(f"caption block [{block.t_in}, {block.t_out}] is past the source end ({end}s)")
        for overlay in self.overlays:
            if overlay.t_out is not None and overlay.t_out > end + TIME_EPS:
                raise ValueError(f"overlay [{overlay.t_in}, {overlay.t_out}] is past the source end ({end}s)")
        return self

    @model_validator(mode="after")
    def _edit_is_total(self) -> "EditSpec":
        """§4.4's totality: removals and segments partition the source exactly.

        `EditDecisions` already proved the partition is gapless, ordered and
        non-overlapping from 0.0; only the far end needs the source duration.
        An empty `EditDecisions` is the pre-`plan_edit` state and is allowed.
        """
        decisions = self.edit
        if not decisions.removals and not decisions.segments:
            return self
        if not decisions.covers(self.source.duration):
            raise ValueError(
                f"edit decisions cover the source only to {decisions.covered_until}s "
                f"of {self.source.duration}s; every second must be removed or in a segment (§4.4)"
            )
        return self

    @model_validator(mode="after")
    def _captions_do_not_overlap(self) -> "EditSpec":
        ordered = sorted(self.captions, key=lambda b: b.t_in)
        for prev, nxt in zip(ordered, ordered[1:]):
            if prev.overlaps(nxt):
                raise ValueError(
                    f"caption blocks overlap: [{prev.t_in}, {prev.t_out}] and [{nxt.t_in}, {nxt.t_out}]"
                )
        return self

    # --- conveniences the compiler and verifier both want --------------------

    @property
    def transcript(self) -> str:
        """Everything said in the source, in order. The §9.2 diff starts here."""
        return " ".join(w.text for block in sorted(self.captions, key=lambda b: b.t_in) for w in block.words)

    def transcript_after_edit(self, threshold: Tier) -> str:
        """The *expected* transcript for a profile: source minus removals, minus
        segments below `threshold` (§9.2).

        Diffing rendered audio against the raw transcript would flag every
        successful edit as a failure, and a check that fires on correct behaviour
        gets ignored within a week.
        """
        if not self.edit.segments and not self.edit.removals:
            return self.transcript  # nothing has been decided yet; the whole take survives
        kept = self.edit.selected(threshold)
        words = []
        for block in sorted(self.captions, key=lambda b: b.t_in):
            for word in block.words:
                mid = (word.t_in + word.t_out) / 2
                if self.edit.is_removed(mid):
                    continue
                if any(seg.contains(mid) for seg in kept):
                    words.append(word.text)
        return " ".join(words)
