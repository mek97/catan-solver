/* catan-solver — live dashboard.
   Board geometry comes from /api/geometry verbatim; position, moves, and log
   come from the live colonist feed. Advisory only: nothing is ever clicked. */

const COLORS = ["red", "blue", "orange", "green"];
const PIP_DOTS = { 2: 1, 3: 2, 4: 3, 5: 4, 6: 5, 8: 5, 9: 4, 10: 3, 11: 2, 12: 1 };

const state = { geometry: null, config: null, rec: null, myColor: null, on: true, timer: null };

const $ = (s) => document.querySelector(s);
const el = (tag, attrs = {}) => {
  const n = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
  return n;
};

async function getJSON(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || `${r.status}`);
  return r.json();
}

/* ---------------- hex art ---------------- */

function hexIcon(resource, cx, cy) {
  const g = el("g", { class: "hex-icon" });
  const put = (tag, attrs, cls) => {
    const n = el(tag, attrs);
    n.setAttribute("class", `hex-icon ${cls}`);
    g.appendChild(n);
  };
  if (resource === "wood") {
    put("path", { d: `M ${cx} ${cy - 20} l 9 15 h -18 z` }, "dark");
    put("path", { d: `M ${cx} ${cy - 11} l 11 17 h -22 z` }, "dark");
    put("rect", { x: cx - 2, y: cy + 6, width: 4, height: 7 }, "dark");
  } else if (resource === "sheep") {
    put("ellipse", { cx: cx - 2, cy: cy, rx: 15, ry: 10 }, "light");
    put("circle", { cx: cx + 12, cy: cy - 4, r: 6 }, "light");
  } else if (resource === "wheat") {
    put("rect", { x: cx - 1.5, y: cy - 16, width: 3, height: 30, rx: 1.5 }, "dark");
    for (let i = 0; i < 3; i++) {
      const y = cy - 12 + i * 8;
      put("ellipse", { cx: cx - 7, cy: y, rx: 6, ry: 3, transform: `rotate(-28 ${cx - 7} ${y})` }, "dark");
      put("ellipse", { cx: cx + 7, cy: y, rx: 6, ry: 3, transform: `rotate(28 ${cx + 7} ${y})` }, "dark");
    }
  } else if (resource === "brick") {
    for (let r = 0; r < 3; r++) {
      const off = r % 2 ? -7 : 0;
      for (let c = 0; c < 2; c++) {
        put("rect", {
          x: cx - 16 + off + c * 17, y: cy - 14 + r * 10,
          width: 14, height: 7.5, rx: 1.5,
        }, "clay");
      }
    }
  } else if (resource === "ore") {
    put("path", { d: `M ${cx - 16} ${cy + 10} l 9 -17 l 9 17 z` }, "stone");
    put("path", { d: `M ${cx - 3} ${cy + 10} l 11 -21 l 11 21 z` }, "stone");
  } else if (resource === "desert") {
    put("rect", { x: cx - 2.5, y: cy - 16, width: 5, height: 28, rx: 2.5 }, "dark");
    put("path", { d: `M ${cx - 2} ${cy - 4} h -8 v -8`, fill: "none", stroke: "#1c3a22", "stroke-width": 4.5 }, "dark");
    put("path", { d: `M ${cx + 2} ${cy - 8} h 8 v -6`, fill: "none", stroke: "#1c3a22", "stroke-width": 4.5 }, "dark");
  }
  return g;
}

