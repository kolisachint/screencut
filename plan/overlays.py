"""`plan_overlays` — template, anchor, text (§6.3, §7.1).

Taste, bounded by a closed template set. The model picks which of four templates
to use, where in normalized source space it points, what it says, and when it is
on screen. It does not invent a layout: free-form generation would be
unpredictable, untestable and unlearnable, and under full autonomy (#12) every
instance would be discovered at review time.

**This is the case that looks like generation and is not** (§1.1). A fixed
template filled with text is a lower-third — editing furniture, not invented
content — and the schema is what keeps it that way: `OverlayTemplate` is an enum
and `anchor` is a normalized point, so most wrong answers are *invalid* answers
rather than plausible ones, which is risk R5's real mitigation.

**Why there is a `reconcile` here at all.** `OverlayPlan` validates in isolation
— an overlay from 55s to 61s is a well-formed `OverlayIntent`. It is `EditSpec`
that knows the source is 60s long, and it *raises* on the overrun. Without this
step a model stage would fail the whole job at apply time, which §7.4 forbids in
so many words: an LLM stage failure must not fail the job. So the fragment is
intent, exactly as `plan_edit`'s is, and arithmetic makes it a valid document.

**No dependency on the edit, deliberately.** Overlays are planned against the
full source timeline and `compile/timeline.py` drops the ones that land in
removed material — one wasted overlay is worth a stage dependency removed, and
the alternative puts one model call behind another for no editorial gain.
"""

from __future__ import annotations

from spec.overlays import OverlayIntent, OverlayPlan, OverlayTemplate
from spec.types import TIME_EPS

MIN_SPAN_S = 0.4
"""Shorter than this is a flash rather than an overlay.

`compile/timeline.py` already refuses to composite a fragment below its own
minimum after a cut splits one. This is the same judgement one step earlier, on
the range the model asked for rather than on what survived the edit.
"""


INSTRUCTION = """\
You are the overlay stage of a screen-recording pipeline. You are given the
narration with word timings and a summary of where the cursor was, with
positions.

Choose the overlays that earn their place on screen.

Templates, and what each is for:

- "callout_arrow" — points at a thing on screen and names it. Needs an anchor
  on the thing itself.
- "highlight_box" — frames a region being talked about. Anchor at its centre.
- "label_chip" — a short label sitting near what it labels. Anchor beside it.
- "progress_pill" — a single whole-video progress indicator. It takes no anchor
  and no times; return null for anchor, t_in and t_out. At most one.

Rules:

- Anchors are normalized source coordinates: (0,0) is the top-left of the
  recording, (1,1) the bottom-right. Never pixels.
- Anchor on what the narration is talking about at that moment, and use the
  cursor summary to find it — a click at (0.62, 0.31) while the script says
  "the export button" is where the export button is.
- Prefer the upper two-thirds of the frame. Captions are burned along the
  bottom, and an overlay sitting on one is a verification failure.
- Times are source seconds and must lie inside the recording. An overlay should
  be on screen while the thing it labels is being talked about, and for at
  least a second.
- Text is a few words. An overlay is a label, not a sentence.
- Few is better than many. An overlay every ten seconds is a lot; one on every
  sentence is chrome.
"""


def build_content(words: list, focus: str, duration: float) -> str:
    """Everything the stage needs, in the prompt (§7.3)."""
    from plan.context import transcript_lines

    return "\n".join(
        [
            f"Source duration: {duration:.2f}s",
            "",
            "Templates available: " + ", ".join(t.value for t in OverlayTemplate),
            "",
            "Transcript, [start-end] word:",
            transcript_lines(words),
            "",
            focus,
        ]
    )


def reconcile(plan: OverlayPlan, duration: float) -> list[OverlayIntent]:
    """The model's intent into overlays `EditSpec` will accept.

    Clamped and dropped rather than retried, for the same reason `plan_edit`
    derives its partition: the mistakes this fixes are float arithmetic against a
    duration the fragment's own schema cannot see, and a retry buys a round trip
    to fix a boundary by a tenth of a second. What a retry *is* for — a template
    that is not a template, an anchor outside the frame — the fragment schema
    already rejects before this runs.

    At most one progress pill survives. Two whole-output pills is two of the same
    element stacked on itself, which is a rendering nobody asked for rather than a
    second opinion.
    """
    kept: list[OverlayIntent] = []
    seen_pill = False
    for overlay in plan.overlays:
        if overlay.spans_whole_output:
            if seen_pill:
                continue
            seen_pill = True
            kept.append(overlay)
            continue

        t_in = min(max(overlay.t_in or 0.0, 0.0), duration)
        t_out = min(max(overlay.t_out or 0.0, 0.0), duration)
        if t_out - t_in < MIN_SPAN_S - TIME_EPS:
            continue
        kept.append(overlay.model_copy(update={"t_in": t_in, "t_out": t_out}))

    kept.sort(key=lambda o: (o.t_in is not None, o.t_in or 0.0))
    return kept
