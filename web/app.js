"use strict";

/* Swap — single-page UI.
 * Every number shown is fetched from the FastAPI layer at runtime; nothing is hard-coded except
 * the measured fairness study, which is quoted from the README and labelled as such.
 * Organised in small functions; no frameworks, no build step. */

/* ----------------------------------------------------------------- tiny helpers */

const $ = (id) => document.getElementById(id);
const $$ = (selector, root) => Array.from((root || document).querySelectorAll(selector));

function el(tag, props, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value == null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = String(value);
    else if (key === "html") node.innerHTML = value;
    else if (key.startsWith("on") && typeof value === "function") node.addEventListener(key.slice(2), value);
    else if (key === "dataset") Object.assign(node.dataset, value);
    else node.setAttribute(key, value === true ? "" : String(value));
  }
  for (const child of children.flat()) {
    if (child == null || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

function setStatus(node, text, tone) {
  if (!node) return;
  clear(node);
  node.hidden = !text;
  if (text) node.append(el("span", { class: tone ? `status-${tone}` : "", text }));
}

const reducedMotion = () =>
  window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

async function api(path, options) {
  let res;
  try {
    res = await fetch(path, options);
  } catch (err) {
    throw new Error("Could not reach the server. Is the app running?");
  }
  if (!res.ok) {
    throw new Error(friendlyStatus(res.status));
  }
  try {
    return await res.json();
  } catch (err) {
    throw new Error("The server sent something that was not valid data.");
  }
}

function friendlyStatus(status) {
  if (status === 404) return "That was not found. The pool may have changed — try generating one again.";
  if (status === 422) return "That input was not valid. Check the value and try again.";
  if (status >= 500) return "The server had a problem with that request. Please try again.";
  return `The request failed (error ${status}). Please try again.`;
}

function animateNumber(node, value) {
  const target = Number(value);
  if (!node) return;
  if (!Number.isFinite(target)) { node.textContent = String(value); return; }
  const from = Number(node.dataset.value || 0);
  node.dataset.value = String(target);
  if (reducedMotion() || from === target) { node.textContent = String(target); return; }
  const started = performance.now();
  const duration = 520;
  const tick = (now) => {
    const t = Math.min(1, (now - started) / duration);
    const eased = 1 - Math.pow(1 - t, 3);
    node.textContent = String(Math.round(from + (target - from) * eased));
    if (t < 1) requestAnimationFrame(tick);
    else node.textContent = String(target);
  };
  requestAnimationFrame(tick);
}

function scrollTo(id) {
  const node = $(id);
  if (!node) return;
  node.scrollIntoView({ behavior: reducedMotion() ? "auto" : "smooth", block: "start" });
}

let flashTimer = null;
function flash(node) {
  if (!node) return;
  node.classList.remove("flash");
  void node.offsetWidth;
  node.classList.add("flash");
  clearTimeout(flashTimer);
  flashTimer = setTimeout(() => node.classList.remove("flash"), 1600);
}

/* ------------------------------------------------------------- vocabulary (plain words) */

const OUTCOMES = {
  answered_with_path: {
    label: "Answered, with the path shown", tone: "ok",
    meaning: "The graph found this and here is the evidence.",
  },
  not_in_graph: {
    label: "The graph does not know (not the same as safe)", tone: "warn",
    meaning: "The search came back empty. That is not the same as safe.",
  },
  unknown_entity: {
    label: "That patient does not exist", tone: "bad",
    meaning: "This id is not in the current pool.",
  },
  from_memory: {
    label: "Remembered from an earlier run", tone: "info",
    meaning: "This came from a stored earlier answer, not a fresh lookup.",
  },
};

function outcomeBadge(outcome) {
  const info = OUTCOMES[outcome] || { label: String(outcome), tone: "muted", meaning: "" };
  return el("span", { class: `badge badge-${info.tone}`, title: info.meaning, text: info.label });
}

const DIFFICULTY = [
  { at: 0.05, word: "easy", note: "Easy to match: this patient accepts most donors." },
  { at: 0.45, word: "medium", note: "Medium: about half of donors would be rejected." },
  { at: 0.90, word: "hard", note: "Hard: this patient rejects about 9 in 10 donors (highly sensitised)." },
];

function difficulty(pra) {
  const value = Number(pra);
  let best = DIFFICULTY[0];
  for (const level of DIFFICULTY) {
    if (Math.abs(value - level.at) < Math.abs(value - best.at)) best = level;
  }
  return best;
}

function abo(letter) {
  return letter || "?";
}

/* ------------------------------------------------------------------------- state */

const state = {
  maxLen: 3,
  size: 200,
  seed: 1000,
  priorityOn: false,
  priorityWeight: 3,
  filter: "all",
  nodesById: {},
  chosen: [],
  unmatched: [],
  proof: null,
  detailCy: null,
  loadedOnce: false,
};

function priorityParam() {
  return state.priorityOn ? `&priority=${state.priorityWeight}` : "";
}

/* --------------------------------------------------------------- loading + wiring */

async function boot() {
  wire();
  setLenButtons();
  buildAskChips();
  renderFairnessPanel();
  await ensurePool();
  await refreshAll();
  loadCypher();
  state.loadedOnce = true;
}

async function ensurePool() {
  setStatus($("result-status"), "Loading the current pool…");
  try {
    const proof = await api("/proof");
    if (!proof.loaded) {
      setStatus($("result-status"), "Preparing the default pool (200 pairs, seed 1000)…");
      await api("/pool", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ n: state.size, seed: state.seed }),
      });
    }
  } catch (err) {
    setStatus($("result-status"), err.message, "bad");
  }
}

