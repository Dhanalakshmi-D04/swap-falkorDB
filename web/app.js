"use strict";

const PALETTE = [
  "#2b6cb0", "#dd6b20", "#2f855a", "#c53030", "#805ad5",
  "#b7791f", "#d53f8c", "#0987a0", "#6b8e23", "#4a5568",
];

const state = {
  maxLen: 3,
  priorityOn: false,
  priorityWeight: 2,
  nodesById: {},
  covered: new Set(),
  cy: null,
};

const $ = (id) => document.getElementById(id);

async function api(path, options) {
  const res = await fetch(path, options);
  if (!res.ok) {
    let detail = "";
    try { detail = JSON.stringify(await res.json()); } catch (e) { detail = res.statusText; }
    throw new Error(`${res.status} ${detail}`);
  }
  return res.json();
}

function badge(outcome) {
  const known = ["answered_with_path", "from_memory", "not_in_graph", "unknown_entity"];
  const cls = known.includes(outcome) ? outcome : "unknown";
  return `<span class="badge ${cls}">${outcome}</span>`;
}

function fmtArgs(args) {
  const text = JSON.stringify(args || {});
  return text.length > 60 ? text.slice(0, 57) + "..." : text;
}

/* ---------------------------------------------------------------- pool + best set */

async function generatePool() {
  const n = Number($("size").value);
  const seed = Number($("seed").value);
  $("generate").disabled = true;
  try {
    await api("/pool", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ n, seed }),
    });
    await loadBestSetAndGraph();
    await loadProof();
  } catch (err) {
    $("metric-outcome").textContent = "error";
    $("explain").innerHTML = `<p class="bad">Could not load pool: ${err.message}. Is FalkorDB up (make up)?</p>`;
  } finally {
    $("generate").disabled = false;
    await loadTrace();
  }
}

function priorityParam() {
  return state.priorityOn ? `&priority=${state.priorityWeight}` : "";
}

async function loadBestSetAndGraph() {
  const [graph, best] = await Promise.all([
    api(`/graph?max_len=${state.maxLen}`),
    api(`/tools/best_set?max_len=${state.maxLen}${priorityParam()}`),
  ]);

  state.nodesById = {};
  for (const node of graph.nodes) state.nodesById[node.id] = node;

  const result = best.result;
  const chosen = Array.isArray(result.chosen) ? result.chosen : [];
  state.covered = new Set(chosen.flat());

  $("metric-matched").textContent = result.matched ?? "0";
  $("metric-cycles").textContent = result.counts ? result.counts.cycles : "0";
  $("metric-maxlen").textContent = state.maxLen;
  $("metric-outcome").textContent = best.outcome;

  drawGraph(chosen);
  renderUnmatched();
  $("graph-note").textContent =
    `${chosen.length} cycle(s) chosen by best_set(max_len=${state.maxLen}` +
    `${state.priorityOn ? `, priority=${state.priorityWeight}` : ""}); one colour per cycle`;
}

function drawGraph(chosen) {
  const elements = [];
  const seenNodes = new Set();
  chosen.forEach((cycle, index) => {
    const color = PALETTE[index % PALETTE.length];
    for (const id of cycle) {
      if (seenNodes.has(id)) continue;
      seenNodes.add(id);
      const node = state.nodesById[id] || {};
      elements.push({
        data: {
          id: String(id),
          label: `${id}\n${node.donor_abo || "?"}\u2192${node.patient_abo || "?"}`,
          color,
        },
      });
    }
    const len = cycle.length;
    for (let i = 0; i < len; i += 1) {
      const source = cycle[i];
      const target = cycle[(i + 1) % len];
      elements.push({ data: { id: `e${index}_${i}`, source: String(source), target: String(target), color } });
    }
  });

  const style = [
    {
      selector: "node",
      style: {
        "background-color": "data(color)",
        label: "data(label)",
        color: "#ffffff",
        "font-size": 8,
        "text-valign": "center",
        "text-halign": "center",
        "text-wrap": "wrap",
        "text-max-width": 44,
        width: 30,
        height: 30,
      },
    },
    {
      selector: "edge",
      style: {
        width: 2,
        "line-color": "data(color)",
        "target-arrow-shape": "triangle",
        "target-arrow-color": "data(color)",
        "arrow-scale": 0.9,
        "curve-style": "bezier",
      },
    },
  ];

  const container = $("cy");
  if (state.cy) { state.cy.destroy(); state.cy = null; }
  container.innerHTML = "";
  if (elements.length === 0) {
    container.innerHTML = '<p class="muted" style="padding:1rem">No swap cycle found in this pool.</p>';
    return;
  }
  state.cy = cytoscape({
    container,
    elements,
    style,
    layout: { name: "circle", padding: 24, animate: false },
    wheelSensitivity: 0.2,
  });
}

