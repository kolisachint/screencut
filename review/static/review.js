// The review page (architecture.md §8).
//
// Plain ES modules and no build step, typed against the *generated* spec types
// (decision #7): the shapes below come from `schemas/screencut.ts`, which comes
// from the Pydantic models, so a spec change that this page has not caught up
// with fails `make typecheck` rather than rendering an empty column. `tsc`
// checks this file with `checkJs`; nothing compiles it, and the browser runs
// exactly what is committed.
//
// The page is a form, deliberately. Overlay preview and playback are later
// phases, and building them before knowing which corrections get made most
// often optimizes the wrong interaction (§8).

/** @typedef {import("../../schemas/screencut").EditSpec} EditSpec */
/** @typedef {import("../../schemas/screencut").RenderProfile} RenderProfile */
/** @typedef {import("../../schemas/screencut").Corrections} Corrections */
/** @typedef {import("../../schemas/screencut").CorrectionDiff} CorrectionDiff */
/** @typedef {import("../../schemas/screencut").Removal} Removal */
/** @typedef {import("../../schemas/screencut").Segment} Segment */
/** @typedef {import("../../schemas/screencut").Tier} Tier */

// The verification report is a pipeline record rather than a spec document
// (§9.1), so it is not in the generated types and is declared here. It is the
// one shape on this page that is not generated, and it is four fields.
/** @typedef {{check: string, severity: "pass"|"info"|"warn"|"fail", message: string, value: number|null, limit: number|null}} Finding */
/** @typedef {{job_id: string, profile: string, render: string, findings: Finding[]}} VerifyReport */

/**
 * @typedef {{
 *   job_id: string,
 *   status: string,
 *   degradations: string[],
 *   spec: EditSpec,
 *   proposed: EditSpec,
 *   profiles: RenderProfile[],
 *   proposed_profiles: RenderProfile[],
 *   corrections: Corrections,
 *   diff: CorrectionDiff,
 *   reports: Record<string, VerifyReport>,
 *   renders: string[],
 *   decision: string | null,
 *   ran?: string[],
 *   cached?: string[],
 * }} JobPayload
 */

/** @type {Tier[]} Loosest last, the order §4.4.1 ranks them in. */
const TIERS = ["essential", "supporting", "optional"];

const main = /** @type {HTMLElement} */ (document.getElementById("main"));
const statusLine = /** @type {HTMLElement} */ (document.getElementById("status"));

/** @type {JobPayload | null} */
let job = null;

/** Reinstated removal spans, keyed by `t_in-t_out`. @type {Map<string, {t_in: number, t_out: number}>} */
const reinstated = new Map();
/** Re-tiered segments, keyed by `t_in`. @type {Map<number, Tier>} */
const retiered = new Map();
/** Budget overrides by profile name. @type {Map<string, number>} */
const budgets = new Map();

/**
 * @param {string} tag
 * @param {Record<string, string>} [attributes]
 * @param {(Node | string)[]} [children]
 * @returns {HTMLElement}
 */
function el(tag, attributes = {}, children = []) {
  const node = document.createElement(tag);
  for (const [name, value] of Object.entries(attributes)) {
    if (name === "class") node.className = value;
    else node.setAttribute(name, value);
  }
  for (const child of children) node.append(child);
  return node;
}

/** @param {number} seconds */
const time = (seconds) => `${seconds.toFixed(2)}s`;

/** @param {number} seconds */
const span = (seconds) => `${Math.floor(seconds / 60)}:${(seconds % 60).toFixed(1).padStart(4, "0")}`;

/**
 * @param {string} url
 * @param {unknown} [body]
 * @returns {Promise<any>}
 */
async function api(url, body) {
  const response = await fetch(
    url,
    body === undefined
      ? {}
      : {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(body),
        },
  );
  const text = await response.text();
  if (!response.ok) throw new Error(text || response.statusText);
  return text ? JSON.parse(text) : null;
}

// --- the index ---------------------------------------------------------------

