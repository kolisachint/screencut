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

*Which* of them the learner may move is `learnable=True` on the field, and the set
it produces is §10's list exactly: the §4.3 focus tunables, caption geometry, and
`duration_budget`. §4.6's trim tunables are on that list too and are not here —
they are global rather than per-profile and live in `constraints.yaml`. Everything
unmarked is unmarked for a reason stated at the field: an output dimension is not
a preference, and neither is a font or a focus mode.
"""

from __future__ import annotations

import math
from enum import Enum
from typing import Annotated

from pydantic import Field, model_validator

from spec.origin import Stage, spec_field
from spec.types import Normalized, PositiveSeconds, Rect, SpecModel


ESTIMATED_CHAR_WIDTH_RATIO = 0.58
"""Advance width per character for a bold sans face, as a fraction of font size.

An estimate, not a measurement: measuring would put a font stack in the spec
package. It is accurate enough to catch a caption that cannot possibly fit its
box, which is the failure it exists to catch — and one that is otherwise found by
watching a render with the text running off both edges."""


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

    def pixels(self, width: int, height: int) -> tuple[int, int, int, int]:
        """The safe area's edges in output pixels: (left, top, right, bottom).

        Rounded **inward** — ceil the low edges, floor the high ones — so turning a
        normalized inset into pixels never lands a pixel outside the area it was
        meant to stay within. Shared by everything that sizes or places into the
        safe area, because two callers rounding independently disagree by a pixel
        and §9.1 then reports a real-looking failure that is only arithmetic.
        """
        return (
            math.ceil(self.left * width),
            math.ceil(self.top * height),
            math.floor((1.0 - self.right) * width),
            math.floor((1.0 - self.bottom) * height),
        )

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

    box: Rect = spec_field(
        produced_by=Stage.CONFIG,
        learnable=True,
        description="Where the caption block sits in the output frame.",
    )
    font_family: str = spec_field(default="Inter", produced_by=Stage.CONFIG)
    """Not learnable. §10 puts fonts in the hand-written tier, and `constraints.yaml`
    is where one is chosen — a median over a set of font names is not a preference,
    it is a category error."""

    type_scale: Annotated[float, Field(gt=0.0, le=0.5)] = spec_field(
        default=0.045,
        produced_by=Stage.CONFIG,
        learnable=True,
        description="Cap height as a fraction of output height.",
    )
    max_chars_per_line: Annotated[int, Field(gt=0)] = spec_field(
        default=32, produced_by=Stage.CONFIG, learnable=True
    )
    max_lines: Annotated[int, Field(gt=0)] = spec_field(default=2, produced_by=Stage.CONFIG, learnable=True)
    min_display_s: PositiveSeconds = spec_field(default=0.8, produced_by=Stage.CONFIG, learnable=True)
    kinetic: bool = spec_field(default=False, produced_by=Stage.CONFIG)
    """Word-highlight captions rather than plain timed blocks (§6.2).

    Not learnable, and for `focus.mode`'s reason: this says which renderer a
    profile uses, not a number about one, and there is no median of two
    renderers. It lives on the profile rather than in the spec because the spec
    already carries what both renderers read — the per-word timings — and which
    of them draws them is a property of the output, not of the take. That is why
    this phase needed no migration and moved no golden spec.

    Off by default. A profile opts in, because the plain block is the one that
    stays legible at any pace and the highlight is what a short buys with its
    attention span."""


class FocusProjection(SpecModel):
    """The §4.3 tunables. Every one is a scalar the preference store learns by median,
    and every one is a number you can also just set by hand when a job needs it."""

    mode: FocusMode = spec_field(produced_by=Stage.CONFIG)
    """Not learnable. Which projection a profile uses is what the profile *is* —
    vertical crops and widescreen zooms (§4.3) — and there is no median of two
    modes. The tunables underneath it are the preferences."""

    zoom_factor: Annotated[float, Field(ge=1.0, le=4.0)] = spec_field(
        default=1.4,
        produced_by=Stage.CONFIG,
        learnable=True,
        description=(
            "Magnification at a dwell region in zoom mode. In crop mode it tightens the "
            "constant window beyond the aspect fit, and 1.0 is the honest default there — "
            "cropping 9:16 out of 16:9 already costs a 1.8x upscale."
        ),
    )
    min_dwell_ms: Annotated[int, Field(ge=0)] = spec_field(default=600, produced_by=Stage.CONFIG, learnable=True)
    min_gap_ms: Annotated[int, Field(ge=0)] = spec_field(default=1200, produced_by=Stage.CONFIG, learnable=True)
    ease_ms: Annotated[int, Field(ge=0)] = spec_field(default=350, produced_by=Stage.CONFIG, learnable=True)
    crop_lag_ms: Annotated[int, Field(ge=0)] = spec_field(default=250, produced_by=Stage.CONFIG, learnable=True)
    max_crop_delta_per_frame: Annotated[float, Field(gt=0.0, le=1.0)] = spec_field(
        default=0.012,
        produced_by=Stage.CONFIG,
        learnable=True,
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
        learnable=True,
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

    @model_validator(mode="after")
    def _captions_fit_their_box(self) -> "RenderProfile":
        """`type_scale` is a fraction of height and the box is a fraction of width,
        so the two can drift apart silently — and do, since a profile is edited one
        field at a time. Estimated, so it catches gross mismatches only; that is the
        class of mistake worth failing at load rather than at review."""
        line = self.captions.max_chars_per_line * ESTIMATED_CHAR_WIDTH_RATIO * self.captions.type_scale * self.height
        box = self.captions.box.w * self.width
        if line > box:
            raise ValueError(
                f"profile {self.name}: {self.captions.max_chars_per_line} characters at "
                f"type_scale {self.captions.type_scale} need about {line:.0f}px but the caption box is "
                f"{box:.0f}px wide — lower max_chars_per_line or type_scale, or widen the box"
            )
        return self


SHORTS_9X16 = RenderProfile(
    name="shorts_9x16",
    width=1080,
    height=1920,
    fps=30.0,
    duration_budget=15.0,
    safe_area=SafeArea(top=0.10, right=0.06, bottom=0.16, left=0.06),
    captions=CaptionStyle(
        box=Rect(x=0.06, y=0.66, w=0.88, h=0.16),
        type_scale=0.040,
        max_chars_per_line=20,
        max_lines=3,
        # §6.2's trigger, taken literally: plain blocks look plain at a short's
        # pace, and twenty characters a line is little enough text that a lit
        # word is easy to find. The demo keeps plain blocks — a widescreen line
        # is read in one glance and a highlight travelling across it is noise.
        kinetic=True,
    ),
    focus=FocusProjection(mode=FocusMode.CROP_PATH, zoom_factor=1.0, crop_lag_ms=250),
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