async function refreshAll() {
  await Promise.all([loadHeadline(), loadGraph(), loadTrace()]);
}

async function loadHeadline() {
  try {
    const proof = await api("/proof");
    state.proof = proof;
    renderHeadline(proof);
    renderProof(proof);
  } catch (err) {
    setStatus($("result-status"), err.message, "bad");
  }
}

async function loadGraph() {
  $("swap-status").hidden = false;
  setStatus($("swap-status"), "Finding swaps and checking who is left out…");
  setStatus($("unmatched-status"), "Loading…");
  let graph;
  let best;
  try {
    [graph, best] = await Promise.all([
      api(`/graph?max_len=${state.maxLen}`),
      api(`/tools/best_set?max_len=${state.maxLen}${priorityParam()}`),
    ]);
  } catch (err) {
    setStatus($("swap-status"), err.message, "bad");
    setStatus($("unmatched-status"), "");
    return;
  }

  state.nodesById = {};
  for (const node of graph.nodes || []) state.nodesById[node.id] = node;
  state.chosen = Array.isArray(best.result && best.result.chosen) ? best.result.chosen : [];
  const covered = new Set(state.chosen.flat());
  state.unmatched = (graph.nodes || [])
    .map((n) => n.id)
    .filter((id) => !covered.has(id))
    .sort((a, b) => a - b);

  renderSwaps();
  renderUnmatched();
  populateRemoveChoices();
  updateFairnessLive();
  setStatus($("swap-status"), state.chosen.length ? "" : "No swap was found for this pool. Try a bigger pool or a longer swap.");
}

/* ------------------------------------------------------ section 3: the big result */

function renderHeadline(proof) {
  if (!proof || !proof.loaded) {
    setStatus($("result-status"), proof && proof.message ? proof.message : "No pool is loaded yet.", "warn");
    return;
  }
  const f = proof.falkordb || {};
  const two = f.best_set_2;
  const three = f.best_set_3;
  animateNumber($("n2"), two == null ? "–" : two);
  animateNumber($("n3"), three == null ? "–" : three);
  const gain = two != null && three != null ? three - two : null;
  $("gain").textContent = gain == null ? "–" : `+${gain}`;

  const total = proof.pool ? proof.pool.n : null;
  const scale = total || Math.max(two || 1, three || 1, 1);
  $("bar2").style.width = `${Math.max(4, (100 * (two || 0)) / scale)}%`;
  $("bar3").style.width = `${Math.max(4, (100 * (three || 0)) / scale)}%`;

  const p = proof.pool || {};
  $("pool-line").textContent =
    `${p.n ?? "?"} pairs · ${p.edges ?? "?"} donor→patient links · ${p.hard ?? "?"} hardest-to-match`;
  setStatus($("result-status"), `Live from the pool in the graph (longest swap allowed: ${state.maxLen}-way).`);
}

/* --------------------------------------------------- section 4: the swaps found */

function pairLabel(id) {
  const node = state.nodesById[id] || {};
  return `Pair ${id} · patient ${abo(node.patient_abo)}, donor ${abo(node.donor_abo)}`;
}

function cycleSentence(cycle) {
  const ids = cycle.map((id) => Number(id));
  if (ids.length < 2) return `Pair ${ids[0]} is not part of a swap.`;
  const parts = [`Pair ${ids[0]}'s donor gives to pair ${ids[1]}`];
  for (let i = 1; i < ids.length; i += 1) {
    if (i === ids.length - 1) parts.push(`whose donor gives back to pair ${ids[0]}`);
    else parts.push(`whose donor gives to pair ${ids[i + 1]}`);
  }
  const sentence = parts.join(", ");
  return sentence.charAt(0).toUpperCase() + sentence.slice(1) + ".";
}

