/* catan-solver UI. Geometry comes from /api/geometry verbatim -- this file
   never computes hex math, so frontend and backend can't disagree on IDs. */

const RESOURCES = ["wood", "brick", "sheep", "wheat", "ore"];
const DEVS = ["knight", "road_building", "year_of_plenty", "monopoly", "vp"];
const DEV_LABELS = { knight: "knight", road_building: "roads", year_of_plenty: "YoP", monopoly: "mono", vp: "VP" };
const COLORS = ["red", "blue", "orange", "white"];
const HEX_RESOURCES = ["wood", "brick", "sheep", "wheat", "ore", "desert"];
const NUMBERS = [2, 3, 4, 5, 6, 8, 9, 10, 11, 12];
const PIP_DOTS = { 2: 1, 3: 2, 4: 3, 5: 4, 6: 5, 8: 5, 9: 4, 10: 3, 11: 2, 12: 1 };

const state = {
  geometry: null,
  config: null,
  mode: "hex",
  activeColor: "red",
  results: [],
};

const $ = (sel) => document.querySelector(sel);
const svgEl = (tag, attrs = {}) => {
  const el = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  return el;
};

/* ---------------- api ---------------- */

async function getJSON(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${url}: ${r.status}`);
  return r.json();
}

async function sendJSON(url, method, body) {
  const r = await fetch(url, {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || `${url}: ${r.status}`);
  return data;
}

let pushTimer = null;
function schedulePush() {
  clearTimeout(pushTimer);
  pushTimer = setTimeout(() => {
    sendJSON("/api/config", "PUT", state.config).catch(() => {});
  }, 600);
}

function touched() {
  syncJsonTab();
  schedulePush();
  renderBoard();
}

/* ---------------- board rendering ---------------- */

function pieceOwner(kind, id) {
  for (const color of COLORS) {
    const p = state.config.players[color];
    if (!p) continue;
    if (kind === "settlement" && p.settlements.includes(id)) return color;
    if (kind === "city" && p.cities.includes(id)) return color;
    if (kind === "road" && p.roads.includes(id)) return color;
  }
  return null;
}

function removePiece(kind, id) {
  for (const color of COLORS) {
    const p = state.config.players[color];
    if (!p) continue;
    if (kind === "road") p.roads = p.roads.filter((e) => e !== id);
    else {
      p.settlements = p.settlements.filter((v) => v !== id);
      p.cities = p.cities.filter((v) => v !== id);
    }
  }
}

function renderBoard() {
  const svg = $("#board");
  svg.replaceChildren();
  const g = state.geometry;
  const cfg = state.config;
  if (!g || !cfg) return;

  const layerHex = svgEl("g");
  const layerPort = svgEl("g");
  const layerPiece = svgEl("g");
  const layerHl = svgEl("g", { class: "hl", id: "hl-layer" });
  const layerHit = svgEl("g");

  // hexes + tokens + robber
  for (const h of g.hexes) {
    const tile = cfg.hexes[h.id];
    layerHex.appendChild(
      svgEl("polygon", { points: h.points, class: `hex ${tile.resource}` })
    );
    if (tile.number) {
      const token = svgEl("g", { class: "token" });
      token.appendChild(svgEl("circle", { cx: h.cx, cy: h.cy, r: 15 }));
      const t = svgEl("text", {
        x: h.cx,
        y: h.cy + 5,
        class: tile.number === 6 || tile.number === 8 ? "hot" : "",
      });
      t.textContent = tile.number;
      token.appendChild(t);
      const dots = svgEl("text", { x: h.cx, y: h.cy + 12, class: "pipdots" });
      dots.textContent = "•".repeat(PIP_DOTS[tile.number] || 0);
      token.appendChild(dots);
      layerHex.appendChild(token);
    }
    if (cfg.robber_hex === h.id) {
      layerHex.appendChild(
        svgEl("ellipse", { cx: h.cx + 20, cy: h.cy - 16, rx: 8, ry: 11, class: "robber" })
      );
    }
  }

  // ports
  for (const port of cfg.ports || []) {
    const [a, b] = port.vertices;
    const va = g.vertices[a], vb = g.vertices[b];
    const mx = (va.x + vb.x) / 2, my = (va.y + vb.y) / 2;
    const len = Math.hypot(mx, my) || 1;
    const px = mx + (mx / len) * 30, py = my + (my / len) * 30;
    const badge = svgEl("g", { class: "port-badge" });
    const label = port.type === "3:1" ? "3:1" : `2:1 ${port.type}`;
    const w = label.length * 6.4 + 10;
    badge.appendChild(svgEl("rect", { x: px - w / 2, y: py - 9, width: w, height: 17, rx: 4 }));
    const t = svgEl("text", { x: px, y: py + 4 });
    t.textContent = label;
    badge.appendChild(t);
    for (const v of [a, b]) {
      layerPort.appendChild(
        svgEl("line", {
          x1: px, y1: py, x2: g.vertices[v].x, y2: g.vertices[v].y,
          stroke: "#3d5a77", "stroke-width": 1.2, "stroke-dasharray": "3 3",
        })
      );
    }
    layerPort.appendChild(badge);
  }

  // roads then buildings
  for (const e of g.edges) {
    const owner = pieceOwner("road", e.id);
    if (owner) {
      layerPiece.appendChild(
        svgEl("line", {
          x1: e.x1 + (e.x2 - e.x1) * 0.18, y1: e.y1 + (e.y2 - e.y1) * 0.18,
          x2: e.x1 + (e.x2 - e.x1) * 0.82, y2: e.y1 + (e.y2 - e.y1) * 0.82,
          class: `road-piece ${owner}`,
        })
      );
    }
  }
  for (const v of g.vertices) {
    const s = pieceOwner("settlement", v.id);
    const c = pieceOwner("city", v.id);
    if (s) {
      layerPiece.appendChild(
        svgEl("path", {
          d: `M ${v.x - 8} ${v.y + 7} v -9 l 8 -7 l 8 7 v 9 z`,
          class: `piece ${s}`,
        })
      );
    } else if (c) {
      layerPiece.appendChild(
        svgEl("path", {
          d: `M ${v.x - 11} ${v.y + 8} v -12 l 6 -5 l 6 5 v 3 h 10 v 9 z`,
          class: `piece ${c}`,
        })
      );
    }
  }

  // hit targets
  for (const h of g.hexes) {
    const hit = svgEl("polygon", { points: h.points, class: "hit hit-hex" });
    hit.addEventListener("click", (ev) => onHexClick(h, ev));
    layerHit.appendChild(hit);
  }
  for (const v of g.vertices) {
    const hit = svgEl("circle", { cx: v.x, cy: v.y, r: 11, class: "hit hit-vertex" });
    hit.addEventListener("click", () => onVertexClick(v.id));
    layerHit.appendChild(hit);
  }
  for (const e of g.edges) {
    const hit = svgEl("line", { x1: e.x1, y1: e.y1, x2: e.x2, y2: e.y2, class: "hit hit-edge" });
    hit.addEventListener("click", () => onEdgeClick(e.id));
    layerHit.appendChild(hit);
  }

  svg.append(layerHex, layerPort, layerPiece, layerHl, layerHit);
}

/* ---------------- board interaction ---------------- */

function onHexClick(h, ev) {
  if (state.mode === "robber") {
    state.config.robber_hex = h.id;
    touched();
    return;
  }
  if (state.mode !== "hex") return;
  const pop = $("#hex-popover");
  pop.replaceChildren();
  const tile = state.config.hexes[h.id];
  const grid = document.createElement("div");
  grid.className = "res-grid";
  const fills = {
    wood: "#2c7a3f", sheep: "#93c74e", wheat: "#dfb02f",
    brick: "#c05f30", ore: "#7e8896", desert: "#d5c088",
  };
  for (const res of HEX_RESOURCES) {
    const b = document.createElement("button");
    b.textContent = res;
    b.style.background = fills[res];
    if (res === tile.resource) b.style.outline = "2px solid #e8b93c";
    b.onclick = () => {
      tile.resource = res;
      if (res === "desert") tile.number = null;
      touched();
      pop.classList.add("hidden");
    };
    grid.appendChild(b);
  }
  pop.appendChild(grid);
  const sel = document.createElement("select");
  sel.innerHTML =
    '<option value="">no token</option>' +
    NUMBERS.map((n) => `<option ${n === tile.number ? "selected" : ""}>${n}</option>`).join("");
  sel.onchange = () => {
    tile.number = sel.value ? Number(sel.value) : null;
    if (tile.number) tile.resource = tile.resource === "desert" ? "wheat" : tile.resource;
    touched();
    pop.classList.add("hidden");
  };
  pop.appendChild(sel);

  const wrap = $("#board-wrap").getBoundingClientRect();
  pop.style.left = Math.min(ev.clientX - wrap.left + 10, wrap.width - 225) + "px";
  pop.style.top = Math.min(ev.clientY - wrap.top + 10, wrap.height - 150) + "px";
  pop.classList.remove("hidden");
}

function onVertexClick(vid) {
  if (state.mode !== "settlement" && state.mode !== "city") return;
  const key = state.mode === "settlement" ? "settlements" : "cities";
  const had = state.config.players[state.activeColor][key].includes(vid);
  removePiece("settlement", vid); // clears any building there (replaces the arrays)
  if (!had) state.config.players[state.activeColor][key].push(vid);
  touched();
}

function onEdgeClick(eid) {
  if (state.mode !== "road") return;
  const p = state.config.players[state.activeColor];
  if (p.roads.includes(eid)) {
    p.roads = p.roads.filter((e) => e !== eid);
  } else {
    removePiece("road", eid);
    p.roads.push(eid);
  }
  touched();
}

document.addEventListener("click", (ev) => {
  const pop = $("#hex-popover");
  if (!pop.classList.contains("hidden") && !pop.contains(ev.target) && !ev.target.closest(".hit-hex")) {
    pop.classList.add("hidden");
  }
});

/* ---------------- highlights ---------------- */

function highlightMove(move) {
  const layer = $("#hl-layer");
  if (!layer) return;
  layer.replaceChildren();
  if (!move) return;
  const g = state.geometry;
  for (const step of move.steps) {
    if (step.vertex != null) {
      const v = g.vertices[step.vertex];
      layer.appendChild(svgEl("circle", { cx: v.x, cy: v.y, r: 15 }));
    }
    for (const eid of [step.edge, ...(step.edges || [])]) {
      if (eid == null) continue;
      const e = g.edges[eid];
      layer.appendChild(svgEl("line", { x1: e.x1, y1: e.y1, x2: e.x2, y2: e.y2 }));
    }
    if (step.robber_hex != null) {
      const h = g.hexes[step.robber_hex];
      layer.appendChild(svgEl("polygon", { points: h.points }));
    }
  }
}

/* ---------------- panel ---------------- */

function buildPanel() {
  const hand = $("#hand");
  hand.replaceChildren();
  for (const r of RESOURCES) {
    const label = document.createElement("label");
    label.innerHTML = `${r}<input type="number" min="0" max="30" data-res="${r}" />`;
    hand.appendChild(label);
  }
  hand.addEventListener("input", (ev) => {
    const r = ev.target.dataset.res;
    if (r) {
      state.config.me.hand[r] = Number(ev.target.value) || 0;
      syncJsonTab();
      schedulePush();
    }
  });

  const devs = $("#devcards");
  devs.replaceChildren();
  for (const d of DEVS) {
    const label = document.createElement("label");
    label.innerHTML = `${DEV_LABELS[d]}<input type="number" min="0" max="14" data-dev="${d}" />`;
    devs.appendChild(label);
  }
  devs.addEventListener("input", (ev) => {
    const d = ev.target.dataset.dev;
    if (d) {
      state.config.me.dev_cards[d] = Number(ev.target.value) || 0;
      syncJsonTab();
      schedulePush();
    }
  });

  const tbody = $("#players-table tbody");
  tbody.replaceChildren();
  for (const color of COLORS) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td><span class="dot" style="background: var(--${color})"></span></td>
      <td><input type="number" min="0" max="12" data-p="${color}" data-f="vp_visible" /></td>
      <td><input type="number" min="0" max="30" data-p="${color}" data-f="resource_count" /></td>
      <td><input type="number" min="0" max="25" data-p="${color}" data-f="dev_card_count" /></td>
      <td><input type="number" min="0" max="14" data-p="${color}" data-f="knights_played" /></td>
      <td><input type="checkbox" data-p="${color}" data-f="longest_road" /></td>
      <td><input type="checkbox" data-p="${color}" data-f="largest_army" /></td>`;
    tbody.appendChild(tr);
  }
  tbody.addEventListener("input", (ev) => {
    const { p, f } = ev.target.dataset;
    if (!p) return;
    state.config.players[p][f] =
      ev.target.type === "checkbox" ? ev.target.checked : Number(ev.target.value) || 0;
    syncJsonTab();
    schedulePush();
  });

  $("#my-color").addEventListener("change", (ev) => {
    state.config.me.color = ev.target.value;
    state.config.turn = ev.target.value;
    touched();
  });
  $("#phase").addEventListener("change", (ev) => {
    state.config.phase = ev.target.value;
    syncJsonTab();
    schedulePush();
  });
  $("#pending-robber").addEventListener("change", (ev) => {
    state.config.pending = ev.target.checked ? "move_robber" : null;
    syncJsonTab();
    schedulePush();
  });
  $("#dev-bought").addEventListener("change", (ev) => {
    state.config.me.dev_card_bought_this_turn = ev.target.checked;
    syncJsonTab();
    schedulePush();
  });
  $("#dev-played").addEventListener("change", (ev) => {
    state.config.me.dev_card_played_this_turn = ev.target.checked;
    syncJsonTab();
    schedulePush();
  });
}