async function showIndex() {
  const { jobs } = await api("/api/jobs");
  statusLine.textContent = `${jobs.length} job${jobs.length === 1 ? "" : "s"}`;
  const rows = jobs.map(
    /** @param {{job_id: string, status: string, degradations: string[], updated_at: string, decision: string|null}} row */
    (row) =>
      el("tr", {}, [
        el("td", {}, [el("a", { href: `/jobs/${row.job_id}` }, [row.job_id])]),
        el("td", {}, [row.status]),
        el("td", { class: row.degradations.length ? "warn" : "" }, [
          row.degradations.length ? `${row.degradations.length} degraded` : "—",
        ]),
        el("td", {}, [row.decision ?? "—"]),
        el("td", { class: "note" }, [row.updated_at]),
      ]),
  );
  main.replaceChildren(
    el("table", {}, [
      el("thead", {}, [
        el("tr", {}, ["job", "status", "stages", "review", "updated"].map((h) => el("th", {}, [h]))),
      ]),
      el("tbody", {}, rows),
    ]),
  );
}

// --- one job -----------------------------------------------------------------

/** @param {string} jobId */
async function showJob(jobId) {
  job = /** @type {JobPayload} */ (await api(`/api/jobs/${jobId}`));
  seedFromSaved(job);
  render();
}

/**
 * Start from the corrections already on disk, so reloading the page shows what
 * is rendering rather than an empty form that would silently withdraw them on
 * the next re-render.
 * @param {JobPayload} payload
 */
function seedFromSaved(payload) {
  reinstated.clear();
  retiered.clear();
  budgets.clear();
  for (const removal of payload.corrections.reinstated) {
    reinstated.set(key(removal.t_in, removal.t_out), { t_in: removal.t_in, t_out: removal.t_out });
  }
  for (const segment of payload.corrections.retiered) retiered.set(segment.t_in, segment.tier);
  for (const [name, budget] of Object.entries(payload.corrections.budgets)) budgets.set(name, budget);
}

/**
 * @param {number} t_in
 * @param {number} t_out
 */
const key = (t_in, t_out) => `${t_in.toFixed(6)}-${t_out.toFixed(6)}`;

function render() {
  if (!job) return;
  const spec = job.proposed; // the form edits the *proposal*; corrections are the layer
  statusLine.textContent = `${job.job_id} — ${job.status}${job.decision ? ` (${job.decision})` : ""}`;

  const sections = [];
  if (job.degradations.length) sections.push(degradedBanner(job.degradations));
  const failed = Object.values(job.reports).filter((r) => r.findings.some((f) => f.severity === "fail"));
  if (failed.length) sections.push(failedBanner(failed));

  sections.push(el("h2", {}, ["Renders"]), profilesSection(job));
  sections.push(el("h2", {}, ["Removals"]), removalsTable(spec));
  sections.push(el("h2", {}, ["Segments"]), segmentsTable(spec));
  sections.push(actions());
  main.replaceChildren(...sections);
}

/** @param {string[]} degradations */
function degradedBanner(degradations) {
  // Decision #12: full auto, reviewed at the end — which makes this page the
  // only place a degraded job announces itself (§7.4).
  return el("div", { class: "banner degraded" }, [
    el("h2", {}, ["Ran degraded"]),
    el("p", { class: "note" }, ["A stage could not run and fell back. What you are watching is not what the pipeline would have made."]),
    el("ul", {}, degradations.map((note) => el("li", {}, [note]))),
  ]);
}

/** @param {VerifyReport[]} reports */
function failedBanner(reports) {
  const lines = reports.flatMap((report) =>
    report.findings
      .filter((finding) => finding.severity === "fail")
      .map((finding) => el("li", { class: "finding fail" }, [`${report.profile}: ${finding.check} — ${finding.message}`])),
  );
  return el("div", { class: "banner failed" }, [el("h2", {}, ["Verification failed"]), el("ul", {}, lines)]);
}

