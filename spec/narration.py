"""Narration provenance (decisions #8 and #20).

Decision #20 permits exactly one synthesis: your voice, your script, your
reference audio. architecture.md §1.1 calls that a schema-and-config matter
rather than a matter of intent, so it is one — a synthesized narration without an
explicitly recorded voice reference does not validate.

`brief` and `script` are the two halves of decision #8, and the origins say which
is which: the brief is yours (`Stage.HUMAN`), the script is either yours or
`script_draft`'s. §1.1 puts written language in scope precisely because it never
reaches a frame without passing through you — and the brief is where you are in
that loop, so a job with neither is a job `script_draft` refuses rather than
invents from.
"""

from __future__ import annotations

from enum import Enum

from pydantic import model_validator

from spec.origin import Stage, spec_field
from spec.types import SpecModel


class NarrationSource(str, Enum):
    RECORDED = "recorded"
    """The take's own audio. The phase-4 path, and the default."""

    SYNTHESIZED = "synthesized"
    """F5-TTS from your own reference audio (decision #20)."""


class Narration(SpecModel):
    source: NarrationSource = spec_field(default=NarrationSource.RECORDED, produced_by=Stage.CONFIG)
    brief: str | None = spec_field(
        default=None,
        produced_by=Stage.HUMAN,
        description="What the video should say, in your words. `script_draft` turns it into lines to read.",
    )
    script: str | None = spec_field(
        default=None,
        produced_by=Stage.SCRIPT_DRAFT,
        description="Supplied or AI-drafted (decision #8). Null when narration is whatever you said on the take.",
    )
    voice_reference_path: str | None = spec_field(
        default=None,
        produced_by=Stage.CONFIG,
        description="Per-job reference audio, relative to the job directory. Required to synthesize.",
    )
    voice_reference_text: str | None = spec_field(
        default=None,
        produced_by=Stage.CONFIG,
        description="What the reference clip says. F5-TTS conditions on it, and phase 0 passed it explicitly.",
    )
    voice_consent_note: str | None = spec_field(
        default=None,
        produced_by=Stage.HUMAN,
        description="Recorded on the job so the boundary of decision #20 is auditable, not assumed.",
    )
    audio_path: str | None = spec_field(
        default=None,
        produced_by=Stage.TTS,
        description="Synthesized narration, relative to the job directory. Null until `tts` has run.",
    )

    @model_validator(mode="after")
    def _synthesis_needs_your_voice(self) -> "Narration":
        if self.source is NarrationSource.SYNTHESIZED:
            if not self.voice_reference_path:
                raise ValueError("synthesized narration requires an explicit per-job voice reference (decision #20)")
            if not self.script and not self.brief:
                # A script *or* the brief `script_draft` will draft one from.
                # Decision #20's boundary is that the words are yours, and both
                # of these are yours — `script_draft` drafts a brief, it does not
                # choose a subject (§1.1, plan/script.py). Requiring the script
                # itself would make a job that has not run `script_draft` yet an
                # invalid document, which would put the stage out of reach of the
                # one recipe that needs it.
                raise ValueError(
                    "synthesized narration requires a script to read, or a brief for "
                    "script_draft to draft one from (decision #8)"
                )
            if not self.voice_reference_text:
                # F5-TTS conditions on what the reference says as well as on how it
                # sounds, and phase 0 ran it with the text supplied. Letting it be
                # blank hands the reference to an ASR path nobody here has run
                # (environment findings §4), inside a stage that is already the
                # slowest one.
                raise ValueError("synthesized narration requires the voice reference's own text")
        elif self.audio_path:
            # A recorded take's narration *is* its own audio track. A synthesized
            # file hanging off a recorded job would be an audio spine the compiler
            # silently prefers over the recording (compile/graph.py), which is a
            # wrong render rather than an invalid document — so make it invalid.
            raise ValueError("only synthesized narration carries its own audio file")
        return self
