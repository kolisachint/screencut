"""The FFmpeg option guard (environment findings §8).

FFmpeg 9 removed `-filter_complex_script` and FFmpeg 6 does not have
`-/filter_complex`, so the render stage has to ask which one it is talking to.
The decision is tested against captured help text rather than against the
installed binary, because the interesting case is always the version this machine
does not have.
"""

from __future__ import annotations

import subprocess

import pytest

from compile.ffmpeg import (
    GRAPH_FROM_FILE_GENERIC,
    GRAPH_FROM_FILE_LEGACY,
    graph_option,
    graph_option_for,
    with_graph_option,
)

# Verbatim from `ffmpeg -h full`, which is where the option either is or is not.
HELP_WITH_LEGACY = """\
-filter_complex_threads  number of threads for -filter_complex
-lavfi filter_graph  create a complex filtergraph
-filter_complex graph_description  create a complex filtergraph
-filter_complex_script filename  read complex filtergraph description from a file
"""

# FFmpeg 9: the line is gone, and nothing replaces it — `-/filter_complex` is
# generic `-/option file` syntax and is never listed as an option of its own.
HELP_WITHOUT_LEGACY = """\
-filter_complex_threads  number of threads for -filter_complex
-lavfi filter_graph  create a complex filtergraph
-filter_complex graph_description  create a complex filtergraph
"""


def test_an_ffmpeg_that_lists_filter_complex_script_is_given_it():
    assert graph_option_for(HELP_WITH_LEGACY) == GRAPH_FROM_FILE_LEGACY


def test_an_ffmpeg_without_filter_complex_script_is_given_the_generic_form():
    assert graph_option_for(HELP_WITHOUT_LEGACY) == GRAPH_FROM_FILE_GENERIC


def test_filter_complex_threads_alone_does_not_look_like_the_removed_option():
    """The two share a prefix, and a substring test reads FFmpeg 9 as FFmpeg 8 —
    silently, since the wrong branch only fails once a render runs."""
    assert graph_option_for("-filter_complex_threads  number of threads\n") == GRAPH_FROM_FILE_GENERIC


def test_the_installed_ffmpeg_accepts_the_option_the_guard_picks():
    """The guard against reality: whatever it chose, this FFmpeg parses it.

    `-f null` with no inputs makes FFmpeg reach option parsing and stop, so a
    surviving `Option not found` is the guard being wrong rather than the graph.
    """
    option = graph_option()
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostdin", option, "/nonexistent-graph.txt", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    assert "Option not found" not in result.stderr, result.stderr


def test_a_stored_command_is_rewritten_for_the_ffmpeg_that_will_run_it():
    """A compile artifact outlives the FFmpeg it was built against, and a cache
    hit plus a toolchain upgrade is exactly that pairing."""
    stored = ["ffmpeg", "-y", "-i", "in.mp4", GRAPH_FROM_FILE_LEGACY, "graph.txt", "-map", "[vout]"]
    rewritten = with_graph_option(stored, GRAPH_FROM_FILE_GENERIC)
    assert rewritten == ["ffmpeg", "-y", "-i", "in.mp4", GRAPH_FROM_FILE_GENERIC, "graph.txt", "-map", "[vout]"]


def test_rewriting_stops_at_the_option_so_a_later_argument_spelled_like_it_survives():
    """The option occurs once. Everything after it is a value, and a value that
    happens to read as an option is still a value."""
    stored = ["ffmpeg", GRAPH_FROM_FILE_LEGACY, GRAPH_FROM_FILE_LEGACY]
    rewritten = with_graph_option(stored, GRAPH_FROM_FILE_GENERIC)
    assert rewritten == ["ffmpeg", GRAPH_FROM_FILE_GENERIC, GRAPH_FROM_FILE_LEGACY]
