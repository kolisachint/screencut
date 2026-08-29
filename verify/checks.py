"""The deterministic checks (architecture.md §9.1).

Arithmetic, all of it — and a check a model can talk its way out of is not a
check. Three of them earn their place by catching failures that are invisible in
a still frame: crop judder, a cut landing mid-word, and a budget overrun that
would otherwise render long in silence.

Each check returns findings rather than raising, so one failure does not hide the
rest. Under decision #12 the whole job is reviewed at the end, and a report that
stops at the first problem makes that a serial process.
"""

from __future__ import annotations

from pathlib import Path

from compile.captions import line_count, wrap
from compile.graph import ViewRect, clamp_to_safe_area, view_rects
from compile.overlays import OverlayAsset
from compile.timeline import EditedTimeline
from plan.focus import CropPathPlan, FocusPlan
from spec.editspec import EditSpec
from spec.profiles import RenderProfile
from spec.types import TIME_EPS, Rect

from verify.probe import Loudness, MediaFacts, measure_loudness, probe
from verify.report import Finding, Severity, VerificationReport

DURATION_TOLERANCE_S = 0.25
"""How far a render may sit from the timeline it was projected from.

Not zero: the container's duration runs a little past the video because the last
AAC frame is padded to a whole frame. Anything larger is a real mismatch."""

LOUDNESS_TOLERANCE_LU = 1.0
DIALOGUE_TO_BED_MIN_DB = 12.0
"""How far narration must sit above the bed while it is speaking (§9.1)."""


def verify_render(
    spec: EditSpec,
    profile: RenderProfile,
    timeline: EditedTimeline,
    focus: FocusPlan,
    render: Path,
    assets: list[OverlayAsset] | None = None,
) -> VerificationReport:
    findings: list[Finding] = []
    findings += check_render_integrity(timeline, render)
    findings += check_loudness(spec, render)
    findings += check_dialogue_to_bed(spec)
    findings += check_captions(timeline, profile)
    findings += check_crop_continuity(focus, profile)
    findings += check_edit_integrity(spec, timeline)
    findings += check_budget(timeline, profile)
    findings += check_cuts_land_between_words(spec)
    findings += check_trim_composition(spec)
    if assets is not None:
        findings += check_overlays(timeline, focus, profile, spec, assets)
    return VerificationReport(
        job_id=spec.job_id, profile=profile.name, render=str(render), findings=findings
    )


def _ok(check: str, message: str, value: float | None = None, limit: float | None = None) -> Finding:
    return Finding(check=check, severity=Severity.PASS, message=message, value=value, limit=limit)


def _fail(check: str, message: str, value: float | None = None, limit: float | None = None) -> Finding:
    return Finding(check=check, severity=Severity.FAIL, message=message, value=value, limit=limit)


# --- the render itself -------------------------------------------------------


def check_render_integrity(timeline: EditedTimeline, render: Path) -> list[Finding]:
    render = Path(render)
    if not render.is_file() or render.stat().st_size == 0:
        return [_fail("render", f"no render at {render}")]
    facts: MediaFacts = probe(render)
    drift = abs(facts.duration - timeline.duration)
    if drift > DURATION_TOLERANCE_S:
        return [
            _fail(
                "render_duration",
                f"render is {facts.duration:.2f}s but the timeline projected {timeline.duration:.2f}s",
                value=drift,
                limit=DURATION_TOLERANCE_S,
            )
        ]
    return [_ok("render_duration", f"{facts.duration:.2f}s, as projected", value=drift, limit=DURATION_TOLERANCE_S)]


