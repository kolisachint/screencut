"""Phase 0: is local F5-TTS viable on a base-model M1 Air?

The phases doc asks four things of this: does MPS work, is
`PYTORCH_ENABLE_MPS_FALLBACK=1` needed, how long does thirty seconds take, and
what is peak memory — with the pointed reminder that "a CPU fallback on 8GB is
also a second copy of the tensors, so the memory number is as much the verdict as
the timing is".

So all three device paths are attempted in sequence — MPS, MPS with the fallback
flag, and CPU — and each is recorded separately. A single "it worked" would hide
which one worked, and the difference between them is the difference between phase
8 shipping a `tts` stage and phase 8 starting by building `RemoteRunner`.

A "no" here blocks nothing. That is the point of the stage-contract seam.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from tools.measure import Run, run_once, write_results

#: ASR and TTS deliberately share one environment. `bench_serialize` has to hold
#: both sets of weights in a single process to test §16's serialization claim,
#: and it cannot do that across two virtualenvs.
TTS_PYTHON = Path(__file__).resolve().parents[2] / ".venv-asr" / "bin" / "python"
FFMPEG = "/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg"

#: torchaudio 2.13 loads audio through torchcodec, which dlopens FFmpeg's shared
#: libraries by soname and does *not* find Homebrew's without help. Without this
#: F5-TTS fails inside `infer` with a RuntimeError about libtorchcodec that names
#: FFmpeg and MPS in the same breath, which reads like an Apple Silicon problem
#: and is not one. Exactly the same class of bug as cairocffi's libcairo lookup.
DYLD_ENV = {
    "DYLD_FALLBACK_LIBRARY_PATH": "/opt/homebrew/lib",
    #: F5-TTS pulls in `multiprocess`, whose resource tracker respawns the
    #: interpreter with an empty PYTHONHASHSEED and dies on Python 3.12 with
    #: "Fatal Python error: config_init_hash_seed". It dies at *teardown*, after
    #: a perfectly good inference, but it takes the exit code with it. Since
    #: §7.4 counts a nonzero exit as stage failure, a phase-8 `tts` stage would
    #: have degraded on every successful synthesis. Pinning the seed is the fix.
    "PYTHONHASHSEED": "0",
}

#: A single-chunk generation. F5-TTS splits long text into batches, and the batch
#: path is where this stack falls over, so the short case is what separates "MPS
#: does not work" from "MPS works until it batches" — two very different verdicts
#: for phase 8.
SHORT_TEXT = "The export button lives in the top right corner of every view."

#: Thirty seconds of text, as the phases doc specifies. Long enough that the
#: chunking behaviour shows up; short enough to run three times on one machine.
GEN_TEXT = (
    "The export button lives in the top right corner of every view. "
    "It produces a comma separated file that matches exactly what is on screen, "
    "including any filters you have applied. If you have a saved view open, the "
    "export carries that view's name, so the file is already labelled when it "
    "lands in your downloads folder. Anyone on your team can open the same link "
    "and see the same numbers."
)

#: The reference clip is cut from the same `say` sample the ASR bench uses, so
#: both halves of the "back to back in one process" test share an input.
REF_TEXT = (
    "Today I want to walk you through the new analytics dashboard we shipped "
    "last week."
)


def make_reference(wav: Path, ref: Path, seconds: float = 7.0) -> None:
    """Cut a short reference clip. F5-TTS wants roughly 5-10 seconds."""
    ref.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
         "-i", str(wav), "-t", str(seconds), "-ar", "24000", "-ac", "1", str(ref)],
        check=True,
    )


INFER_SCRIPT = '''
import sys, time, torch
device = sys.argv[1]
ref_audio, ref_text, gen_text, out_dir = sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5]

print("torch", torch.__version__, "mps_available", torch.backends.mps.is_available())

from f5_tts.api import F5TTS

model = F5TTS(device=device)
started = time.perf_counter()
wav, sr, _ = model.infer(
    ref_file=ref_audio,
    ref_text=ref_text,
    gen_text=gen_text,
    file_wave=out_dir + "/out.wav",
    remove_silence=False,
)
print("INFER_SECONDS", round(time.perf_counter() - started, 3))
print("AUDIO_SECONDS", round(len(wav) / sr, 3))
print("DEVICE_USED", device)
'''


def attempt(
    label: str, device: str, env: dict[str, str], paths: dict[str, Path],
    gen_text: str = GEN_TEXT,
) -> tuple[Run, dict[str, Any]]:
    script = paths["work"] / "infer.py"
    script.write_text(INFER_SCRIPT)
    run = run_once(
        label,
        [str(TTS_PYTHON), str(script), device, str(paths["ref"]), REF_TEXT,
         gen_text, str(paths["work"])],
        env=env,
    )
    detail: dict[str, Any] = {
        "label": label,
        "device": device,
        "text": "short/single-batch" if gen_text is SHORT_TEXT else "30s/multi-batch",
        "mps_fallback_env": env.get("PYTORCH_ENABLE_MPS_FALLBACK", "unset"),
        "exit_code": run.exit_code,
        "wall_s": run.wall_s,
        "peak_rss_mb": round(run.peak_rss_bytes / 1024**2, 1),
        "swap_delta_mb": round(run.swap_delta_bytes / 1024**2, 1),
    }
    for stream in (run.stdout_full, run.stderr_tail):
        for line in stream.splitlines():
            if line.startswith("INFER_SECONDS"):
                detail["infer_s"] = float(line.split()[1])
            elif line.startswith("AUDIO_SECONDS"):
                detail["audio_s"] = float(line.split()[1])

    # Success is "it produced audio", not "it exited zero". The first attempt at
    # this benchmark recorded F5-TTS as broken on MPS because a teardown crash
    # masked three good syntheses; the harness was wrong, not the machine.
    detail["ok"] = detail.get("infer_s") is not None
    detail["clean_exit"] = run.ok
    if detail.get("infer_s") and detail.get("audio_s"):
        detail["realtime_factor"] = round(detail["audio_s"] / detail["infer_s"], 2)
    if not detail["ok"]:
        detail["error"] = run.stderr_tail[-800:]
    return run, detail


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wav", type=Path, default=Path("/tmp/phase0/sample.wav"))
    parser.add_argument("--workdir", type=Path, default=Path("/tmp/phase0/tts"))
    args = parser.parse_args()

    if not TTS_PYTHON.exists():
        raise SystemExit(f"no TTS environment at {TTS_PYTHON}")

    args.workdir.mkdir(parents=True, exist_ok=True)
    ref = args.workdir / "reference.wav"
    if not ref.exists():
        make_reference(args.wav, ref)

    paths = {"work": args.workdir, "ref": ref}
    fallback = {**DYLD_ENV, "PYTORCH_ENABLE_MPS_FALLBACK": "1"}
    attempts = [
        ("mps/short", "mps", DYLD_ENV, SHORT_TEXT),
        ("mps+fallback/short", "mps", fallback, SHORT_TEXT),
        ("cpu/short", "cpu", fallback, SHORT_TEXT),
        ("mps/30s", "mps", DYLD_ENV, GEN_TEXT),
        ("mps+fallback/30s", "mps", fallback, GEN_TEXT),
        ("cpu/30s", "cpu", fallback, GEN_TEXT),
    ]

    details: list[dict[str, Any]] = []
    for label, device, env, gen_text in attempts:
        print(f"--- {label} ---")
        _, detail = attempt(label, device, env, paths, gen_text)
        details.append(detail)
        if detail["ok"]:
            print(
                f"  ok   {detail['wall_s']:.1f}s wall, "
                f"{detail.get('infer_s', '?')}s infer, "
                f"{detail['peak_rss_mb']:.0f}MB peak, "
                f"{detail.get('realtime_factor', '?')}x realtime, "
                f"exit={detail['exit_code']}"
            )
        else:
            print(f"  FAIL {detail.get('error', '')[-300:]}")

    working = [d for d in details if d["ok"]]
    full = [d for d in details if d["ok"] and d["text"] == "30s/multi-batch"]
    best_rt = max((d.get("realtime_factor", 0) for d in working), default=0)
    verdict = {
        "any_path_works": bool(working),
        "mps_works_single_batch": any(
            d["ok"] and d["device"] == "mps" and d["text"].startswith("short")
            for d in details),
        "mps_works_multi_batch": any(
            d["ok"] and d["device"] == "mps" and d["text"].startswith("30s")
            for d in details),
        "cpu_works": any(d["ok"] and d["device"] == "cpu" for d in details),
        "needs_mps_fallback_env": (
            not any(d["ok"] and d["label"] == "mps/short" for d in details)
            and any(d["ok"] and d["label"] == "mps+fallback/short" for d in details)
        ),
        "best_realtime_factor": best_rt,
        "best_peak_rss_mb": min((d["peak_rss_mb"] for d in working), default=None),
        # The verdict the phases doc actually asked for. Anything slower than
        # real time makes narration cost more than recording it yourself, which
        # is the point at which phase 8 should build `RemoteRunner` instead.
        "viable_locally": bool(full) and best_rt >= 1.0,
    }
    path = write_results("tts", {"verdict": verdict, "attempts": details})
    print(f"\n{json.dumps(verdict, indent=2)}\nwrote {path}")


if __name__ == "__main__":
    main()