function syncPanel() {
  const cfg = state.config;
  $("#my-color").value = cfg.me.color;
  $("#phase").value = cfg.phase;
  $("#pending-robber").checked = cfg.pending === "move_robber";
  $("#dev-bought").checked = cfg.me.dev_card_bought_this_turn;
  $("#dev-played").checked = cfg.me.dev_card_played_this_turn;
  for (const input of document.querySelectorAll("#hand input")) {
    input.value = cfg.me.hand[input.dataset.res] ?? 0;
  }
  for (const input of document.querySelectorAll("#devcards input")) {
    input.value = cfg.me.dev_cards[input.dataset.dev] ?? 0;
  }
  for (const input of document.querySelectorAll("#players-table input")) {
    const p = cfg.players[input.dataset.p] || {};
    if (input.type === "checkbox") input.checked = !!p[input.dataset.f];
    else input.value = p[input.dataset.f] ?? 0;
  }
}

function syncJsonTab() {
  $("#json").value = JSON.stringify(state.config, null, 2);
}

function setWarnings(warnings) {
  const el = $("#warnings");
  if (warnings && warnings.length) {
    el.textContent = warnings.map((w) => `⚠ ${w}`).join("\n");
    el.classList.remove("hidden");
  } else {
    el.classList.add("hidden");
  }
}

