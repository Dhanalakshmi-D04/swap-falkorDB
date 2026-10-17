"use strict";

/* Swap — single-page, guided UI.
 * Every number shown is fetched from the FastAPI layer at runtime; nothing is hard-coded except
 * the measured fairness study, which is quoted from the README and labelled as such.
 * Five steps (the idea, results, left out, what if, behind the scenes) are routed by the URL hash.
 * No frameworks, no build step. */

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
    else node.setAttribute(key, value === true ? "" : String(value));
  }
  for (const child of children.flat()) {
    if (child == null || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

const strong = (text) => el("strong", { text });
const plural = (n, one, many) => (Number(n) === 1 ? one : many || `${one}s`);

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
  if (!res.ok) throw new Error(friendlyStatus(res.status));
  try {
    return await res.json();
  } catch (err) {
    throw new Error("The server sent something that was not valid data.");
  }
}

const postJson = (body) => ({
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});

function friendlyStatus(status) {
  if (status === 404) return "That was not found. The pool may have changed, so try building one again.";
  if (status === 422) return "That input was not valid. Check the value and try again.";
  if (status >= 500) return "The server had a problem with that request. Is the graph database (FalkorDB) running?";
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

function ensureVisible(node) {
  if (!node) return;
  const rect = node.getBoundingClientRect();
  const topSafe = 120;
  const bottomSafe = window.innerHeight - (document.body.classList.contains("has-tour") ? 200 : 40);
  if (rect.top < topSafe || rect.bottom > bottomSafe) {
    node.scrollIntoView({ behavior: reducedMotion() ? "auto" : "smooth", block: "center" });
  }
}

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/* --------------------------------------------------------------------- glossary */

/* Plain-language definitions. Every `data-term` in index.html (and every `term()` below) must
 * have an entry here; tests/test_web.py checks it. */
const GLOSSARY = {
  pair: ["Pair", "A patient who needs a kidney, together with the person who is willing to donate one to them but does not match. Every pair in this app is like that: the donor cannot give to their own patient."],
  patient: ["Patient", "A person who needs a kidney transplant."],
  donor: ["Donor", "A person who is willing to give one of their two kidneys. People can live well with one."],
  pool: ["Pool", "A batch of pairs that are looked at together. The bigger the pool, the more swaps are possible."],
  seed: ["Seed (pool number)", "A number that decides which made-up pairs are drawn. The same number always gives the same pool, so results can be reproduced."],
  bloodtype: ["Blood type", "A, B, AB or O. A donor can give to a patient only if the types fit: type O donors fit everyone, type AB patients can receive from everyone, and otherwise the types must be the same."],
  crossmatch: ["Crossmatch", "A lab test of whether the patient's immune system reacts against a particular donor. A reaction means that donor cannot safely give to that patient, even if the blood types fit."],
  sensitised: ["Hard to match (sensitised)", "Some patients' immune systems react against many donors. Doctors measure this as PRA (panel-reactive antibody percent). In this simulation a hard-to-match patient rejects about 9 in 10 donors whose blood type would otherwise fit."],
  twoway: ["2-way swap", "Two pairs trade donors: the first pair's donor gives to the second pair's patient, and the second pair's donor gives to the first pair's patient."],
  threeway: ["3-way swap", "Three pairs in a ring: each pair's donor gives to the next pair's patient, and the last donor gives back to the first pair. It finds matches that 2-way swaps cannot."],
  ring: ["Ring (cycle)", "A closed loop of pairs where every donor gives to the next patient. Everyone in the loop gives and receives, so nobody gives without getting."],
  altruistic: ["Altruistic donor", "A donor who has no patient of their own. They can start a chain: they give to one pair's patient, and that pair's donor then gives to another patient."],
  upperbound: ["Upper bound", "A ceiling that the real answer cannot beat. The max-flow figure also allows chains, so it can be higher than any set of swaps you could actually perform."],
  graph: ["Graph database", "A database that stores things (here, pairs) as dots and the relationships between them (\"this donor can give to that patient\") as links. Swap uses FalkorDB. Finding swaps is then following links around a loop."],
  solver: ["Solver", "A program that picks the best combination from many options. Here it chooses the set of swaps that helps the most patients, with no pair used twice."],
  cypher: ["Cypher", "The language used to ask a graph database questions, for example \"find three pairs that form a loop\". In Swap a person writes every query. The AI never does."],
  synthetic: ["Synthetic", "Made up by a computer using published statistics. No real patient or person is in this app."],
};

function term(key, label) {
  const entry = GLOSSARY[key];
  return el("button", { type: "button", class: "term", "data-term": key, text: label || (entry ? entry[0].toLowerCase() : key) });
}

let tipFor = null;

function showTip(button) {
  const entry = GLOSSARY[button.dataset.term];
  if (!entry) return;
  tipFor = button;
  $("termtip-text").textContent = entry[1];
  const tip = $("termtip");
  tip.hidden = false;
  const rect = button.getBoundingClientRect();
  const width = Math.min(320, window.innerWidth - 24);
  tip.style.width = `${width}px`;
  let left = rect.left + rect.width / 2 - width / 2;
  left = Math.max(12, Math.min(left, window.innerWidth - width - 12));
  tip.style.left = `${left + window.scrollX}px`;
  const below = rect.bottom + 8;
  const height = tip.offsetHeight;
  const top = below + height > window.innerHeight - 12 && rect.top - height - 8 > 12 ? rect.top - height - 8 : below;
  tip.style.top = `${top + window.scrollY}px`;
}

function hideTip() {
  $("termtip").hidden = true;
  tipFor = null;
}

function openGlossary(focusKey) {
  const body = $("drawer-body");
  clear(body);
  $("drawer-title").textContent = "Glossary";
  body.append(el("p", { class: "muted small", text: "Plain-language meanings of the words used in this app." }));
  const list = el("dl", { class: "glossary" });
  const keys = Object.keys(GLOSSARY).sort((a, b) => GLOSSARY[a][0].localeCompare(GLOSSARY[b][0]));
  for (const key of keys) {
    list.append(el("dt", { id: `gloss-${key}`, text: GLOSSARY[key][0] }), el("dd", { text: GLOSSARY[key][1] }));
  }
  body.append(list);
  $("drawer").hidden = false;
  $("drawer-backdrop").hidden = false;
  document.body.classList.add("no-scroll");
  $("drawer-close").focus();
  if (focusKey && $(`gloss-${focusKey}`)) {
    const target = $(`gloss-${focusKey}`);
    target.classList.add("is-focus");
    target.scrollIntoView({ block: "start" });
  }
}

function closeGlossary() {
  $("drawer").hidden = true;
  $("drawer-backdrop").hidden = true;
  document.body.classList.remove("no-scroll");
}

/* ------------------------------------------------------------- vocabulary (plain words) */

const OUTCOMES = {
  answered_with_path: { label: "Answered, with evidence", tone: "ok", meaning: "The graph found this, and the evidence (a path) is shown." },
  not_in_graph: { label: "The graph doesn't know (not the same as safe)", tone: "warn", meaning: "The search came back empty. That means the graph has no record, not that something is safe or impossible." },
  unknown_entity: { label: "No such pair", tone: "bad", meaning: "That id is not in the current pool." },
  from_memory: { label: "Remembered from earlier", tone: "info", meaning: "This came from a stored earlier answer, not a fresh lookup." },
};

function outcomeBadge(outcome) {
  const info = OUTCOMES[outcome] || { label: String(outcome), tone: "muted", meaning: "" };
  return el("span", { class: `badge badge-${info.tone}`, title: info.meaning, text: info.label });
}

const DIFFICULTY = [
  { at: 0.05, word: "easy", label: "Easy to match", note: "This patient's immune system rarely rejects a donor with a fitting blood type (about 1 in 20)." },
  { at: 0.45, word: "medium", label: "Medium", note: "This patient's immune system rejects about 4 or 5 in 10 donors whose blood type fits." },
  { at: 0.90, word: "hard", label: "Hard to match", note: "This patient's immune system rejects about 9 in 10 donors whose blood type fits (highly sensitised)." },
];

function difficulty(pra) {
  const value = Number(pra);
  let best = DIFFICULTY[0];
  for (const level of DIFFICULTY) {
    if (Math.abs(value - level.at) < Math.abs(value - best.at)) best = level;
  }
  return best;
}

const abo = (letter) => letter || "?";
const aboFits = (donor, patient) => donor === "O" || donor === patient || patient === "AB";

function diffPill(pra) {
  const level = difficulty(pra);
  return el("span", { class: `diff diff-${level.word}`, title: level.note, text: level.label });
}

/* ------------------------------------------------------------------------- state */

const STEPS = [
  { id: "learn", label: "The idea" },
  { id: "results", label: "Results" },
  { id: "leftout", label: "Left out" },
  { id: "whatif", label: "What if?" },
  { id: "hood", label: "Proof" },
];

const state = {
  step: "learn",
  maxLen: 3,
  size: 200,
  seed: 1000,
  priorityOn: false,
  priorityWeight: 3,
  hardMark: false,
  nodes: [],
  nodesById: {},
  counts: null,
  plans: {},
  selected: null,
  ready: false,
  token: 0,
  graphs: {},
  explained: {},
  proofStale: true,
  detailSeq: 0,
};

const planKey = (len, priority) => `${len}|${(priority === undefined ? state.priorityOn : priority) ? state.priorityWeight : 0}`;
const planFor = (len) => state.plans[planKey(len)] || null;
const currentPlan = () => planFor(state.maxLen);

function makePlan(cycles, chains) {
  const owner = new Map();
  cycles.forEach((cycle, index) => cycle.forEach((id) => owner.set(Number(id), index)));
  return { cycles, chains: chains || [], matched: owner.size, owner };
}

async function fetchPlan(len, priority) {
  const key = planKey(len, priority);
  const plans = state.plans;
  if (plans[key]) return plans[key];
  const on = priority === undefined ? state.priorityOn : priority;
  const env = await api(`/tools/best_set?max_len=${len}${on ? `&priority=${state.priorityWeight}` : ""}`);
  const result = env.result || {};
  const plan = makePlan(Array.isArray(result.cycles) ? result.cycles : [], result.chains);
  plans[key] = plan;
  return plan;
}

const hardNodes = () => state.nodes.filter((n) => n.hard);
const coveredBy = (plan) => (plan ? plan.owner : new Map());

/* ------------------------------------------------------------------- app errors */

let retryAction = null;

function showError(message, retry) {
  $("app-error-text").textContent = message;
  $("app-error").hidden = false;
  retryAction = retry || null;
  $("app-error-retry").hidden = !retry;
}

function clearError() {
  $("app-error").hidden = true;
  retryAction = null;
}

/* ------------------------------------------------------------------------ router */

function stepFromHash() {
  const match = location.hash.match(/^#\/(\w+)/);
  const found = match && STEPS.find((s) => s.id === match[1]);
  return found ? found.id : "learn";
}

function showStep(id, options) {
  const opts = options || {};
  state.step = id;
  $$("[data-panel]").forEach((panel) => { panel.hidden = panel.dataset.panel !== id; });
  $$(".stepper a").forEach((link) => {
    if (link.dataset.step === id) link.setAttribute("aria-current", "step");
    else link.removeAttribute("aria-current");
  });
  const index = STEPS.findIndex((s) => s.id === id);
  const prev = STEPS[index - 1];
  const next = STEPS[index + 1];
  $("pager-back").hidden = !prev;
  $("pager-back").textContent = prev ? `← ${prev.label}` : "";
  $("pager-back").dataset.go = prev ? prev.id : "";
  $("pager-next").hidden = !next;
  $("pager-next").textContent = next ? `Next: ${next.label} →` : "";
  $("pager-next").dataset.go = next ? next.id : "";
  document.title = `${STEPS[index].label} · swap`;
  hideTip();
  if (!opts.keepScroll) window.scrollTo({ top: 0, behavior: "auto" });
  if (opts.focus !== false) $("main").focus({ preventScroll: true });
  enterStep(id);
}

function goStep(id, options) {
  const target = `#/${id}`;
  if (location.hash !== target) history.replaceState(null, "", target);
  showStep(id, options);
}

function enterStep(id) {
  if (id === "results" || id === "leftout") renderDetails();
  if (id === "leftout") renderLeftout();
  if (id === "whatif") compareFairness();
  if (id === "hood") {
    loadProof();
    loadTrace();
    loadCypher();
  }
}

/* ----------------------------------------------------------- step 1: the story pictures */

const STORY = [
  {
    title: "Meet a pair",
    body: "A patient needs a kidney. Someone close to them is willing to give one. Together they are called a pair.",
    art: () => `
      <rect x="40" y="38" width="280" height="140" rx="22" class="art-box"/>
      <text x="180" y="30" class="art-cap art-center">a pair</text>
      <circle cx="105" cy="105" r="30" class="art-patient"/><text x="105" y="112" class="art-on art-center">B</text>
      <text x="105" y="158" class="art-cap art-center">needs a kidney</text>
      <circle cx="255" cy="105" r="30" class="art-donor"/><text x="255" y="112" class="art-on art-center">A</text>
      <text x="255" y="158" class="art-cap art-center">willing to give</text>
      <path d="M220 105 L150 105" class="art-arrow art-dim" marker-end="url(#arrowhead)"/>`,
  },
  {
    title: "But they don't match",
    body: "The donor's blood type may not fit the patient, or the patient's immune system may reject that donor. Then this donor can't give to their own patient.",
    art: () => `
      <rect x="40" y="38" width="280" height="140" rx="22" class="art-box"/>
      <circle cx="105" cy="100" r="30" class="art-patient"/><text x="105" y="107" class="art-on art-center">B</text>
      <circle cx="255" cy="100" r="30" class="art-donor"/><text x="255" y="107" class="art-on art-center">A</text>
      <path d="M222 100 L146 100" class="art-arrow art-bad" marker-end="url(#arrowhead-bad)"/>
      <text x="184" y="94" class="art-bad-text art-center">✕</text>
      <text x="180" y="152" class="art-cap art-center">wrong blood type, or immune rejection</text>`,
  },
  {
    title: "Two pairs can swap",
    body: "Pair 1's donor fits Pair 2's patient, and Pair 2's donor fits Pair 1's patient. They swap donors, and both patients get a kidney that fits. That is a 2-way swap.",
    art: () => `
      <rect x="22" y="22" width="92" height="176" rx="18" class="art-box"/>
      <text x="68" y="16" class="art-cap art-center">Pair 1</text>
      <rect x="246" y="22" width="92" height="176" rx="18" class="art-box"/>
      <text x="292" y="16" class="art-cap art-center">Pair 2</text>
      <circle cx="68" cy="62" r="24" class="art-patient"/><text x="68" y="69" class="art-on art-center">B</text>
      <circle cx="68" cy="152" r="24" class="art-donor"/><text x="68" y="159" class="art-on art-center">A</text>
      <circle cx="292" cy="62" r="24" class="art-patient"/><text x="292" y="69" class="art-on art-center">A</text>
      <circle cx="292" cy="152" r="24" class="art-donor"/><text x="292" y="159" class="art-on art-center">B</text>
      <path d="M92 144 L268 74" class="art-arrow art-ok" marker-end="url(#arrowhead)"/>
      <path d="M268 144 L92 74" class="art-arrow art-ok" marker-end="url(#arrowhead)"/>
      <text x="180" y="112" class="art-cap art-center art-ink">swap!</text>`,
  },
  {
    title: "Three pairs can make a ring",
    body: "Sometimes two pairs can't swap directly, but three can pass kidneys around a ring: each donor gives to the next pair's patient. That is a 3-way swap, and it unlocks matches that 2-way swaps miss.",
    art: () => `
      <circle cx="180" cy="48" r="28" class="art-pair"/><text x="180" y="53" class="art-on art-center art-sm">Pair 1</text>
      <circle cx="285" cy="158" r="28" class="art-pair"/><text x="285" y="163" class="art-on art-center art-sm">Pair 2</text>
      <circle cx="75" cy="158" r="28" class="art-pair"/><text x="75" y="163" class="art-on art-center art-sm">Pair 3</text>
      <path d="M200 72 L266 134" class="art-arrow art-ok" marker-end="url(#arrowhead)"/>
      <path d="M255 168 L107 168" class="art-arrow art-ok" marker-end="url(#arrowhead)"/>
      <path d="M92 132 L160 74" class="art-arrow art-ok" marker-end="url(#arrowhead)"/>
      <path d="M306 128 Q330 60 214 40" class="art-arrow art-bad art-dash" marker-end="url(#arrowhead-bad)"/>
      <text x="318" y="82" class="art-bad-text">✕</text>
      <text x="180" y="196" class="art-cap art-center">Pair 2's donor doesn't fit Pair 1, so no 2-way swap, but the ring works</text>`,
  },
  {
    title: "A graph finds the best set",
    body: "With hundreds of pairs there are thousands of possible rings. Swap keeps the pairs in a graph (dots joined by \"can give to\" links), lists every ring, then picks the set that helps the most patients, with nobody used twice.",
    art: () => `
      <g class="art-edges">
        <line x1="40" y1="60" x2="110" y2="30"/><line x1="110" y1="30" x2="170" y2="82"/>
        <line x1="40" y1="60" x2="80" y2="140"/><line x1="80" y1="140" x2="170" y2="82"/>
        <line x1="170" y1="82" x2="250" y2="40"/><line x1="250" y1="40" x2="320" y2="90"/>
        <line x1="170" y1="82" x2="230" y2="160"/><line x1="230" y1="160" x2="320" y2="90"/>
        <line x1="80" y1="140" x2="140" y2="190"/><line x1="140" y1="190" x2="230" y2="160"/>
        <line x1="110" y1="30" x2="250" y2="40"/><line x1="40" y1="60" x2="140" y2="190"/>
      </g>
      <path d="M250 40 L320 90 L230 160 Z" class="art-ring"/>
      <path d="M40 60 L80 140" class="art-ring"/>
      <circle cx="40" cy="60" r="12" class="art-pair"/><circle cx="80" cy="140" r="12" class="art-pair"/>
      <circle cx="250" cy="40" r="12" class="art-pair"/><circle cx="320" cy="90" r="12" class="art-pair"/><circle cx="230" cy="160" r="12" class="art-pair"/>
      <circle cx="110" cy="30" r="12" class="art-out"/><circle cx="170" cy="82" r="12" class="art-out"/><circle cx="140" cy="190" r="12" class="art-out"/>
      <text x="180" y="212" class="art-cap art-center">teal: chosen swaps · grey: left out</text>`,
  },
];

const story = { index: 0 };

function renderStory() {
  const slide = STORY[story.index];
  $("story-art").innerHTML = `<svg viewBox="0 0 360 220" class="art" aria-hidden="true">${slide.art()}</svg>`;
  $("story-count").textContent = `${story.index + 1} of ${STORY.length}`;
  $("story-title").textContent = slide.title;
  $("story-body").textContent = slide.body;
  $("story-prev").disabled = story.index === 0;
  $("story-next").textContent = story.index === STORY.length - 1 ? "Go to the results" : "Next";
  $("story-art").setAttribute("aria-label", slide.title);
  $$(".story-dot").forEach((dot, i) => dot.classList.toggle("is-on", i === story.index));
}

function buildStory() {
  const dots = $("story-dots");
  clear(dots);
  STORY.forEach(() => dots.append(el("span", { class: "story-dot" })));
  renderStory();
}

function storyNext() {
  if (story.index >= STORY.length - 1) { goStep("results"); return; }
  story.index += 1;
  renderStory();
}

function storyPrev() {
  story.index = Math.max(0, story.index - 1);
  renderStory();
}

/* ------------------------------------------------------------- loading the pool */

async function loadPool(options) {
  const create = Boolean(options && options.create);
  const token = ++state.token;
  state.ready = false;
  clearError();
  setStatus($("result-status"), create ? "Building the pool and loading it into the graph database…" : "Loading the pool from the graph database…");
  setStatus($("unmatched-status"), "Looking for swaps…");
  $("map").classList.add("is-loading");
  try {
    if (create) await api("/pool", postJson({ n: state.size, seed: state.seed }));
    let graph;
    try {
      graph = await api("/graph?max_len=2");
    } catch (err) {
      if (create) throw err;
      setStatus($("result-status"), "Preparing a first pool…");
      await api("/pool", postJson({ n: state.size, seed: state.seed }));
      graph = await api("/graph?max_len=2");
    }
    if (token !== state.token) return;
    state.nodes = graph.nodes || [];
    state.nodesById = {};
    for (const node of state.nodes) state.nodesById[node.id] = node;
    state.counts = { edges: graph.edge_count, nodes: state.nodes.length };
    state.plans = {};
    state.explained = {};
    state.proofStale = true;
    state.selected = null;
    if (!state.priorityOn) state.plans[planKey(2)] = makePlan(graph.chosen || [], []);
    await Promise.all([fetchPlan(2), fetchPlan(3)]);
    if (token !== state.token) return;
    state.ready = true;
    state.size = state.nodes.length;
    $("size").value = String(Math.min(300, Math.max(20, state.size)));
    $("size-value").textContent = $("size").value;
    $("map").classList.remove("is-loading");
    buildMap();
    renderAll();
    if (state.step === "hood") { loadProof(); loadTrace(); }
  } catch (err) {
    if (token !== state.token) return;
    $("map").classList.remove("is-loading");
    setStatus($("result-status"), "");
    setStatus($("unmatched-status"), "");
    showError(err.message, () => loadPool(options));
  }
}

function renderAll() {
  renderNumbers();
  paintMap();
  renderLegend();
  renderSwaps();
  renderLeftout();
  populateRemoveChoices();
  renderDetails();
  renderFairnessLive();
  renderHeroLive();
  const pool = state.counts || {};
  $("pool-line").textContent = `${pool.nodes ?? "?"} pairs · ${pool.edges ?? "?"} "can give to" links · ${hardNodes().length} hard to match`;
  setStatus($("result-status"), state.priorityOn ? "Priority for hard-to-match patients is on." : "");
}

/* -------------------------------------------------------------- step 2: the numbers */

function renderNumbers() {
  const p2 = planFor(2);
  const p3 = planFor(3);
  if (!p2 || !p3) return;
  const total = state.nodes.length;
  const gain = p3.matched - p2.matched;
  animateNumber($("n2"), p2.matched);
  animateNumber($("n3"), p3.matched);
  $("gain").textContent = gain > 0 ? `+${gain}` : String(gain);
  const host = $("result-sentence");
  clear(host);
  if (gain > 0) {
    host.append("In a pool of ", strong(total), " pairs, allowing rings of three helps ", strong(`${gain} more ${plural(gain, "patient")}`), " than swaps in twos.");
  } else {
    host.append("In this pool of ", strong(total), " pairs, allowing rings of three does not help more patients.");
  }
  if (state.priorityOn) host.append(" Priority for hard-to-match patients is on, so totals can differ slightly from the plain maximum.");
}

function renderHeroLive() {
  const host = $("hero-live");
  const p2 = planFor(2);
  const p3 = planFor(3);
  if (!state.ready || !p2 || !p3) { host.hidden = true; return; }
  const gain = p3.matched - p2.matched;
  clear(host);
  host.append("Right now, on ", strong(`${state.nodes.length} made-up pairs`), ": ", strong(`${p3.matched} patients`), " get a kidney");
  if (gain > 0) host.append(", ", strong(`${gain} more`), " than with 2-way swaps alone");
  host.append(".");
  host.hidden = false;
}

/* ------------------------------------------------------------- step 2: the map of dots */

function dotKind(id) {
  const p2 = planFor(2);
  const p3 = planFor(3);
  const in2 = p2.owner.has(id);
  const in3 = p3.owner.has(id);
  if (state.maxLen === 2) return in2 ? "kept" : in3 ? "maybe" : "out";
  if (in3) return in2 ? "kept" : "gain";
  return "out";
}

function swapSize(id) {
  const plan = currentPlan();
  const index = plan.owner.get(id);
  return index == null ? 0 : plan.cycles[index].length;
}

function dotText(id) {
  const node = state.nodesById[id] || {};
  const kind = dotKind(id);
  const head = `Pair ${id}: patient ${abo(node.patient_abo)}, donor ${abo(node.donor_abo)}`;
  if (kind === "kept") return `${head}. Gets a kidney in a ${swapSize(id)}-way swap.`;
  if (kind === "gain") return `${head}. Gets a kidney in a ${swapSize(id)}-way swap, only because 3-way swaps are allowed.`;
  if (kind === "maybe") return `${head}. Left out with 2-way swaps, but would get a kidney if 3-way swaps were allowed.`;
  return `${head}. Left out.`;
}

function buildMap() {
  const host = $("map");
  clear(host);
  const frag = document.createDocumentFragment();
  state.nodes.forEach((node, i) => {
    frag.append(el("button", { type: "button", class: "dot", "data-id": String(node.id), tabindex: i === 0 ? "0" : "-1" }));
  });
  host.append(frag);
}

function paintMap() {
  if (!state.ready) return;
  const hard = state.hardMark;
  const sel = state.selected;
  const mates = new Set();
  if (sel != null) {
    const plan = currentPlan();
    const index = plan.owner.get(sel);
    if (index != null) plan.cycles[index].forEach((id) => mates.add(Number(id)));
  }
  for (const dot of $$(".dot", $("map"))) {
    const id = Number(dot.dataset.id);
    const node = state.nodesById[id] || {};
    const text = dotText(id);
    dot.className = `dot dot-${dotKind(id)}`;
    if (hard && node.hard) dot.classList.add("is-hard");
    if (sel === id) dot.classList.add("is-selected");
    else if (mates.has(id)) dot.classList.add("is-mate");
    dot.setAttribute("aria-label", text);
    dot.title = text;
    dot.setAttribute("aria-pressed", sel === id ? "true" : "false");
  }
}

function renderLegend() {
  const host = $("map-legend");
  clear(host);
  const items = state.maxLen === 2
    ? [["kept", "Gets a kidney in a 2-way swap"], ["maybe", "Left out now, but a 3-way swap would help them"], ["out", "Left out"]]
    : [["kept", "Gets a kidney either way"], ["gain", "Gets a kidney only when 3-way swaps are allowed"], ["out", "Left out"]];
  for (const [kind, text] of items) {
    host.append(el("li", {}, el("span", { class: `dot dot-${kind} dot-legend`, "aria-hidden": "true" }), text));
  }
  if (state.hardMark) host.append(el("li", {}, el("span", { class: "dot dot-out is-hard dot-legend", "aria-hidden": "true" }), "Ringed: hard to match"));
  $$(".seg").forEach((button) => {
    const on = Number(button.dataset.len) === state.maxLen;
    button.classList.toggle("is-active", on);
    button.setAttribute("aria-pressed", on ? "true" : "false");
  });
}

function mapColumns(dots) {
  if (!dots.length) return 1;
  const top = dots[0].offsetTop;
  let count = 0;
  for (const dot of dots) {
    if (dot.offsetTop !== top) break;
    count += 1;
  }
  return Math.max(1, count);
}

function onMapKeydown(event) {
  const dots = $$(".dot", $("map"));
  const i = dots.indexOf(document.activeElement);
  if (i < 0) return;
  const cols = mapColumns(dots);
  const moves = { ArrowRight: 1, ArrowLeft: -1, ArrowDown: cols, ArrowUp: -cols };
  let j = i;
  if (event.key in moves) j = i + moves[event.key];
  else if (event.key === "Home") j = 0;
  else if (event.key === "End") j = dots.length - 1;
  else return;
  event.preventDefault();
  if (dots[j]) dots[j].focus();
}

function onMapFocus(event) {
  if (!event.target.classList || !event.target.classList.contains("dot")) return;
  $$(".dot", $("map")).forEach((dot) => dot.setAttribute("tabindex", dot === event.target ? "0" : "-1"));
}

function onMapClick(event) {
  const dot = event.target.closest(".dot");
  if (!dot || !state.ready) return;
  selectPair(Number(dot.dataset.id), { reveal: true });
}

function selectPair(id, options) {
  state.selected = id;
  paintMap();
  paintSwapSelection();
  paintLeftoutSelection();
  renderDetails();
  if (options && options.reveal) {
    const host = state.step === "leftout" ? $("detail-leftout") : $("detail-results");
    if (window.innerWidth < 980) ensureVisible(host);
  }
}

/* --------------------------------------------------------- the details panel (shared) */

function destroyGraph(hostId) {
  const cy = state.graphs[hostId];
  if (cy) {
    try { cy.destroy(); } catch (err) { /* already gone */ }
    delete state.graphs[hostId];
  }
}

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function drawGraph(host, key, elements, layout) {
  destroyGraph(key);
  clear(host);
  if (typeof window.cytoscape === "undefined") {
    host.append(el("p", { class: "muted small", text: "The interactive graph is not available; the steps below describe it." }));
    return;
  }
  const accent = cssVar("--accent");
  const style = [
    { selector: "node", style: {
      "background-color": cssVar("--c-pair"), label: "data(label)", color: "#ffffff", "font-size": 11, "font-weight": 700,
      "text-valign": "center", "text-halign": "center", width: 38, height: 38, "border-width": 2, "border-color": cssVar("--surface"),
    } },
    { selector: "node.patient", style: { "background-color": accent, width: 50, height: 50, "font-size": 12 } },
    { selector: "node.hard", style: { "border-color": cssVar("--bad"), "border-width": 4 } },
    { selector: "node.free", style: { "background-color": cssVar("--c-donor") } },
    { selector: "node.busy", style: { "background-color": cssVar("--c-edge") } },
    { selector: "node.me", style: { "border-color": cssVar("--ink"), "border-width": 4 } },
    { selector: "edge", style: {
      width: 2.5, "line-color": cssVar("--c-edge"), "target-arrow-color": cssVar("--c-edge"),
      "target-arrow-shape": "triangle", "curve-style": "bezier", "arrow-scale": 1.2,
    } },
  ];
  state.graphs[key] = window.cytoscape({
    container: host, elements, style, layout,
    userZoomingEnabled: false, userPanningEnabled: false, boxSelectionEnabled: false, autoungrabify: true,
  });
}

function suggestions(hostId) {
  const out = [];
  const plan = currentPlan();
  const matchedIds = [...plan.owner.keys()];
  const leftIds = state.nodes.map((n) => n.id).filter((id) => !plan.owner.has(id));
  if (hostId === "detail-leftout") {
    const hardLeft = leftIds.filter((id) => state.nodesById[id].hard);
    if (hardLeft[0] != null) out.push({ label: `A hard-to-match patient (pair ${hardLeft[0]})`, id: hardLeft[0] });
    const easyLeft = leftIds.find((id) => !state.nodesById[id].hard);
    if (easyLeft != null) out.push({ label: `A patient with an easier match (pair ${easyLeft})`, id: easyLeft });
    return out;
  }
  const gainId = matchedIds.find((id) => dotKind(id) === "gain" || dotKind(id) === "maybe") ?? state.nodes.map((n) => n.id).find((id) => dotKind(id) === "maybe");
  if (gainId != null) out.push({ label: `A pair that 3-way swaps help (pair ${gainId})`, id: gainId });
  const twoId = matchedIds.find((id) => swapSize(id) === 2);
  if (twoId != null) out.push({ label: `A pair in a 2-way swap (pair ${twoId})`, id: twoId });
  const hardLeft = leftIds.find((id) => state.nodesById[id].hard);
  if (hardLeft != null) out.push({ label: `A patient who was left out (pair ${hardLeft})`, id: hardLeft });
  return out;
}

function renderDetails() {
  if (!state.ready) {
    const first = state.step === "leftout" ? $("detail-leftout") : $("detail-results");
    if (first && !first.firstChild) first.append(el("p", { class: "muted", text: "Waiting for the pool…" }));
    return;
  }
  if (state.step === "results") renderDetail($("detail-results"));
  else if (state.step === "leftout") renderDetail($("detail-leftout"));
}

async function renderDetail(host) {
  const seq = ++state.detailSeq;
  host.dataset.seq = String(seq);
  destroyGraph(host.id);
  clear(host);
  const id = state.selected;
  const node = id == null ? null : state.nodesById[id];
  if (!node) { renderDetailEmpty(host); return; }
  const plan = currentPlan();
  const index = plan.owner.get(id);
  if (index != null) renderMatchedDetail(host, id, plan.cycles[index]);
  else await renderLeftOutDetail(host, id, seq);
}

function renderDetailEmpty(host) {
  const isLeft = host.id === "detail-leftout";
  host.append(
    el("p", { class: "detail-kicker", text: isLeft ? "Pick a patient" : "Tap a dot" }),
    el("h3", { text: isLeft ? "Why was this patient left out?" : "What happened to this pair?" }),
    el("p", { class: "muted", text: isLeft
      ? "Choose someone from the list. Swap shows which donors could have helped, and what stood in the way."
      : "Each dot on the map is one pair. Tap one and this panel explains, in plain words, what happened to it." }),
  );
  const picks = suggestions(host.id);
  if (picks.length) {
    host.append(el("p", { class: "small muted", text: "Not sure where to start? Try:" }));
    const row = el("div", { class: "chips chips-wrap" });
    for (const pick of picks) row.append(el("button", { type: "button", class: "chip", text: pick.label, onclick: () => selectPair(pick.id, { reveal: true }) }));
    host.append(row);
  }
}

function cycleStepText(cycle, i) {
  const from = state.nodesById[cycle[i]] || {};
  const to = state.nodesById[cycle[(i + 1) % cycle.length]] || {};
  const next = cycle[(i + 1) % cycle.length];
  const fits = aboFits(from.donor_abo, to.patient_abo);
  return [
    strong(`Pair ${cycle[i]}`), `'s donor (type ${abo(from.donor_abo)}) gives to `, strong(`Pair ${next}`),
    `'s patient (type ${abo(to.patient_abo)})`, fits ? " ✓" : "",
  ];
}

function ringElements(cycle, selectedId) {
  const elements = [];
  cycle.forEach((id, i) => {
    const node = state.nodesById[id] || {};
    const classes = [node.hard ? "hard" : "", id === selectedId ? "me" : ""].filter(Boolean).join(" ");
    elements.push({ data: { id: String(id), label: `#${id}` }, classes });
    elements.push({ data: { id: `e${i}`, source: String(id), target: String(cycle[(i + 1) % cycle.length]) } });
  });
  return elements;
}

function renderMatchedDetail(host, id, cycle) {
  const node = state.nodesById[id] || {};
  const size = cycle.length;
  const p2 = planFor(2);
  const onlyBecause3 = state.maxLen === 3 && !p2.owner.has(id);
  host.append(
    el("p", { class: "detail-kicker" }, el("span", { class: "badge badge-ok", text: "Gets a kidney" })),
    el("h3", { text: `Pair ${id} is in a ${size}-way swap` }),
    el("p", {}, `Patient type ${abo(node.patient_abo)}, own donor type ${abo(node.donor_abo)}: no match, so they join a swap. `, diffPill(node.pra), "."),
  );
  const graphHost = el("div", { class: "graph-host", role: "img", "aria-label": `Ring of pairs ${cycle.join(", ")}` });
  host.append(graphHost);
  drawGraph(graphHost, host.id, ringElements(cycle, id), { name: "circle", padding: 24, animate: false });
  host.append(el("p", { class: "small muted", text: "Arrows mean \"donor gives to\":" }));
  const list = el("ol", { class: "ring-steps" });
  cycle.forEach((_, i) => list.append(el("li", {}, cycleStepText(cycle, i))));
  host.append(list);
  host.append(el("p", { class: "fine-print", text: "Each ✓ is a donor-to-patient link the graph holds. Everyone in the ring gives and receives." }));
  if (onlyBecause3) {
    host.append(el("div", { class: "callout callout-good" }, strong("Only found because 3-way swaps are allowed."), " In the best 2-way-only plan, this pair is left out."));
  }
}

async function loadExplain(id) {
  if (state.explained[id]) return state.explained[id];
  const env = await api(`/tools/explain/${encodeURIComponent(id)}`);
  state.explained[id] = env;
  return env;
}

async function renderLeftOutDetail(host, id, seq) {
  const node = state.nodesById[id] || {};
  host.append(
    el("p", { class: "detail-kicker" }, el("span", { class: "badge badge-muted", text: "Left out" })),
    el("h3", { text: `Pair ${id} is not in a swap` }),
    el("p", { class: "muted", text: "Asking the graph who could have helped…" }),
  );
  let env;
  try {
    env = await loadExplain(id);
  } catch (err) {
    if (host.dataset.seq !== String(seq)) return;
    clear(host);
    host.append(el("p", { class: "status-bad", text: err.message }));
    return;
  }
  if (host.dataset.seq !== String(seq)) return;
  clear(host);

  const result = env.result || {};
  const incoming = (result.incoming_candidates || []).map(Number);
  const reason = result.reasoning || {};
  const plan = currentPlan();
  const covered = plan.owner;
  const hasMaybe = state.maxLen === 2 && planFor(3).owner.has(id);

  host.append(
    el("p", { class: "detail-kicker" }, el("span", { class: "badge badge-muted", text: "Left out" })),
    el("h3", { text: `Why is pair ${id} left out?` }),
    el("p", {}, `Patient blood type ${abo(node.patient_abo)}, donor type ${abo(node.donor_abo)}. `, diffPill(node.pra), "."),
  );

  if (hasMaybe) {
    const ring = planFor(3).cycles[planFor(3).owner.get(id)];
    host.append(el("div", { class: "callout callout-good" },
      strong("Good news with 3-way swaps."), ` If rings of three are allowed, this pair joins the swap ${ring.map((x) => `#${x}`).join(" → ")}. `,
      el("button", { type: "button", class: "btn btn-sm", text: "Show 3-way on the map", onclick: () => setLen(3) })));
  }

  if (reason.abo_incompatible_pairs != null) {
    const wrongType = Number(reason.abo_incompatible_pairs);
    const immune = Number(reason.abo_compatible_but_crossmatch_failed);
    const canGive = incoming.length;
    const total = Math.max(1, wrongType + immune + canGive);
    host.append(el("h4", { text: `The other ${total} donors in the pool` }));
    const bar = el("div", { class: "reason-bar", role: "img", "aria-label": `${wrongType} wrong blood type, ${immune} immune rejection, ${canGive} could give` });
    for (const [cls, n] of [["seg-type", wrongType], ["seg-immune", immune], ["seg-can", canGive]]) {
      if (n > 0) bar.append(el("span", { class: cls, style: `width:${(100 * n) / total}%` }));
    }
    host.append(bar);
    host.append(el("ul", { class: "reason-list" },
      el("li", {}, el("span", { class: "key key-type" }), strong(wrongType), " have the wrong ", term("bloodtype", "blood type"), " for this patient."),
      el("li", {}, el("span", { class: "key key-immune" }), strong(immune), " fit by blood type, but the patient's immune system rejects them (", term("crossmatch", "crossmatch"), ")."),
      el("li", {}, el("span", { class: "key key-can" }), strong(canGive), ` could give to this patient${canGive ? ": " + incoming.slice(0, 12).map((x) => `#${x}`).join(", ") + (canGive > 12 ? ` and ${canGive - 12} more` : "") : "."}`)));
  }

  const shown = incoming.slice(0, 10);
  if (shown.length) {
    const graphHost = el("div", { class: "graph-host graph-host-sm", role: "img", "aria-label": `Donors that could give to pair ${id}` });
    host.append(graphHost);
    const elements = [{ data: { id: "me", label: `#${id}` }, classes: `patient ${node.hard ? "hard" : ""}` }];
    shown.forEach((donorId) => {
      elements.push({ data: { id: `d${donorId}`, label: `#${donorId}` }, classes: covered.has(donorId) ? "busy" : "free" });
      elements.push({ data: { id: `x${donorId}`, source: `d${donorId}`, target: "me" } });
    });
    drawGraph(graphHost, host.id, elements, { name: "concentric", padding: 20, animate: false, concentric: (n) => (n.id() === "me" ? 2 : 1), levelWidth: () => 1, minNodeSpacing: 24 });
    host.append(el("ul", { class: "legend legend-sm" },
      el("li", {}, el("span", { class: "key key-me" }), "this patient"),
      el("li", {}, el("span", { class: "key key-can" }), "donor not used in a swap"),
      el("li", {}, el("span", { class: "key key-busy" }), "donor already giving in another swap")));
  }

  host.append(explainVerdict(id, incoming, covered, result));
  if (env.outcome === "not_in_graph" || !incoming.length) {
    host.append(el("div", { class: "callout" },
      strong("The graph does not know of any donor here."),
      " An empty search is not a claim that no match is possible: a bigger pool, or an altruistic donor, might change it. ",
      el("button", { type: "button", class: "btn btn-sm", text: "Try adding a donor", onclick: () => goStep("whatif") })));
  }
}

function explainVerdict(id, incoming, covered, result) {
  const box = el("div", { class: "verdict" });
  box.append(el("h4", { text: "So why no swap?" }));
  if (!incoming.length) {
    box.append(el("p", { text: "Nobody in this pool can give to this patient, so no swap can end with them getting a kidney." }));
    return box;
  }
  const busy = incoming.filter((x) => covered.has(x));
  const rings = (result.cycles_through || []).filter((c) => c.length <= state.maxLen);
  const blocked = rings.filter((c) => c.some((x) => Number(x) !== id && covered.has(Number(x))));
  box.append(el("p", {}, strong(incoming.length), ` ${plural(incoming.length, "donor", "donors")} could help, but `, strong(busy.length), ` already ${plural(busy.length, "gives", "give")} in another swap, and a donor can only be used once.`));
  if (!rings.length) {
    box.append(el("p", { text: `No ring of up to ${state.maxLen} pairs closes through this patient: there is no way for their own donor to give to someone who leads back to them.` }));
  } else if (blocked.length === rings.length) {
    box.append(el("p", {}, `${rings.length} ${plural(rings.length, "ring")} of up to ${state.maxLen} pairs ${plural(rings.length, "passes", "pass")} through this patient, but `, strong(plural(blocked.length, "it needs", "each needs")), " someone who is already in another swap. Swap picks the set that helps the most people overall, so this patient lost out."));
  } else {
    box.append(el("p", {}, `${rings.length} ${plural(rings.length, "ring")} ${plural(rings.length, "passes", "pass")} through this patient and ${blocked.length} of them clash with other swaps. The best overall plan used other rings instead.`));
  }
  return box;
}

/* ---------------------------------------------------------- step 2: the swap cards */

function renderSwaps() {
  const list = $("swap-list");
  clear(list);
  const plan = currentPlan();
  if (!plan) return;
  $("swap-count").textContent = plan.cycles.length ? `(${plan.cycles.length})` : "";
  if (!plan.cycles.length) {
    list.append(el("li", { class: "muted", text: "No swap was found for this pool. Try a bigger pool, or allow longer swaps." }));
    return;
  }
  plan.cycles.forEach((cycle, index) => {
    const row = el("button", { type: "button", class: "swap-row", "data-index": String(index), onclick: () => selectFromList(cycle[0]) },
      el("span", { class: "swap-n", text: `Swap ${index + 1}` }),
      el("span", { class: "swap-len", text: `${cycle.length}-way` }),
      el("span", { class: "swap-ids", text: `${cycle.map((id) => `#${id}`).join(" → ")} → #${cycle[0]}` }));
    list.append(el("li", {}, row));
  });
  paintSwapSelection();
}

function selectFromList(id) {
  selectPair(id);
  const map = $("map");
  if (map) map.scrollIntoView({ behavior: reducedMotion() ? "auto" : "smooth", block: "center" });
}

function paintSwapSelection() {
  const plan = currentPlan();
  const sel = state.selected;
  const index = plan && sel != null ? plan.owner.get(sel) : null;
  $$(".swap-row").forEach((row) => row.classList.toggle("is-selected", index != null && Number(row.dataset.index) === index));
}

/* --------------------------------------------------------------- step 3: left out */

function matchRate(nodes, owner) {
  const total = nodes.length;
  const matched = nodes.filter((n) => owner.has(n.id)).length;
  return { total, matched, pct: total ? Math.round((100 * matched) / total) : 0 };
}

function factItem(figure, label, note) {
  return el("div", { class: "fact" },
    el("p", { class: "fact-figure" }, strong(figure)),
    el("p", { class: "fact-label", text: label }),
    el("p", { class: "fact-note" }, note));
}

function renderLeftout() {
  if (!state.ready) return;
  const plan = currentPlan();
  const owner = plan.owner;
  const summary = $("leftout-summary");
  clear(summary);
  const left = state.nodes.filter((n) => !owner.has(n.id));
  const total = state.nodes.length;

  const byType = ["O", "A", "B", "AB"]
    .map((t) => ({ t, ...matchRate(state.nodes.filter((n) => n.patient_abo === t), owner) }))
    .filter((r) => r.total >= 5)
    .sort((a, b) => a.pct - b.pct);
  const worst = byType[0];
  const hard = matchRate(state.nodes.filter((n) => n.hard), owner);

  if (worst) {
    summary.append(factItem(`${worst.matched} of ${worst.total}`, `type ${worst.t} patients matched`,
      worst.t === "O" ? [term("bloodtype", "Blood type"), " matters: type O patients can only receive from type O donors."] : [term("bloodtype", "Blood type"), " decides who can give to whom."]));
  }
  if (hard.total) {
    summary.append(factItem(`${hard.matched} of ${hard.total}`, "hard-to-match patients matched",
      ["Their immune system rejects about 9 in 10 donors (", term("sensitised", "sensitised"), ")."]));
  }
  summary.append(factItem(`${left.length} of ${total}`, "pairs left out", "Each donor can be used only once, so patients compete for the same donors."));

  $("unmatched-count").textContent = String(left.length);
  const shown = [...left].sort((a, b) => Number(b.hard) - Number(a.hard) || a.id - b.id);
  const host = $("unmatched-rows");
  clear(host);
  setStatus($("unmatched-status"), left.length ? "" : "Everyone in this pool is covered by a swap.");
  for (const node of shown) {
    host.append(el("button", { type: "button", class: "row-item", "data-id": String(node.id), onclick: () => selectPair(node.id, { reveal: true }) },
      el("span", { class: "row-main" },
        el("strong", { text: `Pair ${node.id}` }),
        el("span", { class: "muted small", text: `patient ${abo(node.patient_abo)}, donor ${abo(node.donor_abo)}` })),
      diffPill(node.pra)));
  }
  paintLeftoutSelection();
}

function paintLeftoutSelection() {
  $$("#unmatched-rows .row-item").forEach((row) => row.classList.toggle("is-selected", Number(row.dataset.id) === state.selected));
}

/* ---------------------------------------------------------------- step 4: what if */

function compareLine(label, before, after) {
  const delta = after - before;
  const sign = delta > 0 ? `+${delta}` : delta < 0 ? `−${Math.abs(delta)}` : "no change";
  return el("div", { class: "compare" },
    el("div", { class: "compare-cell" }, el("span", { class: "compare-num", text: String(before) }), el("span", { class: "compare-label", text: "before" })),
    el("span", { class: "compare-arrow", "aria-hidden": "true", text: "→" }),
    el("div", { class: "compare-cell" }, el("span", { class: "compare-num", text: String(after) }), el("span", { class: "compare-label", text: "after" })),
    el("div", { class: `compare-delta ${delta > 0 ? "up" : delta < 0 ? "down" : ""}` }, strong(sign), el("span", { class: "small muted", text: label })));
}

function pairNames(ids) {
  return ids.map((x) => `pair ${x}`).join(", ");
}

function chainSentence(chain) {
  if (!chain.length) return "";
  if (chain.length === 1) return `The new donor gives to pair ${chain[0]}'s patient.`;
  return `The new donor gives to pair ${chain[0]}'s patient, then pair ${chain[0]}'s donor gives to pair ${chain[1]}'s patient.`;
}

async function runWhatIf(body, out, label) {
  clear(out);
  out.append(el("p", { class: "muted", text: `${label}: trying it on a copy of the pool…` }));
  let env;
  try {
    env = await api("/tools/what_if", postJson(body));
  } catch (err) {
    clear(out);
    out.append(el("p", { class: "status-bad", text: err.message }));
    return null;
  }
  clear(out);
  return env;
}

async function runAltruist() {
  const out = $("whatif-altruist-out");
  const button = $("whatif-altruist");
  button.disabled = true;
  const env = await runWhatIf({ kind: "add_altruistic_donor", donor_abo: "O", max_len: state.maxLen }, out, "Adding a donor");
  button.disabled = false;
  if (!env) return;
  const r = env.result || {};
  const before = r.before || {};
  const after = r.after || {};
  out.append(compareLine("patients matched", before.matched, after.matched));
  const chains = after.chains || [];
  if (chains.length) {
    out.append(el("p", {}, strong("How: "), "the new donor starts a chain. ", chains.map(chainSentence).join(" ")));
  } else {
    out.append(el("p", { text: "The new donor could not start a chain here: no patient in this pool can receive from them." }));
  }
  out.append(el("p", { class: "fine-print", text: "The whole plan is recalculated, so some individual swaps can look different even where the same patients are matched." }));
  if (r.flow_bound) out.append(el("p", { class: "fine-print" }, "The ", term("upperbound", "upper bound"), ` (it also allows chains, so it is not a plan) went from ${r.flow_bound.before} to ${r.flow_bound.after}.`));
  out.append(el("p", { class: "fine-print" }, outcomeBadge(env.outcome), " Your real pool was not changed."));
  if (r.note) out.append(el("p", { class: "fine-print", text: r.note }));
}

async function runRemove(id) {
  const out = $("whatif-remove-out");
  const node = state.nodesById[id];
  if (!node) {
    clear(out);
    out.append(el("p", { class: "muted", text: "Choose a pair first, or press Surprise me." }));
    return;
  }
  const plan = currentPlan();
  const index = plan.owner.get(id);
  const ring = index == null ? null : plan.cycles[index];
  const env = await runWhatIf({ kind: "remove_pair", pair_id: id, max_len: state.maxLen }, out, `Removing pair ${id}`);
  if (!env) return;
  const r = env.result || {};
  if (env.outcome === "unknown_entity") {
    out.append(el("p", { text: r.message || "That pair is not in the pool." }), outcomeBadge(env.outcome));
    return;
  }
  const before = r.before || {};
  const after = r.after || {};
  out.append(compareLine("patients matched", before.matched, after.matched));
  if (ring) {
    const others = ring.filter((x) => x !== id);
    const same = after.matched === before.matched;
    out.append(el("p", {}, `Pair ${id} was in a ${ring.length}-way swap with ${pairNames(others)}. When it leaves, that swap can't happen, so Swap looks for new swaps for ${others.length === 1 ? "the other pair" : "the others"}${same ? ", and finds enough to keep the total the same" : ""}.`));
  } else {
    out.append(el("p", { text: `Pair ${id} wasn't in a swap, so removing it barely matters here.` }));
  }
  out.append(el("p", { class: "fine-print" }, outcomeBadge(env.outcome), " Your real pool was not changed."));
}

function populateRemoveChoices() {
  const select = $("remove-pair");
  const current = select.value;
  clear(select);
  select.append(el("option", { value: "", text: "Choose a pair…" }));
  const plan = currentPlan();
  if (!plan) return;
  const inSwap = el("optgroup", { label: "In a swap" });
  const left = el("optgroup", { label: "Left out" });
  for (const node of [...state.nodes].sort((a, b) => a.id - b.id)) {
    const option = el("option", { value: String(node.id), text: `Pair ${node.id} · patient ${abo(node.patient_abo)}, donor ${abo(node.donor_abo)}` });
    (plan.owner.has(node.id) ? inSwap : left).append(option);
  }
  select.append(inSwap, left);
  if (current && state.nodesById[current]) select.value = current;
}

async function setFairness(on) {
  state.priorityOn = on;
  $("fairness").checked = on;
  setStatus($("result-status"), "Re-planning the swaps…");
  try {
    await Promise.all([fetchPlan(2), fetchPlan(3)]);
    if (state.priorityOn !== on) return;
    renderAll();
    await compareFairness();
  } catch (err) {
    showError(err.message, () => setFairness(on));
  }
}

function hardMatched(plan) {
  const hard = hardNodes();
  return { matched: hard.filter((n) => plan.owner.has(n.id)).length, total: hard.length };
}

async function compareFairness() {
  if (!state.ready) return;
  const host = $("fairness-live");
  try {
    await Promise.all([fetchPlan(state.maxLen, false), fetchPlan(state.maxLen, true)]);
  } catch (err) {
    clear(host);
    host.append(el("p", { class: "status-bad", text: err.message }));
    return;
  }
  renderFairnessLive();
}

function renderFairnessLive() {
  const host = $("fairness-live");
  clear(host);
  const off = state.plans[planKey(state.maxLen, false)];
  const on = state.plans[planKey(state.maxLen, true)];
  if (!off || !on) {
    host.append(el("p", { class: "muted small", text: "Working out the comparison…" }));
    return;
  }
  const a = hardMatched(off);
  const b = hardMatched(on);
  host.append(el("div", { class: "fair-grid" },
    fairCell("Priority off", a, off.matched, !state.priorityOn),
    fairCell("Priority on", b, on.matched, state.priorityOn)));
  host.append(el("p", { class: "fine-print", text: "Computed live on this pool. The patients counted are the ones who are hard to match." }));
}

function fairCell(title, hard, total, active) {
  return el("div", { class: `fair-cell${active ? " is-active" : ""}` },
    el("p", { class: "fair-title", text: title + (active ? " (showing)" : "") }),
    el("p", { class: "fair-big" }, strong(`${hard.matched}`), el("span", { class: "muted", text: ` of ${hard.total} hard-to-match patients matched` })),
    el("p", { class: "small muted", text: `${total} patients matched in total` }));
}

function renderFairnessPanel() {
  const host = $("fairness-panel");
  clear(host);
  const rows = [
    ["off (weight 1)", "76.5% ± 7.7", "115.7 ± 7.3"],
    ["on (weight 3)", "96.8% ± 3.0", "114.7 ± 7.1"],
  ];
  const table = el("table", { class: "mini" });
  table.append(
    el("thead", {}, el("tr", {}, el("th", { text: "priority" }), el("th", { text: "hard-to-match patients matched" }), el("th", { text: "all patients matched" }))),
    el("tbody", {}, rows.map(([a, b, c]) => el("tr", {}, el("td", { text: a }), el("td", { class: "num", text: b }), el("td", { class: "num", text: c })))));
  host.append(table);
  host.append(el("p", { class: "fine-print", text: "Run with make verify-fairness. Priority lifted the hard-to-match share by 20.3 ± 7.1 points (better on 20 of 20 pools) and cost 1.0 matched patient on average. Quoted from the README." }));
}

/* ------------------------------------------------------- step 5: behind the scenes */

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
  table.append(el("thead", {}, el("tr", {}, ["tool", "arguments", "ms", "rows", "outcome"].map((h) => el("th", { text: h })))));
  const tbody = el("tbody");
  for (const t of rows) {
    tbody.append(el("tr", {},
      el("td", { class: "mono", text: t.tool }),
      el("td", { class: "mono args", title: JSON.stringify(t.args || {}), text: fmtArgs(t.args) }),
      el("td", { class: "num", text: String(t.ms) }),
      el("td", { class: "num", text: String(t.rows) }),
      el("td", {}, outcomeBadge(t.outcome))));
  }
  table.append(tbody);
  host.append(table);
}

function fmtArgs(args) {
  const text = JSON.stringify(args || {});
  return text.length > 54 ? `${text.slice(0, 51)}…` : text;
}

function buildOutcomeKey() {
  const host = $("outcome-key");
  clear(host);
  for (const info of Object.values(OUTCOMES)) {
    host.append(el("li", {}, el("span", { class: `badge badge-${info.tone}`, text: info.label }), " ", el("span", { class: "muted small", text: info.meaning })));
  }
}

async function loadProof() {
  if (!state.proofStale) return;
  const host = $("proof");
  setStatus($("proof-status"), "Running the independent check…");
  let proof;
  try {
    proof = await api("/proof");
  } catch (err) {
    setStatus($("proof-status"), err.message, "bad");
    return;
  }
  state.proofStale = false;
  renderProof(proof, host);
}

function renderProof(proof, host) {
  clear(host);
  if (!proof || !proof.loaded) {
    setStatus($("proof-status"), (proof && proof.message) || "No pool loaded yet.", "muted");
    return;
  }
  setStatus($("proof-status"), "");
  const checks = proof.checks || [];
  const ok = proof.all_checks_pass;
  host.append(el("div", { class: `proof-summary ${ok ? "is-ok" : "is-bad"}` },
    el("span", { class: ok ? "tick" : "cross", text: ok ? "✓" : "✗" }),
    el("span", {}, ok ? `The graph database and the independent check agree on all ${checks.length} checks.` : "The graph database and the independent check disagree somewhere. Details below.")));

  const ord = proof.ordering || {};
  host.append(el("p", { class: "fine-print" }, "Sanity rule: ", el("span", { class: "mono", text: ord.invariant || "" }), " → ", el("span", { class: "mono", text: `[${(ord.values || []).join(", ")}]` }), " ", el("span", { class: ord.holds ? "tick" : "cross", text: ord.holds ? "✓ holds" : "✗ violated" })));

  const details = el("details", { class: "mini-details" }, el("summary", { text: "Show the numbers" }));
  const table = el("table", { class: "grid" });
  table.append(el("thead", {}, el("tr", {}, ["check", "independent", "FalkorDB", "verdict"].map((h) => el("th", { text: h })))));
  const tbody = el("tbody");
  for (const check of checks) {
    tbody.append(el("tr", {},
      el("td", { text: check.name }),
      el("td", { class: "num", text: String(check.crosscheck) }),
      el("td", { class: "num", text: String(check.falkordb) }),
      el("td", {}, el("span", { class: check.agrees ? "tick" : "cross", text: check.agrees ? "✓ agrees" : "✗ differs" }))));
  }
  table.append(tbody);
  details.append(table);

  const f = proof.falkordb || {};
  const x = proof.crosscheck || {};
  const numbers = el("table", { class: "grid" });
  numbers.append(el("thead", {}, el("tr", {}, ["number", "FalkorDB", "independent"].map((h) => el("th", { text: h })))));
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
  numbers.append(nbody);
  details.append(numbers);
  details.append(el("p", { class: "fine-print", text: "The independent count is plain Python over the same pool; it never touches the graph." }));
  host.append(details);
}

let cypherLoaded = false;

async function loadCypher() {
  if (cypherLoaded) return;
  let body;
  try {
    body = await api("/cypher");
  } catch (err) {
    setStatus($("cypher-status"), err.message, "bad");
    return;
  }
  cypherLoaded = true;
  setStatus($("cypher-status"), "");
  const host = $("cypher");
  clear(host);
  const queries = body.queries || {};
  const toolNames = Object.keys(queries).filter((k) => !k.startsWith("loader_")).sort();
  const loaderNames = Object.keys(queries).filter((k) => k.startsWith("loader_")).sort();
  host.append(cypherGroup("Queries the tools run", toolNames, queries));
  const loaders = el("details", { class: "mini-details" }, el("summary", { text: `Show the ${loaderNames.length} queries that load a pool into the graph` }));
  loaders.append(cypherGroup("Loading a pool into the graph", loaderNames, queries));
  host.append(loaders);
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
  for (const q of questions) host.append(el("button", { type: "button", class: "chip", text: q, onclick: () => askAgent(q) }));
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
    body = await api("/ask", postJson({ question: text }));
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
  $("ask-mode").textContent = body.mode === "llm" ? "answering with a language model that may only choose from fixed tools" : "tool mode: a keyword router, no AI model configured";
  out.append(el("p", { class: "fine-print", text: `Answer mode: ${mode}.` }));
  out.append(el("p", { class: "lead-line", text: body.answer || "(no answer)" }));
  const rejected = body.rejected_ids || [];
  if (rejected.length) out.append(el("p", { class: "status-bad", text: `Rejected ids (named in the answer but in no tool result): ${rejected.join(", ")}` }));
  const calls = body.tool_calls || [];
  if (calls.length) {
    const table = el("table", { class: "grid" });
    table.append(el("thead", {}, el("tr", {}, ["tool", "arguments", "ms", "rows", "outcome"].map((h) => el("th", { text: h })))));
    const tbody = el("tbody");
    for (const call of calls) {
      tbody.append(el("tr", {},
        el("td", { class: "mono", text: call.tool }),
        el("td", { class: "mono args", text: fmtArgs(call.args) }),
        el("td", { class: "num", text: String(call.ms) }),
        el("td", { class: "num", text: String(call.rows) }),
        el("td", {}, outcomeBadge(call.outcome))));
    }
    table.append(tbody);
    out.append(el("div", { class: "table-wrap" }, table));
  }
}

async function lookUp(id) {
  const out = $("idcheck-out");
  clear(out);
  out.append(el("p", { class: "muted", text: `Looking up pair ${id}…` }));
  let env;
  try {
    env = await api(`/tools/explain/${encodeURIComponent(id)}`);
  } catch (err) {
    clear(out);
    out.append(el("p", { class: "status-bad", text: err.message }));
    return;
  }
  clear(out);
  const result = env.result || {};
  out.append(el("p", {}, outcomeBadge(env.outcome)));
  if (env.outcome === "unknown_entity") {
    out.append(el("p", { text: result.message || `There is no pair ${id} in the pool.` }));
    out.append(el("p", { class: "fine-print", text: "Swap says \"no such pair\" instead of inventing an answer, and that is different from \"this patient was not matched\"." }));
  } else {
    const pair = result.pair || {};
    out.append(el("p", {}, `Pair ${pair.id} exists: patient ${abo(pair.patient_abo)}, donor ${abo(pair.donor_abo)}. ${(result.incoming_candidates || []).length} ${plural((result.incoming_candidates || []).length, "donor")} could give to them.`));
  }
  loadTrace();
}

/* ------------------------------------------------------------------- pool actions */

async function generatePool() {
  const button = $("generate");
  button.disabled = true;
  state.size = Number($("size").value);
  state.seed = Number($("seed").value);
  state.selected = null;
  try {
    await loadPool({ create: true });
  } finally {
    button.disabled = false;
  }
}

async function setLen(len) {
  state.maxLen = Number(len);
  if (!state.ready) { renderLegend(); return; }
  renderLegend();
  paintMap();
  renderSwaps();
  renderLeftout();
  populateRemoveChoices();
  renderDetails();
  renderFairnessLive();
}

/* ----------------------------------------------------------------- guided tour */

const TOUR = [
  {
    step: "learn", target: "#story", title: "The idea",
    text: () => "Patients who need a kidney often have a willing donor who doesn't match them. Step through these five pictures to see how swapping donors fixes that.",
  },
  {
    step: "results", target: ".numbers", title: "The headline",
    text: () => {
      const p2 = planFor(2); const p3 = planFor(3);
      return p2 && p3 ? `If pairs can only swap in twos, ${p2.matched} patients get a kidney. Allow rings of three as well and it is ${p3.matched}: ${p3.matched - p2.matched} more people.` : "Waiting for the numbers…";
    },
    run: async () => { await setLen(2); },
  },
  {
    step: "results", target: "#map", title: "Every dot is a pair",
    text: () => "This is the same pool with 3-way swaps allowed. Green dots are patients who only get a kidney because rings of three are allowed. Hollow grey dots are left out.",
    run: async () => { await setLen(3); },
  },
  {
    step: "results", target: "#detail-results", title: "Tap a dot to see why",
    text: () => "Tapping a dot explains what happened to that pair, and draws its swap as a ring: each arrow is \"this donor gives to that patient\".",
    run: async () => {
      const id = state.nodes.map((n) => n.id).find((x) => dotKind(x) === "gain") ?? [...currentPlan().owner.keys()][0];
      if (id != null) selectPair(id);
    },
  },
  {
    step: "leftout", target: "#detail-leftout", title: "Who is left out, and why",
    text: () => "Not everyone can be matched. Swap shows which donors could have helped, why the others couldn't, and what stood in the way.",
    run: async () => {
      const left = state.nodes.filter((n) => !currentPlan().owner.has(n.id));
      const pick = left.find((n) => n.hard) || left[0];
      if (pick) selectPair(pick.id);
    },
  },
  {
    step: "whatif", target: "#whatif-altruist-card", title: "What if…?",
    text: () => "Try a change on a copy of the pool. Here, one extra donor with no patient of their own joins, and Swap shows how many more people can be helped.",
    run: async () => { await runAltruist(); },
  },
  {
    step: "hood", target: "#proof", title: "Can you trust it?",
    text: () => "Every number is recounted by separate code that never touches the database. If they ever disagreed, you'd see a cross here, not a tick.",
  },
  {
    step: "hood", target: "#hood-title", title: "Your turn",
    text: () => "That's the tour. Go back to the results, change the pool size, tap dots, and ask your own what-if questions.",
  },
];

const tour = { index: 0, active: false, seq: 0 };

function clearTourFocus() {
  $$(".tour-focus").forEach((node) => node.classList.remove("tour-focus"));
}

async function showTourStep(index) {
  tour.index = Math.max(0, Math.min(TOUR.length - 1, index));
  const seq = ++tour.seq;
  const step = TOUR[tour.index];
  $("tour-progress").textContent = `Step ${tour.index + 1} of ${TOUR.length}`;
  $("tour-title").textContent = step.title;
  $("tour-back").disabled = tour.index === 0;
  $("tour-next").textContent = tour.index === TOUR.length - 1 ? "Finish" : "Next";
  clearTourFocus();
  goStep(step.step, { focus: false, keepScroll: step.step === state.step });
  $("tour-caption").textContent = step.run ? `${step.text()}` : step.text();
  if (step.run) { try { await step.run(); } catch (err) { /* a tour step must never break the page */ } }
  if (seq !== tour.seq) return;
  $("tour-caption").textContent = step.text();
  await wait(120);
  const target = step.target ? document.querySelector(step.target) : null;
  if (target) {
    target.classList.add("tour-focus");
    ensureVisible(target);
  }
}

async function startTour() {
  tour.active = true;
  document.body.classList.add("has-tour");
  $("tour").hidden = false;
  await showTourStep(0);
}

function endTour() {
  tour.active = false;
  tour.seq += 1;
  document.body.classList.remove("has-tour");
  $("tour").hidden = true;
  clearTourFocus();
}

/* ------------------------------------------------------------------------- wiring */

function wire() {
  $$(".js-tour").forEach((button) => button.addEventListener("click", startTour));
  $("glossary-open").addEventListener("click", () => openGlossary());
  $("drawer-close").addEventListener("click", closeGlossary);
  $("drawer-backdrop").addEventListener("click", closeGlossary);
  $("termtip-more").addEventListener("click", () => { const key = tipFor && tipFor.dataset.term; hideTip(); openGlossary(key); });

  document.addEventListener("click", (event) => {
    const button = event.target.closest && event.target.closest(".term");
    if (button) {
      event.preventDefault();
      if (tipFor === button) hideTip();
      else showTip(button);
      return;
    }
    if (!event.target.closest("#termtip")) hideTip();
  });

  $("pager-back").addEventListener("click", () => { if ($("pager-back").dataset.go) location.hash = `#/${$("pager-back").dataset.go}`; });
  $("pager-next").addEventListener("click", () => { if ($("pager-next").dataset.go) location.hash = `#/${$("pager-next").dataset.go}`; });
  window.addEventListener("hashchange", () => {
    if (location.hash === "#main") return;
    showStep(stepFromHash());
  });
  $$(".skip-link").forEach((link) => link.addEventListener("click", (event) => {
    event.preventDefault();
    $("main").focus();
  }));

  $("story-next").addEventListener("click", storyNext);
  $("story-prev").addEventListener("click", storyPrev);

  $("app-error-retry").addEventListener("click", () => { const again = retryAction; clearError(); if (again) again(); });

  $("size").addEventListener("input", () => {
    $("size-value").textContent = $("size").value;
    state.size = Number($("size").value);
  });
  $("seed-random").addEventListener("click", () => { $("seed").value = String(Math.floor(Math.random() * 9000) + 1000); });
  $("generate").addEventListener("click", generatePool);
  $$(".seg").forEach((button) => button.addEventListener("click", () => setLen(button.dataset.len)));
  $("hard-mark").addEventListener("change", () => { state.hardMark = $("hard-mark").checked; paintMap(); renderLegend(); });

  const map = $("map");
  map.addEventListener("click", onMapClick);
  map.addEventListener("keydown", onMapKeydown);
  map.addEventListener("focusin", onMapFocus);

  $("whatif-altruist").addEventListener("click", runAltruist);
  $("whatif-remove").addEventListener("click", () => {
    const value = $("remove-pair").value;
    runRemove(value === "" ? null : Number(value));
  });
  $("whatif-random").addEventListener("click", () => {
    const plan = currentPlan();
    if (!plan || !plan.owner.size) return;
    const ids = [...plan.owner.keys()];
    const id = ids[Math.floor(Math.random() * ids.length)];
    $("remove-pair").value = String(id);
    runRemove(id);
  });
  $("fairness").addEventListener("change", () => setFairness($("fairness").checked));

  $("trace-refresh").addEventListener("click", loadTrace);
  $("ask-agent").addEventListener("click", () => askAgent());
  $("q").addEventListener("keydown", (event) => { if (event.key === "Enter") askAgent(); });
  $("id-ask").addEventListener("click", () => {
    const raw = $("id-input").value.trim();
    if (raw !== "") lookUp(Number(raw));
  });
  $("id-input").addEventListener("keydown", (event) => { if (event.key === "Enter") $("id-ask").click(); });
  $("id-missing").addEventListener("click", () => { $("id-input").value = "99999"; lookUp(99999); });

  $("tour-next").addEventListener("click", () => {
    if (tour.index >= TOUR.length - 1) { endTour(); return; }
    showTourStep(tour.index + 1);
  });
  $("tour-back").addEventListener("click", () => showTourStep(tour.index - 1));
  $("tour-skip").addEventListener("click", endTour);

  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    if (!$("termtip").hidden) hideTip();
    else if (!$("drawer").hidden) closeGlossary();
    else if (tour.active) endTour();
  });
}

/* --------------------------------------------------------------------------- boot */

async function boot() {
  wire();
  buildStory();
  buildOutcomeKey();
  buildAskChips();
  renderFairnessPanel();
  renderLegend();
  showStep(stepFromHash(), { focus: false, keepScroll: true });
  await loadPool();
}

boot();
