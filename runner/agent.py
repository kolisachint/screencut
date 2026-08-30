"""Running a coding agent as a pipeline stage (architecture.md §7.2, §7.3).

One adapter, reused by every model stage there will ever be — `plan_edit` here,
then emphasis, `plan_overlays`, `script_draft` and the metadata sidecar. That
reuse is what makes decision #13 cheap: there is no provider SDK in this
repository, no key handling and no bespoke inference layer, because an LLM stage
is a subprocess that happens to be an agent, sitting beside the subprocesses that
happen to be FFmpeg and Whisper.

The invocation is §7.3's, and it is the one phase 0 ran twelve times against a
throwaway schema (`tools/phase0/bench_agent.py`, environment findings §7):

    hoocode -p --mode json --no-tools --no-session --model <model> <prompt>

with cwd pinned to the job directory. Every flag is a restriction. A coding agent
is built to explore and modify a repository; as a stage it must do neither, and
the invocation has to *enforce* that rather than request it.

**Three things phase 0 measured that this is written against.**

Twelve of twelve fragments validated, but **eleven of twelve arrived inside a
```json fence**. Stripping it is not optional, and it is not a schema violation —
counting it as one would make risk R5 look worse than it is and send the
mitigation after the wrong thing.

The latency floor is 5.7 s, typical is 12–36 s, and the worst observed was
**65.8 s on identical input**. Timeouts are set against the maximum rather than
the median, because a stage killed at the median fails roughly as often as the
model is slow, which is not a property anybody wants to debug.

And the schema is a strong instruction rather than a decoding constraint (§7.2),
so the control flow is three steps and not a subsystem: **validate, retry once
with the validation error appended, then degrade** (§7.4).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

BINARY = "hoocode"
DEFAULT_MODEL = "anthropic/claude-sonnet-5"
"""Sonnet was both faster and no less accurate than Haiku over phase 0's twelve
trials. On twelve trials that is queueing variance rather than a property of the
models, so this is a default and not a finding."""

DEFAULT_TIMEOUT_S = 300.0
"""Against phase 0's 65.8 s worst case with room to spare, not against its median."""

PROMPT_VERSION = 1
"""Bumped whenever a stage's prompt text changes.

This is half of §5.2's one cache subtlety that will not announce itself: the same
transcript under a revised prompt is a different artifact, and a key without this
serves the old answer after exactly the change you were trying to evaluate. It
looks like the prompt edit had no effect."""

_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)

Fragment = TypeVar("Fragment", bound=BaseModel)


class AgentUnavailable(RuntimeError):
    """The agent CLI is not on this machine.

    Its own type because §7.4 wants "could not run" and "ran and produced
    nonsense" to land in the same place by different routes — the caller
    degrades either way, and only one of them is worth telling a human about."""


class FragmentRejected(RuntimeError):
    """Two attempts, still no valid fragment. The caller degrades (§7.4)."""


@dataclass
class AgentAttempt:
    """What one round trip did. Kept for the job record, not for control flow."""

    exit_code: int
    fenced: bool
    error: str = ""


@dataclass
class AgentOutcome:
    fragment: BaseModel | None
    attempts: list[AgentAttempt] = field(default_factory=list)

    @property
    def degraded(self) -> bool:
        return self.fragment is None

    @property
    def note(self) -> str:
        if self.fragment is not None:
            retried = " after one retry" if len(self.attempts) > 1 else ""
            return f"fragment accepted{retried}"
        reasons = "; ".join(a.error for a in self.attempts if a.error) or "no attempts"
        return f"degraded: {reasons}"


def available() -> bool:
    return shutil.which(BINARY) is not None


def command(prompt: str, *, model: str) -> list[str]:
    """§7.3's invocation. Every flag here is a restriction, so none is optional.

    `--no-tools` is the important one: a stage that plans an edit has no business
    holding `write`, `edit` or `bash`. `--no-session` keeps one job's fragment
    from conditioning the next, which would make a cached stage and a fresh one
    disagree for reasons no key could capture.
    """
    return [BINARY, "-p", "--mode", "json", "--no-tools", "--no-session", "--model", model, prompt]


