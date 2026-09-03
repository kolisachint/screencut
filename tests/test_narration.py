"""Narration from a script, in your own voice (phase 8, decision #20).

Two halves, and they are tested differently.

**The arithmetic** — `synth/align.py` — is tested directly, because forced
alignment is exactly the kind of thing that looks right in a render and is wrong
by 200 ms. Its job is to put the *script's* words on the *audio's* timings, and
the case that matters most is the one where the two disagree.

**The path** — script in, captioned video out — runs against stand-ins for both
backends, in the shape phase 5 used for the agent and phase 6 for ASR. F5-TTS is
not installed here and, per phase 0, is not something to run here even when it
is (environment findings §4). What that proves is our code end to end: the
invocation, the file handling, the cache, the graph and the round-trip. What it
cannot prove is that a cloned voice sounds like you, or that the model pronounces
"kubectl" — and the second of those is precisely what §9.2 is pointed at, which
is why the mispronunciation test below drives the check rather than the voice.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from ingest.narrated_fixture import build_narrated_spec, write_narrated_fixture
from prefs import load_constraints, resolve_profile
from runner.pipeline import run_job
from runner.stages import JOB_STAGES, RECORDED_STAGES, SYNTHESIZED_STAGES
from spec import Encoder
from spec.editspec import EditSpec
from spec.migrations import load_spec_file
from spec.narration import NarrationSource
from synth.align import MIN_WORD_S, align, script_words
from synth.asr import TranscribedWord
from synth.tts import TtsUnavailable, infer_environment, synthesize
from verify.report import Severity
from verify.transcript import RoundTrip

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")

PROFILE = "demo_16x9"
"""One profile. What this phase adds is job-level — a narration is one narration
whatever it is rendered into (§4.1) — so the second profile would render the same
audio twice and test nothing new."""


def heard(*pairs: tuple[float, float, str]) -> list[TranscribedWord]:
    return [TranscribedWord(t_in=a, t_out=b, text=c) for a, b, c in pairs]


# --- alignment ---------------------------------------------------------------


def test_the_script_wins_the_word_and_the_audio_wins_the_timing():
    """The one rule the whole module exists to enforce.

    Take whisper's word instead and §9.2 is comparing the render against a
    transcript of itself: the round-trip would agree perfectly about a word the
    narration got wrong, which is the failure the round-trip is for.
    """
    words = align(
        "the top right corner",
        heard((0.0, 0.2, "the"), (0.2, 0.5, "top"), (0.5, 0.8, "write"), (0.8, 1.4, "corner")),
        1.5,
    ).words

    assert [w.text for w in words] == ["the", "top", "right", "corner"]
    assert (words[2].t_in, words[2].t_out) == (0.5, 0.8), "the mishearing kept its timing"


def test_punctuation_is_captioned_and_ignored_when_matching():
    """A caption reads "corner." and the anchor is still whisper's "corner"."""
    words = align("the corner.", heard((0.0, 0.3, "the"), (0.3, 0.9, "corner")), 1.0).words
    assert [w.text for w in words] == ["the", "corner."]
    assert words[1].t_out == 0.9


def test_a_run_nobody_heard_is_spread_across_the_gap_it_sits_in():
    """Interpolation between bracketing anchors, weighted by word length.

    An unheard run is ordinary — whisper drops a word in noise — and the words
    still need timings a caption can be built from."""
    words = align(
        "one two three four five",
        heard((0.0, 1.0, "one"), (4.0, 5.0, "five")),
        5.0,
    ).words

    assert [round(w.t_in, 2) for w in words][0] == 0.0
    assert words[-1].t_out == 5.0
    assert all(a.t_out <= b.t_in + 1e-9 for a, b in zip(words, words[1:])), "ascending, no overlap"
    assert words[1].t_in >= 1.0 and words[3].t_out <= 4.0, "the gap, and only the gap"


