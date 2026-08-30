"""`plan_edit`, the agent adapter, and §7.4's degradation (phase 5).

The agent is a subprocess (§5.1, decision #13), which is exactly what makes this
testable without a model: a script named `hoocode` on `PATH` that emits the JSON
event stream hoocode emits exercises the real adapter — the real invocation, the
real event-stream parse, the real fence stripping, the real validation and the
real cache. What it does not test is whether a model edits well, which is a
judgement about footage and belongs in front of a person (`implementation-phases.md`,
phase 5's stop-and-reassess gate).

The replies are shaped like the ones phase 0 actually saw, fence and all
(environment findings §7).
"""

from __future__ import annotations

import json
import os
import shutil
import stat
from pathlib import Path

import pytest

from ingest.cap_fixture import write_bundle
from ingest.fixtures import DEFAULT_BEATS
from plan.edit import (
    UNTIERED_REASON,
    EditPlan,
    ProposedRemoval,
    ProposedSegment,
    build_content,
    focus_summary,
    reconcile,
)
from prefs import resolve_profile
from runner import agent
from runner.cli import main as cli_main
from runner.pipeline import run_job
from spec import Encoder
from spec.edit import Removal, RemovalKind, Tier
from spec.focus import FocusKind, FocusPoint, FocusTrack
from spec.migrations import load_spec_file
from spec.origin import Stage

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")

FAKE = '''#!/usr/bin/env python3
"""Stands in for hoocode: emits the event stream, records that it was called."""
import json, pathlib, sys

here = pathlib.Path(__file__).resolve().parent
calls = here / "calls.txt"
index = len(calls.read_text().splitlines()) if calls.exists() else 0
with calls.open("a") as handle:
    handle.write(sys.argv[-1][:40].replace("\\n", " ") + "\\n")

replies = json.loads((here / "replies.json").read_text())
reply = replies[min(index, len(replies) - 1)]
if reply.get("exit"):
    sys.stderr.write(reply.get("text", ""))
    raise SystemExit(reply["exit"])
if reply.get("silent"):
    raise SystemExit(0)
print(json.dumps({"type": "message_start"}))
print(json.dumps({
    "type": "message_end",
    "message": {"role": "assistant", "content": [{"type": "text", "text": reply["text"]}]},
}))
'''


