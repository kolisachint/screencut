---
description: Where screencut actually stands — what is built, what the tests say, what is blocked, and what comes next.
allowed-tools: Bash(git log:*), Bash(git status:*), Bash(python3 -m pytest:*), Bash(grep:*), Read, Glob
---

Report where this project stands, for someone picking it up cold.

Gather first:

- `git log --oneline -8` and `git status --short`
- `grep -n "built\*\*" docs/implementation-phases.md` — which phases are marked done
- `python3 -m pytest -q 2>&1 | tail -3`

Then answer, briefly:

1. **Built** — which phases, and the one sentence each that says what they gave.
2. **Tests** — the count and whether anything fails.
3. **Blocked** — what phase 4 onward needs that this repository does not have (a
   real recording, an ASR backend, `hoocode`), and which risk each blockage leaves
   open. Read the "What is built, and what is blocked" section of `AGENTS.md`
   rather than re-deriving it.
4. **Next** — the next phase and its exit criteria, in a line or two.

Do not restate the architecture. Do not run renders. If the working tree is dirty,
say what is uncommitted.