function svgLoop(cycle, uid) {
  const n = cycle.length;
  const W = 240, H = 168, cx = W / 2, cy = 80;
  const rx = n >= 3 ? 64 : 74;
  const ry = n >= 3 ? 42 : 0;
  const pos = cycle.map((id, i) => {
    const angle = -Math.PI / 2 + (2 * Math.PI * i) / n;
    return { id, x: cx + rx * Math.cos(angle), y: cy + ry * Math.sin(angle) };
  });

  const arcs = [];
  if (n === 2) {
    const [a, b] = pos;
    arcs.push(`<path d="M${a.x},${a.y - 18} Q${cx},${cy - 52} ${b.x},${b.y - 18}" class="loop-arc" marker-end="url(#arrowhead)"/>`);
    arcs.push(`<path d="M${b.x},${b.y + 18} Q${cx},${cy + 52} ${a.x},${a.y + 18}" class="loop-arc" marker-end="url(#arrowhead)"/>`);
  } else {
    for (let i = 0; i < n; i += 1) {
      const a = pos[i];
      const b = pos[(i + 1) % n];
      const mx = (a.x + b.x) / 2;
      const my = (a.y + b.y) / 2;
      const ctrlX = mx + (cx - mx) * 0.34;
      const ctrlY = my + (cy - my) * 0.34;
      arcs.push(`<path d="M${a.x.toFixed(1)},${a.y.toFixed(1)} Q${ctrlX.toFixed(1)},${ctrlY.toFixed(1)} ${b.x.toFixed(1)},${b.y.toFixed(1)}" class="loop-arc" marker-end="url(#arrowhead)"/>`);
    }
  }

  const nodes = pos
    .map((p) => {
      const node = state.nodesById[p.id] || {};
      return (
        `<g class="loop-node">` +
        `<circle cx="${p.x.toFixed(1)}" cy="${p.y.toFixed(1)}" r="18" class="svg-node svg-pair"/>` +
        `<text x="${p.x.toFixed(1)}" y="${(p.y + 4).toFixed(1)}" class="svg-id">${p.id}</text>` +
        `<text x="${p.x.toFixed(1)}" y="${(p.y + 30).toFixed(1)}" class="svg-sub">${abo(node.patient_abo)} · ${abo(node.donor_abo)}</text>` +
        `</g>`
      );
    })
    .join("");

  return (
    `<svg viewBox="0 0 ${W} ${H}" class="loop-svg" role="img" ` +
    `aria-label="Swap loop of ${n} pairs: ${cycle.join(" gives to ")}">${arcs.join("")}${nodes}</svg>`
  );
}

function swapCard(cycle, index) {
  const length = cycle.length;
  const card = el("button", { type: "button", class: "swap-card", "data-index": String(index) });
  card.append(
    el("div", { class: "swap-head" },
      el("span", { class: "swap-title", text: `Swap ${index + 1}` }),
      el("span", { class: `chip chip-static ${length === 3 ? "chip-3" : "chip-2"}`, text: `${length}-way` })),
    el("div", { class: "swap-svg", html: svgLoop(cycle, index) }),
    el("ul", { class: "swap-pairs" }, cycle.map((id) => el("li", { text: pairLabel(id) }))),
  );
  card.addEventListener("click", () => selectSwap(card, cycle, index));
  return card;
}

function renderSwaps() {
  const grid = $("swap-grid");
  clear(grid);
  const filter = state.filter;
  const shown = state.chosen
    .map((cycle, index) => ({ cycle, index }))
    .filter((item) => filter === "all" || item.cycle.length === Number(filter));
  const total = state.chosen.length;
  $("swap-count").textContent = total ? `showing ${shown.length} of ${total} chosen swaps` : "";
  if (!total) { setStatus($("swap-status"), "No swap was found for this pool. Try a bigger pool or a longer swap."); return; }
  setStatus($("swap-status"), shown.length ? "" : "No swap of this length in the chosen set. Try the other filter.");
  for (const item of shown) grid.append(swapCard(item.cycle, item.index));
}

function selectSwap(card, cycle, index) {
  $$(".swap-card").forEach((node) => node.classList.remove("is-selected"));
  if (card) card.classList.add("is-selected");
  $("detail-title").textContent = `Swap ${index + 1} · ${cycle.length}-way`;
  $("detail-sentence").textContent = cycleSentence(cycle);
  drawDetail(cycle);
}

function drawDetail(cycle) {
  const host = $("detail-graph");
  if (state.detailCy) { state.detailCy.destroy(); state.detailCy = null; }
  clear(host);
  if (typeof window.cytoscape === "undefined") {
    host.append(el("p", { class: "muted small", text: "Interactive graph library not available; the loop is described above." }));
    return;
  }
  const elements = [];
  cycle.forEach((id, i) => {
    const node = state.nodesById[id] || {};
    elements.push({ data: { id: String(id), label: `${id}\n${abo(node.patient_abo)} · ${abo(node.donor_abo)}` } });
    const next = cycle[(i + 1) % cycle.length];
    elements.push({ data: { id: `e${id}_${next}`, source: String(id), target: String(next) } });
  });
  const style = [
    { selector: "node", style: {
      "background-color": "#2f9e8f", label: "data(label)", color: "#ffffff", "font-size": 9,
      "text-valign": "center", "text-halign": "center", "text-wrap": "wrap", "text-max-width": 54,
      width: 46, height: 46,
    } },
    { selector: "edge", style: {
      width: 2, "line-color": "#7aa7a0", "target-arrow-color": "#7aa7a0",
      "target-arrow-shape": "triangle", "curve-style": "bezier", "arrow-scale": 1,
    } },
  ];
  state.detailCy = window.cytoscape({
    container: host, elements, style,
    layout: { name: "circle", padding: 24, animate: !reducedMotion() },
    wheelSensitivity: 0.2, userZoomingEnabled: false, userPanningEnabled: false,
  });
}

