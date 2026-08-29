# golden/

Fixtures whose right answer is known, so that a change to prompts, rules or
learned defaults can be replayed against them before it takes effect
(`../docs/architecture.md` §11).

Renders are slow, so regression compares **specs, not pixels**. The replay
harness — and the split by field origin, strict for deterministic fields and
distributional for model-written ones (§11.1) — arrives in phase 9. What is here
now is what phase 6 needs: a fixture that is deliberately bad, and the findings
§9.1 must produce for it.

## What gets archived, and what does not

A **real take** is archived whole: the media, the recorder sidecar, and the
approved `EditSpec`. There is no other way to get it back.

A **synthetic fixture** is archived as a recipe. `ingest/fixtures.py` is
deterministic and byte-stable — the same command produces the same spec and the
same source video on any machine — so committing 115KB of regenerable focus points
would archive the output of a function next to the function. What is worth
committing is the *answer*: the checks that must fail, and for which profile.

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

## Two of §11's four breakages are missing

Overlapping caption blocks and a juddering crop are **not representable**.
`EditSpec` rejects overlapping blocks and `plan_focus` rate-limits the crop by
construction, so no fixture can carry either. Their checks are exercised in
`tests/test_verify.py` against hand-built inputs, which is the honest place for a
check on something the pipeline cannot produce — and the checks stay, because the
thing that makes them unrepresentable today is code that can change.
