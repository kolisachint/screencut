"""Measurement harness for the phase-0 environment spike.

`docs/implementation-phases.md` phase 0 imposes two rules on how the measuring is
done, and they are the reason this is a module rather than a shell one-liner:

1. **Peak RSS beside every timing.** On a base-model 8GB M1 Air (architecture.md
   §16) resident size is what decides a design; the seconds only describe it. A
   benchmark that reports time alone cannot answer "what is the memory budget per
   stage", which is one of phase 0's four verdicts.
2. **Burst or sustained, said out loud.** A fanless machine gives two different
   answers. The first run of a workload is burst; the steady state after thermal
   headroom is spent is sustained, and only that one governs a real job. So the
   harness repeats a workload and reports both, rather than timing it once and
   letting the reader guess which number they got.

Nothing here is pipeline code - phase 0 is explicit that it contains none. This
exists to produce `docs/measurements/*.json`, which is what the findings document
is written from.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import statistics
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

MEASUREMENTS_DIR = Path(__file__).resolve().parent.parent / "docs" / "measurements"

#: How often the RSS poller samples the process tree. Fast enough to catch a
#: model's weights landing, cheap enough not to distort a short workload.
POLL_INTERVAL_S = 0.05

#: Wrapping every child in `/usr/bin/time -l` to get macOS's *phys_footprint*.
#:
#: This is not belt-and-braces, it is the correction that made this harness
#: honest. Resident set size misses MLX's unified-memory allocations almost
#: entirely: mlx-whisper on `large-v3` polls at 1.33GB RSS and reports a 5.66GB
#: peak footprint for the same run. Footprint is the number macOS charges you
#: and therefore the number that decides whether an 8GB machine swaps, so it is
#: the one the memory-budget verdict is built on. RSS is kept beside it because
#: the gap between them is itself the finding.
TIME_BIN = "/usr/bin/time"

_FOOTPRINT = re.compile(r"(\d+)\s+peak memory footprint")
_MAXRSS = re.compile(r"(\d+)\s+maximum resident set size")


# --------------------------------------------------------------------------
# Process-tree resident size
# --------------------------------------------------------------------------


def _ps_table() -> dict[int, tuple[int, int]]:
    """pid -> (ppid, rss_bytes) for every process, from one `ps` call."""
    proc = subprocess.run(
        ["ps", "-axo", "pid=,ppid=,rss="], capture_output=True, text=True
    )
    table: dict[int, tuple[int, int]] = {}
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) != 3:
            continue
        try:
            pid, ppid, rss_kb = int(parts[0]), int(parts[1]), int(parts[2])
        except ValueError:
            continue
        table[pid] = (ppid, rss_kb * 1024)
    return table


def tree_rss(root: int) -> int:
    """Resident bytes of `root` plus every descendant.

    Summed, not maxed, because the ceiling the 8GB machine hits is the *total*
    footprint: an ASR backend that forks four workers is four copies of the
    weights as far as the swapper is concerned, and reporting the largest single
    process would hide exactly the case phase 0 is looking for.
    """
    table = _ps_table()
    if root not in table:
        return 0
    children: dict[int, list[int]] = {}
    for pid, (ppid, _) in table.items():
        children.setdefault(ppid, []).append(pid)
    total = 0
    stack = [root]
    seen: set[int] = set()
    while stack:
        pid = stack.pop()
        if pid in seen or pid not in table:
            continue
        seen.add(pid)
        total += table[pid][1]
        stack.extend(children.get(pid, ()))
    return total


# --------------------------------------------------------------------------
# Swap
# --------------------------------------------------------------------------

_SWAP_USED = re.compile(r"used\s*=\s*([0-9.]+)([BKMG])")


def swap_used_bytes() -> int:
    """Bytes currently swapped out, from `sysctl vm.swapusage`.

    Phase 0 asks for this explicitly. The failure mode on 8GB is not a crash but
    swap, and swap presents as ordinary slowness - so it has to be read directly
    rather than inferred from a timing that looks disappointing.
    """
    proc = subprocess.run(
        ["sysctl", "-n", "vm.swapusage"], capture_output=True, text=True
    )
    match = _SWAP_USED.search(proc.stdout)
    if not match:
        return 0
    value, unit = float(match.group(1)), match.group(2)
    return int(value * {"B": 1, "K": 1024, "M": 1024**2, "G": 1024**3}[unit])


# --------------------------------------------------------------------------
# A single timed run
# --------------------------------------------------------------------------


@dataclass
class Run:
    """One execution of a workload, with everything phase 0 requires recorded."""

    label: str
    wall_s: float
    peak_rss_bytes: int
    exit_code: int
    swap_delta_bytes: int

    #: macOS phys_footprint: what the OS actually charges, including the
    #: unified-memory allocations RSS does not see. This, not `peak_rss_bytes`,
    #: is what an 8GB budget should be checked against.
    peak_footprint_bytes: int = 0
    cmd: list[str] = field(default_factory=list)
    stdout_tail: str = ""
    stderr_tail: str = ""
    note: str = ""

    #: The whole of stdout, for callers that have to parse it (the agent CLI's
    #: event stream is tens of KB). Excluded from the committed record by
    #: `to_record`, because a results file nobody can read is not a result.
    stdout_full: str = field(default="", repr=False)

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    def to_record(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if k != "stdout_full"}

    @property
    def peak_rss_mb(self) -> float:
        return self.peak_rss_bytes / 1024**2

    @property
    def peak_footprint_mb(self) -> float:
        return (self.peak_footprint_bytes or self.peak_rss_bytes) / 1024**2


def _tail(text: str, limit: int = 2000) -> str:
    text = text.strip()
    return text if len(text) <= limit else "..." + text[-limit:]


def _split_resource_block(stderr: str) -> tuple[str, int, int]:
    """Pull `/usr/bin/time -l`'s trailing block off the child's own stderr.

    Returns `(stderr_without_block, footprint_bytes, maxrss_bytes)`. The block is
    appended after the command's output, so it is found by matching rather than
    by position - a crashing child can interleave.
    """
    footprint = _FOOTPRINT.search(stderr)
    maxrss = _MAXRSS.search(stderr)
    if not footprint and not maxrss:
        return stderr, 0, 0
    lines = [
        line for line in stderr.splitlines()
        if not _FOOTPRINT.search(line) and not _MAXRSS.search(line)
        and not re.match(r"\s+[\d.]+\s+real\s+[\d.]+\s+user", line)
        and not re.match(r"\s+\d+\s+[a-z].*[a-z]$", line)
    ]
    return (
        "\n".join(lines),
        int(footprint.group(1)) if footprint else 0,
        int(maxrss.group(1)) if maxrss else 0,
    )


def run_once(
    label: str,
    cmd: Sequence[str],
    *,
    env: dict[str, str] | None = None,
    cwd: str | Path | None = None,
    stdin_text: str | None = None,
    note: str = "",
) -> Run:
    """Run `cmd`, polling its process tree for peak resident size."""
    full_env = {**os.environ, **(env or {})}
    peak = 0
    swap_before = swap_used_bytes()

    # `-l` appends a resource block to stderr once the child exits; it is split
    # back off below so callers still see only the command's own stderr.
    wrapped = [TIME_BIN, "-l", *cmd] if Path(TIME_BIN).exists() else list(cmd)

    proc = subprocess.Popen(
        wrapped,
        stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=full_env,
        cwd=str(cwd) if cwd else None,
    )

    stop = threading.Event()

    def poll() -> None:
        nonlocal peak
        while not stop.is_set():
            peak = max(peak, tree_rss(proc.pid))
            stop.wait(POLL_INTERVAL_S)

    poller = threading.Thread(target=poll, daemon=True)
    started = time.perf_counter()
    poller.start()
    stdout, stderr = proc.communicate(stdin_text)
    wall = time.perf_counter() - started
    stop.set()
    poller.join(timeout=1.0)

    stderr, footprint, maxrss = _split_resource_block(stderr or "")

    return Run(
        label=label,
        wall_s=round(wall, 3),
        # The poller and `time -l` disagree by design: take whichever saw more.
        peak_rss_bytes=max(peak, maxrss),
        exit_code=proc.returncode,
        swap_delta_bytes=swap_used_bytes() - swap_before,
        peak_footprint_bytes=footprint,
        cmd=list(cmd),
        stdout_tail=_tail(stdout or ""),
        stderr_tail=_tail(stderr),
        note=note,
        stdout_full=stdout or "",
    )


def run_callable(label: str, fn: Callable[[], Any], *, note: str = "") -> Run:
    """Time an in-process callable the same way, for work that has no CLI.

    Used where a backend is a Python library with no usable command line. The
    RSS figure is the *whole interpreter's*, which is the honest number: an
    in-process stage pays for the interpreter and its imports too, and phase 0's
    budget question is about what the machine holds, not what the library claims.
    """
    peak = 0
    swap_before = swap_used_bytes()
    stop = threading.Event()

    def poll() -> None:
        nonlocal peak
        while not stop.is_set():
            peak = max(peak, tree_rss(os.getpid()))
            stop.wait(POLL_INTERVAL_S)

    poller = threading.Thread(target=poll, daemon=True)
    started = time.perf_counter()
    poller.start()
    exit_code, tail = 0, ""
    try:
        result = fn()
        tail = _tail(str(result) if result is not None else "")
    except Exception as exc: # a failure is a finding, not a crash
        exit_code, tail = 1, _tail(f"{type(exc).__name__}: {exc}")
    wall = time.perf_counter() - started
    stop.set()
    poller.join(timeout=1.0)

    return Run(
        label=label,
        wall_s=round(wall, 3),
        peak_rss_bytes=peak,
        exit_code=exit_code,
        swap_delta_bytes=swap_used_bytes() - swap_before,
        stdout_tail=tail,
        note=note,
    )


# --------------------------------------------------------------------------
# Burst vs sustained
# --------------------------------------------------------------------------


@dataclass
class Measurement:
    """A workload run several times, reported as burst *and* sustained.

    `burst_s` is the first run - cold caches, full thermal headroom, the number a
    casual benchmark reports. `sustained_s` is the median of the rest, which is
    what a real job of any length actually gets on a fanless machine.
    """

    label: str
    runs: list[Run]

    @property
    def burst_s(self) -> float:
        return self.runs[0].wall_s

    @property
    def sustained_s(self) -> float:
        later = [r.wall_s for r in self.runs[1:]] or [self.runs[0].wall_s]
        return round(statistics.median(later), 3)

    @property
    def peak_rss_bytes(self) -> int:
        return max(r.peak_rss_bytes for r in self.runs)

    @property
    def peak_footprint_bytes(self) -> int:
        return max(r.peak_footprint_bytes for r in self.runs)

    @property
    def ok(self) -> bool:
        return all(r.ok for r in self.runs)

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "ok": self.ok,
            "burst_s": self.burst_s,
            "sustained_s": self.sustained_s,
            "peak_rss_mb": round(self.peak_rss_bytes / 1024**2, 1),
            "peak_footprint_mb": round(self.peak_footprint_bytes / 1024**2, 1),
            "swap_delta_mb": round(
                max(r.swap_delta_bytes for r in self.runs) / 1024**2, 1
            ),
            "repeats": len(self.runs),
            "runs": [r.to_record() for r in self.runs],
        }


def repeat(
    label: str,
    cmd: Sequence[str],
    *,
    repeats: int = 3,
    env: dict[str, str] | None = None,
    cwd: str | Path | None = None,
    note: str = "",
) -> Measurement:
    """Run a workload `repeats` times back to back, so sustained speed is real.

    Back to back deliberately: leaving a gap lets the chassis cool and produces a
    second burst number wearing a sustained label.
    """
    runs = [
        run_once(f"{label}#{i}", cmd, env=env, cwd=cwd, note=note)
        for i in range(repeats)
    ]
    return Measurement(label=label, runs=runs)


# --------------------------------------------------------------------------
# Host description and result files
# --------------------------------------------------------------------------


def _sysctl(name: str) -> str:
    proc = subprocess.run(["sysctl", "-n", name], capture_output=True, text=True)
    return proc.stdout.strip()


def host_facts() -> dict[str, Any]:
    """Identify the machine, so a committed measurement stays interpretable."""
    return {
        "cpu": _sysctl("machdep.cpu.brand_string"),
        "cores": _sysctl("hw.ncpu"),
        "memory_gb": round(int(_sysctl("hw.memsize") or 0) / 1024**3, 1),
        "os": f"{platform.system()} {platform.mac_ver()[0] or platform.release()}",
        "arch": platform.machine(),
        "python": platform.python_version(),
        "ffmpeg": _binary_version("ffmpeg"),
        "measured_at": time.strftime("%Y-%m-%d"),
    }


def _binary_version(name: str) -> str:
    path = shutil.which(name)
    if not path:
        return "absent"
    proc = subprocess.run([path, "-version"], capture_output=True, text=True)
    first = (proc.stdout or proc.stderr).splitlines()
    return first[0] if first else path


def write_results(name: str, payload: dict[str, Any]) -> Path:
    """Commit a measurement to `docs/measurements/<name>.json`.

    Phase 0's deliverable is a recorded number, not a number someone saw once.
    """
    MEASUREMENTS_DIR.mkdir(parents=True, exist_ok=True)
    out = MEASUREMENTS_DIR / f"{name}.json"
    body = {"host": host_facts(), **payload}
    out.write_text(json.dumps(body, indent=2, sort_keys=False) + "\n")
    return out


def report(measurements: Sequence[Measurement]) -> str:
    """A terminal summary in the shape the findings document wants."""
    width = max((len(m.label) for m in measurements), default=10)
    lines = [
        f"{'workload'.ljust(width)}  {'burst':>9}  {'sustained':>9}  "
        f"{'peak RSS':>10}  {'footprint':>11}  ok"
    ]
    for m in measurements:
        lines.append(
            f"{m.label.ljust(width)}  {m.burst_s:>8.2f}s  {m.sustained_s:>8.2f}s  "
            f"{m.peak_rss_bytes / 1024**2:>8.0f}MB  "
            f"{m.peak_footprint_bytes / 1024**2:>9.0f}MB  {'yes' if m.ok else 'NO'}"
        )
    return "\n".join(lines)
