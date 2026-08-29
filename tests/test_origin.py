"""Field-origin metadata (§11.1).

Golden replay checks a field strictly or distributionally depending on which
stage produced it. That only works if *every* field declares one, so the check
that they do belongs in the test suite rather than in a reviewer's memory.
"""

import pytest

from spec import EditSpec, Origin, RenderProfile, Stage, field_origins
from spec.edit import EditDecisions
from spec.origin import STAGE_ORIGIN, missing_origins, origin_of
from spec.overlays import OverlayPlan
from spec.schema import SCHEMA_MODELS

ROOTS = [EditSpec, RenderProfile, EditDecisions, OverlayPlan]


@pytest.mark.parametrize("model", ROOTS, ids=lambda m: m.__name__)
def test_every_field_declares_its_producing_stage(model):
    assert missing_origins(model) == [], "add spec_field(produced_by=...) to these fields"


def test_origins_are_found_through_nesting():
    paths = {f.path for f in field_origins(EditSpec)}
    assert "source.duration" in paths
    assert "edit.segments.tier" in paths
    assert "captions.words.emphasis" in paths


def test_the_editorial_decisions_are_the_model_written_ones():
    """§7.1's table, enforced. A "no" in it is a design commitment worth as much
    as a "yes", so both halves are asserted."""
    by_path = {f.path: f for f in field_origins(EditSpec)}
    assert by_path["edit.segments.tier"].origin is Origin.MODEL
    assert by_path["edit.removals.kind"].origin is Origin.MODEL
    assert by_path["overlays.template"].origin is Origin.MODEL
    assert by_path["captions.words.emphasis"].origin is Origin.MODEL

    assert by_path["focus.points.x"].origin is Origin.DETERMINISTIC
    assert by_path["captions.words.t_in"].origin is Origin.DETERMINISTIC
    assert by_path["audio.target_lufs"].origin is Origin.DETERMINISTIC
    assert by_path["source.duration"].origin is Origin.DETERMINISTIC


def test_a_render_profile_is_configuration_all_the_way_down():
    assert {f.stage for f in field_origins(RenderProfile)} == {Stage.CONFIG}


def test_every_stage_has_an_origin():
    """A new stage without an entry would silently default to nothing checkable."""
    assert set(STAGE_ORIGIN) == set(Stage)
    assert origin_of(Stage.PLAN_EDIT) is Origin.MODEL
    assert origin_of(Stage.TRIM) is Origin.DETERMINISTIC


@pytest.mark.parametrize("name, model", sorted(SCHEMA_MODELS.items()))
def test_origin_metadata_survives_into_the_json_schema(name, model):
    """The schema is what golden replay and the review UI read, not the Python class."""
    schema = model.model_json_schema()
    blob = str(schema)
    assert "x-screencut-origin" in blob
