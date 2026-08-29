"""The verification report (architecture.md §9).

Three layers exist in the design; this is the first, and the first two exist to
keep garbage from reaching a person. A report is a list of findings rather than a
boolean because §9.1's most useful outputs are *numbers* — the budget overrun in
seconds, the trim override rate — and a check that can only say no cannot say
"7.4 seconds over".
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class Severity(str, Enum):
    PASS = "pass"
    INFO = "info"
    """A number worth seeing that is not a verdict — the trim override rate (§9.1)."""
    WARN = "warn"
    FAIL = "fail"


class Finding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    check: str
    severity: Severity
    message: str
    value: float | None = None
    limit: float | None = None

    def __str__(self) -> str:
        measured = ""
        if self.value is not None:
            measured = f"  [{self.value:g}" + (f" vs {self.limit:g}]" if self.limit is not None else "]")
        return f"{self.severity.value.upper():<5} {self.check:<22} {self.message}{measured}"


class VerificationReport(BaseModel):
    """One report per render (§9.1), stored on the job record."""

    model_config = ConfigDict(extra="forbid")

    job_id: str
    profile: str
    render: str
    findings: list[Finding] = Field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not any(f.severity is Severity.FAIL for f in self.findings)

    @property
    def failures(self) -> list[Finding]:
        return [f for f in self.findings if f.severity is Severity.FAIL]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity is Severity.WARN]

    def summary(self) -> str:
        """Legible enough to act on without reading the code — §9.1's exit criterion.

        Failures first, then warnings, then the numbers. Passes are counted rather
        than listed: a report where the good news is longer than the bad news is a
        report nobody reads to the end.
        """
        lines = [
            f"{self.profile}: {'PASS' if self.passed else 'FAIL'} "
            f"({len(self.failures)} failed, {len(self.warnings)} warned, "
            f"{sum(1 for f in self.findings if f.severity is Severity.PASS)} passed)"
        ]
        for severity in (Severity.FAIL, Severity.WARN, Severity.INFO):
            lines.extend(f"  {finding}" for finding in self.findings if finding.severity is severity)
        return "\n".join(lines)
