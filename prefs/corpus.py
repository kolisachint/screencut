"""What the preference learner will read, and whether there is enough of it yet
(architecture.md §10).

This is the corpus, not the learner. §10.2 activates the learner after ten to
fifteen accepted **real** jobs and says in so many words that building it earlier
leaves dead code that still has to be debugged — so what lives here is the half
that cannot wait: the reader that says what has been collected, and the gate that
says whether it is enough. Statistics, `defaults.json` and exemplar retrieval are
phase 10 proper and are deliberately absent.

The reason the reader lands first is that recording is the part that cannot be
done retroactively. "Go and make videos instead" is the right instruction and it
is also a one-way door: fifteen jobs reviewed under a schema that does not record
what they were accepted under produce a corpus nothing can repair, and the only
remedy then is to review all fifteen again. So `accepted_specs` carries the whole
`RenderProfile` (`runner/db.py`, migration 0004), a correction can address any
learnable tunable rather than the budget alone (`spec/corrections.py`), and a
take says whether it was recorded or generated (`spec/source.py`). This module is
where those three meet, and where the gate stops being a guess.

Three filters, and each is a rule from §10 rather than a convenience:

- **Verification.** §10.1: never learn from a job that failed verification. A
  broken render's corrections poison the defaults.
- **Provenance.** §10.2: real jobs. A fixture arrives with a hand-written spec and
  would teach the learner the fixture's taste under your name.
- **A recorded profile.** A row from before migration 0004 says what was accepted
  and not what it was accepted under, and the missing half cannot be guessed.

Each is counted rather than silently applied. A corpus that reports "3 of 10" is
useful; one that reports "3" while dropping seven without saying so is how a gate
that never opens gets debugged for an afternoon.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from spec.editspec import EditSpec
from spec.migrations import load_spec
from spec.origin import learnable_paths, read_path
from spec.profiles import BUILTIN_PROFILES, RenderProfile
from spec.source import Provenance
from spec.types import approx_eq

from runner import db

ACTIVATION_JOBS = 10
"""Accepted real jobs per profile before the learner may propose anything (§10.2).

The low end of §10.2's "~10-15" on purpose: this number gates whether phase 10
gets built at all, and the minimum sample count that gates whether one *default*
moves is a separate, stricter thing that belongs to the learner. Deciding that
one here would be deciding it without having seen a distribution.
"""


@dataclass(frozen=True)
class Acceptance:
    """One accepted job, as §10 reads it: a spec and the profile it ran under."""

    job_id: str
    spec: EditSpec
    profile: RenderProfile
    accepted_at: str

    def tunable(self, path: str) -> float:
        """This acceptance's value for one learnable tunable."""
        return float(read_path(self.profile.model_dump(mode="json"), path))


@dataclass(frozen=True)
class Skipped:
    """Why acceptances were not counted, in the terms of the rule that dropped them.

    Counts rather than ids: this is a meter, and a reader who needs the ids has a
    database. The distinction that matters is *which rule* fired, because
    "unverified" is something a re-run fixes and "synthetic" never is.
    """

    unverified: int = 0
    synthetic: int = 0
    unknown_provenance: int = 0
    no_profile: int = 0

    @property
    def total(self) -> int:
        return self.unverified + self.synthetic + self.unknown_provenance + self.no_profile


@dataclass(frozen=True)
class ProfileCorpus:
    """Everything the learner would read for one profile, plus what it may not."""

    profile: str
    acceptances: list[Acceptance] = field(default_factory=list)
    skipped: Skipped = field(default_factory=Skipped)

    @property
    def ready(self) -> bool:
        return len(self.acceptances) >= ACTIVATION_JOBS

    def spread(self, path: str) -> list[float]:
        """Every accepted value of one tunable, oldest first.

        The window and the median are the learner's; this hands over the samples
        in the order §10.1's "an old preference can be superseded" is stated in.
        """
        return [acceptance.tunable(path) for acceptance in self.acceptances]

    def corrected(self, path: str) -> int:
        """How many acceptances moved this tunable off the built-in default.

        The count that says whether a tunable has a *signal*, as opposed to ten
        acceptances of the number it already had. A median over the latter
        proposes the default it started from, which is not learning; it is the
        appearance of it, and §10.1's changelog would carry a change nobody made.
        """
        builtin = BUILTIN_PROFILES.get(self.profile)
        if builtin is None:
            return 0
        default = float(read_path(builtin.model_dump(mode="json"), path))
        return sum(1 for value in self.spread(path) if not approx_eq(value, default))


