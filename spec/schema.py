"""JSON Schema emit (decision #7).

One Pydantic definition does four jobs: it constrains what an LLM prompt asks for
(§7.2), validates what comes back, generates the review UI's TypeScript types,
and defines what the learner diffs. This module is the first and third of those.

`x-screencut-origin` rides along on every field, so a schema consumer — golden
replay, the review UI — can see which stage produced a value without a second
source of truth (§11.1).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from pydantic.json_schema import models_json_schema

from spec.corrections import CorrectionDiff, Corrections
from spec.edit import EditDecisions
from spec.editspec import EditSpec
from spec.metadata import Metadata
from spec.overlays import OverlayPlan
from spec.profiles import RenderProfile

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schemas"

#: Documents and fragments worth emitting. The fragments are what LLM stages are
#: asked to return (§7.2) — small and heavily typed, so most wrong answers are
#: invalid answers rather than plausible ones (risk R5). The last two are what
#: review posts and what review records (§8): the same generator serves them, so
#: the page that edits the spec and the pipeline that reads it cannot disagree
#: about the shape of a correction.
#:
#: What is *not* here is as deliberate: `EditPlan`, `EmphasisPlan` and
#: `ScriptDraft` are intent rather than documents (§7.2). They never leave the
#: stage that asked for them — `plan/` turns each into spec fields the same run —
#: so emitting them would publish a shape nothing outside this repository reads.
#: `overlay_plan` is here because it *is* a spec subtree, and `metadata` because
#: it is a document that outlives the job, sitting beside the render (§5.4).
SCHEMA_MODELS: dict[str, type[BaseModel]] = {
    "edit_spec": EditSpec,
    "render_profile": RenderProfile,
    "edit_decisions": EditDecisions,
    "overlay_plan": OverlayPlan,
    "metadata": Metadata,
    "corrections": Corrections,
    "correction_diff": CorrectionDiff,
}


def fragment_schema(model: type[BaseModel]) -> dict[str, Any]:
    """The self-contained schema for one model, for pasting into a prompt."""
    return model.model_json_schema(mode="validation")


def combined_schema() -> dict[str, Any]:
    """Every model above under one shared `$defs`, which is what the TS generator reads."""
    _, schema = models_json_schema(
        [(m, "validation") for m in SCHEMA_MODELS.values()],
        ref_template="#/$defs/{model}",
        title="screencut",
    )
    return schema


def write_schemas(out_dir: Path = SCHEMA_DIR) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name, model in SCHEMA_MODELS.items():
        path = out_dir / f"{name}.schema.json"
        schema = fragment_schema(model)
        schema["$id"] = f"https://screencut.local/schemas/{name}.schema.json"
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        path.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n")
        written.append(path)
    return written


if __name__ == "__main__":
    for path in write_schemas():
        print(path)