def test_nothing_recognized_spreads_the_script_across_the_audio():
    """Coverage 0 is a number, not an error. Silent narration is a real state and
    the words still have to land somewhere inside it."""
    alignment = align("one two three", [], 3.0)
    assert alignment.coverage == 0.0
    assert alignment.words[0].t_in == 0.0
    assert alignment.words[-1].t_out == 3.0


def test_alignment_never_runs_past_the_end_of_the_audio():
    """The floor on word width can push the tail past the end, and a caption after
    the last frame fails `EditSpec._within_source` two stages later, where the
    cause is no longer visible."""
    script = " ".join(f"w{i}" for i in range(40))
    words = align(script, heard((0.0, 0.9, "w0")), 1.0).words

    assert words[-1].t_out <= 1.0 + 1e-9
    assert all(w.t_out >= w.t_in for w in words)
    assert len(words) == len(script_words(script))


def test_a_word_never_gets_a_zero_width_window():
    """`plan_captions` divides by a word's duration, and two words at one instant
    are a block that cannot be laid out."""
    words = align("alpha bravo", heard((1.0, 1.0, "alpha"), (1.0, 1.0, "bravo")), 3.0).words
    assert all(w.t_out - w.t_in >= MIN_WORD_S - 1e-9 for w in words)


# --- the TTS invocation ------------------------------------------------------

FAKE_TTS = '''#!/usr/bin/env python3
"""Stands in for a Python with f5_tts installed.

It is handed the script `synth/tts.py` writes and ignores it, then does what the
real one does from the caller's side: writes a wav where it was told to and
prints the one line the caller parses. `control.json` beside it scripts the two
failure shapes phase 0 actually saw.
"""
import json, math, struct, sys, wave
from pathlib import Path

here = Path(__file__).resolve().parent
control = json.loads((here / "control.json").read_text()) if (here / "control.json").is_file() else {}
device, reference, reference_text, out_wav, text_file = sys.argv[2:7]
words = Path(text_file).read_text().split()
seconds = max(len(words) / float(control.get("words_per_second", 2.8)), 0.4)

(here / "asked.json").write_text(json.dumps(
    {"device": device, "reference_text": reference_text, "words": len(words),
     "hash_seed": __import__("os").environ.get("PYTHONHASHSEED")}
))

if not control.get("write_nothing"):
    rate = 24000
    with wave.open(out_wav, "w") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"".join(
            struct.pack("<h", int(12000 * math.sin(2 * math.pi * 180 * n / rate)))
            for n in range(int(seconds * rate))
        ))
    print("RESULT " + json.dumps({"infer_seconds": 1.0, "duration": round(seconds, 3)}))

raise SystemExit(control.get("exit", 0))
'''


