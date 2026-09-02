"""Human corrections, as a layer over the planners' spec (architecture.md §8).

Review edits fields, and the planners rewrite those same fields on every run. The
two have to coexist, because §5.2 caches the planners: `plan_edit`'s fingerprint
reads the focus track and the source duration, so re-tiering a segment does not
invalidate it, and the next run applies the *cached* fragment straight back over
the correction. The reviewer's decision would survive exactly until the next
render — which is the failure §8 says kills the loop, arriving as silence rather
than as an error.

So a correction is not an edit of `spec.json`. It is a sparse layer beside it,
`corrections.json`, applied after the job-level stages have folded their artifacts
in. The same discipline as `constraints.yaml` over the built-in profiles (§4.1)
and as `EditDecisions` over the timeline (§4.5): state the difference, apply it
last, and keep the thing it differs from intact so the two can be diffed. That
diff is what phase 10 learns from, and it only exists because the proposal was not
overwritten.

**Corrections address content, not indices.** A removal is named by its span and a
segment by where it starts, because an index into a list the model rewrote means
something else afterwards, and silently means it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

from pydantic import Field, model_validator

from spec.edit import EditDecisions, Removal, Segment, Tier
from spec.editspec import EditSpec
from spec.origin import Stage, spec_field
from spec.profiles import RenderProfile
from spec.types import PositiveSeconds, Seconds, SpecModel, TimeSpan, approx_eq

CORRECTIONS_NAME = "corrections.json"
PROPOSED_NAME = "proposed.json"
"""The spec as the planners left it, written beside `spec.json` only while a
correction exists. Without corrections `spec.json` *is* the proposal, and writing
a second copy of it would be a second source of truth that nothing updates."""

REINSTATED_REASON = "reinstated in review"
"""The `reason` on a segment a reviewer put back.

`Segment.reason` is declared `produced_by=plan_edit` because origin metadata is a
property of the field, not of the value (§11.1) — this string is how a reader, and
§10's learner, tells the two apart."""


class StaleCorrection(ValueError):
    """A correction addressing something the current plan does not contain.

    Raised rather than skipped. A correction is a person's decision, and dropping
    one quietly is the same class of failure as overwriting it: the loop appears
    to work and the video does not change.
    """


class ReinstatedRemoval(SpecModel, TimeSpan):
    """A removal the reviewer put back — addressed by the span it cut."""

    t_in: Seconds = spec_field(produced_by=Stage.HUMAN)
    t_out: Seconds = spec_field(produced_by=Stage.HUMAN)


class RetieredSegment(SpecModel):
    """A segment the reviewer re-ranked — addressed by where it starts.

    Its start, not its whole span: a tier is an argument about a passage, and the
    passage is identified by where it begins (§4.4).
    """

    t_in: Seconds = spec_field(produced_by=Stage.HUMAN)
    tier: Tier = spec_field(produced_by=Stage.HUMAN)


class Corrections(SpecModel):
    """What a person changed about one job. Sparse: absent means untouched."""

    reinstated: list[ReinstatedRemoval] = spec_field(
        default_factory=list,
        produced_by=Stage.HUMAN,
        description="Removals to put back. The span returns as a segment; nothing else moves.",
    )
    retiered: list[RetieredSegment] = spec_field(
        default_factory=list,
        produced_by=Stage.HUMAN,
        description="Segments to re-rank (§4.4.1). Which tiers then survive stays arithmetic.",
    )
    budgets: dict[str, PositiveSeconds] = spec_field(
        default_factory=dict,
        produced_by=Stage.HUMAN,
        description=(
            "Per-profile `duration_budget` overrides. 'Make the short shorter' is one "
            "field rather than a dozen re-tierings, and it is §10's budget signal."
        ),
    )

    @model_validator(mode="after")
    def _addresses_each_thing_once(self) -> "Corrections":
        for label, starts in (
            ("removal", [r.t_in for r in self.reinstated]),
            ("segment", [s.t_in for s in self.retiered]),
        ):
            for index, start in enumerate(starts):
                if any(approx_eq(start, other) for other in starts[index + 1 :]):
                    raise ValueError(f"two corrections address the {label} at {start}s")
        return self

    @property
    def empty(self) -> bool:
        return not (self.reinstated or self.retiered or self.budgets)

    # --- on disk -------------------------------------------------------------

    @classmethod
    def load(cls, job_dir: Path | str) -> "Corrections":
        path = Path(job_dir) / CORRECTIONS_NAME
        if not path.is_file():
            return cls()
        return cls.model_validate_json(path.read_text())

    def write(self, job_dir: Path | str) -> Path:
        path = Path(job_dir) / CORRECTIONS_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.model_dump(mode="json"), indent=2) + "\n")
        return path

    # --- applying ------------------------------------------------------------

    def apply_to(self, spec: EditSpec) -> EditSpec:
        """The proposed spec with this layer on top.

        Round-tripped through validation rather than assigned, so a correction that
        breaks §4.4's totality fails here, beside the correction that broke it.
        """
        if not (self.reinstated or self.retiered):
            return spec
        decisions = self._apply_to_edit(spec.edit)
        return EditSpec.model_validate(
            {**spec.model_dump(mode="json"), "edit": decisions.model_dump(mode="json")}
        )

    def apply_to_profile(self, profile: RenderProfile) -> RenderProfile:
        budget = self.budgets.get(profile.name)
        if budget is None or approx_eq(budget, profile.duration_budget):
            return profile
        return profile.model_copy(update={"duration_budget": budget})

    def _apply_to_edit(self, decisions: EditDecisions) -> EditDecisions:
        kept: list[Removal] = []
        returned: list[ReinstatedRemoval] = []
        for removal in decisions.removals:
            match = next(
                (
                    span
                    for span in self.reinstated
                    if approx_eq(span.t_in, removal.t_in) and approx_eq(span.t_out, removal.t_out)
                ),
                None,
            )
            if match is None:
                kept.append(removal)
            else:
                returned.append(match)

        if len(returned) != len(self.reinstated):
            raise StaleCorrection(
                f"{len(self.reinstated) - len(returned)} of {len(self.reinstated)} reinstated "
                "removals are not in the current plan — it was re-planned under them. "
                f"Re-correct the job, or delete {CORRECTIONS_NAME}."
            )

        segments = list(decisions.segments)
        segments.extend(_segment_for(span, decisions.segments) for span in returned)
        segments = [self._retier(segment) for segment in segments]

        addressed = {round(s.t_in, 6) for s in self.retiered}
        found = {round(s.t_in, 6) for s in segments}
        if not addressed <= found:
            raise StaleCorrection(
                f"re-tiered segments at {sorted(addressed - found)} are not in the current plan — "
                f"it was re-planned under them. Re-correct the job, or delete {CORRECTIONS_NAME}."
            )

        return EditDecisions(
            removals=sorted(kept, key=lambda r: r.t_in),
            segments=sorted(segments, key=lambda s: s.t_in),
        )

    def _retier(self, segment: Segment) -> Segment:
        for correction in self.retiered:
            if approx_eq(correction.t_in, segment.t_in):
                return segment.model_copy(update={"tier": correction.tier})
        return segment


