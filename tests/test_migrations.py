"""Spec versioning (§4.2).

The golden set will outlive several schema changes, and v1 golden specs that no
longer load are a golden set silently lost. The mechanism is proved here on a
hand-written v1 document that is never regenerated.
"""

import json
from pathlib import Path

import pytest

from spec import CURRENT_SPEC_VERSION, load_spec, load_spec_file
from spec.migrations import SpecVersionError, migrate, registered_migrations
from spec.source import Provenance

V1 = Path(__file__).parent / "data" / "spec_v1.json"


def test_a_hand_written_v1_spec_loads_at_the_current_version():
    spec = load_spec_file(V1)
    assert spec.spec_version == CURRENT_SPEC_VERSION
    assert spec.job_id == "hand-written-v1"
    assert spec.transcript == "hello there"
    assert spec.edit.covers(spec.source.duration)


def test_the_v1_document_on_disk_is_still_v1():
    """It is an artifact, not a regenerated file. Rewriting it would delete the test."""
    assert json.loads(V1.read_text())["spec_version"] == 1


def test_the_migration_chain_is_contiguous_to_the_current_version():
    steps = registered_migrations()
    assert steps == [(v, v + 1) for v in range(1, CURRENT_SPEC_VERSION)]


def test_migrating_stamps_the_new_version():
    assert migrate({"spec_version": 1})["spec_version"] == CURRENT_SPEC_VERSION


def test_an_already_current_document_is_left_alone():
    doc = {"spec_version": CURRENT_SPEC_VERSION, "job_id": "x"}
    assert migrate(doc) == doc


def test_a_document_from_the_future_is_refused_rather_than_guessed_at():
    with pytest.raises(SpecVersionError, match="newer than this build"):
        migrate({"spec_version": CURRENT_SPEC_VERSION + 1})


def test_a_document_with_no_version_is_refused():
    with pytest.raises(SpecVersionError, match="no spec_version"):
        load_spec({"job_id": "x"})


def test_a_v2_document_reaches_v3_marked_unknown_rather_than_guessed_at():
    """v3 added `source.provenance` (§10.2). A document from before it does not
    say whether its footage was recorded, and a rule that inferred one — from the
    job id, or from whether there is an events sidecar — would put a guess in the
    one corpus §10.1 cannot audit its way out of."""
    spec = load_spec_file(V1)
    assert spec.source.provenance is Provenance.UNKNOWN


def test_the_v1_document_still_carries_no_provenance_of_its_own():
    """The migration supplies it; the artifact is not edited to hide that it
    never had one."""
    assert "provenance" not in json.loads(V1.read_text())["source"]
