"""The metadata sidecar — copy for the post (§7.1, §1.1).

Language, so a model writes it; and the softest stage in the pipeline, because
nothing downstream reads what it produces. The sidecar lands beside the mp4 in
`renders/` and the pipeline ends there (decision #9: publishing is a file on
disk). A wrong title costs a rewrite; it cannot corrupt a spec or a render.

**The one per-profile model stage.** Everything else a model decides here is
aspect-independent on purpose — §4.4.1 keeps tiering out of the profiles so one
`EditSpec` renders at two lengths. Copy is the honest exception: a 20-second
vertical short and a 90-second widescreen demo are two posts, they say different
amounts, and a sidecar that gave them the same description would be describing
neither. So this stage sees the profile, and it sees the *expected* transcript
for that profile's tier threshold (§9.2) rather than the whole take — which is
what the viewer will actually hear.

**Degradation is script-derived** (§7.4's table), and it is the only fallback in
this pipeline that produces *language* without a model. That is fine because it
produces no new language: the first sentence of what you already wrote is your
sentence, cut short.
"""

from __future__ import annotations

import re

from spec.metadata import DESCRIPTION_MAX, TAG_MAX, TAGS_MAX, TITLE_MAX, MetadataCopy

FALLBACK_TITLE_CHARS = 70
FALLBACK_DESCRIPTION_SENTENCES = 3

_SENTENCE = re.compile(r"(?<=[.!?])\s+")
_TAG_SEPARATOR = re.compile(r"[\s_]+")
_TAG_ALLOWED = re.compile(r"[^a-z0-9-]")


INSTRUCTION = """\
You are the metadata stage of a screen-recording pipeline. You are given the
narration a viewer of one particular render will hear, that render's length, and
the shape it was cut for.

Write the copy for the post.

- "title": one line, under 70 characters, saying what the video shows. No
  clickbait, no trailing punctuation, no emoji.
- "description": a short paragraph — two to four sentences — that someone
  scrolling can read and know whether to watch. Say what is demonstrated.
- "tags": a handful of bare lowercase words or hyphenated phrases. No leading
  "#", no spaces inside a tag.
- Describe only what the narration actually says. Do not invent features,
  versions, prices or names that are not in it.
- A vertical short and a widescreen demo of the same recording are different
  posts. Write for the one you were given.
"""


def build_content(
    *, profile: str, aspect: str, duration: float, spoken: str, script: str | None
) -> str:
    """Everything the stage needs, in the prompt (§7.3)."""
    lines = [
        f"Render: {profile} ({aspect}), {duration:.1f}s as edited.",
        "",
        "What a viewer of this render hears:",
        spoken.strip() or "(nothing is said in this render)",
    ]
    if script and script.strip() and script.strip() != spoken.strip():
        # The full script, when the edit dropped part of it. The difference is
        # informative — it is what was cut for *this* profile's budget — and the
        # copy should describe the render, not the take it came from.
        lines += ["", "The full script, for context (the render above is the edit of it):", script.strip()]
    return "\n".join(lines)


def derive_copy(spoken: str, *, job_id: str) -> MetadataCopy:
    """§7.4's fallback: a title and a description out of the words themselves.

    No tags. A tag is a claim about where this belongs, and there is no way to
    derive one from a transcript that is not a guess — a fallback that guesses is
    worse than a fallback that is visibly thin, because only one of them is
    obviously the fallback when it turns up in review.
    """
    sentences = [s.strip() for s in _SENTENCE.split(spoken.strip()) if s.strip()]
    if not sentences:
        # A screen capture with the mic off and no script (§5.3's ordinary job).
        # The job id is the only true thing available, and saying so is better
        # than an empty sidecar that reads like a bug.
        return MetadataCopy(
            title=job_id,
            description=f"Screen recording {job_id}. No narration to describe it.",
        )

    title = sentences[0].rstrip(".!?")
    if len(title) > FALLBACK_TITLE_CHARS:
        title = title[: FALLBACK_TITLE_CHARS - 1].rsplit(" ", 1)[0] + "…"
    description = " ".join(sentences[:FALLBACK_DESCRIPTION_SENTENCES])
    return MetadataCopy(
        title=title[:TITLE_MAX], description=description[:DESCRIPTION_MAX] or title[:TITLE_MAX]
    )


def normalize(copy: MetadataCopy) -> MetadataCopy:
    """Tags into the shape the schema describes, rather than trusted to arrive in it.

    A model asked for bare lowercase tags returns "#Screen Recording" often
    enough that rejecting it would spend a §7.2 retry on punctuation. Stripping
    the hash and the spaces is arithmetic; choosing the words was the taste.
    Duplicates go, order is kept — the first mention is the one the model meant.
    """
    seen: set[str] = set()
    tags: list[str] = []
    for raw in copy.tags:
        tag = _TAG_ALLOWED.sub("", _TAG_SEPARATOR.sub("-", raw.strip().lstrip("#").lower()))
        tag = tag.strip("-")[:TAG_MAX]
        if tag and tag not in seen:
            seen.add(tag)
            tags.append(tag)
    return copy.model_copy(update={"tags": tags[:TAGS_MAX]})
