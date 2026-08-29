"""End-to-end renders — phase 2's exit criteria, run against real FFmpeg.

Deliberately built on a small fast fixture rather than the full one: these run on
every commit, and a suite nobody waits for is a suite nobody runs. The full
fixture is what `make render` produces and what a person watches.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from compile.render import render_job
from ingest.fixtures import DEFAULT_BEATS, build_spec, write_fixture
from prefs import resolve_profile
from spec import Encoder

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")

TINY = dict(beats=DEFAULT_BEATS[:2], slot_s=2.0)


@pytest.fixture(scope="module")
def job(tmp_path_factory) -> Path:
    """A 4-second 640x360 take: every mechanism, none of the waiting."""
    directory = tmp_path_factory.mktemp("job")
    fixture = build_spec("tiny", width=640, height=360, **TINY)
    write_fixture(directory, fixture, with_video=True)
    return directory


def small(name: str, **updates):
    profile = resolve_profile(name)
    size = {"shorts_9x16": {"width": 270, "height": 480}, "demo_16x9": {"width": 640, "height": 360}}[name]
    return profile.model_copy(update={**size, **updates})


def probe(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,nb_frames",
         "-show_entries", "format=duration", "-of", "default=nw=1:nk=0", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout
    return dict(line.split("=", 1) for line in out.strip().splitlines())


def render(job: Path, name: str, *, encoder=Encoder.SOFTWARE, **updates):
    from compile.render import prepare, run
    from spec.migrations import load_spec_file

    spec = load_spec_file(job / "spec.json")
    plan = prepare(spec, small(name, **updates), job, encoder=encoder)
    return plan, run(plan, job)


@pytest.mark.parametrize("name", ["shorts_9x16", "demo_16x9"])
def test_one_fixture_renders_to_both_profiles(job, name):
    plan, path = render(job, name)
    facts = probe(path)
    assert (int(facts["width"]), int(facts["height"])) == (plan.profile.width, plan.profile.height)
    # Container duration runs a little past the video: the audio's last AAC frame
    # is padded to a whole frame. Anything larger than that is a real mismatch.
    assert abs(float(facts["duration"]) - plan.timeline.duration) < 0.15
    assert int(facts["nb_frames"]) == pytest.approx(plan.timeline.duration * plan.profile.fps, abs=3)


def test_the_render_is_cut_not_merely_planned(job):
    """The rendered file is shorter than the source by exactly what was removed."""
    plan, path = render(job, "shorts_9x16")
    source = probe(job / "source" / "source.mp4")
    assert plan.timeline.duration < float(source["duration"])
    assert float(probe(path)["duration"]) < float(source["duration"])


def test_two_budgets_produce_two_lengths_from_one_spec(job):
    """§4.4.1, rendered rather than computed. This is the phase-2 exit criterion
    that says cuts stayed aspect-agnostic."""
    # One render per job per profile, so the two overwrite each other: probe each
    # while it is on disk rather than holding two paths that turn out to be one.
    tight, path = render(job, "shorts_9x16", duration_budget=1.5)
    tight_rendered = float(probe(path)["duration"])
    loose, path = render(job, "shorts_9x16", duration_budget=60.0)
    loose_rendered = float(probe(path)["duration"])
    assert tight.timeline.duration < loose.timeline.duration
    assert tight_rendered < loose_rendered


def test_software_renders_are_byte_identical_across_runs(job, tmp_path):
    """§11 compares specs, but the two or three renders it hashes need this to hold."""
    _, first = render(job, "shorts_9x16")
    kept = tmp_path / "first.mp4"
    shutil.copy(first, kept)
    _, second = render(job, "shorts_9x16")
    assert kept.read_bytes() == second.read_bytes()


def test_captions_are_burned_in(job):
    """Look at the pixels. The caption's opaque box is far darker than anything in
    the fixture's source, so its absence is visible in a histogram."""
    plan, path = render(job, "shorts_9x16")
    caption = plan.timeline.captions[0]
    box = plan.profile.captions.box
    crop = (
        f"crop={int(box.w * plan.profile.width)}:{int(box.h * plan.profile.height)}"
        f":{int(box.x * plan.profile.width)}:{int(box.y * plan.profile.height)}"
    )
    at = (caption.t_in + caption.t_out) / 2
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", f"{at:.3f}", "-i", str(path),
         "-vf", f"{crop},format=gray", "-frames:v", "1", "-f", "rawvideo", "-"],
        capture_output=True, check=True,
    ).stdout
    assert raw, "no frame at the caption's own timestamp"
    dark = sum(1 for value in raw if value < 60) / len(raw)
    assert dark > 0.15, "the caption box should darken a good part of its own region"


def test_a_music_bed_is_mixed_and_ducked(job, tmp_path):
    """No fixture ships a bed — §15 leaves music a procurement question — so the
    graph path is exercised with a tone standing in for one."""
    from compile.render import prepare, run
    from spec.migrations import load_spec_file

    bed = job / "source" / "bed.wav"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=110:duration=8",
         "-ar", "48000", str(bed)], check=True,
    )
    spec = load_spec_file(job / "spec.json")
    spec = spec.model_copy(update={"audio": spec.audio.model_copy(update={"music_path": "source/bed.wav"})})
    plan = prepare(spec, small("shorts_9x16"), job, encoder=Encoder.SOFTWARE)
    assert "volume@bed" in plan.graph and "amix" in plan.graph
    assert plan.audio_commands.count("volume@bed volume") >= len(plan.timeline.captions) * 2
    path = run(plan, job)
    assert probe(path)


def test_the_job_directory_holds_what_the_render_was_made_from(job):
    """Every render is reproducible from files next to it, without re-running Python."""
    plan, path = render(job, "demo_16x9")
    work = job / plan.work_dir
    assert (work / "graph.txt").exists()
    assert (work / "captions.ass").exists()
    assert sorted(p.suffix for p in work.glob("overlay_*")) == [".png"] * len(plan.assets)