def read_corpus(profile: str, db_path: Path | str | None = None) -> ProfileCorpus:
    """The acceptances one profile may be learned from, and what was left out."""
    with db.connect(db_path or db.DEFAULT_DB_PATH) as connection:
        verified = set(db.verified_jobs(connection, profile))
        rows = db.accepted_specs(connection, profile)

    acceptances: list[Acceptance] = []
    unverified = synthetic = unknown = no_profile = 0
    for row in rows:
        if row["job_id"] not in verified:
            unverified += 1
            continue
        if row["profile_json"] is None:
            no_profile += 1
            continue
        spec = load_spec(json.loads(row["spec_json"]))
        if spec.source.provenance is Provenance.SYNTHETIC:
            synthetic += 1
            continue
        if spec.source.provenance is Provenance.UNKNOWN:
            unknown += 1
            continue
        acceptances.append(
            Acceptance(
                job_id=row["job_id"],
                spec=spec,
                profile=RenderProfile.model_validate_json(row["profile_json"]),
                accepted_at=row["accepted_at"],
            )
        )

    return ProfileCorpus(
        profile=profile,
        acceptances=acceptances,
        skipped=Skipped(
            unverified=unverified,
            synthetic=synthetic,
            unknown_provenance=unknown,
            no_profile=no_profile,
        ),
    )


def read_all(db_path: Path | str | None = None) -> dict[str, ProfileCorpus]:
    return {name: read_corpus(name, db_path) for name in BUILTIN_PROFILES}


def report(db_path: Path | str | None = None) -> str:
    """The gate, as a number rather than a feeling.

    Printed by `make corpus`. §10.2's "go and make videos instead" is the correct
    instruction and a hard one to act on without knowing how many are left, and
    the same is true of the tunables: a corpus of ten jobs nobody re-tuned teaches
    nothing about zoom, however ready the job count says it is.
    """
    lines: list[str] = ["preference corpus (§10) — the learner is not built; this is what it would read"]
    tunables = learnable_paths(RenderProfile)
    for name, corpus in read_all(db_path).items():
        have = len(corpus.acceptances)
        verdict = "ready" if corpus.ready else f"{ACTIVATION_JOBS - have} more needed"
        lines.append(f"\n{name}: {have}/{ACTIVATION_JOBS} accepted real jobs — {verdict}")
        skipped = corpus.skipped
        if skipped.total:
            reasons = [
                f"{skipped.unverified} failed verification (§10.1)" if skipped.unverified else "",
                f"{skipped.synthetic} synthetic (§10.2)" if skipped.synthetic else "",
                f"{skipped.unknown_provenance} of unknown provenance" if skipped.unknown_provenance else "",
                f"{skipped.no_profile} recorded before the profile was" if skipped.no_profile else "",
            ]
            lines.append("  skipped: " + ", ".join(r for r in reasons if r))
        # Per tunable, and only where there is something to say. A column of
        # zeroes for fifteen tunables is the shape of a report people stop
        # reading, and every one of these is zero until a real take is corrected.
        signals = [(path, corpus.corrected(path)) for path in tunables]
        moved = [f"{path} ({count})" for path, count in signals if count]
        lines.append(f"  corrected tunables: {', '.join(moved) if moved else 'none'}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Report the preference corpus (§10).")
    parser.add_argument("--db", default=None, help=f"defaults to {db.DEFAULT_DB_PATH}")
    args = parser.parse_args(argv)
    print(report(args.db), end="")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())
