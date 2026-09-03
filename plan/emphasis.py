"""Emphasis selection — which words carry the weight (§7.1, §6.2).

The smallest model stage in the pipeline, and the one whose fragment shape does
the most work. `Word.emphasis` is the only model-written field in the caption
subtree: timing and layout are arithmetic over `align`'s output and profile
geometry, and taste is which of those words you lean on.

**The model returns indices, not words.** A fragment carrying word objects could
come back with the text changed, the timings nudged, or one word quietly missing
— and every one of those is a caption that no longer matches the audio, which
§9.2 would then report as a real failure. Indices into a list the prompt numbered
cannot express any of that. It is the same discipline as `plan_edit` returning
ranges rather than a partition (§7.2): let the model decide the thing only taste
can decide, and let arithmetic hold everything else.

**Nothing renders this yet, and that is §6.2's plan rather than an omission.**
The first compiler draws plain timed blocks and ignores the word array; kinetic
word-highlight rendering is a later, purely compiler-side phase. Model the end
state, render the simple case — the field is populated now so that phase changes
no schema and invalidates no golden spec.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from spec.captions import CaptionBlock

SUGGESTED_SHARE = 0.08
"""Roughly one word in twelve. Guidance in the prompt, not a ceiling in code.

A cap enforced here would be arithmetic overruling taste, which is the one thing
this stage exists to avoid. What the stage does instead is report the share it
got (`runner/stages.py`), on the same argument as §9.1 reporting the trim
override rate as a number rather than a verdict: a model emphasizing half the
transcript is worth seeing before the learner starts averaging over it.
"""


class EmphasisPlan(BaseModel):
    """The §7.2 fragment: positions in the numbered list the prompt supplied."""

    model_config = ConfigDict(extra="forbid")

    emphasize: list[int] = Field(
        default_factory=list,
        description="Indices of words to emphasize, from the numbered transcript in the prompt.",
    )


INSTRUCTION = """\
You are the emphasis stage of a screen-recording pipeline. You are given the
narration split into numbered words, in order.

Return the indices of the words that carry the weight of what is being said.

- Emphasize the word a listener would stress reading the sentence aloud: the
  name of the thing, the number, the verb the sentence turns on, the contrast.
- Do not emphasize function words — "the", "a", "of", "is" — on their own.
- Be sparing. Emphasis that lands on one word in ten reads as emphasis;
  emphasis on one word in three reads as none.
- Return indices only, from the list you were given. Do not return the words.
"""


def numbered_words(blocks: list[CaptionBlock]) -> list:
    """Every caption word in source-time order — the list the indices address.

    Built the same way here and in `apply`, from the same sorted blocks, because
    an index means nothing if the two sides number differently. That is the
    hazard this shape trades for the one it removes, and it is contained by both
    sides calling one function.
    """
    return [word for block in sorted(blocks, key=lambda b: b.t_in) for word in block.words]


def build_content(blocks: list[CaptionBlock]) -> str:
    words = numbered_words(blocks)
    if not words:
        return "Narration: (no words)"
    suggested = max(int(len(words) * SUGGESTED_SHARE), 1)
    lines = [
        f"{len(words)} words. Somewhere around {suggested} of them is the right amount "
        f"of emphasis for a script this length.",
        "",
        "Numbered narration:",
        " ".join(f"[{index}]{word.text}" for index, word in enumerate(words)),
    ]
    return "\n".join(lines)


def apply(blocks: list[CaptionBlock], plan: EmphasisPlan) -> tuple[list[CaptionBlock], int, int]:
    """Set `emphasis` on the words the model named. Returns the blocks, how many
    landed, and how many indices were out of range.

    Out-of-range indices are dropped rather than retried. A fragment that names
    word 900 of 400 is off by a mistake no error message would teach it to fix,
    and it is not worth a round trip when every other index in the list is good —
    §7.2's retry is for a reply the schema rejects, and this one it does not.
    """
    words = numbered_words(blocks)
    wanted = {index for index in plan.emphasize if 0 <= index < len(words)}
    dropped = len({*plan.emphasize}) - len(wanted)
    for index, word in enumerate(words):
        # Assigned rather than rebuilt: `SpecModel` validates on assignment, so a
        # bad value still fails here, and the blocks keep their identity.
        word.emphasis = index in wanted
    return blocks, len(wanted), dropped
