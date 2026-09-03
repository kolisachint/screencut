"""The metadata sidecar — copy for the post (architecture.md §1.1, §5.4).

The pipeline ends at a file plus a sidecar; posting is manual (§1). So this is
the last thing written for a render, it sits beside the mp4 in `renders/`, and
nothing downstream reads it — which is exactly why it is safe for a model to
write: §1.1 puts written language in scope because it never reaches a frame.

**Two models, and the split is the point.** `MetadataCopy` is what the model
returns: three fields of language and nothing else. `Metadata` is the document
published beside the render, and every fact in it — which job, which profile, how
long the render actually is — is written by the pipeline from what it measured.
That is the same rule `Removal.proposed_by` follows (§7.2): a number *about* the
render is not the model's to report, because a sidecar claiming a duration the
file does not have is worse than no sidecar at all.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from spec.origin import Stage, spec_field
from spec.types import SpecModel

TITLE_MAX = 120
DESCRIPTION_MAX = 5000
TAG_MAX = 40
TAGS_MAX = 30
"""Generous ceilings rather than any one platform's. Decision #9 is that
publishing is a file on disk — there is no platform API here to be wrong about,
and a limit copied from one service would be a fact about that service silently
constraining every other."""


class MetadataCopy(SpecModel):
    """The §7.2 fragment. Small and bounded, so most wrong answers are invalid
    answers rather than plausible ones (risk R5)."""

    title: Annotated[str, Field(min_length=1, max_length=TITLE_MAX)] = spec_field(
        produced_by=Stage.METADATA
    )
    description: Annotated[str, Field(min_length=1, max_length=DESCRIPTION_MAX)] = spec_field(
        produced_by=Stage.METADATA
    )
    tags: list[Annotated[str, Field(min_length=1, max_length=TAG_MAX)]] = spec_field(
        default_factory=list,
        produced_by=Stage.METADATA,
        max_length=TAGS_MAX,
        description="Bare words, no leading '#'. Normalized by plan/metadata.py rather than trusted.",
    )


class Metadata(SpecModel):
    """One sidecar, for one render of one profile."""

    job_id: Annotated[str, Field(min_length=1)] = spec_field(produced_by=Stage.SYSTEM)
    profile: Annotated[str, Field(min_length=1)] = spec_field(produced_by=Stage.SYSTEM)
    render: Annotated[str, Field(min_length=1)] = spec_field(
        produced_by=Stage.SYSTEM,
        description="The file this describes, relative to the job directory.",
    )
    duration_s: Annotated[float, Field(ge=0.0)] = spec_field(
        produced_by=Stage.SYSTEM,
        description="The render's length as the compiler projected it — measured, never reported.",
    )
    post: MetadataCopy = spec_field(
        produced_by=Stage.METADATA,
        description="The copy itself. Named for the post rather than `copy`, which shadows a Pydantic method.",
    )
    degraded: bool = spec_field(
        default=False,
        produced_by=Stage.SYSTEM,
        description="True when the copy is §7.4's script-derived fallback rather than the model's.",
    )
