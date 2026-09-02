"""`RemoteRunner` — a stage run somewhere else (architecture.md §5.1, decision #2).

The seam was always going to be used; phase 0 decided when. F5-TTS on the target
machine runs at 0.11x realtime and crashes above one batch (environment findings
§4), so a three-minute narration costs about an hour and the phase-8 opening move
is this file rather than a local `tts` stage. That is the substitution decision #2
deferred, and the whole point of §5.1 is that nothing above it changes: the
pipeline builds the same `StageRequest` and reads the same `StageResult`.

**What travels, and why that is the interesting part.** `StageRequest` says every
path is relative to `job_dir` and the stage runs with `job_dir` as its working
directory. That is not tidiness — it is the property that makes a stage portable,
and until something actually moved a stage it was a claim rather than a fact.
This runner sends the job directory's *inputs*, runs the stage against them, and
brings the artifact back. A stage that reached outside its job directory fails
here, visibly, which is the test `tests/test_remote.py` exists to keep.

`stages/` is sent selectively: the artifacts this request names as inputs, and no
others. A job that has been through a few correction cycles has a `stages/`
directory larger than the recording, and shipping all of it over a network for a
stage that reads one file is the difference between a usable remote and a
theoretical one.

**One transport is written, and it is the one that can be tested here.**
`DirectoryTransport` puts the workspace on a filesystem this machine can see and
runs the stage as a subprocess. An SSH or object-store transport is the same
three methods against a worker, and it is deliberately not written until there is
a worker to write it against — the standing rule in `AGENTS.md` about code for
things nobody has run applies to transports too.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Protocol

from runner.contract import StageFailed, StageRequest, StageResult

DEFAULT_TIMEOUT_S = 7200
"""Longer than `LocalRunner`'s. The stages that go remote are the ones that were
too slow to keep, and a transfer sits on top of that."""

#: Never sent. `renders/` is published output rather than input, and `stages/` is
#: sent one named artifact at a time — see `files_to_send`.
NOT_INPUT = ("renders", "stages")


class Transport(Protocol):
    """Move files to and from a worker, and run a command there.

    Three methods because that is all §5.1 needs: the contract is a subprocess
    reading JSON on stdin, so a transport that can copy a file and start a process
    can carry any stage in this pipeline.
    """

    def send(self, local: Path, remote: str) -> None: ...

    def receive(self, remote: str, local: Path) -> None: ...

    def execute(self, argv: list[str], stdin: str, *, timeout_s: float) -> tuple[int, str, str]: ...

    def workspace(self, name: str) -> str: ...


@dataclass
class DirectoryTransport:
    """A worker reachable as a directory: a mounted volume, or this machine.

    Useful in its own right — a job directory on a share that a bigger box also
    mounts needs no network protocol at all — and it is what proves the portability
    claim in the tests, because a stage that read anything outside the workspace
    would not find it here either.
    """

    root: Path
    python: str = sys.executable
    repo_root: Path = field(default_factory=lambda: Path(__file__).resolve().parent.parent)

    def workspace(self, name: str) -> str:
        return str(Path(self.root) / name)

    def send(self, local: Path, remote: str) -> None:
        destination = Path(remote)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if local.is_dir():
            shutil.copytree(local, destination, dirs_exist_ok=True)
        else:
            shutil.copy2(local, destination)

    def receive(self, remote: str, local: Path) -> None:
        source = Path(remote)
        if not source.exists():
            raise StageFailed(f"the worker produced nothing at {remote}")
        local.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, local, dirs_exist_ok=True)
        else:
            shutil.copy2(source, local)

    def execute(self, argv: list[str], stdin: str, *, timeout_s: float) -> tuple[int, str, str]:
        completed = subprocess.run(
            [self.python, *argv],
            input=stdin,
            capture_output=True,
            text=True,
            cwd=self.repo_root,
            timeout=timeout_s,
        )
        return completed.returncode, completed.stdout, completed.stderr


def files_to_send(job_dir: Path, request: StageRequest) -> list[str]:
    """Job-directory paths this stage needs, relative to the job directory.

    Everything but `stages/` and `renders/` — the recording, the spec, the voice
    reference, `job.json` — plus exactly the stage artifacts this request names.
    A stage reads the spec and then reads paths *out of* the spec, so "send what
    `inputs` names" is not enough; "send everything" is not affordable once the
    cache has a few cycles in it. This is the line between them.
    """
    send: list[str] = []
    for entry in sorted(job_dir.iterdir()):
        if entry.name in NOT_INPUT:
            continue
        send.append(entry.name)
    for path in request.inputs.values():
        if (job_dir / path).exists() and path not in send:
            send.append(path)
    return send


class RemoteRunner:
    """Runs a stage on a worker and brings its artifact home.

    Interchangeable with `LocalRunner`: same `run`, same `StageResult`, same
    `StageFailed` for every way a stage can not produce one (§7.4). The
    `holds_local_weights` flag is accepted and ignored, and that is the point of
    routing a stage here — §16's one-model-at-a-time ceiling is about *this*
    machine's memory, and a stage that runs elsewhere is not spending it.
    """

    def __init__(self, transport: Transport, *, timeout_s: float = DEFAULT_TIMEOUT_S):
        self.transport = transport
        self.timeout_s = timeout_s

    def run(self, request: StageRequest, *, holds_local_weights: bool = False) -> StageResult:
        job_dir = Path(request.job_dir)
        workspace = self.transport.workspace(job_dir.name)
        for relative in self._send(job_dir, request):
            self.transport.send(job_dir / relative, f"{workspace}/{relative}")

        remote_request = request.model_copy(update={"job_dir": workspace})
        try:
            code, stdout, stderr = self.transport.execute(
                ["-m", "runner.stages", request.stage],
                remote_request.model_dump_json(),
                timeout_s=self.timeout_s,
            )
        except subprocess.TimeoutExpired as expired:
            raise StageFailed(f"{request.stage} timed out after {self.timeout_s}s on the worker") from expired

        if code != 0:
            raise StageFailed(f"{request.stage} exited {code} on the worker\n{stderr.strip()}")
        try:
            result = StageResult.model_validate_json(stdout)
        except ValueError as invalid:
            raise StageFailed(
                f"{request.stage} produced unparseable stdout on the worker: {stdout[:400]!r}"
            ) from invalid

        self.transport.receive(f"{workspace}/{result.output}", job_dir / result.output)
        return result

    def _send(self, job_dir: Path, request: StageRequest) -> Iterable[str]:
        return files_to_send(job_dir, request)