/** @param {JobPayload} payload */
function profilesSection(payload) {
  const cards = payload.proposed_profiles.map((profile) => {
    const report = payload.reports[profile.name];
    const budget = budgets.get(profile.name) ?? profile.duration_budget;
    const input = /** @type {HTMLInputElement} */ (
      el("input", { type: "number", min: "0.5", step: "0.5", value: String(budget) })
    );
    input.addEventListener("change", () => {
      const value = Number(input.value);
      if (!Number.isFinite(value) || value <= 0) return;
      // A budget equal to the profile's own is not a correction; recording it as
      // one would put a change in the §10 record that changed nothing.
      if (value === profile.duration_budget) budgets.delete(profile.name);
      else budgets.set(profile.name, value);
    });

    const card = el("div", { class: "profile" }, [el("h3", {}, [profile.name])]);
    if (payload.renders.includes(profile.name)) {
      card.append(
        el("video", {
          controls: "controls",
          preload: "metadata",
          src: `/api/jobs/${payload.job_id}/render/${profile.name}`,
        }),
      );
    } else {
      card.append(el("p", { class: "note" }, ["not rendered yet"]));
    }
    const label = el("label", {}, [`duration budget (${profile.width}x${profile.height}) `]);
    label.append(input);
    card.append(label);
    if (report) card.append(findings(report));
    return card;
  });
  return el("div", { class: "profiles" }, cards);
}

/** @param {VerifyReport} report */
function findings(report) {
  const shown = report.findings.filter((finding) => finding.severity !== "pass");
  const passes = report.findings.length - shown.length;
  return el("div", {}, [
    el("h3", {}, [`verification — ${passes} passed`]),
    el(
      "ul",
      {},
      shown.map((finding) =>
        el("li", { class: `finding ${finding.severity}` }, [
          `${finding.check}: ${finding.message}${finding.value === null ? "" : ` [${finding.value}]`}`,
        ]),
      ),
    ),
  ]);
}

/**
 * Removals grouped by kind (§4.4): the reviewer's question is "why was this
 * cut", and `silence` and `false_start` are answered differently.
 * @param {EditSpec} spec
 */
function removalsTable(spec) {
  if (!spec.edit.removals.length) return el("p", { class: "note" }, ["Nothing was cut."]);
  const byKind = new Map();
  for (const removal of spec.edit.removals) {
    const group = byKind.get(removal.kind) ?? [];
    group.push(removal);
    byKind.set(removal.kind, group);
  }

  const blocks = [];
  for (const [kind, group] of byKind) {
    const cut = group.reduce(
      /** @param {number} total @param {Removal} r */ (total, r) => total + (r.t_out - r.t_in),
      0,
    );
    blocks.push(el("h3", {}, [`${kind} — ${group.length} cuts, ${time(cut)}`]));
    blocks.push(
      el("table", {}, [
        el("tbody", {}, group.map(/** @param {Removal} removal */ (removal) => removalRow(removal))),
      ]),
    );
  }
  return el("div", {}, blocks);
}

/** @param {Removal} removal */
function removalRow(removal) {
  const id = key(removal.t_in, removal.t_out);
  const box = /** @type {HTMLInputElement} */ (el("input", { type: "checkbox" }));
  box.checked = reinstated.has(id);
  const cells = el("tr", {}, []);
  box.addEventListener("change", () => {
    if (box.checked) reinstated.set(id, { t_in: removal.t_in, t_out: removal.t_out });
    else reinstated.delete(id);
    cells.className = box.checked ? "changed" : "";
    label.className = box.checked ? "reinstated" : "";
  });
  const label = el("span", { class: box.checked ? "reinstated" : "" }, [
    `${span(removal.t_in)} → ${span(removal.t_out)} (${time(removal.t_out - removal.t_in)})`,
  ]);

  const put = el("label", {}, []);
  put.append(box, document.createTextNode(" keep it"));
  cells.className = box.checked ? "changed" : "";
  cells.append(el("td", {}, [label]), el("td", { class: "note" }, [`proposed by ${removal.proposed_by}`]), el("td", {}, [put]));
  return cells;
}

