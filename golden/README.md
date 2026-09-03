# golden/

Fixtures whose right answer is known, so that a change to prompts, rules or
learned defaults can be replayed against them before it takes effect
(`../docs/architecture.md` §11).

Renders are slow, so regression compares **specs, not pixels**. `replay.py` is the
harness and `make replay` runs it: it replans each case with the profile loop
skipped entirely, then splits the diff by field origin — strict per-field for a
deterministic producer, distributional over N runs for a model-written one
(§11.1).

```sh
make replay                       # every case
python3 -m golden.replay demo_v1  # one
```

A failing deterministic field is printed with the stage that produced it, which is
usually the whole diagnosis. The distributional half prints the approved value, the
median across runs and the spread, so a wide-but-centred distribution reads as
noise rather than as a regression.

## What gets archived, and what does not

A **real take** is archived whole: the media, the recorder sidecar, and the
approved `EditSpec`. There is no other way to get it back.

A **synthetic fixture** is archived as a recipe. `ingest/fixtures.py` is
deterministic and byte-stable — the same command produces the same spec and the
same source video on any machine — so committing 115KB of regenerable focus points
would archive the output of a function next to the function. What is worth
committing is the *answer*: the checks that must fail, and for which profile.

## demo_v1

    python -m ingest.fixtures --out {out} --job-id golden-demo --no-video

The synthetic fixture, archived as a recipe. It has no `job.json`, so no stage
rewrites its spec — which makes it exactly one thing and a good one: the **strict
half of §11.1 over a whole real `EditSpec`**, several hundred deterministic fields
of focus track, captions, audio and edit decisions, checked to float noise. Every
test in this repository leans on `ingest/fixtures.py` being byte-stable, so the
case checking that has the widest blast radius in the set.

What it cannot do is exercise the model half: nothing here asks a model anything,
so its distributions are a baseline of zeroes and `replay` reports risk R5 as
**unmeasured** rather than as 0%. `--no-video` because a replay compares specs, and
encoding 24 seconds per run to compare no pixels is how a harness stops being run.

## broken_v1

    python -m ingest.fixtures --out data/fixtures/broken01 --job-id fixture --broken

Three breakages, chosen because the schema still permits them:

| Breakage | Check it must trip |
|---|---|
| A cut whose edges land inside words | `cut_mid_word` |
| A caption word too long to wrap | `caption_line_length` |
| An overlay anchored where the caption box is | `overlay_occlusion` |

`expected_findings.json` records the first two, which need only the spec. The
third reads overlay geometry from a compiled render, so `tests/test_verify.py`
asserts it where ffmpeg is available.

`caption_line_length` fails for `shorts_9x16` and not for `demo_16x9`, and that is
the point rather than an oversight: a 34-character word overruns a 20-character
vertical line and fits a 42-character widescreen one. A check that gave the same
answer for both would not be checking the profile.

## §9.2 is not here either, and for a third reason

The transcript round-trip cannot be exercised by any fixture in this directory.
Both fixtures' audio is a synthesized test tone, so ASR of their renders says
nothing whatever the edit did — a fixture cannot mispronounce a word it never
speaks. `tests/test_verify_transcript.py` runs it against constructed transcripts
instead, which is the same honest placement as the two breakages below, arrived at
from the opposite direction: those are checks the pipeline cannot *break*, this is
a check the fixture cannot *feed*.

The first real take promoted here fixes all three at once, and it is what
`SEAM_TOLERANCE_S` and `WER_CEILING` are waiting for.

## The distributional half is waiting on the same take

`Tolerances` in `replay.py` is a set of bands nothing has measured: how far the
retained fraction may move, how many segments count as the same edit, where a cut
may land relative to the approved one. They are stated per case, in one place, with
their provenance said out loud — so that the first real take moves numbers rather
than code. Until one exists, the distributional half runs against the scripted agent
in `tests/test_golden_replay.py`, which proves the harness and nothing about the
models. Same honest placement as §9.2 above, arrived at from the same direction.

## Two of §11's four breakages are missing

Overlapping caption blocks and a juddering crop are **not representable**.
`EditSpec` rejects overlapping blocks and `plan_focus` rate-limits the crop by
construction, so no fixture can carry either. Their checks are exercised in
`tests/test_verify.py` against hand-built inputs, which is the honest place for a
check on something the pipeline cannot produce — and the checks stay, because the
thing that makes them unrepresentable today is code that can change.
