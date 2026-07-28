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

/* ---------------- resource art ----------------
   One drawing per resource, in a 48x48 box, used at both sizes: painted into
   the board hexes and stamped on the cards in your hand and in every trade.
   Same picture in both places is the point -- the card you are being offered
   should be recognisably the tile it comes off.

   Flat colour is what the tiles were before, and "which pale green is sheep
   and which is wood" is a question you should never have to ask mid-turn. */

/* Every scene keeps the middle of the box clear. The number token is an opaque
   disc over the centre of the tile, so anything drawn there is simply not
   there -- the first pass put a sheep behind every "8" and all you could see
   was one ear. Subjects go to the sides and the bottom, and the token sits in
   the hole they leave, the way the printed tiles do it. */
const RES_ART = {
  wood: `
    <path d="M0 48 h48 v-7 q-12 -5 -24 -1 q-12 4 -24 0 z" fill="#1a4d2b"/>
    <path d="M1 45 L11 22 L21 45 Z" fill="#14512a"/>
    <path d="M3 33 L11 11 L19 33 Z" fill="#1e6b38"/>
    <rect x="9" y="42" width="4" height="6" fill="#4a3320"/>
    <path d="M27 46 L37.5 20 L48 46 Z" fill="#113f22"/>
    <path d="M29 33 L37.5 8 L46 33 Z" fill="#1a6033"/>
    <rect x="35.5" y="43" width="4" height="5" fill="#4a3320"/>`,
  sheep: `
    <path d="M0 48 h48 v-9 q-24 7 -48 0 z" fill="#6f9e33"/>
    <ellipse cx="14" cy="37" rx="12" ry="8" fill="#f7f9f4"/>
    <circle cx="6"  cy="33" r="4.5" fill="#f7f9f4"/>
    <circle cx="15" cy="30" r="5.5" fill="#f7f9f4"/>
    <rect x="8"  y="43" width="3" height="5" rx="1.5" fill="#453f38"/>
    <rect x="18" y="43" width="3" height="5" rx="1.5" fill="#453f38"/>
    <ellipse cx="27" cy="35" rx="5" ry="6" fill="#453f38"/>
    <ellipse cx="31" cy="31" rx="3" ry="2.2" fill="#332e29"/>
    <circle cx="29" cy="33" r="1.3" fill="#fff"/>
    <ellipse cx="38" cy="12" rx="8" ry="5.5" fill="#eef1e9"/>
    <circle cx="32" cy="9" r="3.5" fill="#eef1e9"/>
    <ellipse cx="45" cy="11" rx="3.4" ry="4" fill="#453f38"/>`,
  wheat: `
    <path d="M0 48 h48 v-8 q-24 6 -48 0 z" fill="#c2951f"/>
    <g stroke="#9c6c1c" stroke-width="2.3" stroke-linecap="round" fill="none">
      <path d="M8 46 V16"/><path d="M40 46 V16"/><path d="M24 48 V34"/>
    </g>
    <g fill="#f5d35a">
      <ellipse cx="8" cy="11" rx="3.4" ry="5"/>
      <ellipse cx="3"  cy="19" rx="5.4" ry="2.8" transform="rotate(-36 3 19)"/>
      <ellipse cx="13" cy="19" rx="5.4" ry="2.8" transform="rotate(36 13 19)"/>
      <ellipse cx="3"  cy="27" rx="5.4" ry="2.8" transform="rotate(-36 3 27)"/>
      <ellipse cx="13" cy="27" rx="5.4" ry="2.8" transform="rotate(36 13 27)"/>
      <ellipse cx="40" cy="11" rx="3.4" ry="5"/>
      <ellipse cx="35" cy="19" rx="5.4" ry="2.8" transform="rotate(-36 35 19)"/>
      <ellipse cx="45" cy="19" rx="5.4" ry="2.8" transform="rotate(36 45 19)"/>
      <ellipse cx="35" cy="27" rx="5.4" ry="2.8" transform="rotate(-36 35 27)"/>
      <ellipse cx="45" cy="27" rx="5.4" ry="2.8" transform="rotate(36 45 27)"/>
      <ellipse cx="24" cy="31" rx="3" ry="4.4"/>
      <ellipse cx="19" cy="38" rx="5" ry="2.6" transform="rotate(-36 19 38)"/>
      <ellipse cx="29" cy="38" rx="5" ry="2.6" transform="rotate(36 29 38)"/>
    </g>`,
  brick: `
    <path d="M0 48 h48 v-9 q-24 6 -48 0 z" fill="#8c4523"/>
    <g fill="#b84d26">
      <rect x="-2" y="30" width="17" height="8.5" rx="1.5"/>
      <rect x="17" y="30" width="17" height="8.5" rx="1.5"/>
      <rect x="36" y="30" width="14" height="8.5" rx="1.5"/>
      <rect x="6"  y="39.5" width="17" height="8.5" rx="1.5"/>
      <rect x="25" y="39.5" width="17" height="8.5" rx="1.5"/>
      <rect x="-2" y="4" width="15" height="8" rx="1.5"/>
      <rect x="35" y="4" width="15" height="8" rx="1.5"/>
      <rect x="5"  y="13.5" width="15" height="8" rx="1.5"/>
      <rect x="28" y="13.5" width="15" height="8" rx="1.5"/>
    </g>
    <g fill="#dd7043" opacity=".8">
      <rect x="-2" y="30" width="17" height="2.4" rx="1"/>
      <rect x="17" y="30" width="17" height="2.4" rx="1"/>
      <rect x="36" y="30" width="14" height="2.4" rx="1"/>
      <rect x="-2" y="4"  width="15" height="2.2" rx="1"/>
      <rect x="35" y="4"  width="15" height="2.2" rx="1"/>
    </g>`,
  ore: `
    <path d="M-4 48 L12 14 L28 48 Z" fill="#71808f"/>
    <path d="M12 14 L18.5 28 L12 31 L5.5 28 Z" fill="#f0f5fa"/>
    <path d="M20 48 L36 9 L52 48 Z" fill="#5a6773"/>
    <path d="M36 9 L42.5 24 L36 27 L29.5 24 Z" fill="#dde5ee"/>
    <g fill="#39434e">
      <path d="M30 44 l4.5 -6.5 4.5 6.5 -4.5 4.5 z"/>
      <path d="M9 45 l3.5 -5 3.5 5 -3.5 3.5 z"/>
    </g>
    <circle cx="34.5" cy="43" r="1.7" fill="#8fd0e8"/>`,
  desert: `
    <circle cx="39" cy="11" r="6" fill="#f7e2ae"/>
    <path d="M0 40 q12 -8 24 -3 q12 5 24 -3 V48 H0 Z" fill="#c4a068"/>
    <rect x="20" y="14" width="6" height="26" rx="3" fill="#3f7a44"/>
    <path d="M21 27 h-6 v-8" stroke="#3f7a44" stroke-width="5" fill="none" stroke-linecap="round"/>
    <path d="M26 22 h6 v-6" stroke="#357038" stroke-width="5" fill="none" stroke-linecap="round"/>`,
};

