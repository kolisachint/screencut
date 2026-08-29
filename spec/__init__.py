"""The spec is the system (architecture.md, principle 1).

One versioned document — `EditSpec` — is read and written by the planner, the
compiler, the verifier, the review UI and the learner. `RenderProfile` is the
separate document that projects it into an actual render.
"""

from spec.audio import AudioTrack
from spec.captions import CaptionBlock, Word
from spec.edit import (
    EditDecisions,
    Removal,
    RemovalKind,
    Segment,
    Tier,
    choose_threshold,
)
from spec.editspec import EditSpec
from spec.focus import FocusKind, FocusPoint, FocusTrack
from spec.migrations import load_spec, load_spec_file, migrate
from spec.narration import Narration, NarrationSource
from spec.origin import Origin, Stage, field_origins
from spec.overlays import OverlayIntent, OverlayPlan, OverlayTemplate
from spec.profiles import (
    BUILTIN_PROFILES,
    DEMO_16X9,
    SHORTS_9X16,
    CaptionStyle,
    EncodeSettings,
    Encoder,
    FocusMode,
    FocusProjection,
    RenderProfile,
    SafeArea,
    profile,
)
from spec.source import Source
from spec.types import Normalized, Point, Rect, Seconds
from spec.version import CURRENT_SPEC_VERSION

__all__ = [
    "AudioTrack",
    "BUILTIN_PROFILES",
    "CURRENT_SPEC_VERSION",
    "CaptionBlock",
    "CaptionStyle",
    "DEMO_16X9",
    "EditDecisions",
    "EditSpec",
    "EncodeSettings",
    "Encoder",
    "FocusKind",
    "FocusMode",
    "FocusPoint",
    "FocusProjection",
    "FocusTrack",
    "Narration",
    "NarrationSource",
    "Normalized",
    "Origin",
    "OverlayIntent",
    "OverlayPlan",
    "OverlayTemplate",
    "Point",
    "Rect",
    "Removal",
    "RemovalKind",
    "RenderProfile",
    "SHORTS_9X16",
    "SafeArea",
    "Seconds",
    "Segment",
    "Source",
    "Stage",
    "Tier",
    "Word",
    "choose_threshold",
    "field_origins",
    "load_spec",
    "load_spec_file",
    "migrate",
    "profile",
]
