"""`EditSpec` + `RenderProfile` -> an FFmpeg filter graph (architecture.md §6.1).

FFmpeg is the only renderer. This module turns the projected timeline and the
focus plan into a graph, and it is the one place in the system allowed to know
what a pixel is.

The shape of the graph, in order:

    trim each surviving span -> concat -> normalize fps
      -> move the frame (crop path, or zoompan for zoom keyframes)
      -> composite overlays -> fill the progress pill -> burn captions
    trim the same spans of audio -> concat -> duck the bed -> loudness

Two things are worth knowing before reading it.

**Cuts happen first.** Trimming before the spatial work means the expensive scale
runs on the frames that survive rather than on all of them, which on the target
machine (§16) is most of the render time.

**Motion is data, not expression.** The crop path and the overlay positions are
computed per frame in Python and delivered through `sendcmd`, because they are
sampled paths and an FFmpeg expression is a poor container for a sampled path.
Zoom is the exception: its regions are few and its shape is analytic, so it stays
an expression and `zoompan` can hold the varying window size that `crop` cannot.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from plan.focus import CropPathPlan, FocusPlan, ZoomPlan, _trapezoid
from spec.editspec import EditSpec
from spec.profiles import Encoder, FocusMode, RenderProfile

from compile.captions import render_ass
from compile.overlays import OverlayAsset, render_asset
from compile.timeline import EditedTimeline

DUCK_RAMP_S = 0.2
"""How long the bed takes to get out of the way of a word, and to come back."""

PILL_COLOR = "0x4C9AFF@0.95"


@dataclass(frozen=True)
class ViewRect:
    """The region of the source frame visible in one output frame, normalized."""

    x: float
    y: float
    w: float
    h: float

    def project(self, ax: float, ay: float, width: int, height: int) -> tuple[float, float]:
        """A normalized source point, in output pixels."""
        return (ax - self.x) / self.w * width, (ay - self.y) / self.h * height


@dataclass
class RenderPlan:
    """Everything needed to run one render, and nothing that needs running to get."""

    profile: RenderProfile
    timeline: EditedTimeline
    focus: FocusPlan
    encoder: Encoder
    work_dir: Path
    out_path: Path
    graph: str
    commands: str
    audio_commands: str
    ass: str
    assets: list[OverlayAsset] = field(default_factory=list)
    ffmpeg_args: list[str] = field(default_factory=list)


# --- per-frame geometry ------------------------------------------------------


def view_rects(timeline: EditedTimeline, focus: FocusPlan, profile: RenderProfile) -> list[ViewRect]:
    """What the camera sees, per output frame.

    One table serves three consumers — the crop commands, the overlay positions,
    and (in tests) the judder check — so they cannot disagree about where the
    frame was pointing.
    """
    frames = frame_count(timeline, profile)
    rects: list[ViewRect] = []
    zoom_regions = output_zoom_regions(focus, timeline) if isinstance(focus, ZoomPlan) else []
    for index in range(frames):
        output_t = index / profile.fps
        if isinstance(focus, CropPathPlan):
            cx, cy = focus.center_at(timeline.source_at(output_t))
            w, h = focus.window_w, focus.window_h
        else:
            zoom, cx, cy = _zoom_at(zoom_regions, focus.ease, output_t)
            w = h = 1.0 / zoom
        rects.append(
            ViewRect(
                x=min(max(cx - w / 2, 0.0), 1.0 - w),
                y=min(max(cy - h / 2, 0.0), 1.0 - h),
                w=w,
                h=h,
            )
        )
    return rects


def frame_count(timeline: EditedTimeline, profile: RenderProfile) -> int:
    return max(int(round(timeline.duration * profile.fps)), 1)


@dataclass(frozen=True)
class OutputZoomRegion:
    t_in: float
    t_out: float
    cx: float
    cy: float
    zoom: float


def output_zoom_regions(plan: ZoomPlan, timeline: EditedTimeline) -> list[OutputZoomRegion]:
    """Zoom regions projected into output time, split where a cut runs through one.

    The same intersect-with-surviving-spans operation the overlays get. A region
    that fell entirely inside removed footage simply does not appear, which is the
    correct answer and needs no special case.
    """
    out: list[OutputZoomRegion] = []
    for region in plan.regions:
        for span in timeline.spans:
            start = max(region.t_in, span.source_in)
            end = min(region.t_out, span.source_out)
            if end - start <= plan.ease:
                continue  # too short to ease into and back out of
            out.append(
                OutputZoomRegion(
                    t_in=span.to_output(start),
                    t_out=span.to_output(end),
                    cx=region.cx,
                    cy=region.cy,
                    zoom=region.zoom,
                )
            )
    return out


def _zoom_at(regions: list[OutputZoomRegion], ease: float, t: float) -> tuple[float, float, float]:
    total = 0.0
    zoom, cx, cy = 1.0, 0.0, 0.0
    for region in regions:
        weight = _trapezoid(t, region.t_in, region.t_out, ease)
        if weight <= 0.0:
            continue
        total += weight
        zoom += (region.zoom - 1.0) * weight
        cx += region.cx * weight
        cy += region.cy * weight
    if total <= 0.0:
        return 1.0, 0.5, 0.5
    rest = max(0.0, 1.0 - total)
    return zoom, cx + 0.5 * rest, cy + 0.5 * rest


def zoom_expressions(regions: list[OutputZoomRegion], ease: float) -> tuple[str, str, str]:
    """The same trapezoid as `_zoom_at`, as FFmpeg expressions over `in_time`.

    Analytic here rather than sampled because a handful of eased regions is what an
    expression is good at, and because `zoompan` takes no commands — the reason the
    two focus modes use two mechanisms at all.
    """
    if not regions:
        return "1", "0", "0"
    weights = [
        f"clip(min((in_time-{r.t_in - ease:.4f})/{ease:.4f},({r.t_out + ease:.4f}-in_time)/{ease:.4f}),0,1)"
        for r in regions
    ]
    total = "+".join(weights)
    zoom = "1" + "".join(f"+{r.zoom - 1.0:.4f}*{w}" for r, w in zip(regions, weights))
    centre_x = "+".join(f"{r.cx:.4f}*{w}" for r, w in zip(regions, weights))
    centre_y = "+".join(f"{r.cy:.4f}*{w}" for r, w in zip(regions, weights))
    cx = f"({centre_x}+0.5*max(0,1-({total})))"
    cy = f"({centre_y}+0.5*max(0,1-({total})))"
    x = f"clip({cx}*iw-iw/(2*zoom),0,iw-iw/zoom)"
    y = f"clip({cy}*ih-ih/(2*zoom),0,ih-ih/zoom)"
    return zoom, x, y


# --- command streams ---------------------------------------------------------


def _emit(lines: list[str], time: float, command: str) -> None:
    lines.append(f"{time:.4f} {command};")


def build_commands(
    timeline: EditedTimeline,
    rects: list[ViewRect],
    profile: RenderProfile,
    spec: EditSpec,
    assets: dict[int, OverlayAsset],
) -> str:
    """The `sendcmd` script: crop position, overlay positions, pill fill.

    A command is emitted only when its value changes. That is not only smaller —
    it makes the file readable, so a wrong path is something you can see rather
    than something you have to bisect a render for.
    """
    lines: list[str] = []
    source_w, source_h = spec.source.width, spec.source.height
    crop_mode = profile.focus.mode is FocusMode.CROP_PATH
    last: dict[str, int] = {}

    def changed(key: str, value: int) -> bool:
        if last.get(key) == value:
            return False
        last[key] = value
        return True

    for index, rect in enumerate(rects):
        t = index / profile.fps
        if crop_mode:
            x = int(round(rect.x * source_w))
            y = int(round(rect.y * source_h))
            if changed("crop_x", x):
                _emit(lines, t, f"crop@focus x {x}")
            if changed("crop_y", y):
                _emit(lines, t, f"crop@focus y {y}")

        for overlay_index, overlay in enumerate(timeline.overlays):
            asset = assets[overlay_index]
            if overlay.spans_whole_output:
                width = int(round(asset.width * min(t / timeline.duration, 1.0)))
                if changed("pill_w", width):
                    _emit(lines, t, f"drawbox@pill w {max(width, 1)}")
                continue
            if not (overlay.t_in <= t < overlay.t_out) or overlay.anchor is None:
                continue
            px, py = rect.project(*overlay.anchor, profile.width, profile.height)
            x, y = clamp_to_safe_area(int(round(px)) + asset.dx, int(round(py)) + asset.dy, asset, profile)
            if changed(f"o{overlay_index}_x", x):
                _emit(lines, t, f"overlay@o{overlay_index} x {x}")
            if changed(f"o{overlay_index}_y", y):
                _emit(lines, t, f"overlay@o{overlay_index} y {y}")
    return "\n".join(lines) + ("\n" if lines else "")


def clamp_to_safe_area(x: int, y: int, asset: OverlayAsset, profile: RenderProfile) -> tuple[int, int]:
    """Keep an overlay inside the profile's safe area (§9.1).

    An overlay follows the point it labels, and a followed point can leave the
    frame. Clamping keeps it visible and keeps the deterministic check honest;
    the alternative — letting it slide off — is a check that fails on footage that
    looked fine.
    """
    left = int(profile.safe_area.left * profile.width)
    top = int(profile.safe_area.top * profile.height)
    right = int((1.0 - profile.safe_area.right) * profile.width) - asset.width
    bottom = int((1.0 - profile.safe_area.bottom) * profile.height) - asset.height
    return min(max(x, left), max(right, left)), min(max(y, top), max(bottom, top))


def build_audio_commands(timeline: EditedTimeline, spec: EditSpec) -> str:
    """Duck the bed under narration, from the word timings we already have.

    Level measurement would work too and a compressor would work approximately;
    this is arithmetic over data the spec already carries, which makes `duck_db`
    mean the number it says rather than a setting that produces roughly that.
    """
    duck = spec.audio.duck_db
    lines: list[str] = []
    for caption in timeline.captions:
        start, end = caption.t_in, caption.t_out
        for step in range(4):
            fraction = (step + 1) / 4
            _emit(lines, max(start - DUCK_RAMP_S * (1 - fraction), 0.0),
                  f"volume@bed volume {duck * fraction:.2f}dB")
        for step in range(4):
            fraction = (step + 1) / 4
            _emit(lines, end + DUCK_RAMP_S * fraction, f"volume@bed volume {duck * (1 - fraction):.2f}dB")
    return "\n".join(lines) + ("\n" if lines else "")


# --- the graph ---------------------------------------------------------------


def video_chain(spec: EditSpec, profile: RenderProfile, timeline: EditedTimeline, focus: FocusPlan,
                assets: list[OverlayAsset], overlay_input_base: int,
                commands_name: str) -> tuple[list[str], str]:
    parts: list[str] = []
    if isinstance(focus, CropPathPlan):
        crop_w = _even(focus.window_w * spec.source.width)
        crop_h = _even(focus.window_h * spec.source.height)
        spatial = (
            f"sendcmd=f={commands_name},"
            f"crop@focus=w={crop_w}:h={crop_h}:x=0:y=0,"
            f"scale={profile.width}:{profile.height}:flags=bicubic"
        )
    else:
        zoom, x, y = zoom_expressions(output_zoom_regions(focus, timeline), focus.ease)
        spatial = (
            f"scale={profile.width}:{profile.height}:flags=bicubic,"
            f"sendcmd=f={commands_name},"
            f"zoompan=z='{zoom}':x='{x}':y='{y}':"
            f"d=1:s={profile.width}x{profile.height}:fps={profile.fps:g}"
        )
    parts.append(f"[vc]fps={profile.fps:g},{spatial},setsar=1[vfx]")

    label = "vfx"
    for index, (overlay, asset) in enumerate(zip(timeline.overlays, assets)):
        nxt = f"ov{index}"
        if overlay.spans_whole_output:
            x = int(profile.safe_area.left * profile.width)
            y = int((1.0 - profile.safe_area.bottom) * profile.height) - asset.height
            parts.append(f"[{label}][{overlay_input_base + index}:v]overlay@o{index}=x={x}:y={y}[{nxt}]")
            label = nxt
            if asset.fill_rect:
                fx, fy, _, fh = asset.fill_rect
                parts.append(
                    f"[{label}]drawbox@pill=x={x + fx}:y={y + fy}:w=1:h={fh}:"
                    f"color={PILL_COLOR}:t=fill[pill{index}]"
                )
                label = f"pill{index}"
            continue
        parts.append(
            f"[{label}][{overlay_input_base + index}:v]overlay@o{index}=x=0:y=0:"
            f"enable='between(t,{overlay.t_in:.3f},{overlay.t_out:.3f})'[{nxt}]"
        )
        label = nxt
    return parts, label


def audio_chain(spec: EditSpec, music_input: int | None, audio_commands_name: str) -> tuple[list[str], str]:
    parts: list[str] = []
    if music_input is None:
        source = "ac"
    else:
        parts.append(
            f"[{music_input}:a]volume@bed=volume={spec.audio.music_gain_db:g}dB,"
            f"asendcmd=f={audio_commands_name}[bed]"
        )
        parts.append(f"[ac][bed]amix=inputs=2:duration=first:normalize=0[mixed]")
        source = "mixed"
    parts.append(
        f"[{source}]volume={spec.audio.narration_gain_db:g}dB,"
        f"loudnorm=I={spec.audio.target_lufs:g}:TP={spec.audio.true_peak_ceiling_dbtp:g}:LRA=11,"
        f"aresample=48000[aout]"
    )
    return parts, "aout"


def build_graph(spec: EditSpec, profile: RenderProfile, timeline: EditedTimeline, focus: FocusPlan,
                assets: list[OverlayAsset], ass_name: str, commands_name: str,
                audio_commands_name: str, music_input: int | None) -> str:
    parts: list[str] = []
    labels: list[str] = []
    for index, span in enumerate(timeline.spans):
        parts.append(
            f"[0:v]trim=start={span.source_in:.6f}:end={span.source_out:.6f},setpts=PTS-STARTPTS[v{index}]"
        )
        parts.append(
            f"[0:a]atrim=start={span.source_in:.6f}:end={span.source_out:.6f},asetpts=PTS-STARTPTS[a{index}]"
        )
        labels.extend([f"[v{index}]", f"[a{index}]"])
    parts.append(f"{''.join(labels)}concat=n={len(timeline.spans)}:v=1:a=1[vc][ac]")

    overlay_base = 1 if music_input is None else 2
    video, video_label = video_chain(spec, profile, timeline, focus, assets, overlay_base, commands_name)
    parts.extend(video)
    parts.append(f"[{video_label}]ass=f={ass_name}[vout]")
    audio, _ = audio_chain(spec, music_input, audio_commands_name)
    parts.extend(audio)
    return ";\n".join(parts) + "\n"


def encode_args(profile: RenderProfile, encoder: Encoder) -> list[str]:
    """Two encoders, two quality scales (§16).

    Hardware for everyday renders — it is the render-time win on Apple Silicon and,
    on a fanless machine, a thermal one. Software for the golden set, because
    hardware encoders are not bit-reproducible across machines or OS versions and
    §11 hashes frames.
    """
    encode = profile.encode
    if encoder is Encoder.SOFTWARE:
        video = [
            "-c:v", "libx264",
            "-preset", encode.preset,
            "-crf", str(encode.crf),
            "-fflags", "+bitexact",
            "-flags", "+bitexact",
        ]
    else:
        video = ["-c:v", f"{encode.video_codec}_videotoolbox", "-q:v", str(encode.quality)]
    return [
        *video,
        "-pix_fmt", encode.pix_fmt,
        "-c:a", encode.audio_codec,
        "-b:a", f"{encode.audio_bitrate_kbps}k",
        "-ar", "48000",
        "-map_metadata", "-1",
    ]


def _even(value: float) -> int:
    """Encoders want even dimensions, and yuv420p requires them."""
    return max(int(round(value / 2)) * 2, 2)