/* ------------------------------------------------- section 5: who is left out, and why */

function renderUnmatched() {
  const host = $("unmatched-rows");
  clear(host);
  $("unmatched-count").textContent = String(state.unmatched.length);
  setStatus($("unmatched-status"), state.unmatched.length ? "" : "Everyone in this pool is covered by a swap.");
  for (const id of state.unmatched) {
    const node = state.nodesById[id] || {};
    const level = difficulty(node.pra);
    const row = el("div", { class: "row-item" });
    row.append(
      el("div", { class: "row-main" },
        el("strong", { text: `Pair ${id}` }),
        el("span", { class: "muted small", text: `patient ${abo(node.patient_abo)}, donor ${abo(node.donor_abo)}` })),
      el("span", { class: `diff diff-${level.word}`, title: level.note, text: level.word }),
      el("button", { type: "button", class: "btn btn-sm", text: "Explain", onclick: () => explainPair(id) }),
    );
    host.append(row);
  }
}

/* --------------------------------------------------------------- drawer + explain */

function openDrawer(title) {
  $("drawer-title").textContent = title;
  $("drawer").hidden = false;
  $("drawer-backdrop").hidden = false;
  document.body.classList.add("no-scroll");
  $("drawer-close").focus();
}

function closeDrawer() {
  $("drawer").hidden = true;
  $("drawer-backdrop").hidden = true;
  document.body.classList.remove("no-scroll");
}

async function explainPair(id) {
  openDrawer(`Pair ${id}`);
  const body = $("drawer-body");
  clear(body);
  body.append(el("p", { class: "muted", text: `Asking the graph about pair ${id}…` }));
  let env;
  try {
    env = await api(`/tools/explain/${encodeURIComponent(id)}`);
  } catch (err) {
    clear(body);
    body.append(el("p", { class: "status-bad", text: err.message }));
    return;
  }
  renderExplain(env);
  loadTrace();
}

function renderExplain(env) {
  const body = $("drawer-body");
  clear(body);
  const result = env.result || {};
  body.append(el("p", { class: "badge-line" }, outcomeBadge(env.outcome)));

  if (env.outcome === "unknown_entity") {
    body.append(el("p", { text: result.message || "This id is not in the pool." }));
    body.append(el("p", { class: "fine-print", text: "An id that was never in the graph is different from a patient who simply was not matched." }));
    return;
  }

  const pair = result.pair || {};
  const level = difficulty(pair.pra);
  if (pair.id != null) {
    body.append(el("p", {}, el("strong", { text: `Pair ${pair.id}` }), ` — patient blood type ${abo(pair.patient_abo)}, donor blood type ${abo(pair.donor_abo)}.`));
    body.append(el("p", {}, "Difficulty to match: ", el("span", { class: `diff diff-${level.word}`, title: level.note, text: level.word }), "."));
  }

  const incoming = result.incoming_candidates || [];
  const outgoing = result.outgoing_targets || [];
  body.append(el("p", { class: "lead-line" }, el("strong", { text: "Donors that could give to this patient: " }), incoming.length ? incoming.join(", ") : "none the graph knows of"));
  body.append(el("p", {}, el("strong", { text: "Patients this pair's donor could give to: " }), outgoing.length ? outgoing.join(", ") : "none"));

  const reason = result.reasoning || {};
  if (reason.abo_incompatible_pairs != null) {
    body.append(el("p", {}, `Of the other pairs, ${reason.abo_incompatible_pairs} had an incompatible blood type, and ${reason.abo_compatible_but_crossmatch_failed} had a compatible blood type but the patient's immune system still rejected that donor.`));
  }
  if (reason.note) body.append(el("p", { class: "fine-print", text: reason.note }));

  const cycles = result.cycles_through || [];
  if (cycles.length) {
    body.append(el("p", { text: "Swaps that run through this pair:" }));
    const list = el("ul", { class: "candidate-list" });
    for (const cycle of cycles) list.append(el("li", { text: cycleSentence(cycle) }));
    body.append(list);
  }

  if (env.outcome === "not_in_graph" || incoming.length === 0) {
    body.append(el("div", { class: "callout" },
      el("strong", { text: "The graph does not know." }),
      " An empty search is not a claim of safety. Nothing here says this patient cannot be matched elsewhere."));
  }
}

/* ---------------------------------------------------------------- section 6: what if */

