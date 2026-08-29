"""JSON Schema and TypeScript generation (decision #7).

One definition does four jobs: it constrains what an LLM prompt asks for,
validates what comes back, generates the review UI's types, and defines what the
learner diffs. These tests check the two generated artifacts are committed and
current — a stale `screencut.ts` is a review UI editing fields that no longer
exist, discovered at runtime.
"""

import json
from pathlib import Path

import pytest

from spec.schema import SCHEMA_DIR, SCHEMA_MODELS, fragment_schema
from spec.tsgen import render_typescript

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize("name, model", sorted(SCHEMA_MODELS.items()))
def test_committed_schemas_match_the_models(name, model):
    path = SCHEMA_DIR / f"{name}.schema.json"
    assert path.exists(), f"run `make schema` — {path.name} is missing"
    committed = json.loads(path.read_text())
    fresh = fragment_schema(model)
    fresh["$id"] = committed.get("$id")
    fresh["$schema"] = committed.get("$schema")
    assert committed == fresh, f"run `make schema` — {path.name} has drifted"


def test_committed_typescript_matches_the_models():
    path = SCHEMA_DIR / "screencut.ts"
    assert path.read_text() == render_typescript(), "run `make types` — screencut.ts has drifted"


def test_schema_emission_is_deterministic():
    assert fragment_schema(SCHEMA_MODELS["edit_spec"]) == fragment_schema(SCHEMA_MODELS["edit_spec"])


def test_the_generated_types_describe_a_serialized_spec():
    ts = render_typescript()
    assert "export interface EditSpec {" in ts
    assert "export type Tier = " in ts
    # Nullable by design (§4.5): a whole-output overlay has no anchor and no range.
    assert "anchor: Point | null;" in ts
    # Not optional: a serialized spec has written its defaults out.
    assert "words?: " not in ts
    # Origin metadata reaches the UI's own types, not just the JSON Schema (§11.1).
    assert "@producedBy plan_edit (model)" in ts


def test_fragment_schemas_are_self_contained_for_prompts():
    """§7.2 pastes these into a prompt, where a dangling $ref is a wrong answer."""
    for name, model in SCHEMA_MODELS.items():
        schema = fragment_schema(model)
        defs = set(schema.get("$defs", {}))
        refs = _refs(schema)
        assert refs <= defs, f"{name} references {sorted(refs - defs)} it does not define"


def _refs(node) -> set[str]:
    found: set[str] = set()
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str):
            found.add(ref.rsplit("/", 1)[-1])
        for value in node.values():
            found |= _refs(value)
    elif isinstance(node, list):
        for value in node:
            found |= _refs(value)
    return found
