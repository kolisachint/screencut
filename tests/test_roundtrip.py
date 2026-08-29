"""Phase 1 exit criterion: a fixture round-trips.

construct -> serialize -> deserialize -> equal. If this does not hold, nothing
downstream can be trusted, because every stage boundary is a serialization (§5.1).
"""

import json

import pytest
from pydantic import ValidationError

from spec import BUILTIN_PROFILES, CaptionBlock, EditSpec, RenderProfile, Word, load_spec
from ingest.fixtures import build_spec


def test_the_synthetic_fixture_round_trips():
    original = build_spec().spec
    restored = load_spec(json.loads(original.model_dump_json()))
    assert restored == original


def test_round_tripping_through_a_json_object_preserves_every_field():
    original = build_spec().spec
    restored = EditSpec.model_validate(original.model_dump(mode="json"))
    assert restored.model_dump(mode="json") == original.model_dump(mode="json")


@pytest.mark.parametrize("name", sorted(BUILTIN_PROFILES))
def test_builtin_profiles_round_trip(name):
    profile = BUILTIN_PROFILES[name]
    assert RenderProfile.model_validate_json(profile.model_dump_json()) == profile


def test_unknown_fields_are_refused_rather_than_dropped():
    """`extra="forbid"` is load-bearing under decision #13: a model stage returning
    a plausible-but-wrong key must fail validation and trigger the §7.2 retry."""
    doc = build_spec().spec.model_dump(mode="json")
    doc["cut_list"] = [[0, 1]]
    with pytest.raises(ValidationError):
        EditSpec.model_validate(doc)


def test_caption_blocks_carry_word_timings_or_do_not_exist():
    with pytest.raises(ValidationError, match="word timings are not optional"):
        CaptionBlock(t_in=0, t_out=1, words=[])


def test_words_must_lie_inside_their_block():
    with pytest.raises(ValidationError, match="outside block"):
        CaptionBlock(t_in=0, t_out=1, words=[Word(t_in=0, t_out=2, text="over")])
