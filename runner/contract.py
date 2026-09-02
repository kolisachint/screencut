"""The stage contract (architecture.md §5.1).

Each stage is a pure function `(inputs, params) -> artifact`, exposed as a CLI
taking JSON on stdin and paths as arguments. That is the whole interface, and it
is deliberately narrow: it is the seam that lets compute location stay undecided
(decision #2), and it is why running a coding agent as a pipeline stage costs
nothing extra (decision #13) — an LLM stage is a subprocess that happens to be an
agent, sitting alongside the subprocesses that happen to be FFmpeg and Whisper.
"""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field


class StageRequest(BaseModel):
    """What a stage is told. Serialized to JSON and handed to it on stdin.

    Every path is relative to `job_dir`, and the stage is run with `job_dir` as its
    working directory. A stage that cannot see outside its job directory cannot
    accidentally depend on anything that will not travel to a remote worker.
    """

    model_config = ConfigDict(extra="forbid")

    stage: str
    job_dir: str
    inputs: dict[str, str] = Field(
        default_factory=dict,
        description="Logical name -> path of an upstream artifact, relative to the job directory.",
    )
    params: dict[str, Any] = Field(default_factory=dict)
    output: str = Field(description="Where to write this stage's artifact, relative to the job directory.")


class StageResult(BaseModel):
    """What a stage reports. JSON on stdout, and nothing else on stdout."""

    model_config = ConfigDict(extra="forbid")

    stage: str
    output: str
    degraded: bool = Field(
        default=False,
        description=(
            "The stage produced its deterministic fallback rather than its real answer "
            "(§7.4). No stage degrades yet; the field exists because the job record has "
            "to carry it from the first model stage, and review is the only place a "
            "degraded job announces itself (decision #12)."
        ),
    )
    note: str | None = None


class StageFailed(RuntimeError):
    """Nonzero exit, unparseable stdout, or a timeout.

    Collapsed into one branch deliberately (§7.4): across a subprocess boundary
    there is no typed exception hierarchy to discriminate, and every one of these
    has the same correct response.
    """


class Runner(Protocol):
    """`LocalRunner` runs a stage here; `RemoteRunner` runs it on a worker.

    `holds_local_weights` is part of the signature rather than of the request
    because it is a fact about *this* machine's memory (§16), not about the stage's
    inputs: the local runner refuses to start a second stage holding weights, and
    the remote one has nothing to refuse — which is the whole reason phase 8 routes
    `tts` there."""

    def run(self, request: StageRequest, *, holds_local_weights: bool = False) -> StageResult: ...
