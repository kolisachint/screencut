"""`plan_focus` — FocusTrack into a per-profile spatial projection (§4.3).

No model participates in the highest-impact spatial decision in the pipeline. Both
projections fall out of the same track and the same handful of scalars, every one
of which the preference store can later learn by median:

- **crop path** (vertical): a constant-size window that trails the focus point,
  smoothed by `crop_lag_ms` and rate-limited by `max_crop_delta_per_frame`.
- **zoom keyframes** (widescreen): dwell and click clusters become regions the
  frame magnifies into and back out of.

The window is a constant size in crop mode, deliberately. A window that also
changed size would make §9.1's judder check answer two questions at once, and it
would cost a filter graph that re-scales every frame — which on the target machine
(§16) is the difference between a render that fits in the review loop and one that
does not.

Output is in **source time**, like everything else before `compile` (§4.5). The
compiler evaluates it at each output frame's source time, so a cut is a jump in
the path rather than a discontinuity the planner has to know about.
"""

from __future__ import annotations

import math
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field

from spec.editspec import EditSpec
from spec.focus import FocusKind, FocusPoint, FocusTrack
from spec.profiles import FocusMode, RenderProfile

DWELL_KINDS = (FocusKind.CLICK, FocusKind.DWELL, FocusKind.MANUAL)


class PlanModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PathSample(PlanModel):
    """Window centre at one instant, in normalized source coordinates."""

    t: float
    cx: float
    cy: float


class CropPathPlan(PlanModel):
    """A constant-size window moving through the source frame."""

    mode: Literal[FocusMode.CROP_PATH] = FocusMode.CROP_PATH
    window_w: Annotated[float, Field(gt=0.0, le=1.0)]
    window_h: Annotated[float, Field(gt=0.0, le=1.0)]
    fps: Annotated[float, Field(gt=0.0)]
    samples: list[PathSample] = Field(default_factory=list)

    def center_at(self, t: float) -> tuple[float, float]:
        """Linear interpolation between samples; the ends hold."""
        if not self.samples:
            return 0.5, 0.5
        index = min(max(int(t * self.fps), 0), len(self.samples) - 1)
        current = self.samples[index]
        if index + 1 >= len(self.samples):
            return current.cx, current.cy
        nxt = self.samples[index + 1]
        span = nxt.t - current.t
        if span <= 0:
            return current.cx, current.cy
        f = min(max((t - current.t) / span, 0.0), 1.0)
        return current.cx + (nxt.cx - current.cx) * f, current.cy + (nxt.cy - current.cy) * f

    def max_step(self) -> float:
        """Largest centre movement between consecutive samples. §9.1's judder number."""
        return max(
            (
                math.hypot(b.cx - a.cx, b.cy - a.cy)
                for a, b in zip(self.samples, self.samples[1:])
            ),
            default=0.0,
        )


class ZoomRegion(PlanModel):
    """A stretch of source time the frame magnifies into."""

    t_in: float
    t_out: float
    cx: float
    cy: float
    zoom: Annotated[float, Field(ge=1.0)]


class ZoomPlan(PlanModel):
    mode: Literal[FocusMode.ZOOM_KEYFRAMES] = FocusMode.ZOOM_KEYFRAMES
    ease: float = Field(gt=0.0, description="Seconds to ease in and out of a region.")
    regions: list[ZoomRegion] = Field(default_factory=list)

    def envelope_at(self, t: float) -> tuple[float, float, float]:
        """(zoom, cx, cy) at a source time — the same trapezoid the filter graph builds.

        Kept here as well so tests and §9.1 can check the intent without parsing an
        FFmpeg expression, which is the kind of thing that quietly stops matching.
        """
        weight_total = 0.0
        zoom = 1.0
        cx = cy = 0.0
        for region in self.regions:
            weight = _trapezoid(t, region.t_in, region.t_out, self.ease)
            if weight <= 0.0:
                continue
            weight_total += weight
            zoom += (region.zoom - 1.0) * weight
            cx += region.cx * weight
            cy += region.cy * weight
        if weight_total <= 0.0:
            return 1.0, 0.5, 0.5
        rest = max(0.0, 1.0 - weight_total)
        return zoom, cx + 0.5 * rest, cy + 0.5 * rest


FocusPlan = Union[CropPathPlan, ZoomPlan]


def _trapezoid(t: float, t_in: float, t_out: float, ease: float) -> float:
    """1.0 inside the region, ramping from 0 across `ease` at each edge."""
    if ease <= 0.0:
        return 1.0 if t_in <= t < t_out else 0.0
    rise = (t - (t_in - ease)) / ease
    fall = ((t_out + ease) - t) / ease
    return min(max(min(rise, fall), 0.0), 1.0)


def plan_focus(spec: EditSpec, profile: RenderProfile) -> FocusPlan:
    if profile.focus.mode is FocusMode.CROP_PATH:
        return _plan_crop_path(spec, profile)
    return _plan_zoom(spec, profile)


# --- crop path ---------------------------------------------------------------


def window_size(spec: EditSpec, profile: RenderProfile) -> tuple[float, float]:
    """The constant window, as a fraction of the source frame.

    Fit the output's aspect inside the source, then tighten by `zoom_factor`. At
    1.0 the window is the largest one of that aspect the source can give, which is
    also the least upscaling — cropping 9:16 out of 16:9 already costs a 1.8x
    upscale before any tightening is asked for.
    """
    out_aspect = profile.width / profile.height
    src_aspect = spec.source.width / spec.source.height
    if out_aspect < src_aspect:
        w, h = out_aspect / src_aspect, 1.0
    else:
        w, h = 1.0, src_aspect / out_aspect
    tighten = max(profile.focus.zoom_factor, 1.0)
    return min(w / tighten, 1.0), min(h / tighten, 1.0)


