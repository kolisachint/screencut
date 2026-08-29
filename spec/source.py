"""The one source recording a job is built from (decision #24)."""

from __future__ import annotations

from typing import Annotated

from pydantic import Field, field_validator

from spec.origin import Stage, spec_field
from spec.types import PositiveSeconds, SpecModel


class Source(SpecModel):
    """A single raw take, plus the facts about it the compiler needs.

    One source per job, singular and deliberate. Multi-take assembly is a schema
    field *and* a compiler change (decision #24); §4.2's migration registry is
    what makes adding it later ordinary rather than alarming.

    `path` is relative to the job directory, never absolute: a job directory has
    to survive being moved or archived into `golden/`.
    """

    source_id: str = spec_field(produced_by=Stage.INGEST, description="Stable id of the take within the job.")
    path: str = spec_field(produced_by=Stage.INGEST, description="Media path, relative to the job directory.")
    events_path: str | None = spec_field(
        default=None,
        produced_by=Stage.INGEST,
        description="Recorder event sidecar, relative to the job directory. None when the recorder produced none.",
    )
    duration: PositiveSeconds = spec_field(produced_by=Stage.INGEST, description="Source duration in seconds.")
    width: Annotated[int, Field(gt=0)] = spec_field(produced_by=Stage.INGEST)
    height: Annotated[int, Field(gt=0)] = spec_field(produced_by=Stage.INGEST)
    fps: Annotated[float, Field(gt=0.0)] = spec_field(produced_by=Stage.INGEST)
    has_audio: bool = spec_field(default=True, produced_by=Stage.INGEST)

    @field_validator("path", "events_path")
    @classmethod
    def _relative(cls, v: str | None) -> str | None:
        if v is not None and v.startswith("/"):
            raise ValueError("source paths are relative to the job directory")
        return v
