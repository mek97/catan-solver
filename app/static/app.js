/* catan-solver — live dashboard.
   Board geometry comes from /api/geometry verbatim; position, moves, and log
   come from the live colonist feed. Advisory only: nothing is ever clicked. */

const PIP_DOTS = { 2: 1, 3: 2, 4: 3, 5: 4, 6: 5, 8: 5, 9: 4, 10: 3, 11: 2, 12: 1 };
const RES_ABBR = { wood: "🌲", brick: "🧱", sheep: "🐑", wheat: "🌾", ore: "⛰" };

const state = { geometry: null, config: null, rec: null, myColor: null,
                heroMove: null, on: true, timer: null };

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

  // Whoever the server says is in the game, not a fixed four: the 5-6 player
  // extension seats white and brown, and a hardcoded list silently declines to
  // draw their pieces at all.
  const owner = (kind, id) => {
    for (const [c, p] of Object.entries(cfg.players || {})) {
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
  drawHint(state.heroMove);
}

/* Action-typed hints: each step of the recommended move is drawn as a ghost of
   the piece you'd actually place, in your colour, pulsing. Multi-step moves
   (trade → build, road building) are numbered in order. Non-spatial actions
   (buy dev, bank trade, end turn) surface as a banner instead of on the board. */

const STEP_LABEL = {
  build_settlement: "place settlement",
  setup_settlement: "place settlement",
  build_city: "upgrade to city",
  build_road: "place road",
  setup_road: "place road",
  play_road_building: "road building — 2 free roads",
  buy_dev: "buy development card",
  play_knight: "play knight → move robber",
  play_year_of_plenty: "play year of plenty",
  play_monopoly: "play monopoly",
  trade_bank: "bank trade",
  move_robber: "move robber",
  roll_dice: "roll the dice",
  end_turn: "end turn",
};

function ghostSettlement(v, cls) {
  return el("path", { d: `M ${v.x - 11} ${v.y + 10} v -12 l 11 -10 l 11 10 v 12 z`, class: cls });
}
function ghostCity(v, cls) {
  return el("path", { d: `M ${v.x - 15} ${v.y + 11} v -15 l 8 -7 l 8 7 v 5 h 14 v 10 z`, class: cls });
}
function stepBadge(x, y, n) {
  const g = el("g", { class: "step-badge" });
  g.appendChild(el("circle", { cx: x, cy: y, r: 9 }));
  const t = el("text", { x, y: y + 3.5 });
  t.textContent = n;
  g.appendChild(t);
  return g;
}

function drawHint(move) {
  const layer = $("#hints");
  const banner = $("#hint-banner");
  if (!layer) return;
  layer.replaceChildren();
  banner.replaceChildren();
  banner.classList.add("hidden");
  if (!move) return;

  const g = state.geometry;
  const me = state.rec?.players?.find((p) => p.is_me)?.color || "red";
  const spatial = [];   // steps drawn on the board
  const abstract = [];  // steps shown in the banner

  move.steps.forEach((step) => {
    const has = step.vertex != null || step.edge != null
      || (step.edges || []).length || step.robber_hex != null;
    (has ? spatial : abstract).push(step);
  });

  let n = 0;
  const numbered = move.steps.length > 1;
  for (const step of move.steps) {
    n += 1;
    const kind = step.type;
    if (step.vertex != null && g.vertices[step.vertex]) {
      const v = g.vertices[step.vertex];
      layer.appendChild(el("circle", { cx: v.x, cy: v.y, r: 24, class: "halo" }));
      const isCity = kind === "build_city";
      layer.appendChild(isCity ? ghostCity(v, `ghost ${me}`) : ghostSettlement(v, `ghost ${me}`));
      layer.appendChild(isCity ? ghostCity(v, "ghost-outline") : ghostSettlement(v, "ghost-outline"));
      if (numbered) layer.appendChild(stepBadge(v.x + 20, v.y - 16, n));
    }
    for (const eid of [step.edge, ...(step.edges || [])]) {
      if (eid == null || !g.edges[eid]) continue;
      const e = g.edges[eid];
      const x1 = e.x1 + (e.x2 - e.x1) * 0.16, y1 = e.y1 + (e.y2 - e.y1) * 0.16;
      const x2 = e.x1 + (e.x2 - e.x1) * 0.84, y2 = e.y1 + (e.y2 - e.y1) * 0.84;
      layer.appendChild(el("line", { x1, y1, x2, y2, class: `ghost-road ${me}` }));
      layer.appendChild(el("line", { x1, y1, x2, y2, class: "ghost-road-outline" }));
      if (numbered) layer.appendChild(stepBadge(e.mx, e.my, n));
    }
    if (step.robber_hex != null && g.hexes[step.robber_hex]) {
      const h = g.hexes[step.robber_hex];
      layer.appendChild(el("polygon", { points: h.points, class: "robber-target" }));
      layer.appendChild(el("polygon", { points: h.points, class: "robber-ring" }));
      const r = el("g", { class: "ghost-robber" });
      r.appendChild(el("ellipse", { cx: h.cx, cy: h.cy - 26, rx: 8, ry: 10 }));
      r.appendChild(el("rect", { x: h.cx - 6, y: h.cy - 17, width: 12, height: 14, rx: 3 }));
      layer.appendChild(r);
    }
  }

  // banner for actions with no board location, plus a caption for the rest
  const parts = [];
  for (const s of move.steps) {
    let label = STEP_LABEL[s.type] || s.type;
    if (s.type === "trade_bank" && s.give && s.get) {
      const g1 = Object.entries(s.give).map(([r, c]) => `${c} ${r}`).join(", ");
      const g2 = Object.entries(s.get).map(([r, c]) => `${c} ${r}`).join(", ");
      label = `bank ${g1} → ${g2}`;
    }
    if (s.type === "play_monopoly" && s.resource) label = `monopoly on ${s.resource}`;
    if (s.type === "play_year_of_plenty" && s.get) {
      label = `year of plenty: take ${Object.keys(s.get).join(" + ")}`;
    }
    if (s.type === "move_robber" && s.steal_from) label += ` (steal ${s.steal_from})`;
    parts.push(label);
  }
  if (parts.length && (abstract.length || parts.length > 1 || !spatial.length)) {
    banner.classList.remove("hidden");
    banner.innerHTML = parts
      .map((p, i) => `<span class="bstep">${parts.length > 1 ? `<b>${i + 1}</b>` : ""}${p}</span>`)
      .join('<span class="barrow">→</span>');
  }
}

/* ---------------- panel ---------------- */

/* Every recommendation is filed into one of the actions you can actually take
   at the table, so the panel answers "what can I do right now" rather than
   making you scan eleven stacked lists. Categories keep a fixed order and a
   fixed colour, so position alone tells you what you're looking at. */

const CATS = [
  { key: "incoming", name: "Respond",  color: "var(--deal)"  },
  { key: "place",    name: "Place",    color: "var(--place)" },
  { key: "dev",      name: "Dev card", color: "var(--dev)"   },
  { key: "bank",     name: "Bank",     color: "var(--bank)"  },
  { key: "offer",    name: "Offer",    color: "var(--deal)"  },
  { key: "robber",   name: "Robber",   color: "var(--rob)"   },
];

const PLACE_STEPS = new Set([
  "build_settlement", "setup_settlement", "build_city", "build_road", "setup_road",
]);

function categorise(rec) {
  const out = Object.fromEntries(CATS.map((c) => [c.key, []]));
  if (!rec) return out;

  // Classify by the *goal* -- the last step. A combo like "bank 4 brick for a
  // sheep, then buy a dev card" is a dev-card action funded by a bank trade,
  // not a bank trade; filing by the first step mislabels every combo.
  for (const m of rec.moves || []) {
    const goal = m.steps[m.steps.length - 1].type;
    const item = { score: m.score, text: m.location_hint, why: m.reasoning, move: m };
    if (PLACE_STEPS.has(goal)) out.place.push(item);
    else if (goal === "buy_dev" || goal.startsWith("play_")) out.dev.push(item);
    else if (goal === "trade_bank") out.bank.push(item);
    else if (goal === "move_robber") out.robber.push(item);
    // end_turn and roll_dice stay uncategorised: neither is a choice, they
    // are the turn moving on. Rolling gets its own prompt below.
  }
  for (const b of rec.bank_options || []) {
    out.bank.push({ score: b.score, text: b.text, why: b.why });
  }
  for (const d of rec.dev_plays || []) {
    out.dev.push({
      score: d.score, text: `${d.label}: ${d.action}`, why: d.why,
      tag: d.certain ? null : "if", move: { steps: d.steps },
    });
  }
  for (const r of rec.robber || []) {
    out.robber.push({
      score: r.score, text: r.text, why: r.why,
      tag: r.needs_knight ? "if" : null,
      move: { steps: [{ type: "move_robber", robber_hex: r.hex, steal_from: r.steal_from }] },
    });
  }
  for (const a of rec.offer_advice || []) {
    const o = a.offer || {};
    out.incoming.push({
      score: a.score ?? 0, tag: a.verdict,
      text: `${o.from ?? "?"}: give ${(o.wants || []).join(", ") || "?"} → get ${(o.offers || []).join(", ") || "?"}`,
      why: a.text,
    });
  }
  for (const p of rec.proposals || []) {
    out.offer.push({ score: p.score ?? 0, text: p.text, why: null });
  }
  for (const t of rec.trades || []) {
    if (t.type === "want") out.offer.push({ score: 0, text: t.text, why: null });
  }
  // an over-limit hand is a risk, not an obligation: spending it down is a
  // bank action, so it belongs with the bank trades rather than in alerts
  if (rec.discard && !rec.discard.required) {
    out.bank.push({ score: 0.5, text: rec.discard.text, why: null });
  }

  for (const k of Object.keys(out)) {
    out[k].sort((a, b) => (b.score ?? 0) - (a.score ?? 0));
    const seen = new Set();
    out[k] = out[k].filter((x) => !seen.has(x.text) && seen.add(x.text));
  }
  return out;
}

function renderHand(rec) {
  const strip = $("#hand-strip");
  strip.replaceChildren();
  const hand = rec?.hand || {};
  const held = Object.entries(hand).filter(([, n]) => n > 0);
  if (!held.length) {
    strip.innerHTML = '<span class="hcard empty">no cards</span>';
    return;
  }
  strip.innerHTML = held
    .map(([r, n]) => `<span class="hcard ${r}">${n} ${RES_ABBR[r] || r}</span>`)
    .join("");
}

function renderUrgent(rec) {
  const box = $("#urgent");
  box.replaceChildren();
  const add = (tag, text) => {
    const d = document.createElement("div");
    d.className = "alert";
    d.innerHTML = `<div class="atag">${tag}</div><div class="atext"></div>`;
    d.querySelector(".atext").textContent = text;
    box.appendChild(d);
  };
  if (rec?.discard?.required) add("must discard now", rec.discard.text);
  if (rec?.pending === "move_robber") {
    const best = rec.robber?.[0];
    add("move the robber", best ? best.text : "choose a hex");
  }
  if (rec?.pending === "roll") {
    // a knight is the only thing that can happen first, and only sometimes
    // is it worth it -- say which, rather than just "roll"
    const knight = (rec.moves || []).find((m) => m.steps[0].type === "play_knight");
    add("roll the dice", knight && knight.score > 0
      ? `first: ${knight.location_hint}`
      : "nothing else is available until you do");
  }
  const waiting = (rec?.offer_advice || []).filter((a) => a.verdict !== "cannot");
  if (waiting.length && !rec?.my_turn) {
    add(`${waiting.length} offer${waiting.length > 1 ? "s" : ""} on the table`,
        waiting[0].text);
  }
}

/* The best action is the best across *every* category, not just the solver's
   move list. Dev-card plays, robber placements and trade proposals are scored
   elsewhere, so ranking only rec.moves could show "end your turn" while a
   29-point knight sat one row below. */
function bestOverall(groups) {
  let best = null;
  for (const cat of CATS) {
    for (const item of groups[cat.key] || []) {
      if (!best || (item.score ?? 0) > (best.item.score ?? 0)) best = { cat, item };
    }
  }
  return best;
}

function renderPrimary(rec, groups) {
  const box = $("#primary");
  const best = bestOverall(groups);
  const pass = rec?.moves?.find((m) => m.steps[0].type === "end_turn");

  if (!best) {
    box.innerHTML = pass
      ? `<div class="hero" style="--cat:var(--dim)">
           <div class="hero-top"><span class="hero-cat">pass</span></div>
           <div class="hero-act">end your turn</div>
           <div class="hero-why">Nothing available — bank your cards.</div>
         </div>`
      : `<div class="hero"><div class="hero-empty">No recommendation yet.</div></div>`;
    state.heroMove = null;
    return;
  }

  const { cat, item } = best;
  const label = rec?.my_turn ? cat.name : `next turn — ${cat.name}`;
  box.innerHTML = `
    <div class="hero" style="--cat:${cat.color}">
      <div class="hero-top">
        <span class="hero-cat">${label}</span>
        ${item.tag ? `<span class="opt-tag tag-${item.tag}">${item.tag}</span>` : ""}
        <span class="hero-score">${(item.score ?? 0).toFixed(1)}</span>
      </div>
      <div class="hero-act"></div>
      ${item.why ? '<div class="hero-why"></div>' : ""}
    </div>`;
  box.querySelector(".hero-act").textContent = item.text;
  if (item.why) box.querySelector(".hero-why").textContent = item.why;
  state.heroMove = item.move || null;
}

const openCats = new Set(["incoming"]);

function renderActions(rec, groups) {
  const box = $("#actions");
  box.replaceChildren();
  // Order: what expires, then what is worth most, then everything empty.
  //
  // Ranking by score only became meaningful once every score was in turns.
  // Before that, placements were weighted pips and an incoming offer carried a
  // hand-tuned scarcity term that could swing it by more than any real move --
  // sorting on that would have floated Respond to the top permanently while
  // looking like a ranking.
  //
  // Two things still outrank value. An offer on the table has a clock on it and
  // is gone if ignored, which no score expresses; and scores move every poll,
  // so a category only overtakes another by a clear margin, or the panel
  // reshuffles under the cursor while you are reading it.
  const STICKY = 0.5;
  const rank = (c) => {
    const best = groups[c.key]?.[0];
    if (!best) return null;
    return Math.round((best.score ?? 0) / STICKY);
  };
  const ranked = [...CATS].sort((a, b) => {
    const ra = rank(a), rb = rank(b);
    if (ra === null && rb === null) return CATS.indexOf(a) - CATS.indexOf(b);
    if (ra === null) return 1;
    if (rb === null) return -1;
    // an offer you can still answer comes first whatever it is worth
    const urgent = (c) => (c.key === "incoming" ? 1 : 0);
    if (urgent(a) !== urgent(b)) return urgent(b) - urgent(a);
    if (ra !== rb) return rb - ra;
    return CATS.indexOf(a) - CATS.indexOf(b);
  });
  for (const cat of ranked) {
    const items = groups[cat.key] || [];
    const wrap = document.createElement("section");
    wrap.className = `cat${items.length ? "" : " empty"}${openCats.has(cat.key) && items.length ? " open" : ""}`;
    wrap.style.setProperty("--cat", cat.color);
    const best = items[0];
    wrap.innerHTML = `
      <div class="cat-head">
        <i class="cat-key"></i>
        <span class="cat-name">${cat.name}</span>
        <span class="cat-best"></span>
        <span class="cat-n">${items.length || ""}</span>
        <span class="cat-chev">${items.length ? "▶" : ""}</span>
      </div>
      <div class="cat-body"></div>`;
    wrap.querySelector(".cat-best").textContent = best ? best.text : "nothing available";

    const body = wrap.querySelector(".cat-body");
    items.forEach((it) => {
      const row = document.createElement("div");
      row.className = "opt";
      row.innerHTML = `<span class="opt-s">${it.score ? it.score.toFixed(1) : "—"}</span>
        <span><span class="opt-t"></span>${
          it.tag ? `<span class="opt-tag tag-${it.tag}">${it.tag}</span>` : ""
        }${it.why ? '<div class="opt-w"></div>' : ""}</span>`;
      row.querySelector(".opt-t").textContent = it.text;
      if (it.why) row.querySelector(".opt-w").textContent = it.why;
      if (it.move) {
        row.addEventListener("mouseenter", () => drawHint(it.move));
        row.addEventListener("mouseleave", () => drawHint(state.heroMove));
      }
      body.appendChild(row);
    });

    if (items.length) {
      wrap.querySelector(".cat-head").addEventListener("click", () => {
        wrap.classList.toggle("open");
        openCats.has(cat.key) ? openCats.delete(cat.key) : openCats.add(cat.key);
      });
    }
    box.appendChild(wrap);
  }
}

// What a player has done at the trade table, as chips. This is the payoff of
// recording responses: "green gives ore, orange never will" is worth more than
// any production estimate, because it already happened.
function tradeRead(hist, color) {
  const rows = [
    ["gives", hist.will_give?.[color], "yes"],
    ["won't", hist.wont_give?.[color], "no"],
    ["needs", hist.wants?.[color], "want"],
  ];
  const html = rows
    .map(([label, counts, cls]) => {
      const chips = Object.entries(counts || {})
        .sort((a, b) => b[1] - a[1])
        .slice(0, 4)
        .map(([r, n]) => `<span class="tchip ${cls}">${RES_ABBR[r] || r}${n > 1 ? `×${n}` : ""}</span>`)
        .join("");
      return chips ? `<span class="tlbl">${label}</span>${chips}` : "";
    })
    .join("");
  return html ? `<div class="ptrade">${html}</div>` : "";
}

function renderIntel(rec) {
  const players = $("#players");
  players.replaceChildren();
  const hist = rec?.trade_history || {};
  (rec?.players || []).forEach((p) => {
    const card = document.createElement("div");
    card.className = `pcard${p.is_me ? " me" : ""}`;
    const known = Object.entries(p.hand?.known || {}).filter(([, n]) => n > 0);
    const chips = known.map(([r, n]) => `<span class="chip ${r}">${n}${RES_ABBR[r]}</span>`).join("");
    const unk = p.hand?.unknown ? `<span class="chip unk">${p.hand.unknown}?</span>` : "";
    const prod = Object.entries(p.production || {})
      .sort((a, b) => Number(a[0]) - Number(b[0]))
      .map(([num, res]) => {
        const txt = Object.entries(res).map(([r, n]) => `${n > 1 ? n : ""}${RES_ABBR[r]}`).join("");
        return `<span class="pnum${num === "6" || num === "8" ? " hot" : ""}"><b>${num}</b>${txt}</span>`;
      }).join("");
    card.innerHTML = `
      <div class="phead"><span class="dot" style="background:var(--${p.color})"></span>
        <span class="nm">${p.color}${p.is_me ? " (you)" : ""}</span>
        <span class="vp">${p.vp_visible} VP</span></div>
      <div class="pstats">${p.settlements}s ${p.cities}c ${p.roads}r · ${p.cards} cards · ${p.dev_cards} dev${
        p.dev_used ? ` (${p.dev_used} played)` : ""}</div>
      <div class="chips">${chips}${unk || (known.length ? "" : '<span class="chip unk">—</span>')}</div>
      ${prod ? `<div class="prod">${prod}</div>` : ""}
      ${p.is_me ? "" : tradeRead(hist, p.color)}`;
    players.appendChild(card);
  });

  const rollsEl = $("#rolls");
  rollsEl.replaceChildren();
  (rec?.rolls || []).slice(-20).forEach((n) => {
    const s = document.createElement("span");
    s.className = `roll-chip${n === 7 ? " seven" : ""}`;
    s.textContent = n;
    rollsEl.appendChild(s);
  });
  const dice = $("#dice");
  dice.replaceChildren();
  if (rec?.dice?.rolls) {
    const { counts, expected } = rec.dice;
    const max = Math.max(1, ...Object.values(counts), ...Object.values(expected || {}));
    for (let n = 2; n <= 12; n++) {
      const c = counts[String(n)] || 0, e = (expected || {})[String(n)] || 0;
      const b = document.createElement("div");
      b.className = "dbar";
      b.title = `${n}: rolled ${c}× (expected ${e})`;
      b.innerHTML = `<div class="stack">
          <div class="exp" style="height:${Math.round((e / max) * 32)}px"></div>
          <div class="bar${n === 7 ? " seven" : n === 6 || n === 8 ? " hot" : ""}"
               style="height:${Math.round((c / max) * 32) + 2}px"></div>
        </div><div class="lbl">${n}</div>`;
      dice.appendChild(b);
    }
    $("#dice-sub").textContent = `${rec.dice.rolls} rolls · cold ${rec.dice.coldest.join(",")}`;
  }
}

function renderPanel(rec) {
  const mineTurn = rec?.my_turn;
  const lbl = $("#turn-label");
  lbl.textContent = rec ? (mineTurn ? "your turn" : `${rec.turn ?? "…"}'s turn`) : "waiting for a game";
  lbl.className = mineTurn ? "mine" : "";
  $("#turn-sub").textContent = rec ? rec.phase : "start a game in the attached browser";

  const t = $("#timer");
  if (rec?.timer) {
    const left = Math.max(0, rec.timer.remaining);
    t.textContent = `${Math.floor(left / 60)}:${String(Math.floor(left % 60)).padStart(2, "0")}`;
    t.className = left <= 10 ? "urgent" : "";
  } else t.textContent = "";

  renderHand(rec);
  renderUrgent(rec);
  const groups = categorise(rec);
  renderPrimary(rec, groups);
  renderActions(rec, groups);
  drawHint(state.heroMove);
  renderPlan(rec);
  renderIntel(rec);
}

const PLAN_LABEL = {
  settlement: "settle", city: "city", dev: "dev cards",
  army: "largest army", longest_road: "longest road",
};

// The route to ten points. Worth its own line even on a turn where nothing is
// affordable: "nothing to do" and "nothing to aim for" are different answers,
// and only the second one is ever really true.
function renderPlan(rec) {
  const el = $("#plan");
  const plan = rec?.plan;
  if (!plan?.steps?.length) {
    el.classList.add("hidden");
    return;
  }
  el.classList.remove("hidden");
  const need = 10 - plan.vp;
  el.innerHTML =
    `<div class="plan-head"><span>${plan.vp} VP · ${need} to go</span>` +
    `<span class="plan-eta">~${plan.turns} turns</span></div>` +
    plan.steps
      .map(
        (s) => `<div class="plan-step">
          <span class="plan-at">t+${s.at}</span>
          <span class="plan-kind">${PLAN_LABEL[s.kind] || s.kind}</span>
          <span class="plan-vp">+${s.vp}</span>
          <span class="plan-where">${s.where || ""}</span>
        </div>`,
      )
      .join("");
}

function renderLog(events) {
  const box = $("#log");
  const j = (a) => (a || []).join(",");
  const fmt = (e) => {
    const w = e.color ? `${e.color} ` : "";
    switch (e.kind) {
      case "dice_rolled": return `▸ ${w}rolled ${e.total}`;
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
}


/* ---------------- polling ---------------- */

function setLive(cls, text) {
  $("#live-dot").className = cls;
  $("#live-text").textContent = text;
}

async function ensureGeometry(hexCount) {
  if (!hexCount || state.geometry?.hex_count === hexCount) return;
  await loadGeometry();
}

async function loadGeometry() {
  state.geometry = await getJSON("/api/geometry");
  if (state.geometry.view_box) {
    $("#board").setAttribute("viewBox", state.geometry.view_box);
  }
}


async function poll() {
  try {
    const status = await getJSON("/api/live/status");
    state.myColor = status.my_color;
    if (!status.connected) { setLive("err", status.error ? "feed error" : "connecting…"); return; }
    if (!status.has_state) {
      setLive("on", status.resyncing
        ? "game in progress — resyncing…"
        : "connected — waiting for a game");
      renderPanel(null);
      return;
    }
    const [st, rec, log] = await Promise.all([
      getJSON("/api/live/state"),
      getJSON("/api/live/moves"),
      getJSON("/api/live/log?limit=200"),
    ]);
    state.config = st.config;
    state.rec = rec;
    // A 5-6 player game is a different board, not a bigger one, and the swap
    // can happen mid-session. Re-fetch the corners before drawing on them.
    await ensureGeometry(st.config?.hexes?.length);
    renderBoard();
    renderPanel(rec);
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
  await loadGeometry();
  await fetch("/api/live/start", { method: "POST" });
  poll();
  state.timer = setInterval(poll, 2000);
}

init();