def check_loudness(spec: EditSpec, render: Path) -> list[Finding]:
    measured: Loudness | None = measure_loudness(Path(render))
    if measured is None:
        return [Finding(check="loudness", severity=Severity.WARN, message="no audio to measure")]
    findings: list[Finding] = []
    target = spec.audio.target_lufs
    drift = abs(measured.integrated_lufs - target)
    if drift > LOUDNESS_TOLERANCE_LU:
        findings.append(
            _fail("loudness", f"{measured.integrated_lufs:g} LUFS against a target of {target:g}",
                  value=measured.integrated_lufs, limit=target)
        )
    else:
        findings.append(_ok("loudness", f"{measured.integrated_lufs:g} LUFS", value=measured.integrated_lufs, limit=target))

    ceiling = spec.audio.true_peak_ceiling_dbtp
    if measured.true_peak_dbtp > ceiling:
        findings.append(
            _fail("true_peak", f"peaks at {measured.true_peak_dbtp:g} dBTP, above the {ceiling:g} ceiling",
                  value=measured.true_peak_dbtp, limit=ceiling)
        )
    else:
        findings.append(_ok("true_peak", f"{measured.true_peak_dbtp:g} dBTP", value=measured.true_peak_dbtp, limit=ceiling))
    return findings


def check_dialogue_to_bed(spec: EditSpec) -> list[Finding]:
    """Spec arithmetic rather than measurement.

    Separating narration from bed in a finished mix would mean stems; the ratio the
    graph actually applies is `narration_gain - (music_gain + duck)`, which is
    known exactly and is the number that decides whether the bed buries the voice.
    """
    if not spec.audio.music_path:
        return []
    audio = spec.audio
    ratio = audio.narration_gain_db - (audio.music_gain_db + audio.duck_db)
    if ratio < DIALOGUE_TO_BED_MIN_DB:
        return [_fail("dialogue_to_bed", f"narration sits only {ratio:g} dB above the ducked bed",
                      value=ratio, limit=DIALOGUE_TO_BED_MIN_DB)]
    return [_ok("dialogue_to_bed", f"narration sits {ratio:g} dB above the ducked bed",
                value=ratio, limit=DIALOGUE_TO_BED_MIN_DB)]


# --- captions and overlays ---------------------------------------------------


def check_captions(timeline: EditedTimeline, profile: RenderProfile) -> list[Finding]:
    findings: list[Finding] = []
    style = profile.captions

    overlaps = [
        (a, b)
        for a, b in zip(timeline.captions, timeline.captions[1:])
        if b.t_in < a.t_out - TIME_EPS
    ]
    if overlaps:
        a, b = overlaps[0]
        findings.append(_fail("caption_overlap",
                              f"{len(overlaps)} overlapping blocks, first at {a.t_out:.2f}s into {b.t_in:.2f}s",
                              value=len(overlaps)))
    else:
        findings.append(_ok("caption_overlap", "no blocks overlap"))

    short = [c for c in timeline.captions if c.t_out - c.t_in < style.min_display_s - TIME_EPS]
    if short:
        briefest = min(c.t_out - c.t_in for c in short)
        findings.append(Finding(
            check="caption_duration", severity=Severity.WARN,
            message=f"{len(short)} blocks are shorter than {style.min_display_s:g}s, briefest {briefest:.2f}s",
            value=briefest, limit=style.min_display_s,
        ))
    else:
        findings.append(_ok("caption_duration", f"every block holds for {style.min_display_s:g}s or more"))

    longest = 0
    over_lines = 0
    for caption in timeline.captions:
        wrapped = wrap(caption.text, style.max_chars_per_line)
        longest = max(longest, max((len(line) for line in wrapped.split("\\N")), default=0))
        over_lines += line_count(caption, profile) > style.max_lines
    if longest > style.max_chars_per_line:
        findings.append(_fail("caption_line_length",
                              f"a line runs to {longest} characters, over the profile's {style.max_chars_per_line}",
                              value=longest, limit=style.max_chars_per_line))
    else:
        findings.append(_ok("caption_line_length", f"longest line is {longest} characters",
                            value=longest, limit=style.max_chars_per_line))
    if over_lines:
        findings.append(_fail("caption_lines",
                              f"{over_lines} blocks need more than {style.max_lines} lines",
                              value=over_lines, limit=style.max_lines))
    else:
        findings.append(_ok("caption_lines", f"no block needs more than {style.max_lines} lines"))

    if not profile.safe_area.contains(style.box):
        findings.append(_fail("caption_safe_area", "the caption box falls outside the safe area"))
    else:
        findings.append(_ok("caption_safe_area", "the caption box sits inside the safe area"))
    return findings


