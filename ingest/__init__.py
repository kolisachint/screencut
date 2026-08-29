"""Ingest: recorder adapters and the synthetic fixture generator.

Ingest is an **adapter boundary** from day one (risk R1). `FocusTrack` is our
format, not the recorder's, and the adapter is the only code that knows what a
recorder emits. Synthetic fixtures assume only sampled `(t, x, y)` plus click
timestamps — the floor any recorder could plausibly provide. Building on the
richest plausible data and receiving the poorest means rewriting the planner;
building on the floor and receiving more means extending it.
"""