@pytest.fixture
def tts_stand_in(tmp_path, monkeypatch):
    """Install a fake TTS interpreter and return a handle to script it."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    script = bin_dir / "fake-tts-python"
    script.write_text(FAKE_TTS)
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    class Handle:
        binary = str(script)

        def behaves(self, **control) -> None:
            (bin_dir / "control.json").write_text(json.dumps(control))

        @property
        def asked(self) -> dict:
            return json.loads((bin_dir / "asked.json").read_text())

    return Handle()


@pytest.fixture
def reference(tmp_path) -> Path:
    """A reference clip long enough for `synth/tts.py` to accept it."""
    path = tmp_path / "voice.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", "sine=frequency=180:sample_rate=24000:duration=8",
         "-ac", "1", "-c:a", "pcm_s16le", str(path)],
        check=True,
    )
    return path


def test_the_subprocess_environment_pins_the_hash_seed():
    """Environment findings §4: F5-TTS's teardown dies without it and takes the
    exit code with it, which §7.4 would read as a failed stage on every
    successful synthesis."""
    assert infer_environment({}, None)["PYTHONHASHSEED"] == "0"
    assert "DYLD_FALLBACK_LIBRARY_PATH" not in infer_environment({}, None)
    assert infer_environment({}, "/opt/homebrew/lib")["DYLD_FALLBACK_LIBRARY_PATH"] == "/opt/homebrew/lib"


@needs_ffmpeg
def test_a_synthesis_is_judged_by_its_audio_not_by_its_exit_code(tmp_path, tts_stand_in, reference):
    """The hazard that made phase 0's first TTS run record F5-TTS as broken on all
    three device paths. The harness was wrong, not the machine."""
    tts_stand_in.behaves(exit=1)
    result = synthesize(
        "one two three four",
        reference=reference,
        reference_text="this is my voice",
        out_wav=tmp_path / "narration.wav",
        python=tts_stand_in.binary,
    )
    assert result.duration > 0
    assert not result.clean_exit, "and it says so, because the next PyTorch may make it real"
    assert tts_stand_in.asked["hash_seed"] == "0"


@needs_ffmpeg
def test_a_synthesis_that_wrote_no_audio_is_unavailable_however_it_exited(
    tmp_path, tts_stand_in, reference
):
    with pytest.raises(TtsUnavailable, match="produced no audio"):
        tts_stand_in.behaves(write_nothing=True)
        synthesize(
            "one two three",
            reference=reference,
            reference_text="this is my voice",
            out_wav=tmp_path / "narration.wav",
            python=tts_stand_in.binary,
        )


@needs_ffmpeg
def test_synthesis_without_a_reference_names_the_decision_that_forbids_it(
    tmp_path, tts_stand_in
):
    """Decision #20 permits synthesis of you and nobody else, and §1.1 says that
    is a schema-and-config matter. The schema half is `spec/narration.py`; this is
    the half that refuses to run."""
    with pytest.raises(TtsUnavailable, match="Decision #20"):
        synthesize(
            "one two three",
            reference=tmp_path / "not-here.wav",
            reference_text="this is my voice",
            out_wav=tmp_path / "narration.wav",
            python=tts_stand_in.binary,
        )


# --- the path ----------------------------------------------------------------

SCRIPT = "The export button writes exactly what is on screen, filters included."


PLAN = {
    "removals": [],
    "segments": [
        {"t_in": 0.0, "t_out": 4.0, "tier": "essential", "reason": "the claim"},
        {"t_in": 4.0, "t_out": 8.0, "tier": "supporting", "reason": "the walkthrough"},
    ],
}
"""What the model says about a read script: nothing to remove, and a ranking.

