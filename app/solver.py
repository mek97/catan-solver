"""Heuristic Catan move recommender.

Single-turn evaluation, no lookahead: the user re-runs Solve every turn, so
long-horizon intent lives in the reasoning strings, not a search tree. Scores
are in "weighted pips" -- a build yielding ~1 extra card every ~7 rolls scores
around 5. All tunable weights live in W.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import board, rules
from .models import RESOURCES, BoardConfig, MoveStep, ScoredMove

# production odds exclude 7 -- it pays nobody, it moves the robber
PIPS = {k: v for k, v in rules.PIPS.items() if k != 7}
COSTS = rules.COSTS

W = {
    "prod": 1.0,
    "port": 0.7,
    "expand": 0.5,
    "block": 0.4,
    "trade_discount": 0.9,
}

TOP_N = 8


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _hex_pips(tile) -> int:
    if tile.resource == "desert" or tile.number is None:
        return 0
    return PIPS.get(tile.number, 0)


def _pieces_left(player, kind: str) -> int:
    """Pieces still in a player's supply.

    Upgrading a settlement to a city returns the settlement, so the count on
    the board is the right thing to subtract. The live feed reports this
    directly; the derivation is the fallback for hand-entered boards.
    """
    if player.pieces_left and kind in player.pieces_left:
        return player.pieces_left[kind]
    standing = {
        "settlement": len(player.settlements),
        "city": len(player.cities),
        "road": len(player.roads),
    }[kind]
    return rules.PIECE_SUPPLY[kind] - standing


def _afford(hand: dict[str, int], cost: dict[str, int]) -> bool:
    return all(hand.get(r, 0) >= n for r, n in cost.items())


def _pay(hand: dict[str, int], cost: dict[str, int]) -> dict[str, int]:
    out = dict(hand)
    for r, n in cost.items():
        out[r] = out.get(r, 0) - n
    return out


@dataclass
class Ctx:
    cfg: BoardConfig
    occupied: set[int] = field(default_factory=set)
    my_buildings: set[int] = field(default_factory=set)
    opp_buildings: set[int] = field(default_factory=set)
    my_roads: set[int] = field(default_factory=set)
    all_roads: set[int] = field(default_factory=set)
    my_road_endpoints: set[int] = field(default_factory=set)
    hand: dict[str, int] = field(default_factory=dict)
    my_pips: dict[str, float] = field(default_factory=dict)
    board_pips: dict[str, int] = field(default_factory=dict)
    rates: dict[str, int] = field(default_factory=dict)
    my_vp: int = 0
    vp_mult: float = 1.0
    opp_vp: dict[str, int] = field(default_factory=dict)
    opp_road_len: dict[str, int] = field(default_factory=dict)
    my_road_len: int = 0
    max_opp_knights: int = 0


def _player_pips(cfg: BoardConfig, color: str) -> dict[str, float]:
    p = cfg.players[color]
    pips: dict[str, float] = {r: 0.0 for r in RESOURCES}
    for weight, vids in ((1, p.settlements), (2, p.cities)):
        for v in vids:
            for h in board.VERTEX_HEXES[v]:
                tile = cfg.hexes[h]
                if tile.resource == "desert" or tile.number is None:
                    continue
                # the robber blocks the hex outright -- it produces nothing
                if h == cfg.robber_hex:
                    continue
                pips[tile.resource] += weight * PIPS[tile.number]
    return pips


def _longest_road_length(roads: set[int], enemy: set[int]) -> int:
    if not roads:
        return 0

    def extend(v: int, used: set[int]) -> int:
        best = len(used)
        if v in enemy:
            return best
        for e in board.VERTEX_EDGES[v]:
            if e in roads and e not in used:
                a, b = board.EDGE_VERTICES[e]
                nxt = b if a == v else a
                used.add(e)
                best = max(best, extend(nxt, used))
                used.discard(e)
        return best

    best = 0
    for e in roads:
        a, b = board.EDGE_VERTICES[e]
        best = max(best, extend(a, {e}), extend(b, {e}))
    return best


def build_ctx(cfg: BoardConfig) -> Ctx:
    ctx = Ctx(cfg=cfg)
    me = cfg.me.color
    for color, p in cfg.players.items():
        buildings = set(p.settlements) | set(p.cities)
        ctx.occupied |= buildings
        if color == me:
            ctx.my_buildings = buildings
            ctx.my_roads = set(p.roads)
        else:
            ctx.opp_buildings |= buildings
        ctx.all_roads |= set(p.roads)
    for e in ctx.my_roads:
        ctx.my_road_endpoints.update(board.EDGE_VERTICES[e])
    ctx.hand = {r: cfg.me.hand.get(r, 0) for r in RESOURCES}
    ctx.my_pips = _player_pips(cfg, me)
    ctx.board_pips = {r: 0 for r in RESOURCES}
    for tile in cfg.hexes:
        if tile.resource != "desert" and tile.number is not None:
            ctx.board_pips[tile.resource] += PIPS[tile.number]

    my_ports = {
        port.type
        for port in cfg.ports
        if any(v in ctx.my_buildings for v in port.vertices)
    }
    authoritative = cfg.me.bank_rates or {}
    for r in RESOURCES:
        if r in authoritative:
            ctx.rates[r] = authoritative[r]
        elif r in my_ports:
            ctx.rates[r] = 2
        elif "3:1" in my_ports:
            ctx.rates[r] = 3
        else:
            ctx.rates[r] = 4

    my_p = cfg.players[me]
    ctx.my_vp = (
        len(my_p.settlements)
        + 2 * len(my_p.cities)
        + (2 if my_p.longest_road else 0)
        + (2 if my_p.largest_army else 0)
        + cfg.me.dev_cards.vp
    )
    ctx.vp_mult = 2.0 if ctx.my_vp >= 8 else (1.4 if ctx.my_vp == 7 else 1.0)

    for color, p in cfg.players.items():
        if color == me:
            continue
        vp = (
            len(p.settlements)
            + 2 * len(p.cities)
            + (2 if p.longest_road else 0)
            + (2 if p.largest_army else 0)
        )
        ctx.opp_vp[color] = max(vp, p.vp_visible)
        if p.longest_road_len is not None:
            ctx.opp_road_len[color] = p.longest_road_len
        else:
            enemy = ctx.occupied - set(p.settlements) - set(p.cities)
            ctx.opp_road_len[color] = _longest_road_length(set(p.roads), enemy)
        ctx.max_opp_knights = max(ctx.max_opp_knights, p.knights_played)
    my_reported = cfg.players[me].longest_road_len
    ctx.my_road_len = (
        my_reported if my_reported is not None
        else _longest_road_length(ctx.my_roads, ctx.opp_buildings)
    )
    return ctx


# --- per-vertex value -------------------------------------------------------


def _scarcity(ctx: Ctx, r: str) -> float:
    bp = ctx.board_pips.get(r, 0)
    if bp <= 0:
        return 1.6
    total = sum(ctx.board_pips.values())
    return _clamp(total / (5.0 * bp), 0.6, 1.6)


def _diversity(ctx: Ctx, r: str) -> float:
    mp = ctx.my_pips.get(r, 0.0)
    if mp == 0:
        return 1.4
    return max(1.0, 1.2 - 0.05 * mp)


def vertex_prod(ctx: Ctx, vid: int, raw: bool = False) -> float:
    total = 0.0
    for h in board.VERTEX_HEXES[vid]:
        tile = ctx.cfg.hexes[h]
        pips = _hex_pips(tile)
        if pips == 0:
            continue
        if raw:
            total += pips
        else:
            total += pips * _scarcity(ctx, tile.resource) * _diversity(ctx, tile.resource)
    return total


def _raw_pips(ctx: Ctx, vid: int) -> int:
    return sum(_hex_pips(ctx.cfg.hexes[h]) for h in board.VERTEX_HEXES[vid])


def _vertex_resources(ctx: Ctx, vid: int) -> set[str]:
    return {
        ctx.cfg.hexes[h].resource
        for h in board.VERTEX_HEXES[vid]
        if ctx.cfg.hexes[h].resource != "desert" and ctx.cfg.hexes[h].number is not None
    }


def _port_bonus(ctx: Ctx, vid: int) -> tuple[float, str]:
    for port in ctx.cfg.ports:
        if vid in port.vertices:
            if port.type == "3:1":
                return 1.5, "sits on the 3:1 port"
            res_pips = sum(
                _hex_pips(ctx.cfg.hexes[h])
                for h in board.VERTEX_HEXES[vid]
                if ctx.cfg.hexes[h].resource == port.type
            )
            return 3.0 + 0.4 * (ctx.my_pips.get(port.type, 0.0) + res_pips), (
                f"sits on the 2:1 {port.type} port"
            )
    return 0.0, ""


def _expansion(ctx: Ctx, vid: int) -> float:
    """Value of good, placeable vertices reachable within 2 empty edges of vid."""
    value = 0.0
    occupied_after = ctx.occupied | {vid}
    seen = {vid}
    frontier = [(vid, 0)]
    while frontier:
        v, d = frontier.pop()
        if d >= 2:
            continue
        for e in board.VERTEX_EDGES[v]:
            if e in ctx.all_roads:
                continue
            a, b = board.EDGE_VERTICES[e]
            u = b if a == v else a
            if u in seen:
                continue
            seen.add(u)
            if board.is_vertex_placeable(u, occupied_after):
                value += vertex_prod(ctx, u) * 0.3 / (d + 1)
            frontier.append((u, d + 1))
    return value


def _block_value(ctx: Ctx, vid: int) -> float:
    """Bonus for taking a spot the current VP leader could reach within 2 roads."""
    if not ctx.opp_vp:
        return 0.0
    leader = max(ctx.opp_vp, key=lambda c: ctx.opp_vp[c])
    p = ctx.cfg.players[leader]
    frontier_v = set(p.settlements) | set(p.cities)
    for e in p.roads:
        frontier_v.update(board.EDGE_VERTICES[e])
    candidates: set[int] = set()
    seen = set(frontier_v)
    frontier = [(v, 0) for v in frontier_v]
    while frontier:
        v, d = frontier.pop()
        if d >= 2:
            continue
        for e in board.VERTEX_EDGES[v]:
            if e in ctx.all_roads:
                continue
            a, b = board.EDGE_VERTICES[e]
            u = b if a == v else a
            if u in seen:
                continue
            seen.add(u)
            if board.is_vertex_placeable(u, ctx.occupied):
                candidates.add(u)
            frontier.append((u, d + 1))
    top3 = sorted(candidates, key=lambda v: -vertex_prod(ctx, v))[:3]
    if vid in top3:
        return 0.5 * vertex_prod(ctx, vid)
    return 0.0


# --- move scoring -----------------------------------------------------------


def _settlement_move(ctx: Ctx, vid: int) -> ScoredMove:
    prod = vertex_prod(ctx, vid)
    port, port_note = _port_bonus(ctx, vid)
    expand = _expansion(ctx, vid)
    block = _block_value(ctx, vid)
    score = (
        W["prod"] * prod + W["port"] * port + W["expand"] * expand + W["block"] * block + 2.0
    ) * ctx.vp_mult
    new_res = _vertex_resources(ctx, vid) - {r for r in RESOURCES if ctx.my_pips.get(r, 0) > 0}
    bits = [f"{_raw_pips(ctx, vid)} pips at this corner"]
    if new_res:
        bits.append(f"adds {'/'.join(sorted(new_res))}, which you don't produce yet")
    if port_note:
        bits.append(port_note)
    if block > 0:
        bits.append("also denies the current leader a strong spot")
    if ctx.my_vp >= 8:
        bits.append("and it is a direct victory point at match point")
    reasoning = "Settlement worth building: " + "; ".join(bits) + "."
    return ScoredMove(
        steps=[MoveStep(type="build_settlement", vertex=vid)],
        score=score,
        reasoning=reasoning,
        location_hint=f"settle the {board.describe_vertex(ctx.cfg.hexes, vid)}",
    )


def _city_move(ctx: Ctx, vid: int) -> ScoredMove:
    prod_raw = sum(
        _hex_pips(ctx.cfg.hexes[h]) * _scarcity(ctx, ctx.cfg.hexes[h].resource)
        for h in board.VERTEX_HEXES[vid]
        if _hex_pips(ctx.cfg.hexes[h]) > 0
    )
    score = (W["prod"] * prod_raw + 2.0) * ctx.vp_mult
    reasoning = (
        f"Upgrading to a city doubles {_raw_pips(ctx, vid)} pips of production"
        f" ({', '.join(sorted(_vertex_resources(ctx, vid)))})."
    )
    return ScoredMove(
        steps=[MoveStep(type="build_city", vertex=vid)],
        score=score,
        reasoning=reasoning,
        location_hint=f"upgrade the settlement at the {board.describe_vertex(ctx.cfg.hexes, vid)}",
    )


def _legal_road_edges(ctx: Ctx, roads: set[int] | None = None) -> list[int]:
    roads = ctx.my_roads if roads is None else roads
    endpoints = set()
    for e in roads:
        endpoints.update(board.EDGE_VERTICES[e])
    legal = []
    for eid in range(72):
        if eid in ctx.all_roads or eid in roads:
            continue
        ok = False
        for v in board.EDGE_VERTICES[eid]:
            if v in ctx.my_buildings:
                ok = True
                break
            # continuing my road network through v -- blocked by enemy buildings
            if v in endpoints and v not in ctx.opp_buildings:
                ok = True
                break
        if ok:
            legal.append(eid)
    return legal


def _road_move(ctx: Ctx, eid: int, roads: set[int] | None = None) -> ScoredMove:
    roads = ctx.my_roads if roads is None else roads
    best_new = 0.0
    best_v = None
    for v in board.EDGE_VERTICES[eid]:
        if board.is_vertex_placeable(v, ctx.occupied):
            p = vertex_prod(ctx, v)
            if p > best_new:
                best_new, best_v = p, v
    new_len = _longest_road_length(roads | {eid}, ctx.opp_buildings)
    cur_len = _longest_road_length(roads, ctx.opp_buildings)
    best_opp = max(ctx.opp_road_len.values(), default=0)
    i_hold = ctx.cfg.players[ctx.cfg.me.color].longest_road
    lr_term = 0.0
    lr_note = ""
    if not i_hold and new_len >= rules.LONGEST_ROAD_MIN and new_len > best_opp:
        lr_term = rules.LONGEST_ROAD_VP * ctx.vp_mult
        lr_note = f"takes Longest Road ({new_len} > {best_opp})"
    elif new_len > cur_len:
        lr_term = 0.3
    score = 0.4 * best_new + lr_term
    bits = []
    if best_v is not None:
        bits.append(
            f"opens the {board.describe_vertex(ctx.cfg.hexes, best_v)} for a future settlement"
        )
    if lr_note:
        bits.append(lr_note)
    if not bits:
        bits.append("extends your network")
    return ScoredMove(
        steps=[MoveStep(type="build_road", edge=eid)],
        score=score,
        reasoning="Road: " + "; ".join(bits) + ".",
        location_hint=f"build a road on the {board.describe_edge(ctx.cfg.hexes, eid)}",
    )


def _robber_moves(ctx: Ctx, step_type: str = "move_robber") -> list[ScoredMove]:
    cfg = ctx.cfg
    me = cfg.me.color
    mean_opp = (sum(ctx.opp_vp.values()) / len(ctx.opp_vp)) if ctx.opp_vp else 0.0
    moves = []
    for hid in range(19):
        if hid == cfg.robber_hex:
            continue
        tile = cfg.hexes[hid]
        pips = _hex_pips(tile)
        denied = 0.0
        self_harm = 0.0
        victims = []
        for color, p in cfg.players.items():
            for v, w in [(v, 1) for v in p.settlements] + [(v, 2) for v in p.cities]:
                if hid not in board.VERTEX_HEXES[v]:
                    continue
                if color == me:
                    self_harm += pips * w
                else:
                    lw = _clamp(1 + 0.25 * (ctx.opp_vp.get(color, 0) - mean_opp), 0.7, 2.0)
                    denied += pips * w * lw
                    if p.resource_count >= 1 and color not in victims:
                        victims.append(color)
        best_victim = None
        steal = 0.0
        for color in victims:
            ev = min(1.0, 0.25 * cfg.players[color].resource_count)
            if ev > steal or best_victim is None:
                steal, best_victim = ev, color
        score = denied - self_harm + steal
        bits = [f"blocks {board.hex_label(cfg.hexes, hid)} production for your opponents"]
        if best_victim:
            bits.append(f"steal from {best_victim} ({cfg.players[best_victim].resource_count} cards)")
        if self_harm > 0:
            bits.append("note: it also touches one of your own spots")
        moves.append(
            ScoredMove(
                steps=[MoveStep(type=step_type, robber_hex=hid, steal_from=best_victim)],
                score=score,
                reasoning="Robber: " + "; ".join(bits) + ".",
                location_hint=f"move the robber to {board.describe_hex(cfg.hexes, hid)}"
                + (f" and steal from {best_victim}" if best_victim else ""),
            )
        )
    moves.sort(key=lambda m: -m.score)
    return moves


def _best_builds(ctx: Ctx, hand: dict[str, int]) -> list[ScoredMove]:
    """All atomic build moves affordable with `hand` (no dev plays, no trades)."""
    out = []
    cfg = ctx.cfg
    my_p = cfg.players[cfg.me.color]
    if _afford(hand, COSTS["settlement"]) and _pieces_left(my_p, "settlement") > 0:
        for vid in range(54):
            if board.is_vertex_placeable(vid, ctx.occupied) and vid in ctx.my_road_endpoints:
                out.append(_settlement_move(ctx, vid))
    if _afford(hand, COSTS["city"]) and _pieces_left(my_p, "city") > 0:
        for vid in my_p.settlements:
            out.append(_city_move(ctx, vid))
    if _afford(hand, COSTS["road"]) and _pieces_left(my_p, "road") > 0:
        for eid in _legal_road_edges(ctx):
            out.append(_road_move(ctx, eid))
    if _afford(hand, COSTS["dev"]):
        army_race = (
            cfg.players[cfg.me.color].knights_played >= ctx.max_opp_knights
            and not cfg.players[cfg.me.color].largest_army
        )
        odds = rules.dev_card_odds()
        # a 25-card deck is 14 knights and 5 victory points, so a card is
        # mostly an army play and sometimes a hidden point -- which is worth
        # far more when a hidden point actually wins the game
        vp_now = odds["victory_point"] * (4.0 if ctx.my_vp >= rules.VICTORY_POINTS_TO_WIN - 1 else 1.0)
        score = 1.2 + 2.0 * vp_now + (1.0 if army_race else 0.0)
        out.append(
            ScoredMove(
                steps=[MoveStep(type="buy_dev")],
                score=score,
                reasoning=(
                    f"Buy a development card: {odds['knight']:.0%} knight, "
                    f"{odds['victory_point']:.0%} a hidden victory point"
                    + (" -- and you are in the Largest Army race" if army_race else "")
                    + "."
                ),
                location_hint="buy a development card",
            )
        )
    return out


def _cost_of(step: MoveStep) -> dict[str, int]:
    return {
        "build_settlement": COSTS["settlement"],
        "build_city": COSTS["city"],
        "build_road": COSTS["road"],
        "buy_dev": COSTS["dev"],
    }.get(step.type, {})


def _dev_plays(ctx: Ctx) -> list[ScoredMove]:
    cfg = ctx.cfg
    dc = cfg.me.dev_cards
    if cfg.me.dev_card_played_this_turn or cfg.me.dev_card_bought_this_turn:
        # v1 simplification: a freshly bought card locks all plays this turn.
        return []
    out: list[ScoredMove] = []
    if dc.knight >= 1:
        robber = _robber_moves(ctx, step_type="play_knight")
        if robber:
            best = robber[0]
            kp = cfg.players[cfg.me.color].knights_played
            takes_army = (
                kp + 1 >= rules.LARGEST_ARMY_MIN
                and kp + 1 > ctx.max_opp_knights
                and not cfg.players[cfg.me.color].largest_army
            )
            army_term = rules.LARGEST_ARMY_VP * ctx.vp_mult if takes_army else 0.5
            best = best.model_copy(deep=True)
            best.score += army_term
            best.reasoning = "Play a knight: " + best.reasoning[len("Robber: "):]
            if takes_army:
                best.reasoning += " Playing it also takes Largest Army (2 VP)."
            best.location_hint = "play your knight, then " + best.location_hint
            out.append(best)
    if dc.road_building >= 1 and _pieces_left(cfg.players[cfg.me.color], "road") >= 2:
        legal1 = _legal_road_edges(ctx)
        if legal1:
            first = max(legal1, key=lambda e: _road_move(ctx, e).score)
            roads2 = ctx.my_roads | {first}
            legal2 = [e for e in _legal_road_edges(ctx, roads2) if e != first]
            edges = [first] + (
                [max(legal2, key=lambda e: _road_move(ctx, e, roads2).score)] if legal2 else []
            )
            score = sum(_road_move(ctx, e).score for e in edges) + 0.5
            out.append(
                ScoredMove(
                    steps=[MoveStep(type="play_road_building", edges=edges)],
                    score=score,
                    reasoning="Road Building: two free roads toward your best expansion spots.",
                    location_hint="play Road Building; "
                    + " then ".join(
                        f"build on the {board.describe_edge(cfg.hexes, e)}" for e in edges
                    ),
                )
            )
    if dc.year_of_plenty >= 1:
        best_combo = None
        for kind in ("settlement", "city", "road", "dev"):
            cost = COSTS[kind]
            missing: dict[str, int] = {}
            short = 0
            for r, n in cost.items():
                lack = n - ctx.hand.get(r, 0)
                if lack > 0:
                    missing[r] = lack
                    short += lack
            if 0 < short <= 2 and all(
                ctx.cfg.bank is None
                or ctx.cfg.bank.get(r, rules.BANK_PER_RESOURCE) >= n
                for r, n in missing.items()
            ):
                builds = _best_builds(ctx, _pay({**ctx.hand, **{
                    r: ctx.hand.get(r, 0) + missing.get(r, 0) for r in RESOURCES
                }}, {}))
                builds = [b for b in builds if _cost_of(b.steps[0]) == cost]
                if builds:
                    b = max(builds, key=lambda m: m.score)
                    cand = (b.score * W["trade_discount"], missing, b)
                    if best_combo is None or cand[0] > best_combo[0]:
                        best_combo = cand
        if best_combo:
            score, missing, b = best_combo
            out.append(
                ScoredMove(
                    steps=[MoveStep(type="play_year_of_plenty", get=missing)] + b.steps,
                    score=score,
                    reasoning=(
                        f"Year of Plenty for {', '.join(f'{n} {r}' for r, n in missing.items())} "
                        f"unlocks a build right now. {b.reasoning}"
                    ),
                    location_hint=f"play Year of Plenty (take {', '.join(missing)}), then {b.location_hint}",
                )
            )
        else:
            scarce = sorted(RESOURCES, key=lambda r: ctx.my_pips.get(r, 0))[:2]
            out.append(
                ScoredMove(
                    steps=[MoveStep(type="play_year_of_plenty", get={r: 1 for r in scarce})],
                    score=1.0,
                    reasoning=f"Year of Plenty: take {scarce[0]} and {scarce[1]}, your scarcest resources.",
                    location_hint=f"play Year of Plenty and take {scarce[0]} + {scarce[1]}",
                )
            )
    if dc.monopoly >= 1 and ctx.opp_vp:
        best_r, best_ev = None, 0.0
        for r in RESOURCES:
            ev = 0.0
            for color in ctx.opp_vp:
                p = ctx.cfg.players[color]
                opp_pips = _player_pips(ctx.cfg, color)
                total = sum(opp_pips.values())
                if total > 0:
                    ev += p.resource_count * (opp_pips[r] / total)
            if ev > best_ev:
                best_ev, best_r = ev, r
        if best_r:
            score = 0.8 * best_ev
            gained = round(best_ev)
            hand2 = dict(ctx.hand)
            hand2[best_r] = hand2.get(best_r, 0) + gained
            enabled = [
                b for b in _best_builds(ctx, hand2) if not _afford(ctx.hand, _cost_of(b.steps[0]))
            ]
            note = ""
            if enabled:
                b = max(enabled, key=lambda m: m.score)
                score += 0.5 * b.score
                note = f" That haul likely funds: {b.location_hint}."
            out.append(
                ScoredMove(
                    steps=[MoveStep(type="play_monopoly", resource=best_r)],
                    score=score,
                    reasoning=f"Monopoly on {best_r}: expect roughly {best_ev:.1f} cards from opponents.{note}",
                    location_hint=f"play Monopoly and name {best_r}",
                )
            )
    return out


def _trade_combos(ctx: Ctx) -> list[ScoredMove]:
    out: list[ScoredMove] = []

    bank = ctx.cfg.bank

    def in_stock(res: str, n: int = 1) -> bool:
        # an empty bank cannot pay out; colonist reports the stock, and when
        # it doesn't we assume the resource is available
        return bank is None or bank.get(res, rules.BANK_PER_RESOURCE) >= n

    def single_trades(hand: dict[str, int]):
        for give in RESOURCES:
            rate = ctx.rates[give]
            if hand.get(give, 0) >= rate:
                for get in RESOURCES:
                    if get != give and in_stock(get):
                        yield give, get, rate

    def apply(hand, give, get, rate):
        h = dict(hand)
        h[give] -= rate
        h[get] = h.get(get, 0) + 1
        return h

    def newly_enabled(hand2):
        return [
            b
            for b in _best_builds(ctx, hand2)
            if not _afford(ctx.hand, _cost_of(b.steps[0])) and b.steps[0].type != "build_road"
        ]

    def trade_step(give, get, rate):
        return MoveStep(type="trade_bank", give={give: rate}, get={get: 1})

    seen: set[tuple] = set()
    for g1, t1, r1 in single_trades(ctx.hand):
        h1 = apply(ctx.hand, g1, t1, r1)
        for b in newly_enabled(h1):
            key = ("1", b.steps[0].type, b.steps[0].vertex)
            if key in seen:
                continue
            seen.add(key)
            out.append(
                ScoredMove(
                    steps=[trade_step(g1, t1, r1)] + b.steps,
                    score=b.score * W["trade_discount"],
                    reasoning=f"Trade {r1} {g1} for 1 {t1} at the bank to unlock a build. {b.reasoning}",
                    location_hint=f"bank-trade {r1} {g1} -> 1 {t1}, then {b.location_hint}",
                )
            )
        for g2, t2, r2 in single_trades(h1):
            h2 = apply(h1, g2, t2, r2)
            for b in newly_enabled(h2):
                key = ("2", b.steps[0].type, b.steps[0].vertex)
                if key in seen:
                    continue
                seen.add(key)
                out.append(
                    ScoredMove(
                        steps=[trade_step(g1, t1, r1), trade_step(g2, t2, r2)] + b.steps,
                        score=b.score * W["trade_discount"] ** 2,
                        reasoning=(
                            f"Two bank trades ({g1}->{t1}, {g2}->{t2}) unlock a build. {b.reasoning}"
                        ),
                        location_hint=(
                            f"bank-trade {r1} {g1} -> 1 {t1} and {r2} {g2} -> 1 {t2}, then {b.location_hint}"
                        ),
                    )
                )
    # discard-risk dump
    if sum(ctx.hand.values()) >= 8:
        trades = list(single_trades(ctx.hand))
        if trades:
            give, _, rate = max(trades, key=lambda t: ctx.hand.get(t[0], 0))
            # take whatever we make least of that the bank can actually pay out
            wanted = [r for r in RESOURCES if r != give and in_stock(r)]
            if not wanted:
                return out
            get = min(wanted, key=lambda r: ctx.my_pips.get(r, 0))
            out.append(
                ScoredMove(
                    steps=[trade_step(give, get, rate)],
                    score=0.8,
                    reasoning=(
                        f"You hold {sum(ctx.hand.values())} cards -- a 7 would cost you half. "
                        f"Trade down surplus {give} before ending the turn."
                    ),
                    location_hint=f"bank-trade {rate} {give} -> 1 {get} to shrink your hand",
                )
            )
    return out


def _build_chains(ctx: Ctx, atomic: list[ScoredMove]) -> list[ScoredMove]:
    builds = [
        m
        for m in sorted(atomic, key=lambda m: -m.score)
        if m.steps[0].type in ("build_settlement", "build_city", "build_road", "buy_dev")
    ][:6]
    out = []
    for i, m1 in enumerate(builds):
        c1 = _cost_of(m1.steps[0])
        hand2 = _pay(ctx.hand, c1)
        for m2 in builds[i + 1:]:
            s1, s2 = m1.steps[0], m2.steps[0]
            if (s1.vertex is not None and s1.vertex == s2.vertex) or (
                s1.edge is not None and s1.edge == s2.edge
            ):
                continue
            if _afford(hand2, _cost_of(s2)):
                out.append(
                    ScoredMove(
                        steps=[s1, s2],
                        score=m1.score + m2.score,
                        reasoning=f"You can afford both this turn. {m1.reasoning} {m2.reasoning}",
                        location_hint=f"{m1.location_hint}; then {m2.location_hint}",
                    )
                )
                if len(out) >= 3:
                    return out
    return out


def _setup_moves(ctx: Ctx) -> list[ScoredMove]:
    cfg = ctx.cfg
    my_p = cfg.players[cfg.me.color]
    first_numbers: set[int] = set()
    if cfg.phase == "setup2" and my_p.settlements:
        for h in board.VERTEX_HEXES[my_p.settlements[0]]:
            if cfg.hexes[h].number is not None:
                first_numbers.add(cfg.hexes[h].number)
    moves = []
    for vid in range(54):
        if not board.is_vertex_placeable(vid, ctx.occupied):
            continue
        prod = vertex_prod(ctx, vid)
        port, port_note = _port_bonus(ctx, vid)
        expand = _expansion(ctx, vid)
        overlap = len(
            {cfg.hexes[h].number for h in board.VERTEX_HEXES[vid] if cfg.hexes[h].number}
            & first_numbers
        )
        score = W["prod"] * prod * 1.2 + W["port"] * port + W["expand"] * expand - 0.5 * overlap
        # free road: point at the best placeable vertex one step beyond
        best_edge, best_val = None, -1.0
        for e in board.VERTEX_EDGES[vid]:
            if e in ctx.all_roads:
                continue
            a, b = board.EDGE_VERTICES[e]
            u = b if a == vid else a
            val = max(
                (
                    vertex_prod(ctx, w)
                    for w in board.VERTEX_ADJ[u]
                    if w != vid and board.is_vertex_placeable(w, ctx.occupied | {vid})
                ),
                default=0.0,
            )
            if val > best_val:
                best_val, best_edge = val, e
        steps = [MoveStep(type="setup_settlement", vertex=vid)]
        hint = f"settle the {board.describe_vertex(cfg.hexes, vid)}"
        if best_edge is not None:
            steps.append(MoveStep(type="setup_road", edge=best_edge))
            hint += f", road on the {board.describe_edge(cfg.hexes, best_edge)}"
        new_res = sorted(_vertex_resources(ctx, vid))
        bits = [f"{_raw_pips(ctx, vid)} pips ({', '.join(new_res)})"]
        if port_note:
            bits.append(port_note)
        if overlap:
            bits.append(f"note: {overlap} number(s) overlap your first settlement")
        moves.append(
            ScoredMove(
                steps=steps,
                score=score,
                reasoning="Setup placement: " + "; ".join(bits) + ".",
                location_hint=hint,
            )
        )
    moves.sort(key=lambda m: -m.score)
    return moves[:TOP_N]


def solve(cfg: BoardConfig) -> list[ScoredMove]:
    ctx = build_ctx(cfg)
    if cfg.pending == "move_robber":
        return _robber_moves(ctx)[:TOP_N]
    if cfg.phase in ("setup1", "setup2"):
        return _setup_moves(ctx)

    moves: list[ScoredMove] = []
    atomic = _best_builds(ctx, ctx.hand)
    moves.extend(atomic)
    moves.extend(_dev_plays(ctx))
    moves.extend(_trade_combos(ctx))
    moves.extend(_build_chains(ctx, atomic))
    moves.append(
        ScoredMove(
            steps=[MoveStep(type="end_turn")],
            score=0.1,
            reasoning="Nothing worth doing -- bank your cards and end the turn.",
            location_hint="end your turn",
        )
    )

    # dedupe on the final step, keep the best-scoring variant
    best: dict[tuple, ScoredMove] = {}
    for m in moves:
        last = m.steps[-1]
        key = (
            last.type,
            last.vertex,
            last.edge,
            tuple(last.edges or []),
            last.robber_hex,
            last.resource,
            len(m.steps),
        )
        if key not in best or m.score > best[key].score:
            best[key] = m
    ranked = sorted(best.values(), key=lambda m: -m.score)
    return ranked[:TOP_N]
