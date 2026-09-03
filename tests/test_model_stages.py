"""Phase 9's four model stages (architecture.md §7.1, §7.2, §7.4).

The adapter, the retry and the degradation path all came from phase 5, so what is
new here is four fragments and what each is allowed to be wrong about. Every one
of these tests is a claim about that boundary: what the model decides, what
arithmetic decides, and what happens when the model decides nothing at all.

None of this says anything about editorial taste, for the same reason phase 5's
tests did not: the agent is a scripted subprocess. What is proved is that our
code holds its side of the contract, which is the only half a test can hold.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from ingest.cap_fixture import write_bundle
from ingest.fixtures import DEFAULT_BEATS
from plan import emphasis as emphasis_plan
from plan import metadata as metadata_plan
from plan import overlays as overlays_plan
from plan import script as script_plan
from plan.context import word_budget
from prefs.loader import AgentConstraints, StageAgent
from runner import agent
from runner.cli import main as cli_main
from runner.pipeline import run_job
from runner.stages import JOB_STAGES, STAGES
from spec import Encoder
from spec.captions import CaptionBlock, Word
from spec.metadata import Metadata, MetadataCopy
from spec.migrations import load_spec_file
from spec.overlays import OverlayIntent, OverlayPlan, OverlayTemplate

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


# --- script_draft ------------------------------------------------------------


def test_a_draft_without_a_brief_is_refused_rather_than_invented():
    """§1.1's boundary, enforced. Written language is in scope because it is
    *your* language; a stage that picks a subject out of a duration and a cursor
    track is generating content, which this design does not do."""
    from spec.focus import FocusTrack

    with pytest.raises(script_plan.NoBrief, match="narration.brief"):
        script_plan.content_for(None, FocusTrack(), 30.0)
    with pytest.raises(script_plan.NoBrief):
        script_plan.content_for("   ", FocusTrack(), 30.0)


def test_the_word_budget_is_arithmetic_and_reaches_the_prompt():
    """A script too long for its recording cannot be trimmed by code without
    mangling language, so the constraint is stated up front rather than checked
    after the fact."""
    from spec.focus import FocusTrack

    content = script_plan.content_for("show the export flow", FocusTrack(), 60.0)
    assert f"about {word_budget(60.0)} words" in content
    assert "show the export flow" in content
    assert word_budget(60.0) == 150


def test_a_synthesized_job_may_start_from_a_brief_with_no_script_yet():
    """Otherwise `script_draft` could not exist: the document it is supposed to
    complete would be invalid until after it had run (decision #8, #20)."""
    from spec.narration import Narration, NarrationSource

    narration = Narration(
        source=NarrationSource.SYNTHESIZED,
        brief="walk through the export flow",
        voice_reference_path="ref.wav",
        voice_reference_text="this is my voice",
    )
    assert narration.script is None

    with pytest.raises(ValueError, match="script to read, or a brief"):
        Narration(
            source=NarrationSource.SYNTHESIZED,
            voice_reference_path="ref.wav",
            voice_reference_text="this is my voice",
        )


# --- emphasis ----------------------------------------------------------------


def blocks(*texts: str) -> list[CaptionBlock]:
    words = [
        Word(t_in=float(i), t_out=float(i) + 0.5, text=text) for i, text in enumerate(texts)
    ]
    return [CaptionBlock(t_in=0.0, t_out=float(len(texts)), words=words)]


def test_emphasis_is_indices_so_the_model_cannot_rewrite_a_word():
    """A fragment carrying word objects could come back with the text changed or
    a timing nudged, and every one of those is a caption that no longer matches
    the audio — which §9.2 would then report as a real failure."""
    assert set(emphasis_plan.EmphasisPlan.model_fields) == {"emphasize"}

    marked, count, dropped = emphasis_plan.apply(
        blocks("open", "the", "export", "panel"), emphasis_plan.EmphasisPlan(emphasize=[2])
    )
    assert [w.text for w in marked[0].words if w.emphasis] == ["export"]
    assert (count, dropped) == (1, 0)


def test_an_index_out_of_range_is_dropped_rather_than_retried():
    """A reply naming word 900 of 4 is off by a mistake no error message teaches
    it to fix, and §7.2's retry is for a reply the *schema* rejects."""
    marked, count, dropped = emphasis_plan.apply(
        blocks("open", "the", "panel"), emphasis_plan.EmphasisPlan(emphasize=[1, 99, -3])
    )
    assert (count, dropped) == (1, 2)
    assert [w.text for w in marked[0].words if w.emphasis] == ["the"]


def test_emphasis_marks_nothing_when_the_model_returns_nothing():
    """§7.4's row: no emphasis markers. A caption list is still a caption list."""
    marked, count, _ = emphasis_plan.apply(blocks("a", "b"), emphasis_plan.EmphasisPlan())
    assert count == 0 and not any(w.emphasis for w in marked[0].words)


def test_both_sides_number_the_words_the_same_way():
    """The hazard the index shape trades for the one it removes, contained by
    both sides calling one function."""
    unordered = [
        CaptionBlock(t_in=2.0, t_out=3.0, words=[Word(t_in=2.0, t_out=2.5, text="second")]),
        CaptionBlock(t_in=0.0, t_out=1.0, words=[Word(t_in=0.0, t_out=0.5, text="first")]),
    ]
    assert [w.text for w in emphasis_plan.numbered_words(unordered)] == ["first", "second"]


# --- plan_overlays -----------------------------------------------------------


def test_an_overlay_past_the_end_of_the_source_is_clamped_not_left_to_fail_the_job():
    """The reason `reconcile` exists at all.

    An overlay from 55s to 61s is a valid `OverlayIntent` and an invalid
    `EditSpec` — only the spec knows the source is 60s long. Without this the
    stage would fail the whole job at apply time, which §7.4 forbids in so many
    words: an LLM stage failure must not fail the job."""
    plan = OverlayPlan(overlays=[
        OverlayIntent(template=OverlayTemplate.LABEL_CHIP, text="late",
                      anchor={"x": 0.5, "y": 0.3}, t_in=55.0, t_out=61.0),
    ])
    kept = overlays_plan.reconcile(plan, 60.0)
    assert (kept[0].t_in, kept[0].t_out) == (55.0, 60.0)


def test_an_overlay_entirely_past_the_source_is_dropped():
    plan = OverlayPlan(overlays=[
        OverlayIntent(template=OverlayTemplate.LABEL_CHIP, text="gone",
                      anchor={"x": 0.5, "y": 0.3}, t_in=70.0, t_out=80.0),
    ])
    assert overlays_plan.reconcile(plan, 60.0) == []


def test_a_flash_of_an_overlay_is_dropped_rather_than_composited():
    """Shorter than `MIN_SPAN_S` is a flash, which is worse than the overlay
    being absent — the same judgement `compile/timeline.py` makes one step later
    on what survives a cut."""
    plan = OverlayPlan(overlays=[
        OverlayIntent(template=OverlayTemplate.LABEL_CHIP, text="blink",
                      anchor={"x": 0.5, "y": 0.3}, t_in=1.0, t_out=1.1),
    ])
    assert overlays_plan.reconcile(plan, 60.0) == []


def test_only_one_progress_pill_survives():
    """Two whole-output pills is one element stacked on itself, which is a
    rendering nobody asked for rather than a second opinion."""
    pill = OverlayIntent(template=OverlayTemplate.PROGRESS_PILL, text="")
    kept = overlays_plan.reconcile(OverlayPlan(overlays=[pill, pill]), 60.0)
    assert len(kept) == 1 and kept[0].spans_whole_output


def test_the_whole_output_overlay_sorts_under_the_anchored_ones():
    """Composite order is list order (`compile/graph.py`), and a progress pill is
    the ground the anchored overlays sit on rather than a thing that covers them."""
    kept = overlays_plan.reconcile(OverlayPlan(overlays=[
        OverlayIntent(template=OverlayTemplate.LABEL_CHIP, text="late",
                      anchor={"x": 0.5, "y": 0.3}, t_in=8.0, t_out=10.0),
        OverlayIntent(template=OverlayTemplate.PROGRESS_PILL, text=""),
        OverlayIntent(template=OverlayTemplate.CALLOUT_ARROW, text="early",
                      anchor={"x": 0.5, "y": 0.3}, t_in=1.0, t_out=3.0),
    ]), 60.0)
    assert [o.text for o in kept] == ["", "early", "late"]


def test_a_template_outside_the_closed_set_is_an_invalid_answer():
    """§6.3, and risk R5's real mitigation: the model chooses from a fixed set, so
    most wrong answers are *invalid* answers rather than plausible ones."""
    with pytest.raises(ValueError):
        OverlayPlan.model_validate({"overlays": [{"template": "exploding_gif", "text": "no"}]})


# --- the metadata sidecar ----------------------------------------------------


def test_the_sidecar_separates_what_was_measured_from_what_was_written():
    """A number *about* the render is not the model's to report — the same rule
    `Removal.proposed_by` follows (§7.2)."""
    assert set(MetadataCopy.model_fields) == {"title", "description", "tags"}
    assert {"job_id", "profile", "render", "duration_s"} <= set(Metadata.model_fields)


def test_tags_are_normalized_rather_than_trusted():
    """A model asked for bare lowercase tags returns "#Screen Recording" often
    enough that rejecting it would spend a retry on punctuation."""
    normalized = metadata_plan.normalize(MetadataCopy(
        title="t", description="d",
        tags=["#Screen Recording", "screen-recording", "  ", "Export!", "export"],
    ))
    assert normalized.tags == ["screen-recording", "export"]


def test_the_fallback_is_script_derived_and_invents_no_tags():
    """§7.4's row. It produces no *new* language: the first sentence of what you
    already wrote is your sentence, cut short. A guessed tag would be a fallback
    that does not look like one."""
    copy = metadata_plan.derive_copy(
        "Here is the export button. Most people miss it. It lives under settings. And more.",
        job_id="take01",
    )
    assert copy.title == "Here is the export button"
    assert copy.description.endswith("It lives under settings.")
    assert copy.tags == []


def test_a_job_with_nothing_said_still_gets_an_honest_sidecar():
    """A screen capture with the mic off is an ordinary job (§5.3), and the job id
    is the only true thing available."""
    copy = metadata_plan.derive_copy("", job_id="take01")
    assert copy.title == "take01" and "No narration" in copy.description


# --- per-stage configuration (decision #13) ----------------------------------


def test_each_stage_may_run_on_its_own_model_and_the_rest_keep_the_default():
    """"Overlay placement is not script drafting." Sparse overrides, same rule as
    `profiles:` — a block that restates the default is a second source of truth."""
    constraints = AgentConstraints(
        model="anthropic/claude-sonnet-5",
        timeout_s=300.0,
        stages={"script_draft": StageAgent(model="anthropic/claude-opus-5"),
                "plan_overlays": StageAgent(timeout_s=90.0)},
    )
    assert constraints.for_stage("script_draft").model == "anthropic/claude-opus-5"
    assert constraints.for_stage("script_draft").timeout_s == 300.0
    assert constraints.for_stage("plan_overlays").model == "anthropic/claude-sonnet-5"
    assert constraints.for_stage("plan_overlays").timeout_s == 90.0
    assert constraints.for_stage("emphasis").model == "anthropic/claude-sonnet-5"


def test_every_model_stage_declares_the_prompt_its_key_is_derived_from():
    """A blank instruction hashes to a constant, which is a cache key that has
    stopped distinguishing the thing it exists to distinguish (§5.2)."""
    model_stages = [s for s in (*JOB_STAGES.values(), *STAGES.values()) if s.model_backed]
    assert {s.name for s in model_stages} == {
        "script_draft", "emphasis", "plan_edit", "plan_overlays", "metadata"
    }
    versions = {s.name: agent.prompt_version(s.instruction) for s in model_stages}
    assert len(set(versions.values())) == len(versions), (
        "two stages sharing a prompt version means editing one re-runs the other"
    )


def test_a_model_stage_cannot_be_registered_without_its_prompt():
    import dataclasses

    from runner.stages import StageSpec, _emphasis_fingerprint, _run_emphasis

    with pytest.raises(ValueError, match="declares no instruction"):
        StageSpec(
            name="nameless", version=1, depends_on=(),
            fingerprint=_emphasis_fingerprint, run=_run_emphasis, model_backed=True,
        )


# --- the whole thing ---------------------------------------------------------


@pytest.fixture(scope="module")
def bundle(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("cap9") / "take.cap"
    return write_bundle(root, beats=DEFAULT_BEATS[:2], width=640, height=360, fps=30.0)


def ingested(bundle, tmp_path, name: str) -> Path:
    job = tmp_path / name
    cli_main(["ingest", str(bundle), "--out", str(job)])
    spec = json.loads((job / "spec.json").read_text())
    spec["source"]["has_audio"] = False  # no ASR on this machine
    (job / "spec.json").write_text(json.dumps(spec, indent=2) + "\n")
    return job


@needs_ffmpeg
def test_killing_the_network_mid_job_still_produces_a_cut_captioned_render(
    bundle, tmp_path, monkeypatch
):
    """Phase 9's first exit criterion, and §7.4's table end to end.

    Every model stage degrades and the job still renders: the edit is `trim`'s,
    there are no emphasis markers and no overlays, and the sidecar carries copy
    derived from what is said rather than nothing at all. A worse video, not a
    failed one — and every degradation is on the job record, because under
    decision #12 review is the only place a degraded job announces itself."""
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH'].split(os.pathsep)[-1]}")
    monkeypatch.setattr(agent, "available", lambda: False)
    job = ingested(bundle, tmp_path, "cut")

    result = run_job(job, db_path=tmp_path / "db.sqlite", encoder=Encoder.SOFTWARE)

    assert all(path.is_file() for path in result.renders.values())
    degraded = {note.split(":")[0].split("/")[-1] for note in result.degradations}
    assert degraded == {"emphasis", "plan_edit", "plan_overlays", "metadata"}

    spec = load_spec_file(job / "spec.json")
    assert spec.edit.covers(spec.source.duration), "the edit is still total (§4.4)"
    assert spec.overlays == [], "§7.4: no overlays"
    assert not any(w.emphasis for b in spec.captions for w in b.words), "§7.4: no markers"

    for profile, sidecar in result.sidecars.items():
        published = Metadata.model_validate_json(sidecar.read_text())
        assert published.degraded and published.post.title
        assert published.profile == profile


@needs_ffmpeg
def test_every_model_stage_returns_a_validated_fragment_and_it_reaches_the_spec(
    bundle, tmp_path, fake_agent
):
    """The other half of the first exit criterion: when the stages do run, what
    they decided is in the document everything else reads (principle 1)."""
    fake_agent.fragments(
        EmphasisPlan={"text": json.dumps({"emphasize": [0]})},
        OverlayPlan={"text": "```json\n" + json.dumps({"overlays": [
            {"template": "callout_arrow", "text": "Export",
             "anchor": {"x": 0.6, "y": 0.3}, "t_in": 1.0, "t_out": 4.0},
        ]}) + "\n```"},
        MetadataCopy={"text": json.dumps({
            "title": "The export button", "description": "Where it lives.",
            "tags": ["#Screen Recording"]})},
    )
    job = ingested(bundle, tmp_path, "planned")
    result = run_job(job, ["demo_16x9"], db_path=tmp_path / "db.sqlite", encoder=Encoder.SOFTWARE)

    assert not result.degradations, result.degradations
    spec = load_spec_file(job / "spec.json")
    assert [o.template for o in spec.overlays] == [OverlayTemplate.CALLOUT_ARROW]
    assert spec.overlays[0].text == "Export"

    sidecar = Metadata.model_validate_json(result.sidecars["demo_16x9"].read_text())
    assert not sidecar.degraded
    assert sidecar.post.title == "The export button"
    assert sidecar.post.tags == ["screen-recording"], "normalized, not trusted"
    assert sidecar.duration_s > 0.0 and sidecar.render.endswith("_demo_16x9.mp4")
    assert result.sidecars["demo_16x9"].parent == result.renders["demo_16x9"].parent


@needs_ffmpeg
def test_a_replay_records_what_the_schemas_rejected(bundle, tmp_path, fake_agent):
    """Risk R5's number, and the first time this pipeline has measured it (§7.2).

    A reply the schema rejects is counted; a *fenced* reply is not, because phase
    0 saw eleven of twelve arrive inside a fence and counting those would put the
    rate an order of magnitude high."""
    fake_agent.fragments(
        OverlayPlan=[{"text": json.dumps({"overlays": [{"template": "nope"}]})},
                     {"text": "```json\n{\"overlays\": []}\n```"}],
    )
    job = ingested(bundle, tmp_path, "violations")
    result = run_job(job, ["demo_16x9"], db_path=tmp_path / "db.sqlite", encoder=Encoder.SOFTWARE)

    assert not result.degradations, "one retry was enough (§7.2)"
    assert result.schema_violations == 1
    assert result.agent_calls == 5, "emphasis, plan_edit, two overlay attempts, metadata"
    overlays = next(o for o in result.outcomes if o.stage == "plan_overlays")
    assert (overlays.agent_calls, overlays.schema_violations) == (2, 1)
