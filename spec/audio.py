"""Audio levels (architecture.md §4.1, §9.1). No model participates: loudness is
measured, and ducking follows from the measurement."""

from __future__ import annotations

from pydantic import model_validator

from spec.origin import Stage, spec_field
from spec.types import Decibels, SpecModel


class AudioTrack(SpecModel):
    """Narration and an optional bed, with the loudness targets §9.1 checks against."""

    narration_gain_db: Decibels = spec_field(default=0.0, produced_by=Stage.AUDIO)
    music_path: str | None = spec_field(
        default=None,
        produced_by=Stage.CONFIG,
        description="Bed, relative to the job directory. A licensed track is a source like any other (§15).",
    )
    music_gain_db: Decibels = spec_field(default=-18.0, produced_by=Stage.AUDIO)
    duck_db: Decibels = spec_field(
        default=-12.0,
        produced_by=Stage.AUDIO,
        description="How far the bed drops under narration. Negative.",
    )
    target_lufs: Decibels = spec_field(default=-14.0, produced_by=Stage.CONFIG)
    true_peak_ceiling_dbtp: Decibels = spec_field(default=-1.0, produced_by=Stage.CONFIG)

    @model_validator(mode="after")
    def _sane(self) -> "AudioTrack":
        if self.duck_db > 0:
            raise ValueError("duck_db lowers the bed under narration and must not be positive")
        if self.true_peak_ceiling_dbtp > 0:
            raise ValueError("true peak ceiling must be at or below 0 dBTP")
        return self