def _segment_for(span: ReinstatedRemoval, neighbours: list[Segment]) -> Segment:
    """The segment a reinstated removal comes back as.

    It takes the higher tier of whatever it touches, and `essential` when it
    touches nothing — never the lowest, because a reviewer who puts a passage back
    and then watches a tight budget drop it again has been told the correction
    worked when it did not. Its own segment rather than an extension of a
    neighbour's, so no argument about a passage is merged into another one.
    """
    touching = [
        segment
        for segment in neighbours
        if approx_eq(segment.t_out, span.t_in) or approx_eq(segment.t_in, span.t_out)
    ]
    tier = max((s.tier for s in touching), key=lambda t: t.rank, default=Tier.ESSENTIAL)
    return Segment(t_in=span.t_in, t_out=span.t_out, tier=tier, reason=REINSTATED_REASON)


# --- the record of what changed ----------------------------------------------


class SpecChange(SpecModel):
    """One proposed -> corrected difference, addressed the way a person reads it."""

    path: Annotated[str, Field(min_length=1)] = spec_field(
        produced_by=Stage.HUMAN,
        description="Dotted path with the span or start time that identifies the element.",
    )
    before: float | str | None = spec_field(produced_by=Stage.HUMAN)
    after: float | str | None = spec_field(
        produced_by=Stage.HUMAN, description="`null` where the correction removed the element."
    )


class CorrectionDiff(SpecModel):
    """The proposed -> corrected diff, which is the phase-10 signal (§10).

    Derived by comparing the two documents rather than by reading the corrections
    that produced them, so a correction that changed nothing does not appear as a
    change — and so the record cannot claim an edit the render did not get.
    """

    changes: list[SpecChange] = spec_field(default_factory=list, produced_by=Stage.HUMAN)

    @property
    def empty(self) -> bool:
        return not self.changes


def diff_specs(
    proposed: EditSpec,
    corrected: EditSpec,
    proposed_profiles: list[RenderProfile] | None = None,
    corrected_profiles: list[RenderProfile] | None = None,
) -> CorrectionDiff:
    """What review changed, in the terms §10 learns from."""
    changes: list[SpecChange] = []

    corrected_removals = corrected.edit.removals
    for removal in proposed.edit.removals:
        if not any(
            approx_eq(removal.t_in, other.t_in) and approx_eq(removal.t_out, other.t_out)
            for other in corrected_removals
        ):
            # The segment it came back as is implied by this line, not a second
            # change: one decision, one row, or the learner counts it twice.
            changes.append(
                SpecChange(
                    path=f"edit.removals[{removal.t_in:.3f}-{removal.t_out:.3f}]",
                    before=removal.kind.value,
                    after=None,
                )
            )

    by_start = {round(s.t_in, 6): s for s in corrected.edit.segments}
    for segment in proposed.edit.segments:
        other = by_start.get(round(segment.t_in, 6))
        if other is not None and other.tier is not segment.tier:
            changes.append(
                SpecChange(
                    path=f"edit.segments[{segment.t_in:.3f}].tier",
                    before=segment.tier.value,
                    after=other.tier.value,
                )
            )

    after_by_name = {p.name: p for p in corrected_profiles or []}
    for profile in proposed_profiles or []:
        other = after_by_name.get(profile.name)
        if other is not None and not approx_eq(profile.duration_budget, other.duration_budget):
            changes.append(
                SpecChange(
                    path=f"profiles.{profile.name}.duration_budget",
                    before=profile.duration_budget,
                    after=other.duration_budget,
                )
            )

    return CorrectionDiff(changes=changes)