function renderUnmatched() {
  const unmatched = Object.keys(state.nodesById)
    .map(Number)
    .filter((id) => !state.covered.has(id))
    .sort((a, b) => a - b);

  $("unmatched-count").textContent = `(${unmatched.length})`;
  const list = $("unmatched-list");
  list.innerHTML = "";
  for (const id of unmatched) {
    const node = state.nodesById[id] || {};
    const li = document.createElement("li");
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = `${id} (${node.donor_abo || "?"}\u2192${node.patient_abo || "?"})`;
    button.title = `explain pair ${id}`;
    button.addEventListener("click", () => explain(id));
    li.appendChild(button);
    list.appendChild(li);
  }
}

/* ---------------------------------------------------------------- explain */

async function explain(id) {
  const panel = $("explain");
  panel.innerHTML = `<p class="muted">asking the graph about pair ${id}...</p>`;
  let env;
  try {
    env = await api(`/tools/explain/${encodeURIComponent(id)}`);
  } catch (err) {
    panel.innerHTML = `<p class="bad">Request failed: ${err.message}</p>`;
    await loadTrace();
    return;
  }
  renderExplain(env);
  await loadTrace();
}

function renderExplain(env) {
  const panel = $("explain");
  const result = env.result || {};
  const lines = [`<div class="rowline"><strong>outcome:</strong> ${badge(env.outcome)}</div>`];

  if (env.outcome === "unknown_entity") {
    lines.push(`<div class="rowline">${result.message || "unknown id"}</div>`);
    panel.innerHTML = lines.join("");
    return;
  }

  const pair = result.pair || {};
  lines.push(
    `<div class="rowline"><strong>pair ${pair.id}</strong> · ` +
    `patient ${pair.patient_abo} · donor ${pair.donor_abo} · PRA ${pair.pra}` +
    `${pair.hard ? " · <strong>highly sensitised</strong>" : ""}</div>`
  );

  const incoming = result.incoming_candidates || [];
  const outgoing = result.outgoing_targets || [];
  lines.push(`<div class="rowline"><strong>who can give to this patient:</strong> ${incoming.length ? incoming.join(", ") : "none"}</div>`);
  lines.push(`<div class="rowline"><strong>who this donor can give to:</strong> ${outgoing.length ? outgoing.join(", ") : "none"}</div>`);

  const reason = result.reasoning || {};
  lines.push(
    `<div class="rowline"><strong>reasoning:</strong> ` +
    `${reason.abo_incompatible_pairs ?? "?"} ABO-incompatible pairs · ` +
    `${reason.abo_compatible_but_crossmatch_failed ?? "?"} ABO-compatible but crossmatch failed</div>`
  );
  if (reason.note) lines.push(`<div class="rowline muted small">${reason.note}</div>`);

  const cycles = result.cycles_through || [];
  lines.push(`<div class="rowline"><strong>cycles through this pair:</strong> ${cycles.length ? cycles.map((c) => "[" + c.join(" \u2192 ") + "]").join(" ") : "none"}</div>`);

  if (env.outcome === "not_in_graph" || incoming.length === 0) {
    const note = result.note || "the graph does not know of any donor that can give to this patient";
    lines.push(
      `<div class="callout"><strong>the graph does not know.</strong> An empty traversal is not a ` +
      `claim of safety. <span class="muted small">${note}</span></div>`
    );
  }

  panel.innerHTML = lines.join("");
}

/* ---------------------------------------------------------------- what-if */

async function whatIf() {
  const panel = $("whatif-out");
  panel.innerHTML = `<p class="muted">re-running the tools on a modified copy of the graph...</p>`;
  let env;
  try {
    env = await api("/tools/what_if", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kind: "add_altruistic_donor", donor_abo: "O", max_len: state.maxLen }),
    });
  } catch (err) {
    panel.innerHTML = `<p class="bad">Request failed: ${err.message}</p>`;
    await loadTrace();
    return;
  }
  renderWhatIf(env);
  await loadTrace();
}