function setFairness(on) {
  state.priorityOn = on;
  $("fairness").checked = on;
  return loadGraph();
}

async function runWhatIf(body, label) {
  const out = $("whatif-out");
  clear(out);
  out.append(el("p", { class: "muted", text: `${label} — re-running the search on a modified copy…` }));
  let env;
  try {
    env = await api("/tools/what_if", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  } catch (err) {
    clear(out);
    out.append(el("p", { class: "status-bad", text: err.message }));
    return;
  }
  renderWhatIf(env);
  loadTrace();
}

function renderWhatIf(env) {
  const out = $("whatif-out");
  clear(out);
  const r = env.result || {};
  out.append(el("p", { class: "badge-line" }, outcomeBadge(env.outcome)));

  if (env.outcome === "unknown_entity") {
    out.append(el("p", { text: r.message || "That pair is not in the pool." }));
    return;
  }

  if (r.kind === "add_altruistic_donor") {
    const before = r.before || {};
    const after = r.after || {};
    out.append(el("p", { class: "lead-line" }, el("strong", { text: `Add one altruistic ${r.donor_abo} donor` })));
    out.append(el("p", {}, `Patients matched: ${before.matched} → ${after.matched} (${r.gain >= 0 ? "+" : ""}${r.gain}).`));
    out.append(el("p", {}, `Swaps chosen: ${before.cycles} → ${after.cycles}${after.chains && after.chains.length ? `, plus ${after.chains.length} chain(s)` : ""}.`));
    if (r.flow_bound) out.append(el("p", { class: "fine-print", text: `Upper bound (also allows chains, not a plan): ${r.flow_bound.before} → ${r.flow_bound.after}.` }));
    appendChanged(out, r.changed);
    if (r.note) out.append(el("p", { class: "fine-print", text: r.note }));
    return;
  }

  if (r.kind === "remove_pair") {
    const p = r.removed_pair || {};
    const before = r.before || {};
    const after = r.after || {};
    out.append(el("p", { class: "lead-line" }, el("strong", { text: `Remove pair ${p.id}` }), ` (patient ${abo(p.patient_abo)}, donor ${abo(p.donor_abo)})`));
    out.append(el("p", {}, `Patients matched: ${before.matched} → ${after.matched} (${r.delta >= 0 ? "+" : ""}${r.delta}).`));
    out.append(el("p", {}, `Pairs in the pool: ${before.pairs} → ${after.pairs}.`));
    appendChanged(out, r.changed);
    return;
  }

  out.append(el("p", { text: "Done." }));
}

function appendChanged(out, changed) {
  changed = changed || {};
  const added = changed.added || [];
  const removed = changed.removed || [];
  if (added.length) {
    out.append(el("p", { text: "New swaps that appeared:" }));
    const list = el("ul", { class: "candidate-list" });
    for (const cycle of added) list.append(el("li", { text: cycleSentence(cycle) }));
    out.append(list);
  }
  if (removed.length) {
    out.append(el("p", { text: "Swaps that dropped out:" }));
    const list = el("ul", { class: "candidate-list" });
    for (const cycle of removed) list.append(el("li", { text: cycleSentence(cycle) }));
    out.append(list);
  }
  if (!added.length && !removed.length) {
    out.append(el("p", { class: "fine-print", text: "The chosen swaps did not change." }));
  }
}

function updateFairnessLive() {
  const host = $("fairness-live");
  clear(host);
  if (!state.priorityOn) {
    host.append(el("p", { class: "fine-print", text: "Weighting is off: the solver just maximises the number of patients matched." }));
    return;
  }
  let hard = 0;
  for (const cycle of state.chosen) {
    for (const id of cycle) {
      const node = state.nodesById[id];
      if (node && node.hard) hard += 1;
    }
  }
  const covered = state.chosen.flat().length;
  host.append(el("p", {}, `Live on this pool: ${covered} patients matched, ${hard} of them in the hardest-to-match group (weight ${state.priorityWeight}).`));
}

function renderFairnessPanel() {
  const host = $("fairness-panel");
  clear(host);
  // Measured over 20 seeds; quoted from the README's "Priority weighting" section.
  const rows = [
    ["off (weight 1)", "76.5% ± 7.7", "115.7 ± 7.3"],
    ["on (weight 3)", "96.8% ± 3.0", "114.7 ± 7.1"],
  ];
  const table = el("table", { class: "mini" });
  const thead = el("thead");
  thead.append(el("tr", {}, el("th", { text: "weighting" }), el("th", { text: "hardest-to-match matched" }), el("th", { text: "all matched" })));
  const tbody = el("tbody");
  for (const [a, b, c] of rows) tbody.append(el("tr", {}, el("td", { text: a }), el("td", { class: "num", text: b }), el("td", { class: "num", text: c })));
  table.append(thead, tbody);
  host.append(table);
  host.append(el("p", { class: "fine-print", text: "Measured over 20 random pools, command: make verify-fairness. Weighting lifted the hardest-to-match share by +20.3 ± 7.1 points and improved it on 20/20 seeds; the cost was 1.0 matched patient on average." }));
}

function populateRemoveChoices() {
  const select = $("remove-pair");
  const current = select.value;
  clear(select);
  select.append(el("option", { value: "", text: "Choose a pair to remove…" }));
  for (const node of Object.values(state.nodesById).sort((a, b) => a.id - b.id)) {
    select.append(el("option", { value: String(node.id), text: `Pair ${node.id} (patient ${abo(node.patient_abo)}, donor ${abo(node.donor_abo)})` }));
  }
  if (current && state.nodesById[current]) select.value = current;
}

/* ------------------------------------------------------- section 7: under the hood */

async function loadTrace() {
  const host = $("trace");
  let body;
  try {
    body = await api("/trace");
  } catch (err) {
    setStatus($("trace-status"), err.message, "bad");
    return;
  }
  setStatus($("trace-status"), "");
  clear(host);
  const rows = (body.traces || []).slice(-14).reverse();
  if (!rows.length) { host.append(el("p", { class: "muted small", text: "No tool calls recorded yet." })); return; }
  const table = el("table", { class: "grid" });
  const thead = el("thead");
  thead.append(el("tr", {}, el("th", { text: "tool" }), el("th", { text: "arguments" }), el("th", { text: "ms" }), el("th", { text: "rows" }), el("th", { text: "outcome" })));
  const tbody = el("tbody");
  for (const t of rows) {
    tbody.append(el("tr", {},
      el("td", { class: "mono", text: t.tool }),
      el("td", { class: "mono args", title: JSON.stringify(t.args || {}), text: fmtArgs(t.args) }),
      el("td", { class: "num", text: String(t.ms) }),
      el("td", { class: "num", text: String(t.rows) }),
      el("td", {}, outcomeBadge(t.outcome))));
  }
  table.append(thead, tbody);
  host.append(table);
}

function fmtArgs(args) {
  const text = JSON.stringify(args || {});
  return text.length > 54 ? `${text.slice(0, 51)}…` : text;
}

function renderProof(proof) {
  const host = $("proof");
  clear(host);
  if (!proof || !proof.loaded) {
    setStatus($("proof-status"), (proof && proof.message) || "No pool loaded yet.", "muted");
    return;
  }
  setStatus($("proof-status"), "");

  const ord = proof.ordering || {};
  const orderLine = el("p", { class: "fine-print" });
  orderLine.append(`${ord.invariant || "ordering invariant"} `, el("span", { class: "mono", text: `[${(ord.values || []).join(", ")}]` }), " ");
  orderLine.append(el("span", { class: ord.holds ? "tick" : "cross", text: ord.holds ? "✓ holds" : "✗ violated" }));
  host.append(orderLine);

  const checks = el("table", { class: "grid" });
  const chead = el("thead");
  chead.append(el("tr", {}, el("th", { text: "check" }), el("th", { text: "independent" }), el("th", { text: "FalkorDB" }), el("th", { text: "verdict" })));
  const cbody = el("tbody");
  for (const check of proof.checks || []) {
    cbody.append(el("tr", {},
      el("td", { text: check.name }),
      el("td", { class: "num", text: String(check.crosscheck) }),
      el("td", { class: "num", text: String(check.falkordb) }),
      el("td", {}, el("span", { class: check.agrees ? "tick" : "cross", text: check.agrees ? "✓ agrees" : "✗ differs" }))));
  }
  checks.append(chead, cbody);
  host.append(checks);
  host.append(el("p", { class: "fine-print", text: "The independent count is plain Python over the same pool; it never touches the graph." }));

  const f = proof.falkordb || {};
  const x = proof.crosscheck || {};
  const numbers = el("table", { class: "grid" });
  const nhead = el("thead");
  nhead.append(el("tr", {}, el("th", { text: "number" }), el("th", { text: "FalkorDB" }), el("th", { text: "independent" })));
  const nbody = el("tbody");
  const nrows = [
    ["2-cycle count", (f.cycles || {})[2], (x.cycles || {})[2]],
    ["3-cycle count", (f.cycles || {})[3], (x.cycles || {})[3]],
    ["best set, 2-way (patients)", f.best_set_2, "—"],
    ["best set, up to 3-way (patients)", f.best_set_3, "—"],
    ["cycle cover, any length", f.unbounded_cycle_cover, "—"],
    ["max-flow bound (upper bound, allows chains)", f.flow_bound, x.matching_bound],
  ];
  for (const [name, a, b] of nrows) {
    nbody.append(el("tr", {}, el("td", { text: name }), el("td", { class: "num", text: a == null ? "—" : String(a) }), el("td", { class: "num", text: b == null ? "—" : String(b) })));
  }
  numbers.append(nhead, nbody);
  host.append(numbers);
  host.append(el("p", { class: "fine-print" }, "All checks pass: ", el("span", { class: proof.all_checks_pass ? "tick" : "cross", text: proof.all_checks_pass ? "✓ yes" : "✗ no" })));
}

async function loadCypher() {
  let body;
  try {
    body = await api("/cypher");
  } catch (err) {
    setStatus($("cypher-status"), err.message, "bad");
    return;
  }
  setStatus($("cypher-status"), "");
  const host = $("cypher");
  clear(host);
  const queries = body.queries || {};
  const toolNames = Object.keys(queries).filter((k) => !k.startsWith("loader_")).sort();
  const loaderNames = Object.keys(queries).filter((k) => k.startsWith("loader_")).sort();
  host.append(cypherGroup("Queries the tools run", toolNames, queries));
  host.append(cypherGroup("Loading a pool into the graph", loaderNames, queries));
}

function cypherGroup(title, names, queries) {
  const group = el("div", { class: "cypher-group" });
  group.append(el("h4", { text: title }));
  for (const name of names) {
    const details = el("details", { class: "cypher-item" });
    details.append(el("summary", { text: name }));
    details.append(el("pre", {}, el("code", { text: queries[name] })));
    group.append(details);
  }
  return group;
}

function buildAskChips() {
  const host = $("ask-chips");
  clear(host);
  const questions = [
    "who can be matched at up to 3-way?",
    "how many cycles are there?",
    "explain pair 17",
    "what is the max-flow bound?",
    "what if we add an altruistic O donor?",
  ];
  for (const q of questions) {
    host.append(el("button", { type: "button", class: "chip", text: q, onclick: () => askAgent(q) }));
  }
}

async function askAgent(question) {
  const text = (question || $("q").value || "").trim();
  if (!text) return;
  $("q").value = text;
  const out = $("ask-out");
  clear(out);
  out.append(el("p", { class: "muted", text: "Asking…" }));
  let body;
  try {
    body = await api("/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question: text }),
    });
  } catch (err) {
    clear(out);
    out.append(el("p", { class: "status-bad", text: err.message }));
    return;
  }
  renderAsk(body);
  loadTrace();
}