/**
 * Segments with their tier and the reason for it (§4.4). The reason is the
 * whole point: a tier with no reason cannot be argued with.
 * @param {EditSpec} spec
 */
function segmentsTable(spec) {
  if (!spec.edit.segments.length) return el("p", { class: "note" }, ["Nothing has been tiered yet."]);
  const rows = spec.edit.segments.map((segment) => segmentRow(segment));
  return el("table", {}, [
    el("thead", {}, [el("tr", {}, ["span", "tier", "why"].map((h) => el("th", {}, [h])))]),
    el("tbody", {}, rows),
  ]);
}

/** @param {Segment} segment */
function segmentRow(segment) {
  const select = /** @type {HTMLSelectElement} */ (el("select", {}));
  for (const tier of TIERS) {
    const option = /** @type {HTMLOptionElement} */ (el("option", { value: tier }, [tier]));
    option.selected = (retiered.get(segment.t_in) ?? segment.tier) === tier;
    select.append(option);
  }
  const row = el("tr", { class: retiered.has(segment.t_in) ? "changed" : "" }, []);
  select.addEventListener("change", () => {
    const chosen = /** @type {Tier} */ (select.value);
    if (chosen === segment.tier) retiered.delete(segment.t_in);
    else retiered.set(segment.t_in, chosen);
    row.className = retiered.has(segment.t_in) ? "changed" : "";
  });
  row.append(
    el("td", {}, [`${span(segment.t_in)} → ${span(segment.t_out)}`]),
    el("td", { class: `tier-${segment.tier}` }, [select]),
    el("td", { class: "note" }, [segment.reason]),
  );
  return row;
}

// --- committing --------------------------------------------------------------

function actions() {
  const rerender = /** @type {HTMLButtonElement} */ (el("button", { class: "primary" }, ["Re-render"]));
  const accept = /** @type {HTMLButtonElement} */ (el("button", {}, ["Accept"]));
  const reject = /** @type {HTMLButtonElement} */ (el("button", {}, ["Reject"]));
  const said = el("span", { class: "ran" }, []);

  rerender.addEventListener("click", async () => {
    if (!job) return;
    for (const button of [rerender, accept, reject]) button.disabled = true;
    said.textContent = "rendering…";
    try {
      const payload = /** @type {JobPayload} */ (
        await api(`/api/jobs/${job.job_id}/corrections`, corrections())
      );
      job = payload;
      seedFromSaved(payload);
      render();
      // What ran and what did not is the exit criterion of this phase, so the
      // page says it out loud rather than leaving it to be inferred from a
      // stopwatch.
      statusLine.textContent = `${payload.job_id} — ran ${(payload.ran ?? []).join(", ") || "nothing"}`;
    } catch (error) {
      said.textContent = String(error);
      for (const button of [rerender, accept, reject]) button.disabled = false;
    }
  });

  for (const [button, decision] of [
    [accept, "accepted"],
    [reject, "rejected"],
  ]) {
    /** @type {HTMLButtonElement} */ (button).addEventListener("click", async () => {
      if (!job) return;
      job = /** @type {JobPayload} */ (
        await api(`/api/jobs/${job.job_id}/decision`, { decision })
      );
      render();
    });
  }

  return el("div", { class: "actions" }, [rerender, accept, reject, said]);
}

/** @returns {Corrections} */
function corrections() {
  /** @type {Record<string, number>} */
  const budgeted = {};
  for (const [name, budget] of budgets) budgeted[name] = budget;
  return {
    reinstated: [...reinstated.values()],
    retiered: [...retiered.entries()].map(([t_in, tier]) => ({ t_in, tier })),
    budgets: budgeted,
  };
}

// --- routing -----------------------------------------------------------------

const match = window.location.pathname.match(/^\/jobs\/([^/]+)$/);
const start = match ? showJob(match[1]) : showIndex();
start.catch((error) => {
  main.textContent = String(error);
});