/* ---------------- results ---------------- */

function renderResults() {
  const el = $("#results");
  el.replaceChildren();
  state.results.forEach((move, i) => {
    const card = document.createElement("div");
    card.className = "move";
    card.innerHTML = `
      <div class="head"><span class="rank">#${i + 1}</span><span class="score">score ${move.score.toFixed(1)}</span></div>
      <div class="hint"></div>
      <div class="why"></div>`;
    card.querySelector(".hint").textContent = move.location_hint;
    card.querySelector(".why").textContent = move.reasoning;
    card.addEventListener("mouseenter", () => highlightMove(move));
    card.addEventListener("mouseleave", () => highlightMove(null));
    el.appendChild(card);
  });
}

async function onSolve() {
  const btn = $("#solve");
  btn.disabled = true;
  btn.textContent = "Solving…";
  try {
    const data = await sendJSON("/api/solve", "POST", state.config);
    state.results = data.moves;
    setWarnings(data.warnings);
    renderResults();
  } catch (err) {
    setWarnings([`solve failed: ${err.message}`]);
  } finally {
    btn.disabled = false;
    btn.textContent = "Solve";
  }
}

/* ---------------- screenshot parsing ---------------- */

async function parseFile(file) {
  const status = $("#parse-status");
  status.classList.remove("hidden", "err");
  status.textContent = "Reading the board… (Claude: seconds; Codex fallback: a minute or two)";
  const form = new FormData();
  form.append("file", file);
  try {
    const r = await fetch("/api/parse", { method: "POST", body: form });
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || `HTTP ${r.status}`);
    state.config = data.config;
    state.results = [];
    renderResults();
    syncPanel();
    syncJsonTab();
    renderBoard();
    setWarnings(data.warnings);
    status.textContent = `Parsed with ${data.backend === "codex" ? "Codex" : "Claude"}. Check the board and fix anything the vision pass missed.`;
  } catch (err) {
    status.classList.add("err");
    status.textContent = `Parse failed: ${err.message}`;
  }
}

