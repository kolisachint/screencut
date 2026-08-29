// Not shipped — this file exists so `make typecheck` proves the generated types
// are usable, not merely syntactically valid. The review UI (§8) will build on
// exactly these shapes.

import type { EditSpec, RenderProfile, Segment, Tier } from "./screencut";

const budgetFits = (segments: Segment[], budget: number): boolean =>
  segments.reduce((total, s) => total + (s.t_out - s.t_in), 0) <= budget;

// The §4.4.1 projection rule, in the language the review UI speaks.
const RANK: Record<Tier, number> = { optional: 0, supporting: 1, essential: 2 };

export function selectForBudget(spec: EditSpec, profile: RenderProfile): Segment[] {
  const thresholds: Tier[] = ["optional", "supporting", "essential"];
  for (const threshold of thresholds) {
    const kept = spec.edit.segments.filter((s) => RANK[s.tier] >= RANK[threshold]);
    if (budgetFits(kept, profile.duration_budget)) return kept;
  }
  return spec.edit.segments.filter((s) => s.tier === "essential");
}

// Nullable-by-design fields must be nullable in TS too: a whole-output overlay
// carries no anchor and no time range (§4.5).
export function anchoredOverlayCount(spec: EditSpec): number {
  return spec.overlays.filter((o) => o.t_in !== null && o.anchor !== null).length;
}

export function captionText(spec: EditSpec): string {
  return spec.captions.flatMap((b) => b.words.map((w) => w.text)).join(" ");
}
