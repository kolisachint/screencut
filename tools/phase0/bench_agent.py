"""Phase 0, risk R5: does the agent CLI round-trip a JSON Schema reliably?

The §7.3 invocation, run for real: print mode, JSON events, tools off, a fixed cwd
pointed at a throwaway job directory, and the whole schema in the prompt because
with tools off there is no second way to get it there.

What this is measuring is not "does it work once" but the two numbers phase 5
needs before it can be designed against: the **round-trip latency**, which sets
the floor under every model stage, and the **schema-violation rate**, which is
what decides whether §7.2's validate-retry-degrade is three lines of control flow
or a subsystem.

Deliberately not pipeline code. The extraction below is a probe that establishes
what the phase-5 adapter will have to do; the adapter itself is phase 5's.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from tools.measure import Run, host_facts, run_once, write_results

# --------------------------------------------------------------------------
# A throwaway fragment schema
# --------------------------------------------------------------------------


class Segment(BaseModel):
    """Shaped like the real thing without being it — §4.4's tiering, in miniature."""

    start: float = Field(ge=0)
    end: float = Field(ge=0)
    tier: Literal["essential", "supporting", "optional"]
    reason: str = Field(min_length=1, max_length=120)


class Removal(BaseModel):
    start: float = Field(ge=0)
    end: float = Field(ge=0)
    kind: Literal["silence", "filler", "false_start", "tangent"]


class ThrowawayPlan(BaseModel):
    """Enums, nested arrays, bounded strings and floats — the features a real
    fragment uses. A schema of three scalars would pass and prove nothing."""

    title: str = Field(min_length=1, max_length=60)
    removals: list[Removal]
    segments: list[Segment] = Field(min_length=1)


PROMPT = """\
You are a pipeline stage. Return exactly one JSON object conforming to the JSON \
Schema below. Return no prose and no explanation.

JSON Schema:
{schema}

Job content — a transcript with word timings from a 30-second screen recording:
{content}

Propose removals for the disfluencies and dead air, then tier what remains.
"""

CONTENT = """\
[0.00-1.90] (silence)
[1.90] um  [2.20] so  [2.55] today  [2.90] I  [3.05] want  [3.30] to  [3.50] show
[3.80] you  [4.00] the  [4.20] new  [4.55] dashboard
[5.10-7.40] (silence)
[7.40] uh  [7.75] sorry  [8.10] let  [8.30] me  [8.50] start  [8.90] over
[9.40] the  [9.60] new  [9.95] dashboard  [10.5] loads  [11.0] in  [11.3] under
[11.7] a  [11.9] second
[12.4] and  [12.7] you  [12.9] can  [13.2] filter  [13.7] by  [14.0] any  [14.4] column
[15.0-18.2] (silence)
[18.2] which  [18.6] is  [18.9] the  [19.1] part  [19.5] people  [20.0] ask  [20.4] about
[21.0] most
[21.6-30.0] (silence)
"""

#: The model returned its object inside a ```json fence on the very first manual
#: try (see docs/environment-findings.md). Stripping it is not optional, and it
#: is not a schema violation either — counting it as one would make R5 look worse
#: than it is and would send phase 5 chasing the wrong mitigation.
_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def extract_fragment(stdout: str) -> tuple[str | None, bool]:
    """Pull the final assistant text out of the JSON event stream.

    Returns `(text, fenced)`. `fenced` records whether a code fence had to be
    stripped, because that is a distinct failure class from invalid JSON and the
    two want different mitigations.
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
        parts = [
            block.get("text", "")
            for block in message.get("content") or []
            if block.get("type") == "text"
        ]
        joined = "".join(parts).strip()
        if joined:
            text = joined
    if text is None:
        return None, False

    match = _FENCE.search(text)
    if match:
        return match.group(1).strip(), True
    # Some replies wrap the object in a sentence; take the outermost braces.
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        return text[start : end + 1], False
    return text, False


# --------------------------------------------------------------------------
# One trial
# --------------------------------------------------------------------------


def trial(index: int, model: str, job_dir: str) -> tuple[Run, dict[str, Any]]:
    schema = json.dumps(ThrowawayPlan.model_json_schema(), indent=2)
    prompt = PROMPT.format(schema=schema, content=CONTENT)

    run = run_once(
        f"{model}#{index}",
        [
            "hoocode",
            "-p",
            "--mode",
            "json",
            "--no-tools",  # §7.3: a planning stage holds no write, edit or bash
            "--no-session",
            "--model",
            model,
            prompt,
        ],
        cwd=job_dir,  # §7.3: fixed cwd, the job directory and nothing above it
    )

    fragment, fenced = extract_fragment(run.stdout_full)
    verdict: dict[str, Any] = {
        "trial": index,
        "model": model,
        "exit_code": run.exit_code,
        "wall_s": run.wall_s,
        "fenced": fenced,
        "parsed": False,
        "valid": False,
        "error": "",
    }
    if fragment is None:
        verdict["error"] = "no assistant text in event stream"
        return run, verdict
    try:
        json.loads(fragment)
        verdict["parsed"] = True
    except json.JSONDecodeError as exc:
        verdict["error"] = f"json: {exc}"
        return run, verdict
    try:
        ThrowawayPlan.model_validate_json(fragment)
        verdict["valid"] = True
    except ValidationError as exc:
        verdict["error"] = f"schema: {exc.error_count()} errors"
    return run, verdict


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trials", type=int, default=6)
    parser.add_argument(
        "--models",
        default="anthropic/claude-haiku-4-5,anthropic/claude-sonnet-5",
        help="Model choice is a flag (decision #13); stages differ in how much "
        "thinking they deserve, so the floor is measured at more than one tier.",
    )
    parser.add_argument("--job-dir", default="/tmp/phase0/jobdir")
    args = parser.parse_args()

    verdicts: list[dict[str, Any]] = []
    for model in args.models.split(","):
        model = model.strip()
        for i in range(args.trials):
            _, verdict = trial(i, model, args.job_dir)
            verdicts.append(verdict)
            flag = "ok " if verdict["valid"] else "BAD"
            fence = " fenced" if verdict["fenced"] else ""
            print(
                f"  {flag} {model:<32} {verdict['wall_s']:>6.2f}s{fence} {verdict['error']}"
            )

    summary: dict[str, Any] = {}
    for model in {v["model"] for v in verdicts}:
        rows = [v for v in verdicts if v["model"] == model]
        times = [v["wall_s"] for v in rows]
        summary[model] = {
            "trials": len(rows),
            "valid": sum(v["valid"] for v in rows),
            "parsed": sum(v["parsed"] for v in rows),
            "fenced": sum(v["fenced"] for v in rows),
            "burst_s": times[0],
            "sustained_s": round(statistics.median(times[1:] or times), 2),
            "min_s": round(min(times), 2),
            "max_s": round(max(times), 2),
        }

    path = write_results(
        "agent_roundtrip", {"summary": summary, "trials": verdicts}
    )
    print(f"\n{json.dumps(summary, indent=2)}\nwrote {path}")


if __name__ == "__main__":
    main()