function bindDropzone() {
  const dz = $("#dropzone");
  const input = $("#file-input");
  dz.addEventListener("click", () => input.click());
  input.addEventListener("change", () => input.files[0] && parseFile(input.files[0]));
  dz.addEventListener("dragover", (ev) => {
    ev.preventDefault();
    dz.classList.add("drag");
  });
  dz.addEventListener("dragleave", () => dz.classList.remove("drag"));
  dz.addEventListener("drop", (ev) => {
    ev.preventDefault();
    dz.classList.remove("drag");
    const file = ev.dataTransfer.files[0];
    if (file) parseFile(file);
  });
  // paste a screenshot straight from the clipboard
  document.addEventListener("paste", (ev) => {
    const item = [...(ev.clipboardData?.items || [])].find((i) => i.type.startsWith("image/"));
    if (item) parseFile(item.getAsFile());
  });
}

/* ---------------- live feed ---------------- */

const live = { on: false, timer: null, lastLogId: null };

function renderLiveInfo(status, rec) {
  const info = $("#live-info");
  info.classList.remove("hidden");
  const turn = rec
    ? (rec.my_turn ? '<span class="badge">YOUR TURN</span>' : `turn: ${rec.turn ?? "?"}`)
    : "waiting for a game…";
  const gaps = status.gaps?.length
    ? `<div style="color:#e0a869">⚠ ${status.gaps.length} dropped event(s) — hit Rebuild</div>`
    : "";
  const dice = rec?.dice
    ? `<div>rolls: ${rec.dice.rolls} · cold: ${rec.dice.coldest.join(", ")}</div>`
    : "";
  info.innerHTML = `
    <div>${turn} · phase: ${rec?.phase ?? "—"} · you: ${status.my_color ?? "—"}</div>
    <div>frames ${status.frames} · events ${status.events} · applied ${status.applied}</div>
    ${dice}${gaps}`;
}

