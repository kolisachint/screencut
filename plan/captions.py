"""Word timings -> `CaptionBlock`s (architecture.md §6.2, phase 4).

Deterministic, and §7.1 says so: what a caption *says* is the transcript, when it
appears is arithmetic, and where it sits is the profile. None of that is taste, so
no model participates. The one model-written field anywhere in the caption subtree
is `Word.emphasis`, and that arrives in phase 9.

**Blocks are chosen once, for every profile at once.** §4.1 is that one `EditSpec`
serves N profiles, and `EditSpec.captions` is part of that document — so there
cannot be a 9:16 caption list and a 16:9 one. Line *wrapping* is per profile and
already belongs to `compile/captions.py`; what is decided here is where one block
ends and the next begins. Sizing those against the **tightest** profile is what
makes one list correct everywhere: a block that fits the narrowest box fits the
wider one with room to spare, while sizing against the widest overflows the
narrow one and there is no third answer that is one list.

Breaks are chosen in the order a reader notices them:

1. **A sentence ends.** Punctuation is the break a viewer already expects.
2. **A pause opens.** Silence longer than `PAUSE_S` is the speaker breaking their
   own line; §4.6's `trim` will often cut there too.
3. **Capacity runs out.** The fallback, not the rule. A block split only on
   capacity reads as though it was cut mid-thought, because it was.

`min_display_s` is a floor on how long a block stays up, and it is deliberately
*not* enforced by extending blocks: two blocks that both grow overlap, and
`EditSpec` rejects overlapping caption blocks outright. A block too short for its
profile is a §9.1 finding on the render, which is the layer that can see it.
"""

from __future__ import annotations

import re

from spec.captions import CaptionBlock, Word
from spec.profiles import RenderProfile

SENTENCE_END = re.compile(r"[.!?…]['\")\]]*$")
"""Sentence-final punctuation, allowing a closing quote or bracket after it."""

PAUSE_S = 0.45
"""Gap between two words that reads as the end of a phrase rather than a breath.

Below this a break lands inside a thought. Well above it and a slow speaker gets
one block per sentence, which overflows every box and hands the whole job to the
capacity fallback."""

MIN_WORDS = 2
"""Never break a block down to a single word on punctuation alone.

An abbreviation or an initial ends a "sentence" as far as the regex is concerned,
and one word flashing alone on screen is the visible cost of believing it."""


def capacity(profile: RenderProfile) -> int:
    """Characters a block may hold in this profile before it stops fitting."""
    style = profile.captions
    return style.max_chars_per_line * style.max_lines


def tightest(profiles: list[RenderProfile]) -> int:
    """The capacity every profile can honour. See the module note."""
    return min((capacity(p) for p in profiles), default=64)


def plan_captions(
    words: list[Word],
    profiles: list[RenderProfile],
    *,
    pause_s: float = PAUSE_S,
) -> list[CaptionBlock]:
    """Group words into blocks. Source time throughout, no edit applied (§4.5)."""
    limit = tightest(profiles)
    blocks: list[CaptionBlock] = []
    current: list[Word] = []

    for index, word in enumerate(words):
        candidate = current + [word]
        if current and _length(candidate) > limit:
            # Capacity is a hard stop, so the overflowing word opens the next block.
            blocks.append(_block(current))
            current = [word]
            continue
        current = candidate
        if len(current) < MIN_WORDS:
            continue
        following = words[index + 1] if index + 1 < len(words) else None
        if following is None:
            continue
        if SENTENCE_END.search(word.text) or following.t_in - word.t_out >= pause_s:
            blocks.append(_block(current))
            current = []

    if current:
        blocks.append(_block(current))
    return blocks


def _length(words: list[Word]) -> int:
    return sum(len(w.text) for w in words) + len(words) - 1


def _block(words: list[Word]) -> CaptionBlock:
    """A block spans exactly its words.

    Not a hair either side: `EditSpec` rejects overlapping blocks, and padding a
    block by a constant is how two adjacent ones come to overlap on a fast speaker
    — which fails validation at the end of a long job rather than here.
    """
    return CaptionBlock(t_in=words[0].t_in, t_out=words[-1].t_out, words=list(words))