def check_overlays(
    timeline: EditedTimeline,
    focus: FocusPlan,
    profile: RenderProfile,
    spec: EditSpec,
    assets: list[OverlayAsset],
) -> list[Finding]:
    """Anchors inside the safe area, and nothing sitting on a caption.

    Positions come from the same `view_rects` and `clamp_to_safe_area` the graph
    uses, so the check cannot disagree with the render about where an overlay was.

    Compared in **pixels**, not in normalized coordinates. Placement happens in
    pixels, and an overlay that fits its safe area exactly fails a normalized
    comparison by one floating-point ulp — a failure that looks real, is not, and
    costs more trust than the check earns.
    """
    rects = view_rects(timeline, focus, profile)
    left, top, right, bottom = profile.safe_area.pixels(profile.width, profile.height)
    caption = _pixel_box(profile.captions.box, profile)
    occluding = 0
    outside = 0
    for index, overlay in enumerate(timeline.overlays):
        if index >= len(assets):
            break
        asset = assets[index]
        for frame, rect in _frames_of(overlay, timeline, profile, rects):
            x, y = _overlay_position(overlay, asset, rect, profile)
            box = (x, y, x + asset.width, y + asset.height)
            if x < left or y < top or box[2] > right or box[3] > bottom:
                outside += 1
                break
            if _overlaps(box, caption) and _caption_on_screen(timeline, frame / profile.fps):
                occluding += 1
                break
    findings = []
    if outside:
        findings.append(_fail("overlay_safe_area", f"{outside} overlays leave the safe area", value=outside))
    else:
        findings.append(_ok("overlay_safe_area", "every overlay stays inside the safe area"))
    if occluding:
        findings.append(_fail("overlay_occlusion", f"{occluding} overlays sit on a caption", value=occluding))
    else:
        findings.append(_ok("overlay_occlusion", "no overlay sits on a caption"))
    return findings


