"""A stage run somewhere else (phase 8, architecture.md §5.1).

`StageRequest` has claimed since phase 3 that every path is relative to the job
directory and that a stage cannot see outside it. Nothing tested the claim,
because nothing moved a stage. Phase 0 made the move necessary — F5-TTS is 0.11x
realtime on the target machine (environment findings §4) — so these are the tests
that turn the claim into a fact.

The transport here puts the workspace on the same filesystem. That is not a
weaker test than a network one for the property being tested: the stage runs
against a directory it did not build, containing only what was sent, and a stage
reaching for anything else fails exactly as it would over a wire.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from runner.contract import StageFailed, StageRequest
from runner.remote import DirectoryTransport, RemoteRunner, files_to_send


@pytest.fixture
def job(tmp_path) -> Path:
    """A job directory with a recording, a spec, and a cache full of old artifacts."""
    job_dir = tmp_path / "job"
    (job_dir / "source").mkdir(parents=True)
    (job_dir / "stages").mkdir()
    (job_dir / "source" / "source.mp4").write_bytes(b"not really a video")
    (job_dir / "spec.json").write_text(json.dumps({"spec_version": 2, "job_id": "j"}))
    (job_dir / "stages" / "wanted.json").write_text('{"words": []}')
    for stale in ("old1.json", "old2.json", "old3.mp4"):
        (job_dir / "stages" / stale).write_bytes(b"x" * 4096)
    (job_dir / "renders").mkdir()
    (job_dir / "renders" / "j_demo_16x9.mp4").write_bytes(b"y" * 4096)
    return job_dir


def request_for(job: Path) -> StageRequest:
    return StageRequest(
        stage="echo",
        job_dir=str(job),
        inputs={"spec": "spec.json", "transcript": "stages/wanted.json"},
        params={},
        output="stages/out.json",
    )


def test_only_the_inputs_travel_and_the_old_cache_stays_home(job):
    """The line between "send what `inputs` names" and "send everything".

    A stage reads the spec and then reads paths *out of* the spec — the recording,
    the voice reference — so naming inputs is not enough. But a job a few
    correction cycles old has a `stages/` directory larger than the recording, and
    shipping all of it for a stage that reads one file is the difference between a
    usable remote and a theoretical one.
    """
    sent = files_to_send(job, request_for(job))

    assert "source" in sent and "spec.json" in sent
    assert "stages/wanted.json" in sent, "the artifact this request actually names"
    assert not any(name.startswith("stages/old") for name in sent), "superseded artifacts stay home"
    assert "renders" not in sent, "published output is not an input"


STAGE_MODULE = '''
"""A stand-in worker package: one stage that reads its inputs and writes a file."""
import json, sys
from pathlib import Path

request = json.loads(sys.stdin.read())
job = Path(request["job_dir"])
transcript = json.loads((job / request["inputs"]["transcript"]).read_text())
out = job / request["output"]
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps({"saw": sorted(p.name for p in job.iterdir()),
                           "words": transcript["words"]}))
print(json.dumps({"stage": request["stage"], "output": request["output"],
                  "note": "ran on the worker"}))
'''


@pytest.fixture
def worker(tmp_path) -> DirectoryTransport:
    """A worker whose `runner.stages` is a stub, so the transport is what is tested."""
    root = tmp_path / "worker"
    repo = tmp_path / "worker-repo"
    (repo / "runner").mkdir(parents=True)
    (repo / "runner" / "__init__.py").write_text("")
    (repo / "runner" / "stages.py").write_text(STAGE_MODULE)
    return DirectoryTransport(root=root, repo_root=repo)


def test_a_stage_runs_on_the_worker_and_its_artifact_comes_home(job, worker):
    """The whole contract in one assertion: same request in, same result out, and
    the artifact on this machine afterwards."""
    result = RemoteRunner(worker).run(request_for(job))

    assert result.note == "ran on the worker"
    assert (job / "stages" / "out.json").is_file(), "the artifact came back"
    saw = json.loads((job / "stages" / "out.json").read_text())["saw"]
    assert "spec.json" in saw and "source" in saw
    assert "renders" not in saw, "the worker never saw what it was not sent"


def test_the_flag_that_serializes_local_stages_is_not_the_worker_s_problem(job, worker):
    """§16 serializes stages that hold *this machine's* memory. A stage that runs
    elsewhere is not spending it, which is the whole reason `tts` is routed here."""
    result = RemoteRunner(worker).run(request_for(job), holds_local_weights=True)
    assert result.output == "stages/out.json"


def test_a_worker_that_fails_is_a_stage_failure_like_any_other(job, tmp_path):
    """§7.4 collapses every way a stage can fail into one branch, and a remote one
    has more ways than a local one. None of them is a new kind of failure."""
    repo = tmp_path / "broken-repo"
    (repo / "runner").mkdir(parents=True)
    (repo / "runner" / "__init__.py").write_text("")
    (repo / "runner" / "stages.py").write_text("import sys; sys.exit(9)")

    runner = RemoteRunner(DirectoryTransport(root=tmp_path / "w2", repo_root=repo))
    with pytest.raises(StageFailed, match="exited 9 on the worker"):
        runner.run(request_for(job))


def test_a_worker_that_says_it_wrote_a_file_it_did_not_is_caught(job, tmp_path):
    """The failure a local runner cannot have: a plausible `StageResult` and no
    artifact behind it. Trusting stdout here would leave the pipeline recording a
    cache row for a file that is not on this machine."""
    repo = tmp_path / "lying-repo"
    (repo / "runner").mkdir(parents=True)
    (repo / "runner" / "__init__.py").write_text("")
    (repo / "runner" / "stages.py").write_text(
        'import json, sys; sys.stdin.read();'
        ' print(json.dumps({"stage": "echo", "output": "stages/out.json"}))'
    )

    runner = RemoteRunner(DirectoryTransport(root=tmp_path / "w3", repo_root=repo))
    with pytest.raises(StageFailed, match="produced nothing"):
        runner.run(request_for(job))