The empty `removals` is the shape of exit criterion three — there are no
disfluencies in a script somebody wrote — and it is here rather than left to the
degradation path because a degraded artifact is deliberately not cached (§7.4),
which would make the cache tests below untestable for the wrong reason."""


@pytest.fixture
def narrated(tmp_path, tts_stand_in, whisper_stand_in, fake_agent, monkeypatch):
    """A narrated job wired to all three stand-ins, ready to run.

    The ASR stand-in is asked to hear the script back verbatim, which is what a
    correct synthesis sounds like. It serves both of §5.3's calls in this job —
    `align` listening to the narration and `verify_transcript` listening to the
    render — and that is not a shortcut: nothing is cut from this narration, so
    the render says exactly what the narration said.
    """
    job = tmp_path / "narrated"
    fixture = build_narrated_spec(
        "narrated-test", width=320, height=180, slot_s=2.0, script=SCRIPT
    )
    write_narrated_fixture(job, fixture)
    fake_agent.fragments(EditPlan={"text": f"```json\n{json.dumps(PLAN)}\n```"})

    constraints = load_constraints().model_copy(deep=True)
    constraints.asr.models_dir = str(tmp_path)
    constraints.tts.python = tts_stand_in.binary
    monkeypatch.setattr("runner.pipeline.load_constraints", lambda: constraints)

    def spoken(seconds_per_word: float = 1 / 2.8):
        return [
            TranscribedWord(
                t_in=index * seconds_per_word,
                t_out=(index + 1) * seconds_per_word - 0.01,
                text=word.strip(".,"),
            )
            for index, word in enumerate(SCRIPT.split())
        ]

    whisper_stand_in(spoken())

    class Handle:
        dir = job
        hears = staticmethod(lambda words: whisper_stand_in(words))
        script = SCRIPT
        as_spoken = staticmethod(spoken)

    return Handle()


def go(job: Path, database: Path):
    return run_job(job, [PROFILE], db_path=database, encoder=Encoder.SOFTWARE)


@needs_ffmpeg
def test_a_script_becomes_a_narrated_captioned_video(narrated, tmp_path):
    """Phase 8's first exit criterion, end to end: script in, narrated and
    captioned video out."""
    result = go(narrated.dir, tmp_path / "screencut.db")

    assert [o.stage for o in result.outcomes if o.profile == "job"] == list(SYNTHESIZED_STAGES)
    render = result.renders[PROFILE]
    assert render.is_file()

    spec = load_spec_file(narrated.dir / "spec.json")
    assert spec.narration.source is NarrationSource.SYNTHESIZED
    assert spec.narration.audio_path, "the wav `tts` wrote is on the spec, not only in stages/"
    assert (narrated.dir / spec.narration.audio_path).is_file()
    assert spec.transcript == narrated.script, "the captions are the script, word for word"

    # Every stage says what it did, and these two are the phase's own numbers: what
    # the most expensive stage cost, and how much of the script the audio actually
    # accounted for. They were computed and thrown away before this phase.
    notes = {o.stage: o.note for o in result.outcomes}
    assert "realtime" in (notes["tts"] or "")
    assert "anchored" in (notes["align"] or "")

    heard_track = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
         "stream=codec_type", "-of", "csv=p=0", str(render)],
        capture_output=True, text=True, check=True,
    ).stdout
    assert "audio" in heard_track, "a silent screen capture came out with a voice on it"


@needs_ffmpeg
def test_the_round_trip_hears_the_script_in_the_render(narrated, tmp_path):
    """Phase 8's second exit criterion. §9.2 came first for exactly this reason:
    it is the check that listens to a synthesized narration and says whether it
    read the script it was given."""
    result = go(narrated.dir, tmp_path / "screencut.db")
    report = result.reports[PROFILE]

    finding = next(f for f in report.findings if f.check == "transcript_round_trip")
    assert finding.severity is Severity.PASS, finding.message
    assert report.passed, [f.message for f in report.failures]


@needs_ffmpeg
def test_a_mispronounced_word_is_a_real_difference_and_not_a_seam(narrated, tmp_path):
    """The failure the criterion is actually about. TTS says "filters" as
    "fitters"; the captions say what the script says; §9.2 hears the difference.

    Nothing else in this pipeline can catch it: the frame is correct, the timing
    is correct, and the spec is correct. Only listening to the render finds it.
    """
    go(narrated.dir, tmp_path / "screencut.db")

    mispronounced = narrated.as_spoken()
    wrong = next(i for i, w in enumerate(mispronounced) if w.text == "filters")
    mispronounced[wrong] = TranscribedWord(
        t_in=mispronounced[wrong].t_in, t_out=mispronounced[wrong].t_out, text="fitters"
    )
    narrated.hears(mispronounced)

    result = run_job(
        narrated.dir, [PROFILE], db_path=tmp_path / "screencut.db",
        encoder=Encoder.SOFTWARE, force=True,
    )
    round_trip = RoundTrip.model_validate_json(
        (narrated.dir / next(
            o.path for o in result.outcomes if o.stage == "verify_transcript"
        )).read_text()
    )
    assert [d.expected for d in round_trip.real] == ["filters"]
    assert round_trip.at_seam == [], "a mispronunciation is not the edit's fault"


@needs_ffmpeg
def test_trim_finds_nothing_to_cut_in_synthesized_narration(narrated, tmp_path):
    """Phase 8's third exit criterion, in the form arithmetic can state it.

    A read script has no "um" and no dead air, so §4.6's proposal is empty and
    `plan_edit` has nothing to clean up. If this ever starts proposing removals,
    the tunables are cutting speech rather than filler — worth knowing before
    §10's learner starts averaging over it.
    """
    result = go(narrated.dir, tmp_path / "screencut.db")
    proposals = json.loads(
        (narrated.dir / next(o.path for o in result.outcomes if o.stage == "trim")).read_text()
    )
    assert proposals == []

    spec = load_spec_file(narrated.dir / "spec.json")
    assert spec.edit.removals == [], "nothing was cut, so the whole capture survives"


@needs_ffmpeg
def test_re_running_a_narrated_job_synthesizes_nothing(narrated, tmp_path):
    """Principle 4, where it costs the most. A narration is the most expensive
    artifact this pipeline makes — about an hour for three minutes on the target
    machine (environment findings §4) — so a second run that re-synthesized would
    end the review loop for narrated jobs on its own."""
    database = tmp_path / "screencut.db"
    go(narrated.dir, database)
    assert go(narrated.dir, database).did_no_work


@needs_ffmpeg
def test_editing_the_script_re_synthesizes_and_re_recording_the_screen_does_not(
    narrated, tmp_path
):
    """What a stage reads is what invalidates it, and `tts` reads no video.

    The screen recording is the thing most likely to be re-taken while the script
    stays put, and it is exactly the change that must not cost another hour of
    synthesis."""
    database = tmp_path / "screencut.db"
    go(narrated.dir, database)

    spec = json.loads((narrated.dir / "spec.json").read_text())
    source = narrated.dir / spec["source"]["path"]
    source.write_bytes(source.read_bytes() + b"\x00")
    ran = {name.split("/")[-1] for name in go(narrated.dir, database).ran()}
    assert "tts" not in ran and "align" not in ran, "the narration did not change"
    assert "render" in ran, "but the video did"


@needs_ffmpeg
def test_a_narration_longer_than_the_recording_fails_by_name(narrated, tmp_path):
    """It is a real mistake with an obvious remedy, and left alone it surfaces two
    stages later as a caption block past the end of the source — true, and
    useless. Holding the last frame to cover the overrun would be a decision about
    synthesizing video, which is not a stage's to make (§1.1)."""
    narrated.hears([TranscribedWord(t_in=0.0, t_out=1.0, text="the")])
    spec = json.loads((narrated.dir / "spec.json").read_text())
    spec["source"]["duration"] = 0.5
    spec["focus"]["points"] = []
    (narrated.dir / "spec.json").write_text(json.dumps(spec))

    with pytest.raises(Exception, match="Shorten the script or record more screen"):
        go(narrated.dir, tmp_path / "screencut.db")