function renderBoard() {
  const svg = $("#board");
  svg.replaceChildren();
  const g = state.geometry, cfg = state.config;
  if (!g || !cfg) return;

  const sand = el("g"), tiles = el("g"), ports = el("g");
  const pieces = el("g"), hints = el("g", { class: "hint", id: "hints" });

  // beach: slightly larger hexes behind the tiles
  for (const h of g.hexes) {
    const p = el("polygon", { points: h.points, class: "hex-sand" });
    p.setAttribute("transform", `translate(${h.cx} ${h.cy}) scale(1.14) translate(${-h.cx} ${-h.cy})`);
    sand.appendChild(p);
  }

  for (const h of g.hexes) {
    const t = cfg.hexes[h.id];
    tiles.appendChild(el("polygon", { points: h.points, class: `hex ${t.resource}` }));
    tiles.appendChild(hexIcon(t.resource, h.cx, h.cy));
    if (t.number) {
      const tok = el("g", { class: "token" });
      tok.appendChild(el("circle", { cx: h.cx, cy: h.cy, r: 16 }));
      const hot = t.number === 6 || t.number === 8;
      const num = el("text", { x: h.cx, y: h.cy + 3, class: `num${hot ? " hot" : ""}` });
      num.textContent = t.number;
      tok.appendChild(num);
      const pips = el("text", { x: h.cx, y: h.cy + 12, class: `pips${hot ? " hot" : ""}` });
      pips.textContent = "•".repeat(PIP_DOTS[t.number] || 0);
      tok.appendChild(pips);
      tiles.appendChild(tok);
    }
    if (cfg.robber_hex === h.id) {
      const r = el("g");
      r.appendChild(el("ellipse", { cx: h.cx + 22, cy: h.cy - 12, rx: 7, ry: 9, class: "robber" }));
      r.appendChild(el("rect", { x: h.cx + 17, y: h.cy - 4, width: 10, height: 12, rx: 3, class: "robber" }));
      tiles.appendChild(r);
    }
  }

  for (const port of cfg.ports || []) {
    const [a, b] = port.vertices;
    const va = g.vertices[a], vb = g.vertices[b];
    if (!va || !vb) continue;
    const mx = (va.x + vb.x) / 2, my = (va.y + vb.y) / 2;
    const len = Math.hypot(mx, my) || 1;
    const px = mx + (mx / len) * 34, py = my + (my / len) * 34;
    ports.appendChild(el("line", { x1: px, y1: py, x2: va.x, y2: va.y, class: "port-line" }));
    ports.appendChild(el("line", { x1: px, y1: py, x2: vb.x, y2: vb.y, class: "port-line" }));
    const badge = el("g", { class: "port-badge" });
    const label = port.type === "3:1" ? "3:1" : `2:1`;
    badge.appendChild(el("rect", { x: px - 15, y: py - 10, width: 30, height: 19, rx: 5 }));
    const t1 = el("text", { x: px, y: py - 1 }); t1.textContent = label;
    badge.appendChild(t1);
    if (port.type !== "3:1") {
      const t2 = el("text", { x: px, y: py + 7 }); t2.textContent = port.type.slice(0, 5);
      badge.appendChild(t2);
    }
    ports.appendChild(badge);
  }

  const owner = (kind, id) => {
    for (const c of COLORS) {
      const p = cfg.players[c];
      if (!p) continue;
      if (kind === "road" && p.roads.includes(id)) return c;
      if (kind === "s" && p.settlements.includes(id)) return c;
      if (kind === "c" && p.cities.includes(id)) return c;
    }
    return null;
  };

  for (const e of g.edges) {
    const o = owner("road", e.id);
    if (!o) continue;
    const x1 = e.x1 + (e.x2 - e.x1) * 0.16, y1 = e.y1 + (e.y2 - e.y1) * 0.16;
    const x2 = e.x1 + (e.x2 - e.x1) * 0.84, y2 = e.y1 + (e.y2 - e.y1) * 0.84;
    pieces.appendChild(el("line", { x1, y1, x2, y2, class: "road-shadow" }));
    pieces.appendChild(el("line", { x1, y1, x2, y2, class: `road ${o}` }));
  }
  for (const v of g.vertices) {
    const s = owner("s", v.id), c = owner("c", v.id);
    if (s) {
      pieces.appendChild(el("path", {
        d: `M ${v.x - 9} ${v.y + 8} v -10 l 9 -8 l 9 8 v 10 z`, class: `piece ${s}`,
      }));
    } else if (c) {
      pieces.appendChild(el("path", {
        d: `M ${v.x - 13} ${v.y + 9} v -13 l 7 -6 l 7 6 v 4 h 12 v 9 z`, class: `piece ${c}`,
      }));
    }
  }

  svg.append(sand, tiles, ports, pieces, hints);
  drawHint(state.rec?.moves?.[0]);
}

