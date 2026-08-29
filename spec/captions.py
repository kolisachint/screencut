"""Caption blocks, which always carry per-word timings (architecture.md §6.2).

`align` produces word timings anyway, so carrying them costs nothing, and they
turn out to do two jobs: kinetic captions later (a purely compiler-side phase
needing no migration), and — the one that matters sooner — letting `compile`
trim a block to a range exactly when a cut lands inside it (§4.5).

Model the end state, render the simple case.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field, model_validator

from spec.origin import Stage, spec_field
from spec.types import TIME_EPS, Seconds, SpecModel, TimeSpan


class Word(SpecModel, TimeSpan):
    """One spoken word with its alignment window.

    `emphasis` is the only model-written field in the caption subtree (§7.1:
    emphasis selection is taste, timing and layout are not) — which is exactly
    the mixed-origin case §11.1's per-field metadata exists for.
    """

    t_in: Seconds = spec_field(produced_by=Stage.ALIGN)
    t_out: Seconds = spec_field(produced_by=Stage.ALIGN)
    text: Annotated[str, Field(min_length=1)] = spec_field(produced_by=Stage.ALIGN)
    emphasis: bool = spec_field(default=False, produced_by=Stage.EMPHASIS)

    @model_validator(mode="after")
    def _not_inverted(self) -> "Word":
        if self.t_out < self.t_in - TIME_EPS:
            raise ValueError(f"word '{self.text}' is inverted: [{self.t_in}, {self.t_out}]")
        return self


class CaptionBlock(SpecModel, TimeSpan):
    """A displayed block, in source time, with the words that make it up."""

    t_in: Seconds = spec_field(produced_by=Stage.PLAN_CAPTIONS)
    t_out: Seconds = spec_field(produced_by=Stage.PLAN_CAPTIONS)
    words: list[Word] = spec_field(
        default_factory=list,
        produced_by=Stage.PLAN_CAPTIONS,
        description="Words in this block, ascending. Never empty in practice; empty is rejected.",
    )

    @model_validator(mode="after")
    def _consistent(self) -> "CaptionBlock":
        if self.t_out <= self.t_in + TIME_EPS:
            raise ValueError(f"caption block is inverted or empty: [{self.t_in}, {self.t_out}]")
        if not self.words:
            raise ValueError("caption block carries no words; word timings are not optional (§6.2)")
        for prev, nxt in zip(self.words, self.words[1:]):
            if nxt.t_in < prev.t_in - TIME_EPS:
                raise ValueError("words must ascend in t_in")
        first, last = self.words[0], self.words[-1]
        if first.t_in < self.t_in - TIME_EPS or last.t_out > self.t_out + TIME_EPS:
            raise ValueError(
                f"words [{first.t_in}, {last.t_out}] fall outside block [{self.t_in}, {self.t_out}]"
            )
        return self

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)

    def words_in(self, t_in: float, t_out: float) -> list[Word]:
        """Words whose midpoint survives a range — the §4.5 trim operation.

        Midpoint rather than overlap, so a cut landing inside a word drops it
        rather than leaving a clipped fragment on screen.
        """
        return [w for w in self.words if t_in - TIME_EPS <= (w.t_in + w.t_out) / 2 < t_out - TIME_EPS]