function renderAsk(body) {
  const out = $("ask-out");
  clear(out);
  const mode = body.mode === "llm" ? "language model" : "tool mode (keyword router)";
  out.append(el("p", { class: "fine-print", text: `Answer mode: ${mode}.` }));
  out.append(el("p", { class: "lead-line", text: body.answer || "(no answer)" }));
  const rejected = body.rejected_ids || [];
  if (rejected.length) out.append(el("p", { class: "status-bad", text: `Rejected ids (named in the answer but in no tool result): ${rejected.join(", ")}` }));
  const calls = body.tool_calls || [];
  if (calls.length) {
    const table = el("table", { class: "grid" });
    const thead = el("thead");
    thead.append(el("tr", {}, el("th", { text: "tool" }), el("th", { text: "arguments" }), el("th", { text: "ms" }), el("th", { text: "rows" }), el("th", { text: "outcome" })));
    const tbody = el("tbody");
    for (const call of calls) {
      tbody.append(el("tr", {},
        el("td", { class: "mono", text: call.tool }),
        el("td", { class: "mono args", text: fmtArgs(call.args) }),
        el("td", { class: "num", text: String(call.ms) }),
        el("td", { class: "num", text: String(call.rows) }),
        el("td", {}, outcomeBadge(call.outcome))));
    }
    table.append(thead, tbody);
    out.append(table);
  }
}