function drawHint(move) {
  const layer = $("#hints");
  if (!layer) return;
  layer.replaceChildren();
  if (!move || !state.rec?.my_turn) return;
  const g = state.geometry;
  for (const step of move.steps) {
    if (step.vertex != null && g.vertices[step.vertex]) {
      const v = g.vertices[step.vertex];
      layer.appendChild(el("circle", { cx: v.x, cy: v.y, r: 21, class: "halo" }));
      layer.appendChild(el("circle", { cx: v.x, cy: v.y, r: 18 }));
    }
    for (const eid of [step.edge, ...(step.edges || [])]) {
      if (eid == null || !g.edges[eid]) continue;
      const e = g.edges[eid];
      layer.appendChild(el("line", { x1: e.x1, y1: e.y1, x2: e.x2, y2: e.y2 }));
    }
    if (step.robber_hex != null && g.hexes[step.robber_hex]) {
      layer.appendChild(el("polygon", { points: g.hexes[step.robber_hex].points }));
    }
  }
}

/* ---------------- panel ---------------- */

function renderPanel(rec, status) {
  const mine = rec?.my_turn;
  const lbl = $("#turn-label");
  lbl.textContent = rec ? (mine ? "YOUR TURN" : `${rec.turn ?? "…"}'s turn`) : "waiting for a game";
  lbl.className = mine ? "mine" : "";
  const hand = rec ? Object.entries(rec.hand).filter(([, n]) => n).map(([r, n]) => `${n} ${r}`).join(" · ") : "";
  $("#turn-sub").textContent = rec
    ? `${rec.phase}${hand ? " · " + hand : " · no cards"}`
    : "start a game in the CDP browser";

  const best = $("#best");
  const top = rec?.moves?.[0];
  if (top && mine) {
    best.innerHTML = `<div class="best-card">
      <div class="best-tag">do this</div>
      <div class="best-hint"></div>
      <div class="best-why"></div></div>`;
    best.querySelector(".best-hint").textContent = top.location_hint;
    best.querySelector(".best-why").textContent = top.reasoning;
  } else if (top) {
    best.innerHTML = `<div class="best-card" style="border-color:var(--line)">
      <div class="best-tag" style="color:var(--dim)">when your turn comes</div>
      <div class="best-hint"></div></div>`;
    best.querySelector(".best-hint").textContent = top.location_hint;
  } else {
    best.innerHTML = `<div class="best-empty">No recommendation yet.</div>`;
  }

  const alts = $("#alts");
  alts.replaceChildren();
  (rec?.moves || []).slice(1, 6).forEach((m) => {
    const d = document.createElement("div");
    d.className = "alt";
    d.innerHTML = `<span class="score">${m.score.toFixed(1)}</span><span class="txt"></span>`;
    d.querySelector(".txt").textContent = m.location_hint;
    d.addEventListener("mouseenter", () => drawHint(m));
    d.addEventListener("mouseleave", () => drawHint(rec.moves[0]));
    alts.appendChild(d);
  });
  $("#alts-block").classList.toggle("hidden", !(rec?.moves?.length > 1));

  const trades = $("#trades");
  trades.replaceChildren();
  (rec?.trades || []).forEach((t) => {
    const d = document.createElement("div");
    d.className = `tip ${t.type}`;
    d.textContent = t.text;
    trades.appendChild(d);
  });
  $("#trades-block").classList.toggle("hidden", !(rec?.trades?.length));

  const players = $("#players");
  players.replaceChildren();
  (rec?.players || []).forEach((p) => {
    const d = document.createElement("div");
    d.className = `pl${p.is_me ? " me" : ""}`;
    d.innerHTML = `<span class="dot" style="background:var(--${p.color})"></span>
      <span class="nm">${p.color}${p.is_me ? " (you)" : ""}</span>
      <span class="st">${p.vp_visible}vp · ${p.settlements}s ${p.cities}c ${p.roads}r · ${p.cards}🂠</span>`;
    players.appendChild(d);
  });
  $("#players-block").classList.toggle("hidden", !(rec?.players?.length));

  const dice = $("#dice");
  dice.replaceChildren();
  if (rec?.dice?.rolls) {
    const counts = rec.dice.counts;
    const max = Math.max(1, ...Object.values(counts));
    for (let n = 2; n <= 12; n++) {
      const c = counts[String(n)] || 0;
      const b = document.createElement("div");
      b.className = "dbar";
      b.title = `${n}: ${c} roll(s)`;
      b.innerHTML = `<div class="bar${n === 6 || n === 8 ? " hot" : ""}" style="height:${
        Math.round((c / max) * 30) + 2}px"></div><div class="lbl">${n}</div>`;
      dice.appendChild(b);
    }
  }
  $("#dice-block").classList.toggle("hidden", !rec?.dice?.rolls);
}