/** The same drawing as an inline <svg>, for anywhere that isn't the board. */
function resArt(resource, size = 26) {
  return `<svg class="art" viewBox="0 0 48 48" width="${size}" height="${size}"
            aria-hidden="true">${RES_ART[resource] || ""}</svg>`;
}

function hexIcon(resource, cx, cy, clipId) {
  // Scaled up until the scene fills the tile rather than floating in it, and
  // clipped to the tile so it can: the ground each scene stands on is wider
  // than the hex at its base, and without a clip it spills over the edge onto
  // the neighbour.
  // Two groups, not one: a clip-path is resolved in the coordinate system the
  // element itself establishes, so putting both on one <g> scales the clip
  // polygon by 2.15 as well and it stops lining up with the tile. The outer
  // group clips in board space; the inner one does the scaling.
  const outer = el("g", { class: "hex-icon" });
  if (clipId) outer.setAttribute("clip-path", `url(#${clipId})`);
  const inner = el("g", {
    transform: `translate(${cx} ${cy}) scale(1.62) translate(-24 -24)`,
  });
  inner.innerHTML = RES_ART[resource] || "";
  outer.appendChild(inner);
  return outer;
}

/** A resource card the size of a playing card, the way the game draws them. */
function resCard(resource, n, extra = "") {
  return `<div class="rcard ${resource} ${extra}">
            ${resArt(resource, 30)}
            <span class="rcount">${n}</span>
          </div>`;
}

