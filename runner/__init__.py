"""The runner: the stage contract, the cache, and the job record.

This package exists to defer decision #2. Every heavy stage is a CLI behind a
`Runner`, so `LocalRunner` shells out to a subprocess today and a future
`RemoteRunner` ships inputs to a GPU worker without the pipeline noticing. Only
`LocalRunner` gets built.

The cache is not an optimization here (principle 4, §16). The review loop is
iterative by design, and if a caption tweak re-synthesizes the voiceover the
corrections that feed §10 stop happening.
"""

from runner.contract import Runner, StageRequest, StageResult
from runner.local import LocalRunner
from runner.pipeline import JobRun, StageOutcome, run_job

__all__ = [
    "JobRun",
    "LocalRunner",
    "Runner",
    "StageOutcome",
    "StageRequest",
    "StageResult",
    "run_job",
]