def _frames_of(overlay, timeline: EditedTimeline, profile: RenderProfile, rects: list[ViewRect]):
    start = int(overlay.t_in * profile.fps)
    end = min(int(overlay.t_out * profile.fps), len(rects))
    step = max((end - start) // 12, 1)  # sampled: an overlay's path is smooth
    for frame in range(start, end, step):
        yield frame, rects[frame]


def _overlay_position(overlay, asset: OverlayAsset, rect: ViewRect, profile: RenderProfile) -> tuple[int, int]:
    if overlay.anchor is None:
        left, _, _, bottom = profile.safe_area.pixels(profile.width, profile.height)
        return left, bottom - asset.height
    px, py = rect.project(*overlay.anchor, profile.width, profile.height)
    return clamp_to_safe_area(int(round(px)) + asset.dx, int(round(py)) + asset.dy, asset, profile)


def _pixel_box(box: Rect, profile: RenderProfile) -> tuple[int, int, int, int]:
    return (
        round(box.x * profile.width),
        round(box.y * profile.height),
        round(box.right * profile.width),
        round(box.bottom * profile.height),
    )


def _overlaps(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _caption_on_screen(timeline: EditedTimeline, t: float) -> bool:
    return any(c.t_in - TIME_EPS <= t < c.t_out for c in timeline.captions)


# --- motion ------------------------------------------------------------------


def check_crop_continuity(focus: FocusPlan, profile: RenderProfile) -> list[Finding]:
    """Judder is *the* failure mode of automated vertical reframing, it is invisible
    in a still frame, and it is catchable with arithmetic. That is the whole case
    for this check."""
    if not isinstance(focus, CropPathPlan):
        return [_ok("crop_continuity", "no crop path in this profile")]
    ceiling = profile.focus.max_crop_delta_per_frame
    worst = focus.max_step()
    if worst > ceiling + 1e-9:
        return [_fail("crop_continuity", f"the crop moves {worst:.4f} in one frame", value=worst, limit=ceiling)]
    return [_ok("crop_continuity", f"largest step {worst:.4f}", value=worst, limit=ceiling)]


# --- the edit ----------------------------------------------------------------


def check_edit_integrity(spec: EditSpec, timeline: EditedTimeline) -> list[Finding]:
    """It cannot tell you a cut was tasteful. It can tell you the model did not
    hallucinate a moment that was never recorded, and totality means it cannot
    quietly lose one either (§9.1)."""
    decisions = spec.edit
    findings: list[Finding] = []
    if not decisions.segments and not decisions.removals:
        return [Finding(check="edit_integrity", severity=Severity.INFO,
                        message="no edit decisions yet; the whole take survives")]

    covered = decisions.covered_until
    if abs(covered - spec.source.duration) > TIME_EPS:
        findings.append(_fail("edit_totality",
                              f"decisions cover {covered:.3f}s of a {spec.source.duration:.3f}s source",
                              value=covered, limit=spec.source.duration))
    else:
        findings.append(_ok("edit_totality", "every second is removed or in a segment"))

    selected = decisions.selected_duration(timeline.threshold)
    if abs(selected - timeline.duration) > TIME_EPS:
        findings.append(_fail("edit_selection",
                              f"the selected tiers total {selected:.3f}s but the timeline runs {timeline.duration:.3f}s",
                              value=selected, limit=timeline.duration))
    else:
        findings.append(_ok("edit_selection",
                            f"{timeline.threshold.value} and above totals {selected:.2f}s", value=selected))
    return findings


def check_budget(timeline: EditedTimeline, profile: RenderProfile) -> list[Finding]:
    """`essential` alone overrunning is the expected way a profile fails (§4.4.1),
    and it is reported with the overrun in seconds rather than rendering long."""
    if timeline.budget_overrun > TIME_EPS:
        return [_fail("budget",
                      f"{timeline.threshold.value} alone runs {timeline.budget_overrun:.2f}s over "
                      f"the {profile.duration_budget:g}s budget",
                      value=timeline.budget_overrun, limit=0.0)]
    return [_ok("budget", f"{timeline.duration:.2f}s inside a {profile.duration_budget:g}s budget",
                value=timeline.duration, limit=profile.duration_budget)]


def check_cuts_land_between_words(spec: EditSpec) -> list[Finding]:
    """A cut through a word clips it. Invisible in a still frame, audible
    immediately — the failure §9.2 was written to catch, caught here for free
    because the spec already carries per-word timings (§6.2)."""
    words = [w for block in spec.captions for w in block.words]
    if not words or not spec.edit.removals:
        return []
    offenders = [
        (removal, word, edge)
        for removal in spec.edit.removals
        for word in words
        for edge in (removal.t_in, removal.t_out)
        if word.t_in + TIME_EPS < edge < word.t_out - TIME_EPS
    ]
    if offenders:
        removal, word, edge = offenders[0]
        return [_fail("cut_mid_word",
                      f"{len(offenders)} cut edges land inside a word, first at {edge:.3f}s inside {word.text!r}",
                      value=len(offenders))]
    return [_ok("cut_mid_word", "every cut lands between words")]


def check_trim_composition(spec: EditSpec) -> list[Finding]:
    """Not a verdict; a number on the report (§9.1).

    The true override rate needs `trim`'s proposal to compare against, which
    arrives with phase 5. What is computable now is who proposed what — and a
    model that stops keeping any of `trim`'s removals is worth seeing before the
    learner starts averaging over it.
    """
    removals = spec.edit.removals
    if not removals:
        return []
    from spec.origin import Stage

    from_trim = sum(1 for r in removals if r.proposed_by is Stage.TRIM)
    return [Finding(
        check="trim_composition", severity=Severity.INFO,
        message=f"{from_trim} of {len(removals)} removals came from trim, "
                f"{len(removals) - from_trim} from plan_edit",
        value=from_trim / len(removals),
    )]
