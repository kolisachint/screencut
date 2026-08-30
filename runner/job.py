"""What a job asks the pipeline to do, beyond rendering the spec it already has.

A fixture arrives with a complete `EditSpec` — hand-authored captions and all —
and running `transcribe` over it would replace known-good data with whatever ASR
made of a test tone. An ingested recording arrives with a spec that is missing
exactly the fields the job-level stages produce. The two cases are different, and
nothing in the spec distinguishes them: `captions: []` means "not planned yet" in
one job and "this take is silent" in the next.

So the job says. `job.json` is pipeline configuration for one job — which stages
it needs, and where its recording came from — and it sits beside `spec.json`
rather than inside it because it describes the *run*, not the edit. A job
directory without one is a job whose spec is complete as given, which is every
fixture in this repository.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

JOB_NAME = "job.json"


class JobConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stages: list[str] = Field(
        default_factory=list,
        description="Job-level stages to run before the per-profile ones. Order comes from JOB_ORDER.",
    )
    recorder: str | None = Field(default=None, description="Which adapter produced this job.")
    recording: str | None = Field(
        default=None,
        description="The bundle this was ingested from. Provenance only — it is never read again.",
    )

    @classmethod
    def load(cls, job_dir: Path | str) -> "JobConfig":
        path = Path(job_dir) / JOB_NAME
        if not path.is_file():
            return cls()
        return cls.model_validate_json(path.read_text())

    def write(self, job_dir: Path | str) -> Path:
        path = Path(job_dir) / JOB_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.model_dump(mode="json"), indent=2) + "\n")
        return path
