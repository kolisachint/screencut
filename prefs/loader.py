"""Reading `constraints.yaml` and layering it over the built-in profiles.

The overrides are sparse and deep-merged, so this file states differences rather
than restating defaults. A file that restates defaults is a second source of truth
which drifts from the first, and the drifted one is always the one being read.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from spec.profiles import BUILTIN_PROFILES, Encoder, RenderProfile

CONSTRAINTS_PATH = Path(__file__).resolve().parent / "constraints.yaml"


class CaptionConstraints(BaseModel):
    model_config = ConfigDict(extra="forbid")
    font_family: str | None = None


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
