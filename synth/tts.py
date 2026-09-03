"""Narration from a script, in your own voice (decision #20, phase 8).

The one synthesis this design permits, and the boundary is a schema-and-config
matter rather than a matter of intent (§1.1): `spec/narration.py` refuses to
validate a synthesized narration without an explicitly recorded per-job voice
reference, and this module refuses to run without one it can read.

**Written against the invocation phase 0 ran, and only that one.** F5-TTS has a
CLI as well as an API; phase 0 measured the API (`tools/phase0/bench_tts.py`), so
that is what a stage gets. The standing rule in `AGENTS.md` is against writing
code for output nobody has seen, and a second entry point would be a second
unverified one.

Two hazards phase 0 found the hard way, both handled here rather than discovered
again (environment findings §4):

- **A successful synthesis can still exit nonzero.** F5-TTS pulls in
  `multiprocess`, whose resource tracker respawns the interpreter with an empty
  `PYTHONHASHSEED` and dies at *teardown*, after good audio has been written. It
  takes the exit code with it, and §7.4 counts a nonzero exit as failure — so a
  `tts` stage judging by exit status would have degraded on every successful run.
  `PYTHONHASHSEED=0` fixes the exit code; judging by produced audio fixes it
  whether or not the pin holds on the next PyTorch.
- **Two libraries, one FFmpeg.** `torchaudio` loads audio through `torchcodec`,
  which dlopens FFmpeg by soname. The variable that finds Homebrew's is the same
  one that breaks cairo elsewhere in this project, which is why it is a per-stage
  subprocess environment and not a global.

**This is the stage phase 0 said not to run locally.** 0.11x realtime at best, and
the chunked path crashes on MPS: a three-minute narration costs about an hour on
the target machine. `runner/remote.py` is the answer, and `runner/stages.py`
routes this stage there.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

SAMPLE_RATE = 24000
"""What F5-TTS generates at, and what the reference clip is resampled to."""

MIN_REFERENCE_S = 3.0
"""F5-TTS wants roughly 5-10 seconds of reference, and a clip well under that
clones nothing recognisable. Too *long* is not a problem — the clip is cut to
`reference_seconds` — so only the floor is enforced."""


class TtsUnavailable(RuntimeError):
    """The backend, its weights or its reference audio are not on this machine.

    Its own type for the same reason `AsrUnavailable` has one: "not installed
    here" and "ran and failed" want different words in front of a person, and
    only one of them is worth retrying.
    """


class Synthesis(BaseModel):
    """What `tts` writes beside its audio. Source time, like everything else."""

    model_config = ConfigDict(extra="forbid")

    audio_path: str = Field(description="The synthesized narration, relative to the job directory.")
    duration: float = Field(ge=0.0, description="Seconds of narration produced.")
    backend: str = "f5-tts"
    device: str = "cpu"
    infer_seconds: float | None = Field(
        default=None,
        description="Wall time inside `infer`. Phase 0's realtime factor, measured per job.",
    )
    clean_exit: bool = Field(
        default=True,
        description="False when the teardown crash of environment findings §4 took the exit code.",
    )


#: Run in a subprocess of its own, which is the §5.1 contract and also the only
#: way this import coexists with the ASR one — phase 0 found that pulling
#: `mlx_whisper` and `f5_tts` into one process breaks FFmpeg's shared libraries a
#: third way. The script prints its two numbers before F5-TTS gets a chance to
#: die at teardown, which is what makes "judge by produced audio" implementable.
INFER_SCRIPT = '''
import json, sys, time

device, ref_audio, ref_text, out_wav = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
gen_text = open(sys.argv[5], encoding="utf-8").read()

from f5_tts.api import F5TTS

model = F5TTS(device=device)
started = time.perf_counter()
wav, sr, _ = model.infer(
    ref_file=ref_audio,
    ref_text=ref_text,
    gen_text=gen_text,
    file_wave=out_wav,
    remove_silence=False,
)
print("RESULT " + json.dumps({
    "infer_seconds": round(time.perf_counter() - started, 3),
    "duration": round(len(wav) / sr, 3),
}), flush=True)
'''


def reference_command(reference: Path, out: Path, seconds: float = 10.0) -> list[str]:
    """Cut and resample the voice reference the way phase 0 did.

    Mono at F5-TTS's own rate, so nothing downstream resamples it badly, and
    bounded in length because the model conditions on the clip rather than
    learning from it.
    """
    return [
        "ffmpeg", "-y", "-hide_banner", "-nostdin", "-loglevel", "error",
        "-i", str(reference),
        "-t", f"{seconds:g}", "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-c:a", "pcm_s16le",
        str(out),
    ]


def infer_command(python: str, script: Path, *, device: str, reference: Path,
                  reference_text: str, out_wav: Path, text_file: Path) -> list[str]:
    return [
        python, str(script), device, str(reference), reference_text, str(out_wav), str(text_file)
    ]


def infer_environment(base: dict[str, str], library_path: str | None) -> dict[str, str]:
    """The subprocess environment phase 0 needed, and nothing global.

    `PYTHONHASHSEED` is the exit-code fix. `DYLD_FALLBACK_LIBRARY_PATH` is macOS's
    FFmpeg lookup and is set only when configured, because on any other platform
    it is noise and on this one it is the variable that breaks cairo.
    """
    env = {**base, "PYTHONHASHSEED": "0"}
    if library_path:
        env["DYLD_FALLBACK_LIBRARY_PATH"] = library_path
    return env


def audio_duration(path: Path) -> float:
    """Seconds of audio, from the container rather than from the generator.

    The number `infer` reports is what the model produced; this is what landed on
    disk. They agree in the ordinary case, and when they do not it is the file the
    compiler is about to read that matters.
    """
    output = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout
    return float(json.loads(output)["format"]["duration"])


def synthesize(
    script: str,
    *,
    reference: Path | str,
    reference_text: str,
    out_wav: Path | str,
    python: str = "python3",
    device: str = "cpu",
    library_path: str | None = None,
    reference_seconds: float = 10.0,
    env: dict[str, str] | None = None,
) -> Synthesis:
    """Script in, narration out. Raises `TtsUnavailable` when the box cannot do it.

    There is no degradation path and §7.4's table gains a row saying so: a job
    whose narration is synthesized has no audio to fall back to, exactly as
    `script_draft` has no script to fall back to. Rendering it silent would be
    worse than either failing or degrading, because a silent video looks
    finished.
    """
    reference = Path(reference)
    out_wav = Path(out_wav)
    if not script.strip():
        raise TtsUnavailable("there is no script to read")
    if shutil.which(python) is None and not Path(python).is_file():
        raise TtsUnavailable(
            f"no Python at {python!r} to run F5-TTS with. Phase 0 measured it in an environment "
            f"of its own (environment findings §4); point prefs/constraints.yaml's `tts.python` at one."
        )
    if not reference.is_file():
        raise TtsUnavailable(
            f"no voice reference at {reference}. Decision #20 permits synthesis of you and nobody "
            f"else, so the reference is a required per-job input rather than a default."
        )
    if audio_duration(reference) < MIN_REFERENCE_S:
        raise TtsUnavailable(
            f"the voice reference at {reference} is under {MIN_REFERENCE_S:g}s. F5-TTS conditions on "
            f"the clip, and a clip this short clones nothing recognisable as you."
        )

    out_wav.parent.mkdir(parents=True, exist_ok=True)
    # Success is judged by the file below, so a leftover from an interrupted run
    # would read as this run's output. Content-addressed paths make that rare and
    # not impossible: a crash mid-write leaves a partial wav under the key the
    # next run computes.
    out_wav.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory() as scratch:
        work = Path(scratch)
        clip = work / "reference.wav"
        subprocess.run(reference_command(reference, clip, reference_seconds), check=True)
        runner_script = work / "infer.py"
        runner_script.write_text(INFER_SCRIPT)
        text_file = work / "script.txt"
        # Through a file rather than argv: a narration script is paragraphs, and
        # an argument list is the wrong shape for one on any platform.
        text_file.write_text(script, encoding="utf-8")

        completed = subprocess.run(
            infer_command(
                python, runner_script, device=device, reference=clip,
                reference_text=reference_text, out_wav=out_wav, text_file=text_file,
            ),
            capture_output=True, text=True,
            env=infer_environment(dict(env if env is not None else os.environ), library_path),
        )

    # Success is "it produced audio", not "it exited zero" — the teardown crash
    # of environment findings §4 masked three good syntheses in phase 0's first
    # run, and the harness was wrong, not the machine.
    if not out_wav.is_file() or out_wav.stat().st_size == 0:
        raise TtsUnavailable(
            f"F5-TTS produced no audio (exit {completed.returncode}).\n"
            f"{(completed.stderr or '').strip()[-800:]}"
        )

    reported = _reported(completed.stdout)
    return Synthesis(
        audio_path=str(out_wav),
        duration=audio_duration(out_wav),
        device=device,
        infer_seconds=reported.get("infer_seconds"),
        clean_exit=completed.returncode == 0,
    )


def _reported(stdout: str) -> dict:
    for line in reversed((stdout or "").splitlines()):
        if line.startswith("RESULT "):
            try:
                return json.loads(line[len("RESULT "):])
            except ValueError:
                return {}
    return {}
