"""Render profiles, and the smaller schema-level commitments (§4.1, §6.3, decision #20)."""

import pytest
from pydantic import ValidationError

from spec import (
    DEMO_16X9,
    SHORTS_9X16,
    AudioTrack,
    CaptionStyle,
    EncodeSettings,
    Encoder,
    FocusMode,
    Narration,
    NarrationSource,
    OverlayIntent,
    OverlayTemplate,
    Point,
    Rect,
    RenderProfile,
    SafeArea,
    profile,
)


def test_the_two_builtin_profiles_are_the_two_aspects():
    assert SHORTS_9X16.aspect < 1.0 < DEMO_16X9.aspect
    assert profile("shorts_9x16") is SHORTS_9X16
    with pytest.raises(KeyError, match="unknown render profile"):
        profile("tiktok")


def test_each_profile_carries_both_projections():
    """A render differs from its source in space *and* in time (§4.1)."""
    assert SHORTS_9X16.focus.mode is FocusMode.CROP_PATH
    assert DEMO_16X9.focus.mode is FocusMode.ZOOM_KEYFRAMES
    assert SHORTS_9X16.duration_budget < DEMO_16X9.duration_budget


def test_caption_geometry_is_per_profile():
    """Caption Y in vertical is a different number from caption Y in widescreen; a
    single global default averages them into a value wrong for both (§4.1)."""
    assert SHORTS_9X16.captions.box.y != DEMO_16X9.captions.box.y
    assert SHORTS_9X16.captions.max_chars_per_line < DEMO_16X9.captions.max_chars_per_line


def test_a_caption_box_outside_the_safe_area_is_rejected():
    with pytest.raises(ValidationError, match="outside the safe area"):
        RenderProfile(
            name="bad",
            width=1080,
            height=1920,
            duration_budget=30.0,
            safe_area=SafeArea(top=0.1, right=0.05, bottom=0.1, left=0.05),
            captions=CaptionStyle(box=Rect(x=0.02, y=0.5, w=0.5, h=0.1)),
            focus={"mode": "crop_path"},
        )


def test_golden_renders_have_a_software_encode_path():
    """Hardware encoders are not bit-reproducible across machines (§16)."""
    assert set(Encoder) == {Encoder.VIDEOTOOLBOX, Encoder.SOFTWARE}
    assert DEMO_16X9.encode.encoder is Encoder.VIDEOTOOLBOX
    assert DEMO_16X9.model_copy(update={"encode": DEMO_16X9.encode.model_copy(update={"encoder": Encoder.SOFTWARE})})


def test_normalized_coordinates_are_enforced_not_merely_intended():
    """Principle 2: the model never emits pixels, and the schema is what says so."""
    with pytest.raises(ValidationError):
        Point(x=1920, y=1080)


def test_an_overlay_is_a_template_choice_not_a_layout():
    assert set(OverlayTemplate) == {
        OverlayTemplate.CALLOUT_ARROW,
        OverlayTemplate.HIGHLIGHT_BOX,
        OverlayTemplate.LABEL_CHIP,
        OverlayTemplate.PROGRESS_PILL,
    }
    with pytest.raises(ValidationError):
        OverlayIntent(template="animated_hero_wipe")


def test_an_overlay_spanning_the_whole_output_needs_no_anchor():
    """There is no second time base (§4.5)."""
    pill = OverlayIntent(template=OverlayTemplate.PROGRESS_PILL)
    assert pill.spans_whole_output and pill.anchor is None
    with pytest.raises(ValidationError, match="needs both t_in and t_out"):
        OverlayIntent(template=OverlayTemplate.LABEL_CHIP, anchor=Point(x=0.5, y=0.5), t_in=1.0)


def test_synthesis_is_bounded_by_the_schema_not_by_intent():
    """Decision #20: your voice, your script, your reference audio — or no synthesis."""
    with pytest.raises(ValidationError, match="per-job voice reference"):
        Narration(source=NarrationSource.SYNTHESIZED, script="hello")
    with pytest.raises(ValidationError, match="requires a script"):
        Narration(source=NarrationSource.SYNTHESIZED, voice_reference_path="source/voice.wav")
    # What the reference *says* is an input to F5-TTS, not a nicety: phase 0 ran it
    # with the text supplied, and leaving it blank falls back to a path nobody here
    # has run (environment findings §4).
    with pytest.raises(ValidationError, match="reference's own text"):
        Narration(
            source=NarrationSource.SYNTHESIZED,
            script="hello",
            voice_reference_path="source/voice.wav",
        )
    assert Narration(
        source=NarrationSource.SYNTHESIZED,
        script="hello",
        voice_reference_path="source/voice.wav",
        voice_reference_text="this is my voice",
        voice_consent_note="recorded by me on 2026-01-01",
    )


def test_only_synthesized_narration_carries_its_own_audio():
    """A wav on a recorded job would be an audio spine the compiler silently
    prefers over the take's own track — a wrong render rather than a bad document,
    which is the kind of thing the schema is here to make impossible."""
    with pytest.raises(ValidationError, match="only synthesized narration"):
        Narration(audio_path="stages/abc.wav")


def test_recorded_narration_is_the_default_and_needs_nothing():
    assert Narration().source is NarrationSource.RECORDED


def test_ducking_lowers_the_bed():
    with pytest.raises(ValidationError, match="must not be positive"):
        AudioTrack(duck_db=6.0)
    assert AudioTrack().target_lufs == -14.0


def test_a_rect_that_runs_off_the_frame_is_rejected():
    with pytest.raises(ValidationError, match="runs off the frame"):
        Rect(x=0.8, y=0.1, w=0.5, h=0.1)


def test_quality_is_expressed_once_per_encode_path():
    """`crf` cannot steer VideoToolbox and `quality` cannot steer x264. On the target
    machine the hardware encoder is the default, so a lone `crf` would control nothing."""
    encode = DEMO_16X9.encode
    assert encode.encoder is Encoder.VIDEOTOOLBOX and 1 <= encode.quality <= 100
    assert 0 <= encode.crf <= 51
    with pytest.raises(ValidationError):
        EncodeSettings(quality=0)
    with pytest.raises(ValidationError):
        EncodeSettings(crf=99)


def test_constraints_yaml_layers_over_the_builtin_profiles():
    """§10's hard-constraint tier. Sparse overrides, deep-merged, then re-validated —
    so an override that breaks an invariant fails at load, not at render time."""
    from prefs import Constraints, resolve_profile as resolve

    tuned = Constraints.model_validate({"profiles": {"shorts_9x16": {"focus": {"crop_lag_ms": 900}}}})
    profile = resolve("shorts_9x16", tuned)
    assert profile.focus.crop_lag_ms == 900
    assert profile.focus.max_crop_delta_per_frame == SHORTS_9X16.focus.max_crop_delta_per_frame
    assert profile.duration_budget == SHORTS_9X16.duration_budget

    broken = Constraints.model_validate({"profiles": {"shorts_9x16": {"captions": {"type_scale": 0.2}}}})
    with pytest.raises(ValidationError, match="caption box"):
        resolve("shorts_9x16", broken)
