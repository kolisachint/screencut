"""`LocalRunner` — a stage as a subprocess (architecture.md §5.1).

The only `Runner` that gets built. `RemoteRunner` would ship the same
`StageRequest` to a GPU worker and retrieve the artifact; pipeline code would be
identical under both, which is the whole reason the contract is this narrow.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from runner.contract import StageFailed, StageRequest, StageResult

DEFAULT_TIMEOUT_S = 3600


class LocalRunner:
    """Runs `python -m runner.stages <name>` and reads the result off stdout."""

    def __init__(self, *, timeout_s: float = DEFAULT_TIMEOUT_S, repo_root: Path | None = None):
        self.timeout_s = timeout_s
        self.repo_root = repo_root or Path(__file__).resolve().parent.parent
        self._weights_in_flight: str | None = None

    def run(self, request: StageRequest, *, holds_local_weights: bool = False) -> StageResult:
        if holds_local_weights:
            self._claim_weights(request.stage)
        try:
            completed = subprocess.run(
                [sys.executable, "-m", "runner.stages", request.stage],
                input=request.model_dump_json(),
                capture_output=True,
                text=True,
                cwd=self.repo_root,
                timeout=self.timeout_s,
            )
        except subprocess.TimeoutExpired as expired:
            raise StageFailed(f"{request.stage} timed out after {self.timeout_s}s") from expired
        finally:
            if holds_local_weights:
                self._weights_in_flight = None

        if completed.returncode != 0:
            raise StageFailed(
                f"{request.stage} exited {completed.returncode}\n{completed.stderr.strip()}"
            )
        try:
            return StageResult.model_validate_json(completed.stdout)
        except ValueError as invalid:
            raise StageFailed(
                f"{request.stage} produced unparseable stdout: {completed.stdout[:400]!r}"
            ) from invalid

    def _claim_weights(self, stage: str) -> None:
        """One model resident at a time (§16).

        The pipeline is sequential today, so this never fires. It exists so that the
        first scheduler to run stages in parallel finds out here rather than by
        swapping — which on this machine looks like slowness, not like a bug.
        """
        if self._weights_in_flight is not None:
            raise StageFailed(
                f"cannot start {stage} while {self._weights_in_flight} holds model weights; "
                f"8GB will not hold two (§16)"
            )
        self._weights_in_flight = stage
