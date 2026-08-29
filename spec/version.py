"""The spec version, in its own module so both `EditSpec` and the migration
registry can name it without importing each other."""

CURRENT_SPEC_VERSION = 2
"""Bumped whenever a change to `EditSpec` needs a migration (architecture.md §4.2).

v1 -> v2 is deliberately a no-op: the mechanism is proved and exercised by the
golden set from the first commit, so the first migration that actually has to
move a field is an ordinary change rather than a new subsystem.
"""
