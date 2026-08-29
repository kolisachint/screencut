"""`EditSpec` + `RenderProfile` -> a render.

Principle 2, from the other side: **no model emits an FFmpeg argument**. Everything
in this package is deterministic projection — of time (§4.5), of space (§4.3), of
the fixed overlay template set (§6.3) — from a spec a model may have written into
a filter graph it never sees.
"""

from compile.timeline import EditedCaption, EditedOverlay, EditedTimeline, KeptSpan, project

__all__ = ["EditedCaption", "EditedOverlay", "EditedTimeline", "KeptSpan", "project"]