function renderLiveLog(events) {
  const el = $("#live-log");
  el.classList.remove("hidden");
  const fmt = (e) => {
    const who = e.color ? `${e.color} ` : "";
    switch (e.kind) {
      case "dice_rolled": return `${who}rolled ${e.total}`;
      case "piece_placed": return `${who}placed ${e.piece}`;
      case "piece_bought": return `${who}bought ${e.piece}`;
      case "cards_received": return `${who}got ${(e.cards || []).join(",")}`;
      case "card_stolen": return `${who}stole ${(e.cards || []).join(",")}`;
      case "cards_discarded": return `${who}discarded ${(e.cards || []).join(",")}`;
      case "trade_player": return `${who}traded ${(e.gave || []).join(",")} → ${(e.got || []).join(",")} with ${e.with ?? "?"}`;
      case "trade_bank": return `${who}bank ${(e.gave || []).join(",")} → ${(e.got || []).join(",")}`;
      case "trade_offered": return `${who}offers ${(e.offers || []).join(",")} for ${(e.wants || []).join(",")}`;
      case "robber_moved": return `${who}robber → ${e.tile?.number ?? "?"}-${e.tile?.resource ?? "?"}`;
      case "turn_ended": return `— turn end —`;
      default: return `${who}${e.kind}`;
    }
  };
  el.innerHTML = events
    .slice(-60)
    .map((e) => `<div class="${e.color === state.liveMyColor ? "me" : ""}">${fmt(e)}</div>`)
    .join("");
  el.scrollTop = el.scrollHeight;
}

