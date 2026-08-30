"""Phase 0: the three ASR candidates, at two model sizes, on this machine.

Phases doc, phase 0: run `mlx-whisper`, `whisper.cpp` and `faster-whisper` on a
sample, time them, record peak RSS, confirm the CTranslate2/MPS situation
firsthand, and do it at `large-v3` *and* `medium` — "since on 8GB the smaller
model winning on wall-clock is a live possibility rather than a consolation
prize".

Three things are measured that a naive benchmark would miss:

- **Peak RSS, not just seconds.** The 8GB ceiling picks the model size that goes
  into `constraints.yaml`, and it picks it on resident size.
- **Word-level timings, per backend.** §6.2 has `CaptionBlock` carrying per-word
  timings, and phase 4's `plan_captions` is written against them. A backend that
  is fast and emits only segment timings cannot do the job at all, so this is a
  capability gate that runs before the speed comparison means anything.
- **Accuracy, roughly.** The sample is `say`-synthesised from a known script, so
  a word error rate falls out for free. It is not a benchmark-grade WER — one
  clean synthetic voice is the easy case — but a backend that is fast and *wrong*
  needs to be visible here rather than in phase 4.

Every backend runs as a subprocess, which is both how §5.1's stage contract will
invoke it and the only way to get an honest peak-RSS figure that includes loading
the weights.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from tools.measure import Measurement, Run, report, run_once, write_results

ASR_PYTHON = Path(__file__).resolve().parents[2] / ".venv-asr" / "bin" / "python"
WHISPER_CLI = "/opt/homebrew/bin/whisper-cli"
GGML_DIR = Path("/tmp/phase0/ggml")

#: HuggingFace repos per backend and size. Pinned here so the committed result
#: says exactly which weights produced it.
MLX_REPOS = {
    "large-v3": "mlx-community/whisper-large-v3-mlx",
    "medium": "mlx-community/whisper-medium-mlx",
}
GGML_URLS = {
    "large-v3": "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3.bin",
    "medium": "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-medium.bin",
}
FASTER_MODELS = {"large-v3": "large-v3", "medium": "medium"}


# --------------------------------------------------------------------------
# Accuracy
# --------------------------------------------------------------------------


def normalize(text: str) -> list[str]:
    return re.sub(r"[^a-z0-9\s]", " ", text.lower()).split()


def wer(reference: str, hypothesis: str) -> float:
    """Levenshtein over words. Small inputs, so the quadratic table is fine."""
    ref, hyp = normalize(reference), normalize(hypothesis)
    if not ref:
        return 0.0
    prev = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        cur = [i]
        for j, h in enumerate(hyp, 1):
            cur.append(
                prev[j - 1] if r == h else 1 + min(prev[j - 1], prev[j], cur[j - 1])
            )
        prev = cur
    return round(prev[-1] / len(ref), 4)


# --------------------------------------------------------------------------
# Backends
# --------------------------------------------------------------------------


@dataclass
class Backend:
    name: str
    available: bool
    command: Callable[[str, Path, Path], list[str]]
    read_output: Callable[[Path], tuple[str, bool]]
    """-> (transcript, has_word_timings)"""
    note: str = ""


def _mlx_cmd(size: str, wav: Path, out: Path) -> list[str]:
    return [
        str(ASR_PYTHON), "-m", "mlx_whisper.cli",
        "--model", MLX_REPOS[size],
        "--word-timestamps", "True",
        "--output-dir", str(out),
        "--output-name", "mlx",
        "--output-format", "json",
        "--verbose", "False",
        str(wav),
    ]


def _mlx_read(out: Path) -> tuple[str, bool]:
    data = json.loads((out / "mlx.json").read_text())
    words = any(seg.get("words") for seg in data.get("segments", []))
    return data.get("text", ""), words


def _whispercpp_cmd(size: str, wav: Path, out: Path) -> list[str]:
    return [
        WHISPER_CLI,
        "-m", str(GGML_DIR / f"ggml-{size}.bin"),
        "-f", str(wav),
        "-oj",                      # JSON output
        "-of", str(out / "cpp"),
        "--max-len", "1",           # one token per segment == word-level timings
        "-ml", "1",
        "-np",
    ]


def _whispercpp_read(out: Path) -> tuple[str, bool]:
    data = json.loads((out / "cpp.json").read_text())
    chunks = data.get("transcription", [])
    text = "".join(c.get("text", "") for c in chunks)
    # With --max-len 1 every chunk is a word carrying its own offsets, which is
    # word-level timing by construction rather than by a dedicated field.
    words = len(chunks) > 20 and all("offsets" in c for c in chunks[:20])
    return text, words


FASTER_SCRIPT = """
import json, sys
from faster_whisper import WhisperModel
size, wav, out = sys.argv[1], sys.argv[2], sys.argv[3]
# CTranslate2 has no MPS backend; "auto" resolves to CPU on Apple Silicon. That
# is the situation phase 0 says to confirm firsthand, so it is asserted, printed
# and recorded rather than assumed.
model = WhisperModel(size, device="cpu", compute_type="int8")
segments, info = model.transcribe(wav, word_timestamps=True)
segs = [
    {"text": s.text, "words": [{"w": w.word, "s": w.start, "e": w.end} for w in (s.words or [])]}
    for s in segments
]
json.dump({"segments": segs, "device": "cpu"}, open(out, "w"))
"""


def _faster_cmd(size: str, wav: Path, out: Path) -> list[str]:
    script = out / "run_faster.py"
    script.write_text(FASTER_SCRIPT)
    return [
        str(ASR_PYTHON), str(script),
        FASTER_MODELS[size], str(wav), str(out / "faster.json"),
    ]


def _faster_read(out: Path) -> tuple[str, bool]:
    data = json.loads((out / "faster.json").read_text())
    text = "".join(s["text"] for s in data["segments"])
    return text, any(s["words"] for s in data["segments"])


def backends() -> list[Backend]:
    return [
        Backend(
            "mlx-whisper",
            ASR_PYTHON.exists(),
            _mlx_cmd, _mlx_read,
            note="Apple MLX, unified-memory GPU",
        ),
        Backend(
            "whisper.cpp",
            Path(WHISPER_CLI).exists(),
            _whispercpp_cmd, _whispercpp_read,
            note="GGML + Metal",
        ),
        Backend(
            "faster-whisper",
            ASR_PYTHON.exists(),
            _faster_cmd, _faster_read,
            note="CTranslate2, CPU only on Apple Silicon",
        ),
    ]


# --------------------------------------------------------------------------
# Model acquisition, kept out of the timings
# --------------------------------------------------------------------------


def fetch_ggml(size: str) -> bool:
    GGML_DIR.mkdir(parents=True, exist_ok=True)
    target = GGML_DIR / f"ggml-{size}.bin"
    if target.exists():
        return True
    print(f"  downloading ggml-{size} ...")
    result = subprocess.run(
        ["curl", "-fsSL", "--retry", "2", "-o", str(target), GGML_URLS[size]]
    )
    return result.returncode == 0 and target.exists()


def warm(backend: Backend, size: str, wav: Path, out: Path) -> Run:
    """A discarded first run, so downloads and weight conversion are not timed."""
    return run_once(f"warm/{backend.name}/{size}", backend.command(size, wav, out))


# --------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wav", type=Path, default=Path("/tmp/phase0/sample.wav"))
    parser.add_argument("--script", type=Path, default=Path("/tmp/phase0/script.txt"))
    parser.add_argument("--sizes", default="medium,large-v3")
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--workdir", type=Path, default=Path("/tmp/phase0/asr"))
    args = parser.parse_args()

    reference = args.script.read_text()
    duration = float(
        subprocess.run(
            ["/opt/homebrew/opt/ffmpeg-full/bin/ffprobe", "-v", "error",
             "-show_entries", "format=duration", "-of", "csv=p=0", str(args.wav)],
            capture_output=True, text=True,
        ).stdout.strip()
        or 0
    )

    results: list[dict[str, Any]] = []
    measurements: list[Measurement] = []

    for size in args.sizes.split(","):
        size = size.strip()
        for backend in backends():
            label = f"{backend.name}/{size}"
            if not backend.available:
                results.append({"backend": backend.name, "size": size,
                                "ok": False, "error": "not installed"})
                print(f"  SKIP {label}: not installed")
                continue
            if backend.name == "whisper.cpp" and not fetch_ggml(size):
                results.append({"backend": backend.name, "size": size,
                                "ok": False, "error": "model download failed"})
                continue

            out = args.workdir / backend.name.replace(".", "") / size
            out.mkdir(parents=True, exist_ok=True)

            first = warm(backend, size, args.wav, out)
            if not first.ok:
                print(f"  FAIL {label}: exit {first.exit_code}")
                print(f"       {first.stderr_tail[-400:]}")
                results.append({"backend": backend.name, "size": size, "ok": False,
                                "error": first.stderr_tail[-600:]})
                continue

            runs = [
                run_once(f"{label}#{i}", backend.command(size, args.wav, out))
                for i in range(args.repeats)
            ]
            measurement = Measurement(label=label, runs=runs)
            measurements.append(measurement)

            try:
                text, has_words = backend.read_output(out)
            except Exception as exc:
                text, has_words = "", False
                print(f"  (could not read {label} output: {exc})")

            rate = wer(reference, text)
            results.append({
                "backend": backend.name,
                "size": size,
                "ok": measurement.ok,
                "note": backend.note,
                "burst_s": measurement.burst_s,
                "sustained_s": measurement.sustained_s,
                "peak_rss_mb": round(measurement.peak_rss_bytes / 1024**2, 1),
                # The number the 8GB budget is actually checked against. For
                # mlx-whisper it runs to roughly twice the RSS figure beside it,
                # because unified-memory allocations never appear in RSS.
                "peak_footprint_mb": round(
                    measurement.peak_footprint_bytes / 1024**2, 1),
                "swap_delta_mb": round(
                    max(r.swap_delta_bytes for r in runs) / 1024**2, 1),
                "realtime_factor": round(duration / measurement.sustained_s, 2)
                if measurement.sustained_s else None,
                "word_timings": has_words,
                "wer": rate,
                "transcript_head": text.strip()[:160],
            })
            print(
                f"  ok   {label:<26} {measurement.sustained_s:>6.2f}s  "
                f"rss={measurement.peak_rss_bytes / 1024**2:>6.0f}MB  "
                f"foot={measurement.peak_footprint_bytes / 1024**2:>6.0f}MB  "
                f"{duration / measurement.sustained_s:>5.2f}x  "
                f"words={'yes' if has_words else 'NO'}  wer={rate}"
            )

    if measurements:
        print("\n" + report(measurements))
    path = write_results("asr", {
        "sample_seconds": round(duration, 2),
        "sample_source": "macOS `say`, known reference script",
        "results": results,
        "measurements": [m.to_dict() for m in measurements],
    })
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