function renderWhatIf(env) {
  const panel = $("whatif-out");
  const r = env.result || {};
  const before = r.before || {};
  const after = r.after || {};
  const flow = r.flow_bound || {};
  const changed = r.changed || {};
  const lines = [];

  lines.push(`<div class="rowline"><strong>add one altruistic ${r.donor_abo || "?"} donor</strong> ${badge(env.outcome)}</div>`);
  lines.push(`<div class="rowline">patients matched: <strong>${before.matched}</strong> \u2192 <strong>${after.matched}</strong> (gain ${r.gain >= 0 ? "+" : ""}${r.gain})</div>`);
  lines.push(`<div class="rowline">cycles: ${before.cycles} \u2192 ${after.cycles}${after.chains && after.chains.length ? `, chains: ${JSON.stringify(after.chains)}` : ""}</div>`);
  lines.push(`<div class="rowline">flow bound: ${flow.before} \u2192 ${flow.after} <span class="muted small">(upper bound, also allows chains; not the cycle optimum)</span></div>`);

  const added = (changed.added || []).map((c) => "[" + c.join(" \u2192 ") + "]").join(" ");
  const removed = (changed.removed || []).map((c) => "[" + c.join(" \u2192 ") + "]").join(" ");
  if (added) lines.push(`<div class="rowline">added: ${added}</div>`);
  if (removed) lines.push(`<div class="rowline">removed: ${removed}</div>`);
  if (r.note) lines.push(`<div class="rowline muted small">${r.note}</div>`);

  panel.innerHTML = lines.join("");
}

/* ---------------------------------------------------------------- proof */

async function loadProof() {
  let proof;
  try {
    proof = await api("/proof");
  } catch (err) {
    $("proof").innerHTML = `<p class="bad">Could not load proof: ${err.message}</p>`;
    return;
  }
  if (!proof.loaded) {
    $("proof").innerHTML = `<p class="muted">${proof.message || "no active pool; generate one first"}</p>`;
    return;
  }

  const f = proof.falkordb || {};
  const x = proof.crosscheck || {};
  const ordering = proof.ordering || {};

  $("cmp-2").textContent = f.best_set_2 ?? "–";
  $("cmp-3").textContent = f.best_set_3 ?? "–";
  $("cmp-cover").textContent = f.unbounded_cycle_cover ?? "–";
  $("cmp-bound").textContent = f.flow_bound ?? "–";
  $("ordering-note").textContent =
    `${ordering.invariant || "ordering invariant"} — ` +
    `[${(ordering.values || []).join(", ")}] ` +
    `<span class="${ordering.holds ? "ok" : "bad"}">${ordering.holds ? "holds" : "VIOLATED"}</span>`;

  const checkRows = (proof.checks || [])
    .map((c) => {
      const mark = c.agrees ? '<span class="ok">agree</span>' : '<span class="bad">differ</span>';
      return `<tr><td>${c.name}</td><td class="num">${c.crosscheck}</td><td class="num">${c.falkordb}</td><td>${mark}</td></tr>`;
    })
    .join("");

  const cycles = f.cycles || {};
  const xcycles = x.cycles || {};
  const rows = [
    ["2-cycle count", cycles["2"], xcycles["2"]],
    ["3-cycle count", cycles["3"], xcycles["3"]],
    ["best_set max_len=2 (patients)", f.best_set_2, "–"],
    ["best_set max_len=3 (patients)", f.best_set_3, "–"],
    ["unbounded cycle cover", f.unbounded_cycle_cover, "–"],
    ["max-flow bound (upper bound, allows chains)", f.flow_bound, x.matching_bound],
  ]
    .map((r) => `<tr><td>${r[0]}</td><td class="num">${r[1]}</td><td class="num">${r[2]}</td></tr>`)
    .join("");

  $("proof").innerHTML =
    `<p class="small muted">${x.label || "independent check"}</p>` +
    `<table><thead><tr><th>check</th><th>independent</th><th>FalkorDB</th><th>verdict</th></tr></thead>` +
    `<tbody>${checkRows}</tbody></table>` +
    `<table style="margin-top:0.5rem"><thead><tr><th>number</th><th>FalkorDB</th><th>independent</th></tr></thead>` +
    `<tbody>${rows}</tbody></table>` +
    `<p class="small">all checks pass: <span class="${proof.all_checks_pass ? "ok" : "bad"}">${proof.all_checks_pass}</span></p>`;
}

/* ---------------------------------------------------------------- trace */

