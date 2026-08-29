"""Deterministic planners.

Everything in here is arithmetic over data the pipeline already has — cursor
events, audio levels, word timings. Principle 3: reserve the model for decisions
that are genuinely about taste or language. The model-backed planners arrive in
phase 5 and live alongside these, not inside them.
"""

from plan.focus import CropPathPlan, FocusPlan, PathSample, ZoomPlan, ZoomRegion, plan_focus

__all__ = ["CropPathPlan", "FocusPlan", "PathSample", "ZoomPlan", "ZoomRegion", "plan_focus"]
