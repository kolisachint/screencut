"""Verification (architecture.md §9).

Three layers. The first two exist to keep garbage from reaching a person, and
they are what is built: §9.1's deterministic checks and §9.2's transcript
round-trip. §9.3's perceptual layer waits until there are real failures the first
two are known to miss — a VLM added before that is a check nobody can calibrate.
"""

from verify.checks import verify_render
from verify.report import Finding, Severity, VerificationReport
from verify.transcript import Difference, ExpectedWord, RoundTrip, expected_transcript, round_trip

__all__ = [
    "Difference",
    "ExpectedWord",
    "Finding",
    "RoundTrip",
    "Severity",
    "VerificationReport",
    "expected_transcript",
    "round_trip",
    "verify_render",
]
