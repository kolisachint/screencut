"""The golden set and its replay harness (architecture.md §11).

Fixtures whose right answer is known, so a change to prompts, rules or learned
defaults can be replayed against them before it takes effect. `golden/replay.py`
is the harness; the directories beside it are the cases.

Nothing is re-exported here on purpose: the harness is run as
`python -m golden.replay`, and a package that imports it eagerly makes that a
double import with a warning attached.
"""
