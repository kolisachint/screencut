"""Spec migrations (architecture.md §4.2).

The golden set will outlive several schema changes, and v1 golden specs that no
longer load are a golden set silently lost. So `EditSpec` is never loaded bare:
`load_spec` migrates the raw document up to `CURRENT_SPEC_VERSION` first, then
validates.

A migration is a pure `dict -> dict` for one version step. Steps are registered
in a chain and applied in order, so adding v3 means writing one function, never
touching the ones before it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from spec.editspec import EditSpec
from spec.version import CURRENT_SPEC_VERSION

Migration = Callable[[dict[str, Any]], dict[str, Any]]

_REGISTRY: dict[int, tuple[int, Migration]] = {}


def migration(from_version: int, to_version: int) -> Callable[[Migration], Migration]:
    """Register a single version step."""

    def register(fn: Migration) -> Migration:
        if from_version in _REGISTRY:
            raise RuntimeError(f"a migration from v{from_version} is already registered")
        if to_version != from_version + 1:
            raise RuntimeError("migrations move one version at a time so the chain stays composable")
        _REGISTRY[from_version] = (to_version, fn)
        return fn

    return register


@migration(1, 2)
def _v1_to_v2(doc: dict[str, Any]) -> dict[str, Any]:
    """No-op, and deliberately so.

    v1 and v2 have the same shape. This exists to prove the mechanism works and
    to keep it exercised by the golden set from the first commit, so that the
    first migration which actually has to move a field is an ordinary change
    rather than a new subsystem written under pressure.
    """
    return doc


class SpecVersionError(ValueError):
    pass


def migrate(doc: dict[str, Any], to: int = CURRENT_SPEC_VERSION) -> dict[str, Any]:
    """Raise `doc` to version `to`, applying one registered step at a time."""
    doc = dict(doc)
    version = doc.get("spec_version")
    if version is None:
        raise SpecVersionError("document carries no spec_version; it cannot be migrated safely")
    if not isinstance(version, int):
        raise SpecVersionError(f"spec_version must be an integer, got {version!r}")
    if version > to:
        raise SpecVersionError(
            f"document is spec v{version}, newer than this build's v{to}. Upgrade screencut rather than downgrading the spec."
        )
    while version < to:
        step = _REGISTRY.get(version)
        if step is None:
            raise SpecVersionError(f"no migration registered from spec v{version}")
        next_version, fn = step
        doc = fn(doc)
        version = next_version
        doc["spec_version"] = version
    return doc


def load_spec(doc: dict[str, Any]) -> EditSpec:
    """Migrate then validate. The only supported way to read a spec document."""
    return EditSpec.model_validate(migrate(doc))


def load_spec_file(path: str | Path) -> EditSpec:
    return load_spec(json.loads(Path(path).read_text()))


def registered_migrations() -> list[tuple[int, int]]:
    return sorted((frm, to) for frm, (to, _) in _REGISTRY.items())