/* ------------------------------------------------------------------ guided demo */

const DEMO_STEPS = [
  {
    caption: "A pool of 200 synthetic pairs, generated with seed 1000. Every number is read live from the graph.",
    run: async () => { scrollTo("result"); },
  },
  {
    caption: "If pairs may only swap back and forth (2-way), this many people get a kidney.",
    run: async () => { await setLen(2); scrollTo("result"); flash($("n2").closest(".stat")); },
  },
  {
    caption: "Allow rings of three (3-way) and more people are matched. That difference is the point.",
    run: async () => { await setLen(3); scrollTo("result"); flash($("gain").closest(".stat")); },
  },
  {
    caption: "Each chosen swap is a closed loop. This panel explains, in plain words, who gives to whom.",
    run: async () => { scrollTo("swaps"); const first = $("swap-grid").querySelector(".swap-card"); if (first) first.click(); flash($("swap-detail")); },
  },
  {
    caption: "Some patients are still left out. The graph can explain why, one honest sentence at a time.",
    run: async () => { scrollTo("leftout"); const id = state.unmatched[0]; if (id != null) await explainPair(id); },
  },
  {
    caption: "An id the graph never held answers honestly: the graph does not know that patient.",
    run: async () => { closeDrawer(); $("id-input").value = "99999"; await explainPair(99999); scrollTo("leftout"); },
  },
  {
    caption: "Turn on fairness, then open 'Under the hood' to see the trace, the independent proof and the Cypher.",
    run: async () => { await setFairness(true); $("hood").querySelector("details").open = true; await wait(250); scrollTo("hood"); flash($("proof")); },
  },
];

