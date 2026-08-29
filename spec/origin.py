"""Field-origin metadata (architecture.md §11.1).

Every field of every spec model records the stage that produces it. Golden replay
(§11) reads that metadata to decide how a field is checked: strict per-field
tolerance for a deterministic producer, distributional over N runs for a
model-backed one.

The origin lives on the field rather than in a table beside the code, because a
table and the code drift apart and the version that is wrong is always the table.
The stage -> origin mapping is the one thing that is central, and it can be: it is
a property of the stage itself, fixed by architecture.md §7.1, not of the field.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field
from pydantic.fields import FieldInfo

#: JSON Schema extension key under which origin metadata is emitted.
ORIGIN_KEY = "x-screencut-origin"


class Stage(str, Enum):
    """A producer of spec fields. Mirrors the pipeline of architecture.md §5."""

    CONFIG = "config"
    """Hand-written configuration: render profiles, constraints.yaml."""

    SYSTEM = "system"
    """Bookkeeping written by the pipeline itself: ids, versions, timestamps."""

    HUMAN = "human"
    """Authored by a person — a review-UI correction or a hand-written fixture."""

    INGEST = "ingest"
    PLAN_FOCUS = "plan_focus"
    TRANSCRIBE = "transcribe"
    ALIGN = "align"
    TRIM = "trim"
    PLAN_EDIT = "plan_edit"
    PLAN_CAPTIONS = "plan_captions"
    EMPHASIS = "emphasis"
    PLAN_OVERLAYS = "plan_overlays"
    SCRIPT_DRAFT = "script_draft"
    AUDIO = "audio"


class Origin(str, Enum):
    """How a field's producer behaves, and therefore how golden replay checks it."""

    DETERMINISTIC = "deterministic"
    MODEL = "model"


#: architecture.md §7.1, the surface survey. A "no" in that table is a design
#: commitment, and this is where it is enforced rather than asserted.
STAGE_ORIGIN: dict[Stage, Origin] = {
    Stage.CONFIG: Origin.DETERMINISTIC,
    Stage.SYSTEM: Origin.DETERMINISTIC,
    Stage.HUMAN: Origin.DETERMINISTIC,
    Stage.INGEST: Origin.DETERMINISTIC,
    Stage.PLAN_FOCUS: Origin.DETERMINISTIC,
    Stage.TRANSCRIBE: Origin.DETERMINISTIC,
    Stage.ALIGN: Origin.DETERMINISTIC,
    Stage.TRIM: Origin.DETERMINISTIC,
    Stage.PLAN_CAPTIONS: Origin.DETERMINISTIC,
    Stage.AUDIO: Origin.DETERMINISTIC,
    Stage.PLAN_EDIT: Origin.MODEL,
    Stage.EMPHASIS: Origin.MODEL,
    Stage.PLAN_OVERLAYS: Origin.MODEL,
    Stage.SCRIPT_DRAFT: Origin.MODEL,
}


def origin_of(stage: Stage) -> Origin:
    return STAGE_ORIGIN[stage]


def spec_field(*, produced_by: Stage, **kwargs: Any) -> Any:
    """`pydantic.Field` with the producing stage recorded in the JSON Schema.

    Use for every field of every spec model. `tests/test_origin.py` fails the
    build if a field is added without one.
    """
    extra = dict(kwargs.pop("json_schema_extra", None) or {})
    extra[ORIGIN_KEY] = {
        "stage": produced_by.value,
        "origin": origin_of(produced_by).value,
    }
    return Field(json_schema_extra=extra, **kwargs)


class FieldOrigin(BaseModel):
    """The resolved origin of one field, addressed by dotted path."""

    path: str
    stage: Stage
    origin: Origin


def _field_origin(info: FieldInfo) -> tuple[Stage, Origin] | None:
    extra = info.json_schema_extra
    if not isinstance(extra, dict):
        return None
    meta = extra.get(ORIGIN_KEY)
    if not isinstance(meta, dict):
        return None
    stage = Stage(meta["stage"])
    return stage, Origin(meta["origin"])


def _nested_models(annotation: Any) -> list[type[BaseModel]]:
    """Every BaseModel reachable from a type annotation."""
    found: list[type[BaseModel]] = []
    stack = [annotation]
    seen: set[int] = set()
    while stack:
        node = stack.pop()
        if id(node) in seen:
            continue
        seen.add(id(node))
        if isinstance(node, type) and issubclass(node, BaseModel):
            found.append(node)
            continue
        args = getattr(node, "__args__", ()) or ()
        stack.extend(args)
    return found


def field_origins(model: type[BaseModel]) -> list[FieldOrigin]:
    """Walk `model` and its nested models, returning every field's origin.

    A value object nested under a field — a `Point` under `overlays.anchor` — has
    no origin of its own: its components are part of that field's value and share
    its producing stage. Inheriting is what keeps geometry primitives honest,
    since annotating `Point.x` would mean picking one stage for every user of it.
    """
    return _walk(model, "", None, ())


def missing_origins(model: type[BaseModel]) -> list[str]:
    """Dotted paths of fields with no declared or inherited origin.

    Empty is the only passing value; `tests/test_origin.py` asserts it.
    """
    return _walk(model, "", None, (), missing_only=True)  # type: ignore[return-value]


def _walk(
    model: type[BaseModel],
    prefix: str,
    inherited: tuple[Stage, Origin] | None,
    chain: tuple[type[BaseModel], ...],
    missing_only: bool = False,
) -> list:
    if model in chain:  # a self-referential model would otherwise recurse forever
        return []
    chain = chain + (model,)
    out: list = []
    for name, info in model.model_fields.items():
        path = f"{prefix}{name}"
        found = _field_origin(info) or inherited
        if missing_only:
            if found is None:
                out.append(path)
        elif found is not None:
            stage, origin = found
            out.append(FieldOrigin(path=path, stage=stage, origin=origin))
        for nested in _nested_models(info.annotation):
            out.extend(_walk(nested, f"{path}.", found, chain, missing_only))
    return out
