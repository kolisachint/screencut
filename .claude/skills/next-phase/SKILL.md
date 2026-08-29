---
name: next-phase
description: Implement the next phase of screencut's build plan end to end — read the phase's goal and exit criteria, build it, prove each criterion, update the docs, and commit in the project's style. Use when asked to continue, proceed, or build the next phase.
---

# Implementing a screencut phase

`docs/implementation-phases.md` is the specification. Each phase states its goal,
what gets built, how you know it is finished, and what is deliberately excluded.
Treat the **Exit criteria** as the definition of done and the **Not in this phase**
list as a boundary, not a suggestion.

## Before writing code

1. Read the phase's section in full, and the `architecture.md` sections it cites.
   The reasoning is there; do not re-derive it.
2. **Check the phase is not blocked.** Phases 4 onward need things this repository
   does not contain: a real recording, an ASR backend, `hoocode`. If a phase needs
   one, say so plainly and build only the part that does not. Do not write code
   against an interface nobody has run — three ASR parsers against unseen JSON is
   the exact failure phase 0 exists to prevent.
3. **Verify each unfamiliar mechanism in isolation first.** A five-second `lavfi`
   render that proves `sendcmd` reaches a downstream filter costs a minute and
   saves a day. Do this before designing around the mechanism, not after.

## While building

- Follow the layout in `AGENTS.md`. New packages go where §12 says.
- Cite design sections in comments (`§4.5`, `decision #22`). Say *why*, not what.
- Add tests as you go, named as claims. Every exit criterion needs a test or a
  runnable command that demonstrates it.
- Run the fixture through the real pipeline early and **look at frames**. The
  synthetic fixture has caught every serious bug in this project so far, and it
  caught them by being rendered and looked at, not by passing tests.
- When a check fires on the good fixture, the fixture is usually wrong. Fix the
  fixture. A check that always fires gets ignored within a week.

## Before committing

```sh
make check     # tests, generated-artifact drift, TypeScript typecheck
make run       # the fixture through the pipeline, end to end
make broken    # the deliberately bad fixture — the checks must still fire
```

Update, in this order:

1. `docs/implementation-phases.md` — mark the phase **built**, and add a short
   *How it came out* note where the implementation taught something the plan did
   not know. That note is the most valuable paragraph in the phase.
2. `docs/architecture.md` — the status line, and any section whose claim the
   implementation sharpened.
3. `README.md` — status, quickstart, the layout table.

## The commit message

Look at `git log` first. The style is: a title naming the phase, then prose
sections explaining the decisions that mattered, the bugs found along the way and
what they cost, and the exit criteria with their verdicts. It is a record for
whoever picks this up in six months, not a changelog line.

End every commit message with the trailers already in the log.

## Pushing

Develop and push on the designated feature branch. Never push to `main`.