def extract_fragment(stdout: str) -> tuple[str | None, bool]:
    """The final assistant text out of the JSON event stream.

    Returns `(text, fenced)`. Lifted from the probe in
    `tools/phase0/bench_agent.py`, which is what that probe was for: phase 0
    establishes what the adapter has to do, and phase 5 is where it becomes
    pipeline code.
    """
    text: str | None = None
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") != "message_end":
            continue
        message = event.get("message") or {}
        if message.get("role") != "assistant":
            continue
        joined = "".join(
            block.get("text", "")
            for block in message.get("content") or []
            if block.get("type") == "text"
        ).strip()
        if joined:
            text = joined
    if text is None:
        return None, False

    fenced = _FENCE.search(text)
    if fenced:
        return fenced.group(1).strip(), True
    # Some replies wrap the object in a sentence; take the outermost braces.
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        return text[start : end + 1], False
    return text, False


def build_prompt(instruction: str, model: type[BaseModel], content: str) -> str:
    """§7.2's shape: system, the fragment's own JSON Schema, then the job content.

    The schema comes from the same Pydantic model that will validate the reply, so
    the thing asked for and the thing checked cannot drift — one definition doing
    two of its four jobs at once.
    """
    return (
        f"{instruction}\n\n"
        "Return exactly one JSON object conforming to the JSON Schema below. "
        "Return no prose, no explanation and no markdown fence.\n\n"
        f"JSON Schema:\n{json.dumps(model.model_json_schema(), indent=2)}\n\n"
        f"{content}\n"
    )


def run_stage(
    prompt: str,
    fragment_model: type[Fragment],
    *,
    job_dir: Path | str,
    model: str = DEFAULT_MODEL,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> AgentOutcome:
    """Validate, retry once with the error appended, then give up (§7.2).

    Giving up is not an exception path here — it returns an outcome with no
    fragment, because §7.4 says an LLM stage failure must not fail the job, and a
    raise would make every caller write the same try block.

    Failure means any of: nonzero exit, no assistant text, unparseable JSON,
    schema validation failing twice, or a timeout. Collapsing them into one branch
    is deliberate: across a subprocess boundary there is no typed exception
    hierarchy to discriminate, and every one has the same correct response.
    """
    if not available():
        return AgentOutcome(
            fragment=None,
            attempts=[AgentAttempt(exit_code=-1, fenced=False, error=f"{BINARY} is not on PATH")],
        )

    outcome = AgentOutcome(fragment=None)
    current = prompt
    for attempt in range(2):
        try:
            completed = subprocess.run(
                command(current, model=model),
                capture_output=True, text=True, cwd=str(job_dir), timeout=timeout_s,
            )
        except subprocess.TimeoutExpired:
            outcome.attempts.append(
                AgentAttempt(exit_code=-1, fenced=False, error=f"timed out after {timeout_s:g}s")
            )
            return outcome

        text, fenced = extract_fragment(completed.stdout)
        if completed.returncode != 0:
            outcome.attempts.append(AgentAttempt(completed.returncode, fenced,
                                                 f"exited {completed.returncode}"))
            return outcome  # a nonzero exit is not something a retry fixes
        if text is None:
            outcome.attempts.append(AgentAttempt(0, fenced, "no assistant text in the event stream"))
            return outcome

        try:
            outcome.fragment = fragment_model.model_validate_json(text)
            outcome.attempts.append(AgentAttempt(0, fenced))
            return outcome
        except ValidationError as invalid:
            outcome.attempts.append(AgentAttempt(0, fenced, _summarize(invalid)))
            if attempt == 0:
                current = (
                    f"{prompt}\n\nYour previous reply was rejected by the schema:\n"
                    f"{_summarize(invalid)}\n\nReturn a corrected JSON object."
                )
    return outcome


def _summarize(invalid: ValidationError) -> str:
    """Enough of the error for the model to fix it, not the whole traceback.

    A full Pydantic error over a long removal list is thousands of tokens of
    repetition, and the retry prompt is the one place where paying for that buys
    nothing: the model needs the shape of the mistake, not every instance."""
    lines = []
    for error in invalid.errors()[:5]:
        where = ".".join(str(part) for part in error["loc"]) or "(root)"
        lines.append(f"{where}: {error['msg']}")
    more = invalid.error_count() - len(lines)
    if more > 0:
        lines.append(f"... and {more} more")
    return "; ".join(lines)


def cache_params(model: str = DEFAULT_MODEL, prompt_version: int = PROMPT_VERSION) -> dict[str, Any]:
    """What `runner.cache` refuses to key a model stage without (§5.2)."""
    return {"model": model, "prompt_version": prompt_version}
