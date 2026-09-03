"""Fixtures shared by more than one test module.

Two stand-ins live here, for the same reason: a phase after the one that wrote
them needed the same subprocess for a different purpose.

The agent stand-in arrived with phase 5 to test `plan_edit`, and phase 7 needs it
because a review correction is only interesting on a job whose *planners are
cached* — and the only way to get one of those without a model on the machine is
to script the subprocess the model would be. The ASR stand-in arrived with phase
6 for §9.2's round-trip, and phase 8's `align` runs the same binary for §5.3's
other purpose.

The agent is a subprocess (§5.1, decision #13), which is exactly what makes this
possible: a script named `hoocode` on `PATH` that emits the JSON event stream
hoocode emits exercises the real adapter — the real invocation, the real
event-stream parse, the real fence stripping, the real validation and the real
cache. What it does not test is whether a model edits well, which is a judgement
about footage and belongs in front of a person.

The replies are shaped like the ones phase 0 actually saw, fence and all
(environment findings §7).
"""

import json
import os
import stat

import pytest

from runner import agent

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


# --- the ASR stand-in --------------------------------------------------------

FAKE_WHISPER = """#!/usr/bin/env python3
# A stand-in for `whisper-cli`, in the shape of the agent stand-in above. It
# writes what whisper.cpp's `-oj` writes — the shape `synth/asr.py`'s parser is
# written against — reading the words from a file the test put beside it. It
# proves the stage runs end to end and proves nothing whatever about recognition.
import json, sys
from pathlib import Path

argv = sys.argv[1:]
prefix = Path(argv[argv.index("-of") + 1])
words = json.loads((Path(__file__).parent / "heard.json").read_text())
prefix.with_suffix(".json").write_text(json.dumps({
    "result": {"language": "en"},
    "transcription": [
        {"offsets": {"from": round(w["t_in"] * 1000), "to": round(w["t_out"] * 1000)},
         "text": " " + w["text"]}
        for w in words
    ],
}))
"""


@pytest.fixture
def whisper_stand_in(tmp_path, monkeypatch):
    """Install a fake `whisper-cli` on PATH and return a handle to script it.

    Phase 6 wrote it for the round-trip; phase 8 needs the same thing for `align`,
    which is §5.3's other ASR call and runs the same binary for the other purpose.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    script = bin_dir / "whisper-cli"
    script.write_text(FAKE_WHISPER)
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    # `transcribe` refuses without weights as well as without a binary, and it is
    # right to: "installed but unusable" is the state that otherwise fails deep
    # inside whisper.cpp with an unreadable message.
    (tmp_path / "ggml-large-v3.bin").write_bytes(b"")

    def hears(spoken):
        (bin_dir / "heard.json").write_text(
            json.dumps([{"t_in": w.t_in, "t_out": w.t_out, "text": w.text} for w in spoken])
        )

    hears([])
    return hears