async function loadTrace() {
  let body;
  try {
    body = await api("/trace");
  } catch (err) {
    $("trace").innerHTML = `<p class="bad">Could not load trace: ${err.message}</p>`;
    return;
  }
  const rows = (body.traces || []).slice(-12).reverse();
  if (rows.length === 0) {
    $("trace").innerHTML = `<p class="muted">no tool calls recorded yet</p>`;
    return;
  }
  const html = rows
    .map((t) => {
      const outcomeCls = ["answered_with_path", "from_memory", "not_in_graph", "unknown_entity"].includes(t.outcome)
        ? t.outcome
        : "unknown";
      return `<tr><td>${t.tool}</td><td class="args" title="${fmtArgs(t.args)}">${fmtArgs(t.args)}</td>` +
        `<td>${t.ms} ms</td><td>${t.rows}</td><td><span class="badge ${outcomeCls}">${t.outcome}</span></td></tr>`;
    })
    .join("");
  $("trace").innerHTML =
    `<table><thead><tr><th>tool</th><th>args</th><th>ms</th><th>rows</th><th>outcome</th></tr></thead><tbody>${html}</tbody></table>`;
}

/* ---------------------------------------------------------------- ask the agent */

async function askAgent() {
  const question = $("q").value.trim();
  if (question === "") return;
  const panel = $("ask-out");
  panel.innerHTML = `<p class="muted">asking the agent...</p>`;
  let body;
  try {
    body = await api("/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question }),
    });
  } catch (err) {
    panel.innerHTML = `<p class="bad">Request failed: ${err.message}</p>`;
    await loadTrace();
    return;
  }
  renderAsk(body);
  await loadTrace();
}

function renderAsk(body) {
  const lines = [];
  const rejected = body.rejected_ids || [];
  lines.push(
    `<div class="rowline"><strong>answer</strong> <span class="badge unknown">${body.mode}</span>` +
    `${rejected.length ? ` <span class="bad">rejected ids: ${rejected.join(", ")}</span>` : ""}</div>`
  );
  lines.push(`<div class="rowline">${body.answer || "(no answer)"}</div>`);
  const calls = body.tool_calls || [];
  if (calls.length) {
    const rows = calls
      .map((c) => `<tr><td>${c.tool}</td><td class="args">${fmtArgs(c.args)}</td>` +
        `<td>${c.ms} ms</td><td>${c.rows}</td><td><span class="badge unknown">${c.outcome}</span></td></tr>`)
      .join("");
    lines.push(
      `<table style="margin-top:0.4rem"><thead><tr><th>tool</th><th>args</th><th>ms</th>` +
      `<th>rows</th><th>outcome</th></tr></thead><tbody>${rows}</tbody></table>`
    );
  }
  $("ask-out").innerHTML = lines.join("");
}

/* ---------------------------------------------------------------- wiring */

function setLen(len) {
  state.maxLen = len;
  document.querySelectorAll(".seg").forEach((button) => {
    button.classList.toggle("active", Number(button.dataset.len) === len);
  });
  loadBestSetAndGraph().catch((err) => { $("metric-outcome").textContent = "error"; console.error(err); });
}

function wire() {
  $("size").addEventListener("input", () => { $("size-value").textContent = $("size").value; });
  $("weight").addEventListener("input", () => { $("weight-value").textContent = $("weight").value; });
  $("generate").addEventListener("click", generatePool);
  document.querySelectorAll(".seg").forEach((button) => {
    button.addEventListener("click", () => setLen(Number(button.dataset.len)));
  });
  $("priority").addEventListener("change", () => {
    state.priorityOn = $("priority").checked;
    loadBestSetAndGraph().catch((err) => console.error(err));
  });
  $("weight").addEventListener("change", () => {
    state.priorityWeight = Number($("weight").value);
    if (state.priorityOn) loadBestSetAndGraph().catch((err) => console.error(err));
  });
  $("ask").addEventListener("click", () => {
    const raw = $("ask-id").value.trim();
    if (raw === "") return;
    explain(Number(raw));
  });
  $("ask-id").addEventListener("keydown", (event) => {
    if (event.key === "Enter") $("ask").click();
  });
  $("whatif").addEventListener("click", whatIf);
  $("ask-agent").addEventListener("click", askAgent);
  $("q").addEventListener("keydown", (event) => {
    if (event.key === "Enter") $("ask-agent").click();
  });
}

wire();
generatePool();