@needs_ffmpeg
def test_the_bed_mixes_with_the_narration_and_ducks_under_it(tmp_path):
    """The other half of phase 8's audio build item.

    A synthesized narration replaces the recording's audio rather than mixing
    with it — a screen capture's track is room tone at best — but the *bed* still
    mixes and still ducks, and it ducks from the caption timings, which on this
    path came from `align`. So ducking against a synthesized narration needs no
    mechanism of its own; it needed the timings to be real.
    """
    from compile.render import prepare

    job = tmp_path / "job"
    fixture = build_narrated_spec("bed", width=320, height=180, slot_s=2.0, script=SCRIPT)
    write_narrated_fixture(job, fixture, with_video=False)

    words = [
        {"t_in": i * 0.4, "t_out": (i + 1) * 0.4 - 0.05, "text": w}
        for i, w in enumerate(SCRIPT.split())
    ]
    document = fixture.spec.model_dump(mode="json")
    document["narration"]["audio_path"] = "stages/narration.wav"
    document["audio"]["music_path"] = "source/bed.mp3"
    document["captions"] = [
        {"t_in": words[0]["t_in"], "t_out": words[-1]["t_out"], "words": words}
    ]
    spec = EditSpec.model_validate(document)
    plan = prepare(spec, resolve_profile(PROFILE), job, work_dir=Path("work"))

    args = plan.graph_args
    assert args[args.index("-i") + 1] == spec.source.path
    assert args.index("stages/narration.wav") < args.index("source/bed.mp3"), (
        "input order is source, narration, bed — and the graph is written against those numbers"
    )
    assert "[1:a]apad,atrim" in plan.graph, "the narration is the spine, padded to cover the video"
    assert "[0:a]" not in plan.graph, "the silent capture's track is not the narration"
    assert "[2:a]volume@bed" in plan.graph and "asendcmd" in plan.graph
    assert plan.audio_commands.strip(), "there are words to duck under"


