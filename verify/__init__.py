"""Verification (architecture.md §9).

Three layers. The first two exist to keep garbage from reaching a person, and only
the first is built: the deterministic checks. §9.2's transcript round-trip needs
an ASR backend and §9.3's perceptual layer needs the agent CLI, both of which are
phase 0's to choose.
"""

from verify.checks import verify_render
from verify.report import Finding, Severity, VerificationReport

__all__ = ["Finding", "Severity", "VerificationReport", "verify_render"]
