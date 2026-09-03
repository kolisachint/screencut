"""Preferences, in three tiers (architecture.md §10).

Only the first tier exists yet: `constraints.yaml`, hand-written and never
auto-modified. `defaults.json` (learned numeric defaults) and `exemplars/` arrive
in phase 10, when there is a corpus to learn from — built earlier they are dead
code that still has to be debugged.
"""

from prefs.loader import (
    CONSTRAINTS_PATH,
    Constraints,
    ResolvedAgent,
    load_constraints,
    resolve_profile,
    resolve_profiles,
)

__all__ = [
    "CONSTRAINTS_PATH",
    "Constraints",
    "ResolvedAgent",
    "load_constraints",
    "resolve_profile",
    "resolve_profiles",
]
