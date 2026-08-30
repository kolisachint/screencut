"""Phase 0: does 8GB actually force local stages to be serialized? (§16)

`LocalRunner` already refuses to start a second stage that holds weights. That
refusal was written from an argument, not a measurement — and the phases doc says
this is "the cheapest possible test of the §16 claim that stages must be
serialized, and it is worth knowing before `LocalRunner` is written rather than
after". It was written first, so this is the test arriving late; it can still say
whether the constraint is real or merely cautious.

Two arrangements of the same work:

- **serial** — transcribe in one process, let it exit, then synthesize in
  another. This is what `LocalRunner` does today.
- **resident** — transcribe and synthesize in *one* process, with the ASR weights
  still held when the TTS weights load. This is what `LocalRunner` forbids.

The number that decides it is macOS's *phys_footprint*, not RSS. RSS misses
MLX's unified-memory allocations almost entirely — mlx-whisper on `large-v3`
polls at ~2GB RSS against a ~5.4GB footprint — so a serialization argument built
on RSS would be built on a number roughly half the truth. Swap is read directly
alongside it, because on this machine the failure mode is not a crash but the
swapper, and swap presents as ordinary slowness.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from tools.measure import Run, run_once, swap_used_bytes, write_results

ML_PYTHON = Path(__file__).resolve().parents[2] / ".venv-asr" / "bin" / "python"

#: `multiprocess` respawns the interpreter with an empty PYTHONHASHSEED and dies
#: on Python 3.12 without one — after a successful inference, taking the exit
#: code with it.
ML_ENV = {"PYTHONHASHSEED": "0"}

#: Both halves load audio through soundfile rather than torchcodec.
#:
#: Not a convenience. The first version of this benchmark set
#: DYLD_FALLBACK_LIBRARY_PATH so torchcodec could find Homebrew's FFmpeg, and the
#: *resident* case then failed to load libtorchcodec at all: mlx-whisper and
#: F5-TTS pull in incompatible FFmpeg bindings and whichever imports first wins.
#: That is a real finding — it is an argument for §5.1's subprocess stage
#: contract, since separate processes cannot collide this way — but it confounds
#: the memory question this benchmark exists to answer, so it is routed around.
SHIM = '''
import warnings
warnings.filterwarnings("ignore")
import torch, torchaudio, soundfile as _sf

def _load(uri, *a, **k):
    data, rate = _sf.read(str(uri), dtype="float32", always_2d=True)
    return torch.from_numpy(data.T).contiguous(), rate

torchaudio.load = _load
'''

TRANSCRIBE = '''
import sys, time
import mlx_whisper
t = time.perf_counter()
r = mlx_whisper.transcribe(sys.argv[1], path_or_hf_repo=sys.argv[2], word_timestamps=True)
print("TRANSCRIBE_S", round(time.perf_counter() - t, 3))
print("CHARS", len(r["text"]))
'''

SYNTHESIZE = SHIM + '''
import sys, time
from f5_tts.api import F5TTS
m = F5TTS(device=sys.argv[4])
t = time.perf_counter()
m.infer(ref_file=sys.argv[1], ref_text=sys.argv[2], gen_text=sys.argv[3],
        file_wave=sys.argv[5], remove_silence=False)
print("SYNTHESIZE_S", round(time.perf_counter() - t, 3))
'''

#: The resident case deliberately keeps `result` reachable across the TTS load.
#: Releasing it would measure the serial case again while calling it resident.
RESIDENT = SHIM + '''
import sys, time
wav, repo, ref, ref_text, gen_text, device, out = sys.argv[1:8]

import mlx_whisper
t = time.perf_counter()
result = mlx_whisper.transcribe(wav, path_or_hf_repo=repo, word_timestamps=True)
print("TRANSCRIBE_S", round(time.perf_counter() - t, 3))

from f5_tts.api import F5TTS
model = F5TTS(device=device)
t = time.perf_counter()
model.infer(ref_file=ref, ref_text=ref_text, gen_text=gen_text,
            file_wave=out, remove_silence=False)
print("SYNTHESIZE_S", round(time.perf_counter() - t, 3))
print("STILL_HELD", len(result["text"]))   # keeps the ASR result reachable
'''

REF_TEXT = (
    "Today I want to walk you through the new analytics dashboard we shipped "
    "last week."
)

#: Short enough to stay in one F5-TTS batch. The multi-batch path aborts on this
#: stack (see the TTS findings) and a crash would confound the memory question.
GEN_TEXT = "The export button lives in the top right corner of every view."


def marker(stdout: str, key: str) -> float | None:
    for line in stdout.splitlines():
        if line.startswith(key):
            return float(line.split()[1])
    return None


def summarise(run: Run, keys: tuple[str, ...]) -> dict[str, Any]:
    """Judge by produced output, not exit code.

    `multiprocess` aborts at interpreter teardown *after* a good synthesis, so an
    exit-code verdict marks successful runs as failures. Phase 0's earlier TTS
    pass recorded exactly that and had to be redone.
    """
    found = {k.lower().rstrip("_s") + "_s": marker(run.stdout_full, k) for k in keys}
    return {
        "ok": all(v is not None for v in found.values()),
        "exit_code": run.exit_code,
        "peak_rss_mb": round(run.peak_rss_mb, 1),
        "peak_footprint_mb": round(run.peak_footprint_mb, 1),
        "swap_delta_mb": round(run.swap_delta_bytes / 1024**2, 1),
        "wall_s": run.wall_s,
        **found,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wav", type=Path, default=Path("/tmp/phase0/sample.wav"))
    parser.add_argument("--ref", type=Path,
                        default=Path("/tmp/phase0/tts/reference.wav"))
    parser.add_argument("--repo", default="mlx-community/whisper-large-v3-mlx")
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--workdir", type=Path,
                        default=Path("/tmp/phase0/serialize"))
    args = parser.parse_args()
    args.workdir.mkdir(parents=True, exist_ok=True)

    scripts: dict[str, Path] = {}
    for name, body in (("transcribe", TRANSCRIBE), ("synthesize", SYNTHESIZE),
                       ("resident", RESIDENT)):
        path = args.workdir / f"{name}.py"
        path.write_text(body)
        scripts[name] = path

    results: dict[str, Any] = {
        "baseline_swap_mb": round(swap_used_bytes() / 1024**2, 1),
        "asr_model": args.repo,
        "tts_device": args.device,
    }

    # --- serial: one model resident at a time -----------------------------
    a = run_once("serial/transcribe",
                 [str(ML_PYTHON), str(scripts["transcribe"]), str(args.wav),
                  args.repo], env=ML_ENV)
    b = run_once("serial/synthesize",
                 [str(ML_PYTHON), str(scripts["synthesize"]), str(args.ref),
                  REF_TEXT, GEN_TEXT, args.device,
                  str(args.workdir / "serial.wav")], env=ML_ENV)
    sa, sb = summarise(a, ("TRANSCRIBE_S",)), summarise(b, ("SYNTHESIZE_S",))
    results["serial"] = {
        "ok": sa["ok"] and sb["ok"],
        "transcribe": sa,
        "synthesize": sb,
        # Serialized, the machine only ever has to hold the larger of the two.
        "max_concurrent_footprint_mb": max(
            sa["peak_footprint_mb"], sb["peak_footprint_mb"]),
        "wall_s": round(a.wall_s + b.wall_s, 2),
        "swap_delta_mb": round(sa["swap_delta_mb"] + sb["swap_delta_mb"], 1),
    }

    # --- resident: both models held at once -------------------------------
    c = run_once("resident/both",
                 [str(ML_PYTHON), str(scripts["resident"]), str(args.wav),
                  args.repo, str(args.ref), REF_TEXT, GEN_TEXT, args.device,
                  str(args.workdir / "resident.wav")], env=ML_ENV)
    sc = summarise(c, ("TRANSCRIBE_S", "SYNTHESIZE_S"))
    if not sc["ok"]:
        sc["error"] = c.stderr_tail[-800:]
    results["resident"] = sc

    serial_peak = results["serial"]["max_concurrent_footprint_mb"]
    resident_peak = sc["peak_footprint_mb"]
    #: Of 8192MB, leaving the OS and a browser their share. Crossing this is not
    #: a crash, it is the swapper — which is why swap is checked beside it.
    CEILING_MB = 5500
    results["verdict"] = {
        "serial_peak_footprint_mb": serial_peak,
        "resident_peak_footprint_mb": resident_peak,
        "resident_costs_extra_mb": round(resident_peak - serial_peak, 1),
        "resident_completed": sc["ok"],
        "resident_swapped": sc["swap_delta_mb"] > 250,
        "serialization_required": (
            not sc["ok"]
            or sc["swap_delta_mb"] > 250
            or resident_peak > CEILING_MB
        ),
    }

    print(json.dumps(results, indent=2))
    path = write_results("serialization", results)
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
