"""`script_draft` — the words you will perform (decision #8, §7.1).

The one stage §1.1 adjudicates twice. Written language is in scope because a
script is not media: it reaches a frame only by being read aloud, in your voice,
by you or by decision #20's clone of you. So this stage writes words and nothing
else — no timing, no framing, no claim about the recording.

**It drafts from a brief, and refuses without one.** `narration.brief` is
`Stage.HUMAN` (`spec/narration.py`), and that is the point rather than an
inconvenience: a stage that invents a subject from a duration and a cursor track
is generating content, and §1.1 says this pipeline does not. What the brief gets
alongside it is what the pipeline actually knows — how long the recording is, and
where the demonstration happened — so the script can be paced to the footage it
narrates.

**No degradation path**, and §7.4's table says so: a job whose narration is
synthesized has no script to fall back to, and there is nothing to render. It is
the same row-shape as `tts` one step later, for the same reason.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from plan.context import WORDS_PER_SECOND, focus_summary, word_budget
from spec.focus import FocusTrack

MAX_SCRIPT_CHARS = 20_000


class ScriptDraft(BaseModel):
    """The §7.2 fragment: one field, because the stage decides one thing.

    A `notes` or `rationale` field would be a place for the model to talk, and
    every token spent there is a token not spent on the script — which is the
    whole artifact.
    """

    model_config = ConfigDict(extra="forbid")

    script: Annotated[str, Field(min_length=1, max_length=MAX_SCRIPT_CHARS)]


INSTRUCTION = """\
You are the scriptwriting stage of a screen-recording pipeline. You are given a
brief in the author's own words, the length of the screen recording the script
has to fit, and a summary of where the cursor was busy.

Write the narration the author will read aloud over that recording.

- Write only what is spoken. No headings, no stage directions, no speaker
  labels, no timestamps, no markdown. The whole string is read out.
- Say what the brief says. You are drafting the author's words, not choosing a
  subject: do not introduce claims, features, numbers or names the brief does
  not contain.
- Fit the word budget. It is derived from the recording's length at a normal
  speaking pace, and a script that overruns the recording cannot be rendered.
- Pace it against the cursor summary: the busy stretches are where something is
  being demonstrated and want explaining, the quiet ones want fewer words.
- Plain sentences a person can read in one breath. Contractions are fine.
"""


def build_content(brief: str, track: FocusTrack, duration: float) -> str:
    """Everything the stage needs, in the prompt (§7.3)."""
    budget = word_budget(duration)
    return "\n".join(
        [
            f"Recording length: {duration:.2f}s",
            f"Word budget: about {budget} words "
            f"(at {WORDS_PER_SECOND:g} words/second; going over is a failed job, not a long one)",
            "",
            "Brief, in the author's own words:",
            brief.strip(),
            "",
            focus_summary(track),
        ]
    )


class NoBrief(ValueError):
    """A draft was asked for with nothing to draft from.

    Its own type so `runner/stages.py` can say the one useful thing — write a
    brief, or supply a script — rather than reporting an empty prompt.
    """


def content_for(brief: str | None, track: FocusTrack, duration: float) -> str:
    if not (brief or "").strip():
        raise NoBrief(
            "script_draft has no brief to draft from. Put what the video should say in "
            "narration.brief, or write narration.script yourself and drop the stage from "
            "job.json — this stage drafts your words, it does not choose a subject (§1.1)."
        )
    return build_content(brief or "", track, duration)


def word_count(script: str) -> int:
    return len(script.split())
