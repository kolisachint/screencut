"""The one source recording a job is built from (decision #24)."""

from __future__ import annotations

from enum import Enum
from typing import Annotated

from pydantic import Field, field_validator

from spec.origin import Stage, spec_field
from spec.types import PositiveSeconds, SpecModel


class Provenance(str, Enum):
    """Where the footage came from, which is what makes §10's gate answerable.

    §10.2 activates the learner after ten to fifteen accepted *real* jobs, and
    without this field nothing can tell one from a fixture. Both look identical by
    the time they reach `accepted_specs`: `ingest/cap_fixture.py` writes a bundle
    in Cap's own on-disk format and it is read by the same adapter a real
    recording is. So the distinction has to be recorded at ingest — it cannot be
    recovered later, and a corpus that counts fixtures would have the learner
    learn synthetic taste and report it as yours.
    """

    RECORDED = "recorded"
    """A take from a real recorder. The only kind §10 counts."""

    SYNTHETIC = "synthetic"
    """Generated: `ingest/fixtures.py`, `ingest/narrated_fixture.py`, or a bundle
    `ingest/cap_fixture.py` produced. Renders, verifies and replays like any other
    job — it is only barred from teaching preferences."""

    UNKNOWN = "unknown"
    """Ingested before spec v3, when nothing recorded this. Not counted, because
    the honest answer to "was this real" is that nobody wrote it down — and
    guessing here would be guessing on the learner's behalf."""


class Source(SpecModel):
    """A single raw take, plus the facts about it the compiler needs.

    One source per job, singular and deliberate. Multi-take assembly is a schema
    field *and* a compiler change (decision #24); §4.2's migration registry is
    what makes adding it later ordinary rather than alarming.

    `path` is relative to the job directory, never absolute: a job directory has
    to survive being moved or archived into `golden/`.
    """

    source_id: str = spec_field(produced_by=Stage.INGEST, description="Stable id of the take within the job.")
    provenance: Provenance = spec_field(
        default=Provenance.UNKNOWN,
        produced_by=Stage.INGEST,
        description="Whether this footage was recorded or generated (§10.2).",
    )
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
