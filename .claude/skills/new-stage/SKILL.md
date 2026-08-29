---
name: new-stage
description: Add a stage to screencut's pipeline correctly — the §5.1 CLI contract, the cache fingerprint, dependencies, ordering, and the invalidation tests. Use when adding transcribe, trim, plan_edit, plan_captions, plan_overlays, script_draft, or any new stage.
---

# Adding a pipeline stage

Every stage is a pure function `(inputs, params) -> artifact`, run as a subprocess
CLI (`architecture.md` §5.1). The pipeline walks the graph; it does not know what
any stage does.

## The five edits

All in `runner/stages.py` unless noted.

1. **A fingerprint function.** Return the exact subset of the spec and profile the
   stage reads — *not* the whole spec. This is the decision that makes or breaks
   the review loop: hashing everything means a caption tweak invalidates a focus
   plan, and §8's argument stops being true. Look at `_focus_fingerprint` for the
   shape.
2. **A run function.** Takes a `StageRequest`, writes its artifact, returns a
   `StageResult`. It gets the spec path and upstream artifact paths in `inputs`,
   and the resolved profile in `params` — everything it needs, because with a
   remote runner there is no second way to get it.
3. **A `StageSpec` in `STAGES`**: name, `version` (bump to invalidate this stage
   and its dependents), `depends_on`, the two functions, and the artifact suffix
   or `directory=True`.
4. **`ORDER`** — add it in topological position.
5. **`INPUT_NAMES`** — the logical name downstream stages use for its artifact.

## Two flags that are not decoration

- `holds_local_weights=True` if running it puts a model in memory. 8GB will not
  hold two (§16), and `LocalRunner` refuses to start a second one.
- `model_backed=True` if an LLM writes the artifact. This forces model id and
  prompt version into the cache key, and `runner/cache.py` **refuses to compute a
  key without them**. Without it the cache serves the old answer after exactly the
  change you were trying to evaluate, and it looks like the prompt edit had no
  effect (§5.2).

## A model stage also needs

- The §7.3 invocation: print mode, JSON events, tools off, fixed cwd, everything
  in the prompt.
- Validate the returned fragment, retry once with the validation error appended,
  then degrade per §7.4 and record the degradation on the job record. Failure
  means any of: nonzero exit, unparseable stdout, schema validation failing twice,
  or a timeout — one branch, deliberately.
- A degradation that still renders. A worse video is not a failed one.

## Tests it needs

In `tests/test_runner.py`, extend the parametrized invalidation test: bumping this
stage's version must re-run it and its dependents **and nothing else**. Then check
that a change to something the stage does not read leaves it cached — that is the
fingerprint's whole job, and it is the assertion that catches a fingerprint that
reads too much.

Adding a stage changes the expected stage lists in several existing tests. That is
the point of them.
