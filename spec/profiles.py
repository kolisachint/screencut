"""RenderProfile — how one `EditSpec` becomes one render (architecture.md §4.1).

A profile carries **two** projections, because a render differs from its source
in space *and* in time:

- `focus`: `FocusTrack` -> zoom keyframes or a crop path (§4.3),
- `duration_budget`: tiered `segments` -> a duration (§4.4.1).

Preferences are learned per profile. Caption Y in vertical is a different number
from caption Y in widescreen, and a single global default averages them into a
value wrong for both.

Profile fields are `Stage.CONFIG`: hand-written now, moved by the learner later
(§10) — deterministic either way, and checked strictly by golden replay (§11.1).
"""

from __future__ import annotations

from enum import Enum
from typing import Annotated

from pydantic import Field, model_validator

from spec.origin import Stage, spec_field
from spec.types import Normalized, PositiveSeconds, Rect, SpecModel


class FocusMode(str, Enum):
    ZOOM_KEYFRAMES = "zoom_keyframes"
    """Widescreen: hold the frame, magnify dwell regions."""

    CROP_PATH = "crop_path"
    """Vertical: a moving window that trails the focus point."""


class Encoder(str, Enum):
    VIDEOTOOLBOX = "videotoolbox"
    """Hardware, and the render-time win on Apple Silicon (§16)."""

    SOFTWARE = "software"
    """Bit-reproducible, and therefore what golden frame hashing uses (§16)."""


class SafeArea(SpecModel):
    """Insets as fractions of the output. Captions and overlays live inside it (§9.1)."""

    top: Normalized = spec_field(default=0.08, produced_by=Stage.CONFIG)
    right: Normalized = spec_field(default=0.05, produced_by=Stage.CONFIG)
    bottom: Normalized = spec_field(default=0.12, produced_by=Stage.CONFIG)
    left: Normalized = spec_field(default=0.05, produced_by=Stage.CONFIG)

    @model_validator(mode="after")
    def _leaves_room(self) -> "SafeArea":
        if self.top + self.bottom >= 1.0 or self.left + self.right >= 1.0:
            raise ValueError("safe area insets leave no usable frame")
        return self

    def contains(self, rect: Rect) -> bool:
        return (
            rect.x >= self.left
            and rect.y >= self.top
            and rect.right <= 1.0 - self.right
            and rect.bottom <= 1.0 - self.bottom
        )


class CaptionStyle(SpecModel):
    """Caption box geometry and type scale, in output-normalized coordinates.

    Learned per profile (§4.1) — this is the example that argues for two layers.
    """

    box: Rect = spec_field(produced_by=Stage.CONFIG, description="Where the caption block sits in the output frame.")
    font_family: str = spec_field(default="Inter", produced_by=Stage.CONFIG)
    type_scale: Annotated[float, Field(gt=0.0, le=0.5)] = spec_field(
        default=0.045,
        produced_by=Stage.CONFIG,
        description="Cap height as a fraction of output height.",
    )
    max_chars_per_line: Annotated[int, Field(gt=0)] = spec_field(default=32, produced_by=Stage.CONFIG)
    max_lines: Annotated[int, Field(gt=0)] = spec_field(default=2, produced_by=Stage.CONFIG)
    min_display_s: PositiveSeconds = spec_field(default=0.8, produced_by=Stage.CONFIG)


class FocusProjection(SpecModel):
    """The §4.3 tunables. Every one is a scalar the preference store learns by median,
    and every one is a number you can also just set by hand when a job needs it."""

    mode: FocusMode = spec_field(produced_by=Stage.CONFIG)
    zoom_factor: Annotated[float, Field(ge=1.0, le=4.0)] = spec_field(default=1.4, produced_by=Stage.CONFIG)
    min_dwell_ms: Annotated[int, Field(ge=0)] = spec_field(default=600, produced_by=Stage.CONFIG)
    min_gap_ms: Annotated[int, Field(ge=0)] = spec_field(default=1200, produced_by=Stage.CONFIG)
    ease_ms: Annotated[int, Field(ge=0)] = spec_field(default=350, produced_by=Stage.CONFIG)
    crop_lag_ms: Annotated[int, Field(ge=0)] = spec_field(default=250, produced_by=Stage.CONFIG)
    max_crop_delta_per_frame: Annotated[float, Field(gt=0.0, le=1.0)] = spec_field(
        default=0.012,
        produced_by=Stage.CONFIG,
        description="Normalized crop movement ceiling. Judder is *the* failure mode of automated reframing (§9.1).",
    )