async function livePoll() {
  try {
    const status = await getJSON("/api/live/status");
    state.liveMyColor = status.my_color;
    $("#live-status").textContent = status.connected
      ? (status.has_state ? "live" : "connected — waiting for game")
      : (status.error ? "error" : "connecting…");
    $("#live-status").className = status.connected && status.has_state ? "live" : "dim";

    if (!status.has_state) { renderLiveInfo(status, null); return; }

    const [stateRes, rec, log] = await Promise.all([
      getJSON("/api/live/state"),
      getJSON("/api/live/moves"),
      getJSON("/api/live/log?limit=200"),
    ]);
    state.config = stateRes.config;
    syncPanel();
    syncJsonTab();
    renderBoard();
    state.results = rec.moves;
    renderResults();
    setWarnings([...(stateRes.warnings || []), ...rec.trades.map((t) => t.text)]);
    renderLiveInfo(status, rec);
    renderLiveLog(log.events);
  } catch (err) {
    $("#live-status").textContent = err.message.slice(0, 60);
    $("#live-status").className = "err";
  }
}

async function toggleLive() {
  const btn = $("#live-toggle");
  if (live.on) {
    live.on = false;
    clearInterval(live.timer);
    await fetch("/api/live/stop", { method: "POST" });
    btn.textContent = "Connect";
    btn.classList.remove("on");
    $("#live-status").textContent = "off";
    $("#live-status").className = "dim";
    return;
  }
  live.on = true;
  btn.textContent = "Disconnect";
  btn.classList.add("on");
  $("#live-status").textContent = "connecting…";
  await fetch("/api/live/start", { method: "POST" });
  livePoll();
  live.timer = setInterval(livePoll, 2000);
}

/* ---------------- init ---------------- */

function bindToolbar() {
  $("#modes").addEventListener("click", (ev) => {
    const btn = ev.target.closest("button");
    if (!btn) return;
    state.mode = btn.dataset.mode;
    document.body.dataset.mode = state.mode;
    for (const b of document.querySelectorAll("#modes button")) {
      b.classList.toggle("active", b === btn);
    }
  });
  $("#colors").addEventListener("click", (ev) => {
    const btn = ev.target.closest("button");
    if (!btn) return;
    state.activeColor = btn.dataset.color;
    for (const b of document.querySelectorAll("#colors button")) {
      b.classList.toggle("active", b === btn);
    }
  });
}

async function init() {
  document.body.dataset.mode = state.mode;
  bindToolbar();
  bindDropzone();
  buildPanel();
  $("#solve").addEventListener("click", onSolve);
  $("#live-toggle").addEventListener("click", toggleLive);
  $("#json-apply").addEventListener("click", () => {
    try {
      state.config = JSON.parse($("#json").value);
      syncPanel();
      renderBoard();
      schedulePush();
      setWarnings([]);
    } catch (err) {
      setWarnings([`bad JSON: ${err.message}`]);
    }
  });

  const [geometry, config] = await Promise.all([
    getJSON("/api/geometry"),
    getJSON("/api/config"),
  ]);
  state.geometry = geometry;
  state.config = config;
  syncPanel();
  syncJsonTab();
  renderBoard();
}

init();
