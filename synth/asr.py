"""Open transcription: recorded audio -> words with timings (§5.3, phase 4).

**One backend, and it is the one phase 0 ran.** whisper.cpp won at both model
sizes on the target machine — fastest, and the only candidate whose footprint
tracks its RSS, so the 8GB budget is predictable (environment findings §3). The
standing warning in `AGENTS.md` is against writing parsers for output nobody has
seen; this parser is written against `output_json` in whisper.cpp's own
`examples/cli/cli.cpp` and against the invocation phase 0 confirmed end to end.
A second backend would be a second unverified parser, so there is not one.

This is §5.3's *open transcription*. It is not `align`, which is forced alignment
against a known script and arrives with phase 8's TTS. Conflating them is the bug
§5.3 exists to prevent, so they are separate modules producing separate artifacts.

whisper.cpp's JSON, with `--max-len 1` so every segment is one word:

```json
{"transcription": [
  {"timestamps": {"from": "00:00:00,000", "to": "00:00:00,320"},
   "offsets": {"from": 0, "to": 320},
   "text": " Here"}]}
```

`offsets` are **milliseconds** — `cli.cpp` writes `t0 * 10` over whisper's
centisecond timestamps — and are what this reads. `timestamps` is the same number
formatted for a human, and parsing a rendered string back into a number when the
number is in the next field along is how a rounding bug gets in for free.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

WHISPER_SAMPLE_RATE = 16000
"""What whisper wants. Feeding it anything else makes it resample, badly."""

NON_SPEECH = re.compile(r"^[\[(][^\])]*[\])]$")
"""`[BLANK_AUDIO]`, `(upbeat music)` — whisper's own annotations, not words.

Left in, they become a caption block reading "[BLANK_AUDIO]" burned into the
video, which is the kind of failure that is obvious in a render and invisible in
a transcript."""

PUNCTUATION_ONLY = re.compile(r"^[^\w]+$", re.UNICODE)
"""At `--max-len 1` whisper.cpp emits a lone comma or full stop as its own
segment. As a word it is a caption block containing one comma; joined to the word
before it, it is punctuation."""


class TranscribedWord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    t_in: Annotated[float, Field(ge=0.0)]
    t_out: Annotated[float, Field(ge=0.0)]
    text: Annotated[str, Field(min_length=1)]


class Transcript(BaseModel):
    """What `transcribe` writes. Source time throughout, like everything else."""

    model_config = ConfigDict(extra="forbid")

    words: list[TranscribedWord] = Field(default_factory=list)
    language: str = "en"
    backend: str = "whisper.cpp"
    model: str = ""

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)


class AsrUnavailable(RuntimeError):
    """The backend or its weights are not on this machine.

    Its own type because §7.4's degradation cares about the difference between
    "not installed here" and "ran and failed"."""


# --- parsing ----------------------------------------------------------------


def parse_whisper_cpp(data: dict) -> list[TranscribedWord]:
    """whisper.cpp's `-oj` output into words, in source seconds.

    Two cleanups, both of which change what ends up burned into a frame:
    non-speech annotations are dropped, and a punctuation-only segment is joined
    onto the word before it rather than becoming a word of its own.
    """
    words: list[TranscribedWord] = []
    for segment in data.get("transcription", []):
        text = (segment.get("text") or "").strip()
        if not text or NON_SPEECH.match(text):
            continue
        offsets = segment.get("offsets") or {}
        t_in = float(offsets.get("from", 0)) / 1000.0
        t_out = float(offsets.get("to", 0)) / 1000.0
        if PUNCTUATION_ONLY.match(text):
            if not words:
                continue  # nothing to punctuate; a take opening on a comma is noise
            previous = words[-1]
            words[-1] = TranscribedWord(
                t_in=previous.t_in, t_out=max(previous.t_out, t_out), text=previous.text + text
            )
            continue
        words.append(TranscribedWord(t_in=t_in, t_out=max(t_out, t_in), text=text))
    return words


# --- invocation --------------------------------------------------------------


def audio_command(source: Path, wav: Path) -> list[str]:
    """Pull mono 16 kHz PCM out of the recording.

    whisper.cpp reads wav, mp3, flac and ogg — not the mp4 a screen recorder
    writes — so the extraction is not optional and belongs to the ASR stage
    rather than to the caller.
    """
    return [
        "ffmpeg", "-y", "-hide_banner", "-nostdin", "-loglevel", "error",
        "-i", str(source),
        "-vn", "-ac", "1", "-ar", str(WHISPER_SAMPLE_RATE), "-c:a", "pcm_s16le",
        str(wav),
    ]


def whisper_command(
    binary: str, model: Path, wav: Path, out_prefix: Path, *, language: str, threads: int | None
) -> list[str]:
    """The phase 0 invocation, unchanged.

    `--max-len 1` with `-ml 1` is what makes segments words: whisper.cpp has no
    word-timestamp flag, and one token per segment is word-level timing by
    construction. §6.2's `CaptionBlock` and `plan_captions` are written against
    those timings, so this pair of flags is load-bearing rather than cosmetic.
    """
    command = [
        binary,
        "-m", str(model),
        "-f", str(wav),
        "-l", language,
        "-oj",
        "-of", str(out_prefix),
        "--max-len", "1",
        "-ml", "1",
        "-np",
    ]
    if threads:
        command += ["-t", str(threads)]
    return command


def model_path(models_dir: Path | str, model: str) -> Path:
    return Path(models_dir).expanduser() / f"ggml-{model}.bin"


def transcribe(
    source: Path | str,
    *,
    binary: str = "whisper-cli",
    models_dir: Path | str = "~/.cache/screencut/whisper",
    model: str = "large-v3",
    language: str = "en",
    threads: int | None = None,
    work_dir: Path | str | None = None,
) -> Transcript:
    """Recording in, words out. Raises `AsrUnavailable` when the box cannot do it.

    A recording with no speech in it transcribes to no words, and that is a
    result rather than a failure: a screen capture with the mic off is an
    ordinary job, and it should render captionless rather than degrade.
    """
    source = Path(source)
    weights = model_path(models_dir, model)
    if shutil.which(binary) is None:
        raise AsrUnavailable(
            f"{binary!r} is not on PATH. whisper.cpp is the backend phase 0 chose "
            f"(environment findings §3); install it and put ggml-{model}.bin in {models_dir}."
        )
    if not weights.is_file():
        raise AsrUnavailable(f"no weights at {weights}. Fetch ggml-{model}.bin into {models_dir}.")

    with tempfile.TemporaryDirectory(dir=work_dir) as scratch:
        wav = Path(scratch) / "audio.wav"
        subprocess.run(audio_command(source, wav), check=True)
        prefix = Path(scratch) / "asr"
        subprocess.run(
            whisper_command(binary, weights, wav, prefix, language=language, threads=threads),
            check=True,
        )
        data = json.loads(prefix.with_suffix(".json").read_text())

    return Transcript(
        words=parse_whisper_cpp(data),
        language=(data.get("result") or {}).get("language", language),
        model=model,
    )