class EncodeSettings(SpecModel):
    """Encode settings for both paths, because both are used.

    Quality is expressed twice on purpose. `crf` and `preset` are x264's scales and
    govern the software path; `quality` is VideoToolbox's and governs the hardware
    one. They are not convertible, so a single knob would have to lie about one
    encoder — and on the target machine (§16) the hardware encoder is the default,
    which is the path a lone `crf` would silently fail to control. `compile` reads
    whichever pair matches `encoder`.
    """

    encoder: Encoder = spec_field(default=Encoder.VIDEOTOOLBOX, produced_by=Stage.CONFIG)
    video_codec: str = spec_field(default="h264", produced_by=Stage.CONFIG)
    crf: Annotated[int, Field(ge=0, le=51)] = spec_field(
        default=20, produced_by=Stage.CONFIG, description="Software path only (x264): lower is better."
    )
    preset: str = spec_field(
        default="medium", produced_by=Stage.CONFIG, description="Software path only (x264)."
    )
    quality: Annotated[int, Field(ge=1, le=100)] = spec_field(
        default=60,
        produced_by=Stage.CONFIG,
        description=(
            "Hardware path only (VideoToolbox `-q:v`): higher is better. Phase 2 confirms the "
            "range against the installed FFmpeg before relying on it."
        ),
    )
    pix_fmt: str = spec_field(default="yuv420p", produced_by=Stage.CONFIG)
    audio_codec: str = spec_field(default="aac", produced_by=Stage.CONFIG)
    audio_bitrate_kbps: Annotated[int, Field(gt=0)] = spec_field(default=192, produced_by=Stage.CONFIG)


class RenderProfile(SpecModel):
    name: Annotated[str, Field(min_length=1)] = spec_field(produced_by=Stage.CONFIG)
    width: Annotated[int, Field(gt=0)] = spec_field(produced_by=Stage.CONFIG)
    height: Annotated[int, Field(gt=0)] = spec_field(produced_by=Stage.CONFIG)
    fps: Annotated[float, Field(gt=0.0)] = spec_field(default=30.0, produced_by=Stage.CONFIG)
    duration_budget: PositiveSeconds = spec_field(
        produced_by=Stage.CONFIG,
        description=(
            "Seconds this profile is willing to run. Decides how aggressively it cuts, and it is "
            "one scalar rather than a cut list — which is what makes cuts aspect-agnostic (§4.4.1) "
            "and cut pacing learnable (§10)."
        ),
    )
    safe_area: SafeArea = spec_field(default_factory=SafeArea, produced_by=Stage.CONFIG)
    captions: CaptionStyle = spec_field(produced_by=Stage.CONFIG)
    focus: FocusProjection = spec_field(produced_by=Stage.CONFIG)
    encode: EncodeSettings = spec_field(default_factory=EncodeSettings, produced_by=Stage.CONFIG)

    @property
    def aspect(self) -> float:
        return self.width / self.height

    @model_validator(mode="after")
    def _caption_box_inside_safe_area(self) -> "RenderProfile":
        if not self.safe_area.contains(self.captions.box):
            raise ValueError(f"profile {self.name}: caption box falls outside the safe area")
        return self


SHORTS_9X16 = RenderProfile(
    name="shorts_9x16",
    width=1080,
    height=1920,
    fps=30.0,
    duration_budget=15.0,
    safe_area=SafeArea(top=0.10, right=0.06, bottom=0.16, left=0.06),
    captions=CaptionStyle(
        box=Rect(x=0.08, y=0.66, w=0.84, h=0.16),
        type_scale=0.052,
        max_chars_per_line=24,
        max_lines=3,
    ),
    focus=FocusProjection(mode=FocusMode.CROP_PATH, zoom_factor=1.6, crop_lag_ms=250),
)
"""Vertical short. The tight budget is why it drops tiers the demo keeps (§4.4.1)."""

DEMO_16X9 = RenderProfile(
    name="demo_16x9",
    width=1920,
    height=1080,
    fps=30.0,
    duration_budget=180.0,
    safe_area=SafeArea(top=0.06, right=0.05, bottom=0.10, left=0.05),
    captions=CaptionStyle(
        box=Rect(x=0.14, y=0.78, w=0.72, h=0.11),
        type_scale=0.040,
        max_chars_per_line=42,
        max_lines=2,
    ),
    focus=FocusProjection(mode=FocusMode.ZOOM_KEYFRAMES, zoom_factor=1.35),
)
"""Widescreen demo. A budget loose enough that it typically takes everything."""

BUILTIN_PROFILES: dict[str, RenderProfile] = {p.name: p for p in (SHORTS_9X16, DEMO_16X9)}


def profile(name: str) -> RenderProfile:
    try:
        return BUILTIN_PROFILES[name]
    except KeyError:
        raise KeyError(f"unknown render profile {name!r}; have {sorted(BUILTIN_PROFILES)}") from None