const demo = { index: 0 };

function wait(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function startDemo() {
  $("demo").hidden = false;
  $("demo-caption").textContent = "Setting up a fresh pool of 200 pairs…";
  $("demo-next").disabled = true;
  $("size").value = "200";
  $("size-value").textContent = "200";
  $("seed").value = "1000";
  state.size = 200;
  state.seed = 1000;
  await generatePool();
  demo.index = 0;
  $("demo-next").disabled = false;
  await showDemoStep(0);
}

async function showDemoStep(index) {
  demo.index = Math.max(0, Math.min(DEMO_STEPS.length - 1, index));
  const step = DEMO_STEPS[demo.index];
  $("demo-caption").textContent = step.caption;
  $("demo-progress").textContent = `Step ${demo.index + 1} of ${DEMO_STEPS.length}`;
  $("demo-back").disabled = demo.index === 0;
  $("demo-next").textContent = demo.index === DEMO_STEPS.length - 1 ? "Finish" : "Next";
  try { await step.run(); } catch (err) { /* a demo step must never break the page */ }
}

async function demoNext() {
  if (demo.index >= DEMO_STEPS.length - 1) { endDemo(); return; }
  await showDemoStep(demo.index + 1);
}

async function demoBack() {
  await showDemoStep(demo.index - 1);
}

function endDemo() {
  $("demo").hidden = true;
  closeDrawer();
}

/* ------------------------------------------------------------------- pool actions */

async function generatePool() {
  const button = $("generate");
  button.disabled = true;
  try {
    state.size = Number($("size").value);
    state.seed = Number($("seed").value);
    await api("/pool", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ n: state.size, seed: state.seed }),
    });
    await refreshAll();
  } catch (err) {
    setStatus($("result-status"), err.message, "bad");
  } finally {
    button.disabled = false;
  }
}

async function setLen(len) {
  state.maxLen = Number(len);
  setLenButtons();
  await loadGraph();
}

function setLenButtons() {
  $$(".seg").forEach((button) => {
    button.classList.toggle("is-active", Number(button.dataset.len) === state.maxLen);
  });
}

/* ------------------------------------------------------------------------- wiring */

function wire() {
  $$(".js-demo").forEach((button) => button.addEventListener("click", startDemo));
  $("explore").addEventListener("click", () => scrollTo("result"));

  $("size").addEventListener("input", () => {
    $("size-value").textContent = $("size").value;
    state.size = Number($("size").value);
  });
  $("generate").addEventListener("click", generatePool);
  $$(".seg").forEach((button) => button.addEventListener("click", () => setLen(button.dataset.len)));

  $$("#swap-chips .chip").forEach((chip) => chip.addEventListener("click", () => {
    state.filter = chip.dataset.filter;
    $$("#swap-chips .chip").forEach((c) => c.classList.toggle("is-active", c === chip));
    renderSwaps();
  }));

  $("id-ask").addEventListener("click", () => {
    const raw = $("id-input").value.trim();
    if (raw === "") return;
    explainPair(Number(raw));
  });
  $("id-input").addEventListener("keydown", (event) => { if (event.key === "Enter") $("id-ask").click(); });
  $("id-missing").addEventListener("click", () => { $("id-input").value = "99999"; explainPair(99999); });
  $("drawer-close").addEventListener("click", closeDrawer);
  $("drawer-backdrop").addEventListener("click", closeDrawer);

  $("whatif-altruist").addEventListener("click", () => runWhatIf(
    { kind: "add_altruistic_donor", donor_abo: "O", max_len: state.maxLen },
    "Add one altruistic O donor",
  ));
  $("whatif-remove").addEventListener("click", () => {
    const value = $("remove-pair").value;
    if (value === "") {
      clear($("whatif-out"));
      $("whatif-out").append(el("p", { class: "muted", text: "Choose a pair to remove first." }));
      return;
    }
    runWhatIf({ kind: "remove_pair", pair_id: Number(value), max_len: state.maxLen }, `Remove pair ${value}`);
  });
  $("fairness").addEventListener("change", () => setFairness($("fairness").checked));

  $("trace-refresh").addEventListener("click", loadTrace);
  $("ask-agent").addEventListener("click", () => askAgent());
  $("q").addEventListener("keydown", (event) => { if (event.key === "Enter") askAgent(); });

  $$(".nav-hood").forEach((link) => link.addEventListener("click", () => {
    $("hood").querySelector("details").open = true;
  }));

  $("demo-next").addEventListener("click", demoNext);
  $("demo-back").addEventListener("click", demoBack);
  $("demo-skip").addEventListener("click", endDemo);

  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    if (!$("drawer").hidden) closeDrawer();
    else if (!$("demo").hidden) endDemo();
  });
}

boot();