@needs_ffmpeg
def test_narrate_attaches_the_script_and_copies_the_voice_into_the_job(tmp_path):
    """Phase 8's "script as an optional job input", as a person actually supplies it.

    The reference is copied *into* the job rather than pointed at. Every path in a
    spec is relative to the job directory (§5.1), which is what lets a stage run on
    a worker at all — a reference that lived outside would be the one input that
    silently did not travel.
    """
    from ingest.fixtures import main as fixture_main
    from runner.cli import main as cli_main
    from runner.job import JobConfig

    job = tmp_path / "job"
    fixture_main(["--out", str(job), "--no-video"])
    (tmp_path / "script.txt").write_text(SCRIPT)
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", "sine=frequency=180:sample_rate=24000:duration=8",
         "-ac", "1", "-c:a", "pcm_s16le", str(tmp_path / "mine.wav")],
        check=True,
    )

    assert cli_main([
        "narrate", str(job),
        "--script", str(tmp_path / "script.txt"),
        "--voice", str(tmp_path / "mine.wav"),
        "--voice-text", "this is my voice",
        "--consent", "recorded by me on 2026-01-01",
    ]) == 0

    spec = load_spec_file(job / "spec.json")
    assert spec.narration.source is NarrationSource.SYNTHESIZED
    assert spec.narration.script == SCRIPT
    assert spec.narration.voice_consent_note, "decision #20 wants it auditable, not assumed"
    assert (job / spec.narration.voice_reference_path).is_file(), "copied in, not pointed at"
    assert JobConfig.load(job).stages == list(SYNTHESIZED_STAGES)


def test_the_two_recipes_are_alternatives_and_provide_the_same_thing():
    """§5.3's two ASR calls, said in the graph rather than in prose. A job gets its
    words with timings from one stage or the other, and nothing downstream of
    either has to know which."""
    assert JOB_STAGES["transcribe"].provision == JOB_STAGES["align"].provision == "transcript"
    assert JOB_STAGES["align"].depends_on == ("narration",)
    assert JOB_STAGES["plan_captions"].depends_on == ("transcript",)
    assert "tts" not in JOB_STAGES["transcribe"].depends_on


def test_the_stage_phase_zero_said_not_to_run_here_is_the_one_that_goes_remote():
    """Environment findings §4: 0.11x realtime, and the chunked path crashes.
    Everything else either is fast enough here or reads media that lives here."""
    assert JOB_STAGES["tts"].prefers_remote
    assert not any(
        stage.prefers_remote for name, stage in JOB_STAGES.items() if name != "tts"
    )


def test_a_narrated_job_asks_for_no_transcribe_and_a_recorded_one_for_no_tts():
    """The recipes are disjoint where it matters: a take narrated by whoever
    recorded it has nothing to synthesize, and a synthesized one has no recorded
    speech to transcribe."""
    assert "transcribe" not in SYNTHESIZED_STAGES
    assert set(SYNTHESIZED_STAGES) - {"tts", "align"} == set(RECORDED_STAGES) - {"transcribe"}


def test_the_fixture_reads_over_a_capture_long_enough_to_hold_it():
    """A fixture that sat on the overrun boundary would fail intermittently for a
    reason unrelated to what it tests."""
    fixture = build_narrated_spec()
    words = len(fixture.spec.narration.script.split())
    assert words / 2.5 < fixture.spec.source.duration, "at a slow reading, still inside"
    assert not fixture.spec.source.has_audio, "the mic was off; that is the point"
    assert fixture.spec.captions == [] and not fixture.spec.edit.segments, (
        "captions and cuts come from the stages, not from the fixture"
    )


def test_the_default_profile_geometry_is_untouched_by_any_of_this():
    """A narrated job renders through the same profiles as any other (§4.1)."""
    assert resolve_profile(PROFILE).width > 0
