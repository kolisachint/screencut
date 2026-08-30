"""Which FFmpeg is installed, and what it will accept.

One question, asked once: **how does this FFmpeg read a filter graph from a file?**

FFmpeg 9 removed `-filter_complex_script`, and the generic read-option-from-file
syntax that replaces it — `-/filter_complex` — does not exist before FFmpeg 7
(environment findings §8). There is no option both accept in every version this
project has to run on, and on a clean macOS box today there is no single FFmpeg
that has both libass and the old option, so this is not optional maintenance.

The guard is a **capability probe rather than a version comparison**, because the
thing that changed is exactly the thing `-h full` lists. A version string is a
proxy for that, and a poor one: distribution builds carry suffixes, git builds
report `N-109848-g0d1c2c9c1a` and no number at all, and a proxy that cannot be
parsed has to guess. Asking the binary cannot guess wrong.
"""

from __future__ import annotations

import functools
import re
import shutil
import subprocess

GRAPH_FROM_FILE_LEGACY = "-filter_complex_script"
"""Present up to FFmpeg 8; removed in 9."""

GRAPH_FROM_FILE_GENERIC = "-/filter_complex"
"""The generic `-/option file` form. FFmpeg 7 onward, and the only one 9 has."""

GRAPH_OPTIONS = (GRAPH_FROM_FILE_LEGACY, GRAPH_FROM_FILE_GENERIC)


class FfmpegMissing(RuntimeError):
    pass


def _help_full() -> str:
    if shutil.which("ffmpeg") is None:
        raise FfmpegMissing("ffmpeg is not on PATH")
    return subprocess.run(
        ["ffmpeg", "-hide_banner", "-h", "full"],
        capture_output=True, text=True,
    ).stdout


def graph_option_for(help_text: str) -> str:
    """The option this FFmpeg accepts, given its own `-h full` listing.

    Split from the subprocess call so the decision is testable against the help
    text of an FFmpeg that is not installed — which is every version except the
    one on the machine running the test.

    Where both options exist (FFmpeg 7 and 8) this picks the legacy one. It is the
    branch with years of use behind it, and preferring the newer syntax there would
    make the only versions that can cross-check the two never do so.
    """
    if re.search(r"^-filter_complex_script\b", help_text, re.MULTILINE):
        return GRAPH_FROM_FILE_LEGACY
    return GRAPH_FROM_FILE_GENERIC


@functools.lru_cache(maxsize=1)
def graph_option() -> str:
    """Cached because a render asks once and a job asks per profile."""
    return graph_option_for(_help_full())


def graph_option_or_legacy() -> str:
    """`graph_option`, but never raising when FFmpeg is absent.

    `compile` builds a command it does not run, and building a graph without
    FFmpeg installed is most of what the tests want (`compile/render.py`). The
    placeholder is harmless because `render` rewrites the option against the
    binary it is about to invoke.
    """
    try:
        return graph_option()
    except FfmpegMissing:
        return GRAPH_FROM_FILE_LEGACY


@functools.lru_cache(maxsize=1)
def version() -> str:
    """The first line's version token, for error messages and the render fingerprint.

    Best-effort on purpose: an unparseable version is not a failure here, because
    nothing decides anything on it. `graph_option` probes.
    """
    if shutil.which("ffmpeg") is None:
        raise FfmpegMissing("ffmpeg is not on PATH")
    first = subprocess.run(
        ["ffmpeg", "-version"], capture_output=True, text=True,
    ).stdout.splitlines()[:1]
    match = re.search(r"ffmpeg version (\S+)", first[0] if first else "")
    return match.group(1) if match else "unknown"


def with_graph_option(args: list[str], option: str) -> list[str]:
    """Rewrite a stored command's graph option to the one this FFmpeg accepts.

    `compile` writes the whole command into its manifest and `render` replays it,
    so a manifest built against one FFmpeg can be replayed against another — a
    cached compile plus a toolchain upgrade is precisely that case, and it fails
    with `Option not found` rather than anything that names the cause. The option
    belongs to the binary, so the stage that runs the binary is the one that
    decides it (§5.2).
    """
    rewritten = list(args)
    for index, arg in enumerate(rewritten):
        if arg in GRAPH_OPTIONS:
            rewritten[index] = option
            break  # the option appears once; anything later is a path, not an option
    return rewritten
