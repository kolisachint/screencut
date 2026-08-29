---
description: Start work on a phase of the build plan — read its criteria, check it is not blocked, then implement it.
argument-hint: "<phase number>"
---

Work on phase $1 of `docs/implementation-phases.md`.

Invoke the `next-phase` skill and follow it. Before writing any code:

- Read phase $1's section in full, and the `architecture.md` sections it cites.
- Check the phases it depends on are genuinely built — the `phase-auditor` agent
  answers that against the exit criteria rather than against the doc's claims.
- Check whether phase $1 is blocked. Phases 4 onward need a real recording, a
  chosen ASR backend, or `hoocode`, none of which this repository contains. If it
  is blocked, say so plainly and up front, build only the part that is not
  blocked, and do not write code against an interface nobody has run.

Then build it, prove each exit criterion, update the docs, and commit in the
style already in `git log`.
