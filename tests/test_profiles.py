"""Render profiles, and the smaller schema-level commitments (§4.1, §6.3, decision #20)."""

import pytest
from pydantic import ValidationError

from spec import (
    DEMO_16X9,
    SHORTS_9X16,
    AudioTrack,
    CaptionStyle,
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
    assert Narration(
        source=NarrationSource.SYNTHESIZED,
        script="hello",
        voice_reference_path="source/voice.wav",
        voice_consent_note="recorded by me on 2026-01-01",
    )


def test_recorded_narration_is_the_default_and_needs_nothing():
    assert Narration().source is NarrationSource.RECORDED


def test_ducking_lowers_the_bed():
    with pytest.raises(ValidationError, match="must not be positive"):
        AudioTrack(duck_db=6.0)
    assert AudioTrack().target_lufs == -14.0


def test_a_rect_that_runs_off_the_frame_is_rejected():
    with pytest.raises(ValidationError, match="runs off the frame"):
        Rect(x=0.8, y=0.1, w=0.5, h=0.1)