def _plan_crop_path(spec: EditSpec, profile: RenderProfile) -> CropPathPlan:
    window_w, window_h = window_size(spec, profile)
    fps = profile.fps
    dt = 1.0 / fps
    tau = max(profile.focus.crop_lag_ms, 1) / 1000.0
    alpha = 1.0 - math.exp(-dt / tau)
    ceiling = profile.focus.max_crop_delta_per_frame

    count = max(int(round(spec.source.duration * fps)), 1)
    track = spec.focus
    cx, cy = _track_at(track, 0.0)
    samples: list[PathSample] = []
    for index in range(count):
        t = index * dt
        target_x, target_y = _track_at(track, t)
        # Trail the target rather than track it: a crop that snaps to the cursor
        # reads as nervous, and the lag is one of the scalars §10 can learn.
        next_x = cx + (target_x - cx) * alpha
        next_y = cy + (target_y - cy) * alpha
        cx, cy = _rate_limit(cx, cy, next_x, next_y, ceiling)
        cx = min(max(cx, window_w / 2), 1.0 - window_w / 2)
        cy = min(max(cy, window_h / 2), 1.0 - window_h / 2)
        samples.append(PathSample(t=round(t, 6), cx=round(cx, 6), cy=round(cy, 6)))
    return CropPathPlan(window_w=window_w, window_h=window_h, fps=fps, samples=samples)


def _rate_limit(cx: float, cy: float, nx: float, ny: float, ceiling: float) -> tuple[float, float]:
    """Cap movement per frame. Judder is *the* failure mode of automated reframing."""
    dx, dy = nx - cx, ny - cy
    step = math.hypot(dx, dy)
    if step <= ceiling or step == 0.0:
        return nx, ny
    scale = ceiling / step
    return cx + dx * scale, cy + dy * scale


def _track_at(track: FocusTrack, t: float) -> tuple[float, float]:
    """Cursor position at `t`, interpolated. An empty track means the frame centre."""
    points = track.points
    if not points:
        return 0.5, 0.5
    if t <= points[0].t:
        return points[0].x, points[0].y
    if t >= points[-1].t:
        return points[-1].x, points[-1].y
    lo, hi = 0, len(points) - 1
    while lo + 1 < hi:
        mid = (lo + hi) // 2
        if points[mid].t <= t:
            lo = mid
        else:
            hi = mid
    a, b = points[lo], points[lo + 1]
    span = b.t - a.t
    if span <= 0:
        return a.x, a.y
    f = (t - a.t) / span
    return a.x + (b.x - a.x) * f, a.y + (b.y - a.y) * f


# --- zoom keyframes ----------------------------------------------------------


def _plan_zoom(spec: EditSpec, profile: RenderProfile) -> ZoomPlan:
    tunables = profile.focus
    ease = max(tunables.ease_ms, 1) / 1000.0
    min_dwell = tunables.min_dwell_ms / 1000.0
    min_gap = tunables.min_gap_ms / 1000.0
    zoom = max(tunables.zoom_factor, 1.0)

    clusters = _cluster(spec.focus.points, min_gap)
    regions: list[ZoomRegion] = []
    half = 1.0 / (2.0 * zoom)
    for cluster in clusters:
        t_in, t_out = cluster[0].t, cluster[-1].t
        if t_out - t_in < min_dwell:
            continue  # a transient pass, not a place the eye rested
        weight = sum(p.weight for p in cluster) or 1.0
        cx = sum(p.x * p.weight for p in cluster) / weight
        cy = sum(p.y * p.weight for p in cluster) / weight
        regions.append(
            ZoomRegion(
                t_in=round(t_in, 6),
                t_out=round(t_out, 6),
                cx=round(min(max(cx, half), 1.0 - half), 6),
                cy=round(min(max(cy, half), 1.0 - half), 6),
                zoom=zoom,
            )
        )
    return ZoomPlan(ease=ease, regions=_separate(regions, ease))


def _cluster(points: list[FocusPoint], min_gap: float) -> list[list[FocusPoint]]:
    """Group clicks and dwells separated by less than `min_gap`.

    Movement points are excluded: the cursor being somewhere on its way elsewhere
    is not attention, and treating it as such is what makes automated zoom
    oscillate.
    """
    clusters: list[list[FocusPoint]] = []
    current: list[FocusPoint] = []
    for point in points:
        if point.kind not in DWELL_KINDS:
            continue
        if current and point.t - current[-1].t > min_gap:
            clusters.append(current)
            current = []
        current.append(point)
    if current:
        clusters.append(current)
    return clusters


def _separate(regions: list[ZoomRegion], ease: float) -> list[ZoomRegion]:
    """Pull back region edges so two ramps never overlap and sum above their zoom."""
    out: list[ZoomRegion] = []
    for index, region in enumerate(regions):
        t_in, t_out = region.t_in, region.t_out
        if index:
            gap = t_in - regions[index - 1].t_out
            if gap < 2 * ease:
                t_in = regions[index - 1].t_out + gap / 2 + ease / 2
        if t_out <= t_in:
            continue
        out.append(region.model_copy(update={"t_in": t_in, "t_out": t_out}))
    return out
