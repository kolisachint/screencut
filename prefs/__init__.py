"""Preferences, in three tiers (architecture.md §10).

Only the first tier exists yet: `constraints.yaml`, hand-written and never
auto-modified. `defaults.json` (learned numeric defaults) and `exemplars/` arrive
in phase 10, when there is a corpus to learn from — built earlier they are dead
code that still has to be debugged.

`corpus.py` is the exception, and the reason is that it is not the learner. It
reads what has been accepted and reports how far off §10.2's gate the collection
is. Recording is the one part of phase 10 that cannot be done afterwards: a job
reviewed under a schema that did not record what it was accepted under is not
learnable later, only reviewable again.

It is deliberately not re-exported here. `runner` imports `prefs` to resolve a
profile, so a `prefs` package that imported `runner.db` at module level would
close the loop and break both. Import `prefs.corpus` directly — the one module in
this package that reads the job record rather than a file.
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
