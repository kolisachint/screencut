"""Overlay templates: SVG in, PNG at the profile's resolution out (§6.3).

Overlays are **parameterized templates, not free-form generation**. A small fixed
set, rendered deterministically. The model chooses template, anchor and text; it
does not invent a layout, because free-form generation would be unpredictable,
untestable and unlearnable — and under full autonomy every instance would be
discovered at review time.

SVG rather than a bitmap library because the same template has to come out right
at 1080x1920 and at 1920x1080 (decision #11). Nothing here is resolution-independent
after this point: each asset is rasterized once, at the size it will be composited.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import cairosvg

from spec.overlays import OverlayTemplate
from spec.profiles import ESTIMATED_CHAR_WIDTH_RATIO, RenderProfile

INK = "#0E131C"
"""Chip and callout background. Dark enough for white text over a bright UI."""
PAPER = "#FFFFFF"
ACCENT = "#4C9AFF"

CHAR_WIDTH_RATIO = ESTIMATED_CHAR_WIDTH_RATIO
"""Shared with the profile validator, so a chip and a caption cannot disagree about
how wide a character is."""


@dataclass(frozen=True)
class OverlayAsset:
    """A rasterized overlay and where it goes relative to what it points at."""

    template: str
    path: Path
    width: int
    height: int
    dx: int
    """Offset from the anchor pixel to the asset's left edge."""
    dy: int
    """Offset from the anchor pixel to the asset's top edge."""
    fill_rect: tuple[int, int, int, int] | None = None
    """For the progress pill: the (x, y, w, h) of its fill at 100%, in asset-local
    pixels. The fill itself is drawn by the graph, per frame, from output duration
    (§4.5) — the pill spans the whole output, so its value is a projection rather
    than anything the spec could have anchored."""


def render_asset(
    template: str,
    text: str,
    profile: RenderProfile,
    out_dir: Path,
    index: int,
) -> OverlayAsset:
    builders = {
        OverlayTemplate.LABEL_CHIP.value: _label_chip,
        OverlayTemplate.CALLOUT_ARROW.value: _callout_arrow,
        OverlayTemplate.HIGHLIGHT_BOX.value: _highlight_box,
        OverlayTemplate.PROGRESS_PILL.value: _progress_pill,
    }
    try:
        builder = builders[template]
    except KeyError:
        raise ValueError(f"no template named {template!r}; the set is closed (§6.3)") from None

    svg, width, height, dx, dy, fill = builder(text, profile)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"overlay_{index:02d}_{template}.png"
    cairosvg.svg2png(bytestring=svg.encode("utf-8"), write_to=str(path), output_width=width, output_height=height)
    return OverlayAsset(
        template=template, path=path, width=width, height=height, dx=dx, dy=dy, fill_rect=fill
    )


def _font_stack(profile: RenderProfile) -> str:
    return f"{profile.captions.font_family}, DejaVu Sans, sans-serif"


def _chip_size(text: str, font_size: int, pad: int) -> tuple[int, int]:
    width = int(round(len(text) * font_size * CHAR_WIDTH_RATIO)) + pad * 2
    return max(width, font_size * 3), int(round(font_size * 1.9))


def _label_chip(text: str, profile: RenderProfile):
    """A chip sitting above the thing it names, with a tail pointing down at it."""
    font = max(int(profile.height * 0.028), 12)
    pad = int(font * 0.7)
    tail = int(font * 0.5)
    w, h = _chip_size(text, font, pad)
    total_h = h + tail
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{total_h}">
  <rect x="0" y="0" width="{w}" height="{h}" rx="{h // 4}" fill="{INK}" fill-opacity="0.92"/>
  <polygon points="{w // 2 - tail},{h} {w // 2 + tail},{h} {w // 2},{total_h}" fill="{INK}" fill-opacity="0.92"/>
  <text x="{pad}" y="{int(h * 0.68)}" font-family="{_font_stack(profile)}" font-size="{font}"
        font-weight="600" fill="{PAPER}">{_escape(text)}</text>
</svg>"""
    # Anchored so the tail tip lands on the point, with a small gap.
    return svg, w, total_h, -w // 2, -(total_h + int(font * 0.35)), None


def _callout_arrow(text: str, profile: RenderProfile):
    """An arrow into the point from above-left, with its label along the shaft."""
    font = max(int(profile.height * 0.026), 11)
    pad = int(font * 0.6)
    w, h = _chip_size(text, font, pad)
    shaft = int(font * 2.2)
    total_w, total_h = w + shaft, h + shaft
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="{total_w}" height="{total_h}">
  <rect x="0" y="0" width="{w}" height="{h}" rx="{h // 5}" fill="{INK}" fill-opacity="0.92"/>
  <text x="{pad}" y="{int(h * 0.68)}" font-family="{_font_stack(profile)}" font-size="{font}"
        font-weight="600" fill="{PAPER}">{_escape(text)}</text>
  <line x1="{w // 2}" y1="{h}" x2="{total_w - font}" y2="{total_h - font}"
        stroke="{ACCENT}" stroke-width="{max(font // 5, 2)}" stroke-linecap="round"/>
  <polygon points="{total_w},{total_h} {total_w - font},{total_h - int(font * 0.45)} {total_w - int(font * 0.45)},{total_h - font}"
           fill="{ACCENT}"/>
</svg>"""
    return svg, total_w, total_h, -(total_w - font // 2), -(total_h - font // 2), None


def _highlight_box(text: str, profile: RenderProfile):
    """A stroked rectangle around the region, label riding on its top edge.

    The intent carries no size, so the box is a fixed fraction of the output. A
    size field is a plausible future want and an ordinary schema migration; it is
    not needed to find out whether the rest of this works.
    """
    w = int(profile.width * 0.30)
    h = int(profile.height * 0.14)
    font = max(int(profile.height * 0.024), 11)
    stroke = max(int(profile.height * 0.004), 2)
    label = ""
    if text:
        lw, lh = _chip_size(text, font, int(font * 0.6))
        label = (
            f'<rect x="0" y="0" width="{lw}" height="{lh}" rx="{lh // 5}" fill="{ACCENT}"/>'
            f'<text x="{int(font * 0.6)}" y="{int(lh * 0.7)}" font-family="{_font_stack(profile)}"'
            f' font-size="{font}" font-weight="700" fill="{INK}">{_escape(text)}</text>'
        )
        top = lh
    else:
        top = 0
    total_h = h + top
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{total_h}">
  {label}
  <rect x="{stroke // 2}" y="{top + stroke // 2}" width="{w - stroke}" height="{h - stroke}"
        rx="{stroke * 3}" fill="none" stroke="{ACCENT}" stroke-width="{stroke}"/>
</svg>"""
    return svg, w, total_h, -w // 2, -(top + h // 2), None


def _progress_pill(text: str, profile: RenderProfile):
    """A track across the bottom of the safe area. The graph fills it per frame."""
    left, _, right, _ = profile.safe_area.pixels(profile.width, profile.height)
    w = right - left
    h = max(int(profile.height * 0.008), 4)
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}">
  <rect x="0" y="0" width="{w}" height="{h}" rx="{h // 2}" fill="{PAPER}" fill-opacity="0.28"/>
</svg>"""
    return svg, w, h, 0, 0, (0, 0, w, h)


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