/** A row of cards from a list like ["wood","wood","brick"]. */
function cardRow(list, cls = "") {
  const counts = {};
  for (const r of list || []) counts[r] = (counts[r] || 0) + 1;
  const parts = Object.entries(counts).map(([r, n]) => resCard(r, n, `mini ${cls}`));
  return parts.length ? parts.join("") : '<span class="nocards">nothing</span>';
}

function renderBoard() {
  const svg = $("#board");
  svg.replaceChildren();
  const g = state.geometry, cfg = state.config;
  if (!g || !cfg) return;

  const defs = el("defs");
  const sand = el("g"), tiles = el("g"), ports = el("g");
  const pieces = el("g"), hints = el("g", { class: "hint", id: "hints" });

  // one clip per tile, so each scene stays inside its own hex
  for (const h of g.hexes) {
    const cp = el("clipPath", { id: `clip-h${h.id}` });
    cp.appendChild(el("polygon", { points: h.points }));
    defs.appendChild(cp);
  }
  svg.appendChild(defs);

  // beach: slightly larger hexes behind the tiles
  for (const h of g.hexes) {
    const p = el("polygon", { points: h.points, class: "hex-sand" });
    p.setAttribute("transform", `translate(${h.cx} ${h.cy}) scale(1.14) translate(${-h.cx} ${-h.cy})`);
    sand.appendChild(p);
  }

  for (const h of g.hexes) {
    const t = cfg.hexes[h.id];
    tiles.appendChild(el("polygon", { points: h.points, class: `hex ${t.resource}` }));
    tiles.appendChild(hexIcon(t.resource, h.cx, h.cy, `clip-h${h.id}`));
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

/* Offers used to be a seventh category here. They are now a panel of their own
   above this list, drawn as cards -- the same offer in two places, one of them
   a one-line summary, was the panel's worst duplication. */
const CATS = [
  { key: "place",    name: "Place",    color: "var(--place)" },
  { key: "dev",      name: "Dev card", color: "var(--dev)"   },
  { key: "bank",     name: "Bank",     color: "var(--bank)"  },
  // proposals are drawn as cards in the Trades panel; they stay in the ranking
  // so one can still be the single best thing to do, but they are not listed
  // twice
  { key: "offer",    name: "Offer",    color: "var(--deal)", panel: false },
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
  for (const p of rec.proposals || []) {
    out.offer.push({ score: p.score ?? 0, text: p.text, why: null });
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

const DEV_LABEL = {
  knight: "knight", road_building: "road building",
  year_of_plenty: "year of plenty", monopoly: "monopoly", victory_point: "point",
};

/* The hand lives along the bottom of the screen, where the game puts it and
   where you are already looking. Everything you own is one glance away: the
   resource cards, the development cards, and the two numbers that decide
   whether a 7 hurts. */
function renderHand(rec) {
  const cards = $("#dock-cards"), devbox = $("#dock-dev"), meta = $("#dock-meta");
  const hand = rec?.hand || {};
  const held = Object.entries(hand).filter(([, n]) => n > 0);
  const total = held.reduce((s, [, n]) => s + n, 0);

  cards.innerHTML = held.length
    ? held.map(([r, n]) => resCard(r, n)).join("")
    : '<span class="nocards">no resource cards</span>';

  // Development cards were never shown at all. Buy one and it vanished: the
  // card is unplayable the turn it is bought, so the Dev Card row correctly
  // said "nothing available", and with the card itself invisible that reads as
  // the card having been lost.
  const dev = rec?.my_dev || {};
  const fresh = dev.bought_this_turn || {};
  // which card, if any, we are telling them to play right now -- so the card
  // itself lights up rather than only the sentence about it
  const urge = (rec?.dev_plays || []).find((d) => (d.score ?? 0) > 0);
  const chips = [];
  for (const [card, n] of Object.entries(dev.known || {})) {
    const pending = fresh[card] || 0;
    const ready = n - pending;
    if (ready > 0) {
      const hot = urge && urge.card === card && rec?.my_turn && !dev.played_this_turn;
      chips.push(
        `<div class="dcard${hot ? " play-now" : ""}">
           <span class="dname">${DEV_LABEL[card] || card}</span>
           ${ready > 1 ? `<span class="dn">×${ready}</span>` : ""}
           ${hot ? '<span class="dgo">play now</span>' : ""}
         </div>`);
    }
    if (pending) chips.push(
      `<div class="dcard fresh" title="a card cannot be played the turn it is bought">
         <span class="dname">${DEV_LABEL[card] || card}</span>
         <span class="dn">next turn</span>
       </div>`);
  }
  if (dev.hidden) chips.push(`<div class="dcard unknown"><span class="dname">${dev.hidden} face down</span></div>`);
  if (dev.played_this_turn) chips.push('<div class="dcard spent"><span class="dname">card already played</span></div>');
  devbox.innerHTML = chips.length
    ? `<span class="dock-label">dev</span>` + chips.join("")
    : "";

  const limit = rec?.discard_limit ?? 7;
  const over = total > limit;
  meta.innerHTML = `
    <span class="tally${over ? " over" : ""}">${total}<small>/${limit}</small></span>
    ${over ? `<span class="tally-warn">a 7 costs you ${Math.floor(total / 2)}</span>` : ""}`;
}

/* Trades, as cards. Two lists, both drawn the same way so the eye reads them
   the same way: what to propose, and what has actually happened at the table.
   These were text before -- "orange gave wheat, got sheep" -- which is the
   information but not in the shape you see it on the game screen, so matching
   one against the other meant translating every line. */
function renderTrades(rec) {
  const box = $("#trades");
  const proposals = rec?.proposals || [];
  const log = (rec?.trade_log || []).slice().reverse();   // newest first
  $("#trades-sub").textContent = proposals.length
    ? `${proposals.length} to make` : "";

  const spread = (obj) => Object.entries(obj || {}).flatMap(([r, n]) => Array(n).fill(r));

  const ask = proposals.map((p) => `
    <div class="trow ask">
      <span class="tw ${p.to || ""}">ask ${p.to || "?"}</span>
      <div class="tswap">
        ${cardRow(spread(p.give), "want")}
        <span class="arrow">→</span>
        ${cardRow(spread(p.get), "get")}
      </div>
      ${p.for ? `<span class="tfor">for a ${p.for}</span>` : ""}
      <span class="ts">${(p.score ?? 0).toFixed(1)}</span>
    </div>`).join("");

  const done = log.map((e) => {
    const mine = e.color === state.myColor;
    if (e.kind === "trade_offered") {
      return `<div class="trow offered${mine ? " mine" : ""}">
        <span class="tw ${e.color || ""}">${e.color || "?"} offers</span>
        <div class="tswap">
          ${cardRow(e.offers, "get")}
          <span class="arrow">⇄</span>
          ${cardRow(e.wants, "want")}
        </div>
      </div>`;
    }
    const other = e.kind === "trade_bank" ? "bank" : (e.with || "?");
    return `<div class="trow done${mine ? " mine" : ""}">
      <span class="tw ${e.color || ""}">${e.color || "?"}</span>
      <div class="tswap">
        ${cardRow(e.gave, "want")}
        <span class="arrow">⇄</span>
        ${cardRow(e.got, "get")}
      </div>
      <span class="tfor">with ${other}</span>
    </div>`;
  }).join("");

  box.innerHTML =
    (ask ? `<div class="tgroup"><h4>worth proposing</h4>${ask}</div>` : "") +
    (done ? `<div class="tgroup"><h4>at the table</h4>${done}</div>` : "") ||
    '<div class="tempty">no trades yet</div>';
}

const VERDICT_WORD = {
  accept: "Accept", reject: "Reject", counter: "Counter",
  waiting: "Waiting", cannot: "Can't", yours: "Yours",
};

/* Incoming offers, drawn the way the game draws them -- their cards, an arrow,
   your cards -- with the answer stated as a word rather than a number. These
   sit above everything else because they expire; an offer you read too late is
   the same as an offer you never saw. */
function renderOffers(rec) {
  const box = $("#offers");
  const advice = (rec?.offer_advice || []).filter((a) => a.verdict !== "cannot");
  if (!advice.length) { box.replaceChildren(); box.classList.remove("has"); return; }
  box.classList.add("has");
  // one you have to answer runs on their clock; one you made runs on yours
  advice.sort((a, b) => (a.mine ? 1 : 0) - (b.mine ? 1 : 0));

  box.innerHTML = advice.map((a) => {
    const o = a.offer || {};
    // `offers` is what the creator hands over, `wants` is what they ask for --
    // so which side is yours depends on who made it
    const iGet = a.mine ? o.wants : o.offers;
    const iGive = a.mine ? o.offers : o.wants;
    const counter = a.counter
      ? `<div class="ctr">instead offer
           ${cardRow(Object.entries(a.counter.give || {})
             .flatMap(([r, n]) => Array(n).fill(r)), "want")}</div>`
      : "";
    // who has answered an offer of ours, which is the whole point of showing it
    const said = a.mine
      ? `<div class="said">
           ${(a.accepted || []).map((c) => `<span class="rsp yes ${c}">${c} accepted</span>`).join("")}
           ${(a.declined || []).map((c) => `<span class="rsp no ${c}">${c} declined</span>`).join("")}
           ${!(a.accepted || []).length && !(a.declined || []).length
             ? '<span class="rsp none">no answers yet</span>' : ""}
         </div>`
      : "";
    return `
      <article class="offer v-${a.verdict}">
        <header>
          <span class="who ${a.mine ? "me" : (o.from || "")}">${
            a.mine ? "your offer" : (o.from || "someone")}</span>
          <span class="verdict">${VERDICT_WORD[a.verdict] || a.verdict}</span>
        </header>
        <div class="swap">
          <div class="side"><span class="slab">you get</span>${cardRow(iGet, "get")}</div>
          <span class="arrow">⇄</span>
          <div class="side"><span class="slab">you give</span>${cardRow(iGive, "want")}</div>
        </div>
        ${said}
        <p class="why"></p>
        ${counter}
      </article>`;
  }).join("");

  box.querySelectorAll(".offer").forEach((node, i) => {
    node.querySelector(".why").textContent = advice[i].text || "";
  });
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
  // The race, before anything about our own plan. Being told to build a city
  // while somebody else is one move from ten points is how advice ends up
  // looking like a loss.
  const race = rec?.race;
  if (race?.leader && race.leader_turns <= 4) {
    add(`${race.leader} wins in ~${race.leader_turns.toFixed(0)}`,
        race.behind > 2
          ? `you are ${race.behind.toFixed(0)} turns behind — block or deny, building will not catch up`
          : "close enough to race — keep building");
  }
  if (rec?.pending === "roll") {
    // a knight is the only thing that can happen first, and only sometimes
    // is it worth it -- say which, rather than just "roll"
    const knight = (rec.moves || []).find((m) => m.steps[0].type === "play_knight");
    add("roll the dice", knight && knight.score > 0
      ? `first: ${knight.location_hint}`
      : "nothing else is available until you do");
  }
  // offers used to raise an alert here too; they now have their own panel
  // immediately below, drawn as the cards they actually are
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

const openCats = new Set(["place"]);

function renderActions(rec, groups) {
  const box = $("#actions");
  box.replaceChildren();
  // Order: what is worth most, then everything empty.
  //
  // Ranking by score only became meaningful once every score was in turns.
  // Before that, placements were weighted pips and an incoming offer carried a
  // hand-tuned scarcity term that could swing it by more than any real move --
  // sorting on that would have floated Respond to the top permanently while
  // looking like a ranking.
  //
  // Scores move every poll, so a category only overtakes another by a clear
  // margin, or the panel reshuffles under the cursor while you are reading it.
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
    if (ra !== rb) return rb - ra;
    return CATS.indexOf(a) - CATS.indexOf(b);
  });
  for (const cat of ranked) {
    if (cat.panel === false) continue;
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

// What this player is going for. We work it out anyway to know how fast they
// win; keeping it to ourselves would be a strange way to advise, since which
// corner they want is what decides whether to hurry for it.
function rivalPlan(race, color) {
  const steps = race?.plans?.[color];
  if (!steps?.length) return "";
  const t = race.turns?.[color];
  return `<div class="rival-plan"><span class="tlbl">plan</span>` +
    (t != null && t < 80 ? `<span class="rp-eta">${t.toFixed(0)}t</span>` : "") +
    steps.map((s) => `<span class="rp-step">${PLAN_LABEL[s.kind] || s.kind}` +
      `<i>+${s.vp}</i></span>`).join("") + `</div>`;
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
      ${p.is_me ? "" : tradeRead(hist, p.color)}
      ${p.is_me ? "" : rivalPlan(rec?.race, p.color)}`;
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
  renderOffers(rec);
  const groups = categorise(rec);
  renderPrimary(rec, groups);
  renderActions(rec, groups);
  renderTrades(rec);
  drawHint(state.heroMove);
  renderEngine(rec);
  renderPlan(rec);
  renderIntel(rec);
}

// catanatron's answer, beside ours. Agreement is reassuring; disagreement is
// the interesting case, which is why it is shown even when it differs.
function renderEngine(rec) {
  const box = $("#engine");
  const e = rec?.engine;
  if (!box) return;
  if (!e) { box.classList.add("hidden"); return; }
  box.classList.remove("hidden");
  const ours = state.heroMove?.location_hint || "";
  const agrees = ours && e.where && ours.includes(e.where.slice(0, 18));
  box.innerHTML =
    `<span class="eng-tag${agrees ? " agrees" : ""}">${agrees ? "agrees" : "engine"}</span>` +
    `<span class="eng-text">${e.text}</span>` +
    `<span class="eng-meta">${e.legal_moves} legal · depth ${e.depth}</span>`;
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
  const race = rec?.race;
  const clocks = race
    ? `<div class="plan-race">` +
      Object.entries(race.turns)
        .sort((a, b) => a[1] - b[1])
        .map(([c, t]) => `<span class="clock${c === rec.players?.find(p=>p.is_me)?.color ? " me" : ""}">` +
             `<i style="background:var(--${c})"></i>${t >= 80 ? "—" : t.toFixed(0)}</span>`)
        .join("") + `</div>`
    : "";
  el.innerHTML =
    `<div class="plan-head"><span>${plan.vp} VP · ${need} to go` +
    (plan.strategy ? ` · <b class="plan-strat">${
      {cities: "cities & cards", expand: "settle & expand", mixed: "mixed"}[plan.strategy]
      || plan.strategy}</b>` : "") + `</span>` +
    `<span class="plan-eta">~${plan.turns} turns</span></div>` + clocks +
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