@pytest.fixture
def fake_agent(tmp_path, monkeypatch):
    """Install a fake `hoocode` on PATH and return a handle to script it."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    script = bin_dir / agent.BINARY
    script.write_text(FAKE)
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    class Handle:
        directory = bin_dir

        def replies(self, *replies: dict) -> None:
            (bin_dir / "replies.json").write_text(json.dumps(list(replies)))

        @property
        def calls(self) -> int:
            path = bin_dir / "calls.txt"
            return len(path.read_text().splitlines()) if path.exists() else 0

    handle = Handle()
    handle.replies({"text": "{}"})
    return handle


def plan_json(**overrides) -> str:
    plan = {
        "removals": [{"t_in": 0.0, "t_out": 1.0, "kind": "silence"}],
        "segments": [{"t_in": 1.0, "t_out": 6.0, "tier": "essential", "reason": "the claim"}],
    }
    plan.update(overrides)
    return json.dumps(plan)


# --- the adapter -------------------------------------------------------------


def test_the_invocation_restricts_the_agent_rather_than_asking_it_to_behave():
    """§7.3: a coding agent is built to explore and modify a repository, and as a
    stage it must do neither. Every flag here is a restriction."""
    command = agent.command("hello", model="anthropic/claude-sonnet-5")
    assert command[0] == agent.BINARY
    assert command[1:5] == ["-p", "--mode", "json", "--no-tools"]
    assert "--no-session" in command
    assert command[-1] == "hello"


def test_a_fenced_reply_is_not_a_schema_violation():
    """Eleven of phase 0's twelve replies arrived inside a ```json fence. Counting
    that as invalid would make R5 look worse than it is and send the mitigation
    after the wrong thing."""
    stream = json.dumps({
        "type": "message_end",
        "message": {"role": "assistant", "content": [{"type": "text", "text": '```json\n{"a": 1}\n```'}]},
    })
    text, fenced = agent.extract_fragment(stream)
    assert (text, fenced) == ('{"a": 1}', True)


def test_a_reply_wrapped_in_a_sentence_falls_back_to_the_outermost_braces():
    stream = json.dumps({
        "type": "message_end",
        "message": {"role": "assistant", "content": [{"type": "text", "text": 'Sure: {"a": 2} done'}]},
    })
    assert agent.extract_fragment(stream) == ('{"a": 2}', False)


def test_only_the_last_assistant_message_counts():
    stream = "\n".join(
        json.dumps({
            "type": "message_end",
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
        })
        for text in ('{"a": 1}', '{"a": 2}')
    )
    assert agent.extract_fragment(stream)[0] == '{"a": 2}'


def test_a_stream_with_no_assistant_text_extracts_nothing():
    assert agent.extract_fragment('{"type": "message_start"}') == (None, False)


def test_an_invalid_fragment_is_retried_once_with_the_error_appended(fake_agent, tmp_path):
    """§7.2's whole mitigation is three lines of control flow rather than a
    subsystem, and this is the middle line."""
    fake_agent.replies({"text": '{"removals": "not a list"}'}, {"text": plan_json()})
    outcome = agent.run_stage("go", EditPlan, job_dir=tmp_path)
    assert outcome.fragment is not None
    assert fake_agent.calls == 2
    assert "retry" in outcome.note

    second = (fake_agent.directory / "calls.txt").read_text().splitlines()[1]
    assert second.startswith("go")


def test_a_fragment_invalid_twice_degrades_rather_than_raising(fake_agent, tmp_path):
    """§7.4: an LLM stage failure must not fail the job, so this returns an
    outcome with no fragment rather than raising and making every caller write
    the same try block."""
    fake_agent.replies({"text": '{"removals": "no"}'}, {"text": '{"removals": "still no"}'})
    outcome = agent.run_stage("go", EditPlan, job_dir=tmp_path)
    assert outcome.degraded
    assert fake_agent.calls == 2


def test_a_nonzero_exit_is_not_retried(fake_agent, tmp_path):
    """A retry fixes a schema mistake. It does not fix a binary that failed."""
    fake_agent.replies({"exit": 2, "text": "boom"})
    outcome = agent.run_stage("go", EditPlan, job_dir=tmp_path)
    assert outcome.degraded
    assert fake_agent.calls == 1


def test_an_agent_that_is_not_installed_degrades_without_running_anything(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    outcome = agent.run_stage("go", EditPlan, job_dir=tmp_path)
    assert outcome.degraded
    assert "not on PATH" in outcome.note


def test_the_schema_in_the_prompt_is_the_one_that_validates_the_reply():
    """One definition doing two of its four jobs at once (§7.2) — the thing asked
    for and the thing checked cannot drift."""
    prompt = agent.build_prompt("do it", EditPlan, "content")
    assert json.dumps(EditPlan.model_json_schema(), indent=2) in prompt


def test_a_model_stage_is_keyed_on_its_model_and_prompt_version():
    assert set(agent.cache_params()) == {"model", "prompt_version"}


# --- reconciliation ----------------------------------------------------------


def test_the_model_returns_intent_and_the_partition_is_derived():
    """§4.4's totality holds by construction rather than by the model landing
    float arithmetic, which would buy a retry on almost every call."""
    plan = EditPlan(
        removals=[ProposedRemoval(t_in=0.0, t_out=1.0, kind=RemovalKind.SILENCE)],
        segments=[ProposedSegment(t_in=1.0, t_out=3.0, tier=Tier.OPTIONAL, reason="sign-off")],
    )
    decisions = reconcile(plan, [], 6.0)
    assert decisions.covers(6.0)


def test_a_gap_the_model_did_not_tier_is_kept_as_essential():
    """Losing material nobody looked at is worse than running long, and §9.1
    reports the overrun with a number attached either way."""
    decisions = reconcile(EditPlan(), [], 6.0)
    assert [s.tier for s in decisions.segments] == [Tier.ESSENTIAL]
    assert decisions.segments[0].reason == UNTIERED_REASON


def test_a_removal_overlapping_a_trim_proposal_is_attributed_to_trim():
    """The override rate is a number about the model, so the model does not get to
    write it."""
    proposals = [Removal(t_in=0.0, t_out=1.0, kind=RemovalKind.SILENCE, proposed_by=Stage.TRIM)]
    plan = EditPlan(removals=[
        ProposedRemoval(t_in=0.0, t_out=1.0, kind=RemovalKind.SILENCE),
        ProposedRemoval(t_in=3.0, t_out=3.4, kind=RemovalKind.FALSE_START),
    ])
    decisions = reconcile(plan, proposals, 6.0)
    assert [r.proposed_by for r in decisions.removals] == [Stage.TRIM, Stage.PLAN_EDIT]


def test_a_proposal_the_model_dropped_simply_does_not_survive():
    """Rejecting a trim proposal is the point of §7.1 — a two-second gap can be a
    deliberate beat, and arithmetic cannot tell."""
    proposals = [Removal(t_in=2.0, t_out=4.0, kind=RemovalKind.SILENCE, proposed_by=Stage.TRIM)]
    decisions = reconcile(EditPlan(), proposals, 6.0)
    assert decisions.removals == []
    assert decisions.covers(6.0)


def test_overlapping_model_removals_are_merged_rather_than_rejected():
    plan = EditPlan(removals=[
        ProposedRemoval(t_in=1.0, t_out=3.0, kind=RemovalKind.SILENCE),
        ProposedRemoval(t_in=2.0, t_out=4.0, kind=RemovalKind.FILLER),
    ])
    decisions = reconcile(plan, [], 6.0)
    assert [(r.t_in, r.t_out) for r in decisions.removals] == [(1.0, 4.0)]


def test_a_removal_running_past_the_source_is_clamped_not_rejected():
    plan = EditPlan(removals=[ProposedRemoval(t_in=5.0, t_out=99.0, kind=RemovalKind.SILENCE)])
    decisions = reconcile(plan, [], 6.0)
    assert decisions.removals[0].t_out == 6.0
    assert decisions.covers(6.0)


def test_neighbouring_gaps_with_the_same_tier_and_reason_become_one_segment():
    """A gap the model said nothing about must not arrive in review as fifty
    identical rows."""
    plan = EditPlan(segments=[
        ProposedSegment(t_in=0.0, t_out=2.0, tier=Tier.ESSENTIAL, reason="same"),
        ProposedSegment(t_in=2.0, t_out=4.0, tier=Tier.ESSENTIAL, reason="same"),
    ])
    decisions = reconcile(plan, [], 6.0)
    assert len(decisions.segments) == 2  # the tiered stretch, then the untiered tail
    assert (decisions.segments[0].t_in, decisions.segments[0].t_out) == (0.0, 4.0)


# --- the prompt --------------------------------------------------------------


def test_no_duration_budget_reaches_the_prompt():
    """§4.4.1: tiers are aspect-independent taste decided once. A budget in the
    prompt makes the ranking depend on the profile, and then one `EditSpec` cannot
    render at two lengths and a shorter short costs a model call."""
    profile = resolve_profile("shorts_9x16")
    content = build_content([], [], FocusTrack(), 24.0)
    assert str(profile.duration_budget) not in content
    assert "budget" not in content.lower()


def test_the_focus_summary_carries_clicks_and_dwell_and_not_the_whole_track():
    """The one thing `FocusTrack` knows that the transcript does not is which
    stretches were a demonstration. Movement is not that."""
    track = FocusTrack(points=[
        FocusPoint(t=t / 10, x=0.5, y=0.5, kind=FocusKind.CLICK if t == 5 else FocusKind.DWELL)
        for t in range(40)
    ])
    summary = focus_summary(track)
    assert "clicks" in summary and "dwell" in summary
    assert len(summary.splitlines()) < 10


# --- the stage, end to end ---------------------------------------------------


@pytest.fixture(scope="module")
def bundle(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("cap5") / "take.cap"
    return write_bundle(root, beats=DEFAULT_BEATS[:2], width=640, height=360, fps=30.0)


def _job(bundle, tmp_path, name: str) -> Path:
    job = tmp_path / name
    cli_main(["ingest", str(bundle), "--out", str(job)])
    spec = json.loads((job / "spec.json").read_text())
    spec["source"]["has_audio"] = False  # no ASR on this machine; trim finds no fillers
    (job / "spec.json").write_text(json.dumps(spec, indent=2) + "\n")
    return job


@needs_ffmpeg
def test_an_unreachable_agent_still_produces_a_render(bundle, tmp_path, monkeypatch):
    """Phase 5's exit criterion, and §7.4's first row: killing the network mid-job
    still produces a render — the `trim`-only one, all segments essential."""
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH'].split(os.pathsep)[-1]}")
    monkeypatch.setattr(agent, "available", lambda: False)
    job = _job(bundle, tmp_path, "degraded")

    result = run_job(job, db_path=tmp_path / "db.sqlite", encoder=Encoder.SOFTWARE)
    assert all(path.is_file() for path in result.renders.values())
    assert any("plan_edit" in note for note in result.degradations)

    spec = load_spec_file(job / "spec.json")
    assert spec.edit.covers(spec.source.duration)
    assert all(s.tier is Tier.ESSENTIAL for s in spec.edit.segments)
    assert all(r.proposed_by is Stage.TRIM for r in spec.edit.removals)


@needs_ffmpeg
def test_a_degraded_stage_is_not_cached_so_the_next_run_tries_again(bundle, tmp_path, fake_agent):
    """One lost network must not become permanent."""
    fake_agent.replies({"exit": 1, "text": "no network"})
    job = _job(bundle, tmp_path, "retry")
    db = tmp_path / "db.sqlite"

    run_job(job, db_path=db, encoder=Encoder.SOFTWARE)
    assert fake_agent.calls == 1
    run_job(job, db_path=db, encoder=Encoder.SOFTWARE)
    assert fake_agent.calls == 2


@needs_ffmpeg
def test_re_running_an_unchanged_job_calls_no_model(bundle, tmp_path, fake_agent):
    """Phase 5's exit criterion. Phase 0 measured 6-66s per call, so if this is
    wrong every later phase is slow and expensive (§5.2)."""
    fake_agent.replies({"text": f"```json\n{plan_json()}\n```"})
    job = _job(bundle, tmp_path, "cached")
    db = tmp_path / "db.sqlite"

    first = run_job(job, db_path=db, encoder=Encoder.SOFTWARE)
    assert not first.degradations, first.degradations
    assert fake_agent.calls == 1

    again = run_job(job, db_path=db, encoder=Encoder.SOFTWARE)
    assert again.did_no_work, again.ran()
    assert fake_agent.calls == 1


@needs_ffmpeg
def test_one_spec_renders_at_two_lengths_under_two_budgets_without_a_second_model_call(
    bundle, tmp_path, fake_agent
):
    """Phase 5's exit criterion and §4.4.1's argument in one: tiering is decided
    once, and how much of it survives is arithmetic per profile."""
    fake_agent.replies({"text": json.dumps({
        "removals": [],
        "segments": [
            {"t_in": 0.0, "t_out": 6.0, "tier": "essential", "reason": "the claim"},
            {"t_in": 6.0, "t_out": 12.0, "tier": "optional", "reason": "sign-off"},
        ],
    })})
    job = _job(bundle, tmp_path, "budgets")
    db = tmp_path / "db.sqlite"

    generous = resolve_profile("demo_16x9").model_copy(update={"duration_budget": 30.0})
    tight = generous.model_copy(update={"name": "tight", "duration_budget": 8.0})

    run_job(job, [generous], db_path=db, encoder=Encoder.SOFTWARE)
    run_job(job, [tight], db_path=db, encoder=Encoder.SOFTWARE)
    assert fake_agent.calls == 1, "a shorter short must not cost a model call (§4.4.1)"

    from verify.probe import probe

    long_render = probe(job / "renders" / f"{load_spec_file(job / 'spec.json').job_id}_demo_16x9.mp4")
    short_render = probe(job / "renders" / f"{load_spec_file(job / 'spec.json').job_id}_tight.mp4")
    assert long_render.duration > short_render.duration + 1.0
