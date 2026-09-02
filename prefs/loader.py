"""Reading `constraints.yaml` and layering it over the built-in profiles.

The overrides are sparse and deep-merged, so this file states differences rather
than restating defaults. A file that restates defaults is a second source of truth
which drifts from the first, and the drifted one is always the one being read.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from spec.profiles import BUILTIN_PROFILES, Encoder, RenderProfile

CONSTRAINTS_PATH = Path(__file__).resolve().parent / "constraints.yaml"


class CaptionConstraints(BaseModel):
    model_config = ConfigDict(extra="forbid")
    font_family: str | None = None


class AsrConstraints(BaseModel):
    """Which ASR runs, and where its weights are.

    `backend` is a single fixed value rather than a choice: phase 0 measured the
    alternatives and only this one has a parser written against output somebody
    has seen (`synth/asr.py`). It is spelled out anyway so a future second backend
    is a value change here rather than an archaeology exercise."""

    model_config = ConfigDict(extra="forbid")
    backend: Literal["whisper.cpp"] = "whisper.cpp"
    model: str = "large-v3"
    binary: str = "whisper-cli"
    models_dir: str = "~/.cache/screencut/whisper"
    language: str = "en"


class TtsConstraints(BaseModel):
    """How the one permitted synthesis is invoked (decision #20, phase 8).

    `python` rather than a binary: phase 0 measured F5-TTS through its API in an
    environment of its own, and that is the invocation `synth/tts.py` is written
    against. `device` is a real choice rather than a formality — MPS is 2.2x
    faster than CPU and aborts as soon as the text needs more than one batch
    (environment findings §4), so a narration of any length is a CPU job on this
    machine until that is fixed upstream."""

    model_config = ConfigDict(extra="forbid")
    backend: Literal["f5-tts"] = "f5-tts"
    python: str = "python3"
    device: Literal["cpu", "mps", "cuda"] = "cpu"
    library_path: str | None = None
    """macOS's FFmpeg lookup for torchcodec. Set only on the machine that needs it —
    it is the same variable that breaks cairo elsewhere (environment findings §8)."""
    reference_seconds: float = Field(default=10.0, gt=0.0)


class TrimConstraints(BaseModel):
    """§4.6's tunables. Every one is learnable by median under §10."""

    model_config = ConfigDict(extra="forbid")
    silence_db: float = -35.0
    min_silence_ms: int = Field(default=600, ge=0)
    keep_pad_ms: int = Field(default=120, ge=0)
    filler_words: list[str] = Field(default_factory=lambda: ["um", "uh", "erm", "uhm", "mmm", "hmm"])


class AgentConstraints(BaseModel):
    """Which model the LLM stages run on (decision #13)."""

    model_config = ConfigDict(extra="forbid")
    model: str = "anthropic/claude-sonnet-5"


class VoiceConstraints(BaseModel):
    model_config = ConfigDict(extra="forbid")
    allow_synthesis: bool = True


class EncodeConstraints(BaseModel):
    model_config = ConfigDict(extra="forbid")
    golden_encoder: Encoder = Encoder.SOFTWARE


class Constraints(BaseModel):
    """The hand-written tier. Nothing in here is ever written by the pipeline."""

    model_config = ConfigDict(extra="forbid")

    captions: CaptionConstraints = Field(default_factory=CaptionConstraints)
    asr: AsrConstraints = Field(default_factory=AsrConstraints)
    tts: TtsConstraints = Field(default_factory=TtsConstraints)
    trim: TrimConstraints = Field(default_factory=TrimConstraints)
    agent: AgentConstraints = Field(default_factory=AgentConstraints)
    voice: VoiceConstraints = Field(default_factory=VoiceConstraints)
    encode: EncodeConstraints = Field(default_factory=EncodeConstraints)
    profiles: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
        description="Sparse per-profile overrides, deep-merged onto the built-in profile.",
    )


def load_constraints(path: Path | str = CONSTRAINTS_PATH) -> Constraints:
    raw = yaml.safe_load(Path(path).read_text()) or {}
    return Constraints.model_validate(raw)


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def resolve_profile(name: str, constraints: Constraints | None = None) -> RenderProfile:
    """The built-in profile with `constraints.yaml`'s overrides applied.

    Re-validated after merging, so an override that breaks an invariant — a caption
    box pushed outside the safe area, say — fails here rather than at render time.
    """
    constraints = constraints if constraints is not None else load_constraints()
    try:
        builtin = BUILTIN_PROFILES[name]
    except KeyError:
        raise KeyError(f"unknown render profile {name!r}; have {sorted(BUILTIN_PROFILES)}") from None

    overrides = constraints.profiles.get(name)
    if not overrides:
        profile = builtin
    else:
        profile = RenderProfile.model_validate(_deep_merge(builtin.model_dump(mode="json"), overrides))

    font = constraints.captions.font_family
    if font and font != profile.captions.font_family:
        profile = profile.model_copy(
            update={"captions": profile.captions.model_copy(update={"font_family": font})}
        )
    return profile


def resolve_profiles(constraints: Constraints | None = None) -> dict[str, RenderProfile]:
    constraints = constraints if constraints is not None else load_constraints()
    return {name: resolve_profile(name, constraints) for name in BUILTIN_PROFILES}