function renderLog(events) {
  const box = $("#log");
  const fmt = (e) => {
    const w = e.color ? `${e.color} ` : "";
    const j = (a) => (a || []).join(",");
    switch (e.kind) {
      case "dice_rolled": return `🎲 ${w}rolled ${e.total}`;
      case "piece_placed": return `${w}placed ${e.piece}`;
      case "piece_bought": return `${w}bought ${e.piece}`;
      case "cards_received": return `${w}got ${j(e.cards)}`;
      case "card_stolen": return `${w}stole ${j(e.cards)}`;
      case "cards_discarded": return `${w}discarded ${j(e.cards)}`;
      case "trade_player": return `${w}traded ${j(e.gave)} → ${j(e.got)} w/ ${e.with ?? "?"}`;
      case "trade_bank": return `${w}bank ${j(e.gave)} → ${j(e.got)}`;
      case "trade_offered": return `${w}offers ${j(e.offers)} for ${j(e.wants)}`;
      case "robber_moved": return `${w}robber → ${e.tile?.number ?? "?"}-${e.tile?.resource ?? "?"}`;
      case "turn_ended": return `———`;
      default: return `${w}${e.kind}`;
    }
  };
  box.innerHTML = events.slice(-80).map((e) => {
    const cls = e.color === state.myColor ? "me" : e.kind === "dice_rolled" ? "roll" : "";
    return `<div class="${cls}">${fmt(e)}</div>`;
  }).join("");
  box.scrollTop = box.scrollHeight;
  $("#log-block").classList.toggle("hidden", !events.length);
}

/* ---------------- polling ---------------- */

function setLive(cls, text) {
  $("#live-dot").className = cls;
  $("#live-text").textContent = text;
}

async function poll() {
  try {
    const status = await getJSON("/api/live/status");
    state.myColor = status.my_color;
    if (!status.connected) { setLive("err", status.error ? "feed error" : "connecting…"); return; }
    if (!status.has_state) {
      setLive("on", "connected — waiting for a game");
      renderPanel(null, status);
      return;
    }
    const [st, rec, log] = await Promise.all([
      getJSON("/api/live/state"),
      getJSON("/api/live/moves"),
      getJSON("/api/live/log?limit=200"),
    ]);
    state.config = st.config;
    state.rec = rec;
    renderBoard();
    renderPanel(rec, status);
    renderLog(log.events);
    const gaps = status.gaps?.length ? ` · ⚠ ${status.gaps.length} gap` : "";
    setLive("on", `live · ${status.events} events${gaps}`);
  } catch (err) {
    setLive("err", String(err.message).slice(0, 40));
  }
}

async function toggle() {
  state.on = !state.on;
  $("#live-toggle").textContent = state.on ? "⏸" : "▶";
  if (state.on) {
    await fetch("/api/live/start", { method: "POST" });
    poll();
    state.timer = setInterval(poll, 2000);
  } else {
    clearInterval(state.timer);
    setLive("", "paused");
  }
}

async function openUrl(ev) {
  ev.preventDefault();
  const input = $("#open-url");
  const url = input.value.trim();
  if (!url) return;
  setLive("", "opening…");
  try {
    const r = await fetch(`/api/live/open?url=${encodeURIComponent(url)}`, { method: "POST" });
    const d = await r.json();
    if (d.error) throw new Error(d.error);
    input.blur();
    setTimeout(poll, 2500);
  } catch (err) {
    setLive("err", String(err.message).slice(0, 44));
  }
}

async function init() {
  $("#live-toggle").addEventListener("click", toggle);
  $("#open-bar").addEventListener("submit", openUrl);
  state.geometry = await getJSON("/api/geometry");
  await fetch("/api/live/start", { method: "POST" });
  poll();
  state.timer = setInterval(poll, 2000);
}

init();
