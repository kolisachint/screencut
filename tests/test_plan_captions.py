"""`plan_captions`: word timings into blocks (§6.2, phase 4)."""

from __future__ import annotations

import pytest

from plan.captions import MIN_WORDS, PAUSE_S, capacity, plan_captions, tightest
from prefs import resolve_profile
from spec.captions import Word

SHORTS = resolve_profile("shorts_9x16")
DEMO = resolve_profile("demo_16x9")
BOTH = [SHORTS, DEMO]


def words(*spec: tuple[str, float, float]) -> list[Word]:
    return [Word(text=t, t_in=a, t_out=b) for t, a, b in spec]


def evenly(text: str, *, start: float = 0.0, each: float = 0.30, gap: float = 0.0) -> list[Word]:
    out: list[Word] = []
    t = start
    for token in text.split():
        out.append(Word(text=token, t_in=t, t_out=t + each))
        t += each + gap
    return out


def test_a_sentence_ending_closes_the_block():
    blocks = plan_captions(evenly("this ships today. that one does not"), BOTH)
    assert [b.text for b in blocks] == ["this ships today.", "that one does not"]


def test_a_pause_closes_the_block_even_mid_sentence():
    said = evenly("open the settings panel") + evenly("and export it", start=4.0)
    blocks = plan_captions(said, BOTH)
    assert [b.text for b in blocks] == ["open the settings panel", "and export it"]


def test_a_gap_shorter_than_a_pause_does_not_break_the_line():
    said = evenly("open the panel", gap=PAUSE_S * 0.5)
    assert len(plan_captions(said, BOTH)) == 1


def test_one_word_never_stands_alone_on_a_full_stop():
    """`Fig. 2` ends a sentence as far as any regex is concerned, and a lone word
    flashing on screen is what believing it looks like in the render."""
    blocks = plan_captions(evenly("see fig. two below"), BOTH)
    assert all(len(b.words) >= MIN_WORDS for b in blocks)


def test_a_block_never_exceeds_the_capacity_of_the_tightest_profile():
    """§4.1: one `EditSpec` serves N profiles, so there is one caption list. Only
    the narrowest box makes that list correct in every profile."""
    limit = tightest(BOTH)
    blocks = plan_captions(evenly(" ".join(["word"] * 60)), BOTH)
    assert blocks
    assert all(len(b.text) <= limit for b in blocks)
    assert limit == min(capacity(SHORTS), capacity(DEMO))


def test_sizing_against_the_wider_profile_alone_would_overflow_the_narrower_one():
    """The check on the check: this is the failure the tightest-profile rule
    prevents, so it has to be reachable when the rule is dropped."""
    assert capacity(DEMO) > capacity(SHORTS)
    wide = plan_captions(evenly(" ".join(["word"] * 60)), [DEMO])
    assert any(len(b.text) > capacity(SHORTS) for b in wide)


def test_a_block_spans_exactly_its_words():
    """Padding either side is how two blocks on a fast speaker come to overlap,
    which `EditSpec` rejects — at the end of a long job rather than here."""
    said = evenly("open the panel")
    block = plan_captions(said, BOTH)[0]
    assert block.t_in == said[0].t_in
    assert block.t_out == said[-1].t_out


def test_blocks_do_not_overlap_and_ascend():
    blocks = plan_captions(evenly("one two three. four five six. seven eight nine"), BOTH)
    for previous, following in zip(blocks, blocks[1:]):
        assert following.t_in >= previous.t_out


def test_every_word_survives_into_exactly_one_block():
    said = evenly("here is the dashboard. and this is the export button people miss")
    blocks = plan_captions(said, BOTH)
    assert [w.text for b in blocks for w in b.words] == [w.text for w in said]


def test_a_recording_with_no_speech_produces_no_blocks():
    """A screen capture with the mic off is an ordinary job, not a failure."""
    assert plan_captions([], BOTH) == []


def test_a_single_word_take_still_produces_a_block():
    assert [b.text for b in plan_captions(evenly("hello"), BOTH)] == ["hello"]


def test_a_word_longer_than_the_whole_capacity_gets_its_own_block():
    long_word = "x" * (tightest(BOTH) + 10)
    blocks = plan_captions(words(("open", 0.0, 0.3), (long_word, 0.3, 0.6), ("now", 0.6, 0.9)), BOTH)
    assert [b.text for b in blocks] == ["open", long_word, "now"]
