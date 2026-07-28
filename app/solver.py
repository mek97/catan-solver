"""Catan move recommender.

Two stages, deliberately separated. This module *generates* candidate moves --
what is legal, affordable, and worth a second look -- using cheap board
heuristics (pips, scarcity, reachability) tuned by W. Then `app.economy` prices
each one by applying it and asking how much sooner the game is won.

That split is the whole design. Heuristics are good at "is this worth
considering" and bad at "is this better than that", because a number in
weighted pips cannot be compared to a victory point. So nothing here ranks
anything: a move's score is the turns it takes off the race to ten points,
which is the only currency the game actually settles in.

The one exception is the robber, which is priced in turns added to the people
it blocks -- see `_score_robber`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from . import board, economy, rules
from .models import RESOURCES, BoardConfig, MoveStep, MyState, ScoredMove

# production odds exclude 7 -- it pays nobody, it moves the robber
PIPS = {k: v for k, v in rules.PIPS.items() if k != 7}
COSTS = rules.COSTS

# Generation weights, in weighted pips. These no longer rank anything -- solve()
# replaces every score with turns off the race to ten points -- but they are not
# decoration either: they order candidates inside a family, and _trade_combos
# picks which trade to enumerate by them. Scaling them all changes nothing;
# changing them relative to each other changes what gets considered.
W = {
    "prod": 1.0,
    "port": 0.7,
    "expand": 0.5,
    "block": 0.4,
    "trade_discount": 0.9,
}

TOP_N = 8

# Two settlements on the same number boom and bust together, and the turns
# model averages that difference away -- it sees expected cards, not the
# variance. Charged here in turns per overlapping pip.
OVERLAP_TURNS = 0.12

# awards exactly one player can hold
EXCLUSIVE = ("longest_road", "army")

# Most a single robber placement may be credited with costing one opponent.
# Blocking the only hex of a thin position can look like it ends their game,
# and without a ceiling a knight outscores every build on the board. The
# robber also only sits there until the next 7, which no delta reflects.
ROBBER_CAP = 4.0


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
    # Colonist reports the rates you have *now*, which is authoritative about
    # rule variants our port parsing cannot know about. But a candidate move
    # that settles a port would change them, and the reported number knows
    # nothing about a settlement that has not been placed -- taking it verbatim
    # priced every port at zero in live games, so the solver never once saw a
    # reason to settle one. Take the better of the two: simulation only ever
    # adds buildings, so the derived rate can only improve on reality.
    authoritative = cfg.me.bank_rates or {}
    for r in RESOURCES:
        derived = 2 if r in my_ports else (3 if "3:1" in my_ports else 4)
        reported = authoritative.get(r)
        ctx.rates[r] = min(reported, derived) if reported is not None else derived

    my_p = cfg.players[me]
    # Counted from the board, but never below what the player is reported to
    # hold: colonist's figure includes hidden point cards and awards, and for
    # anyone but us the board is all we can see. Reading it from buildings
    # alone told the race that a rival on seven points still needed nine of
    # them, so the front-runner looked like the least urgent player at the
    # table -- and the robber went after whoever was least dangerous.
    ctx.my_vp = max(
        len(my_p.settlements)
        + 2 * len(my_p.cities)
        + (2 if my_p.longest_road else 0)
        + (2 if my_p.largest_army else 0)
        + cfg.me.dev_cards.vp,
        my_p.vp_visible,
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
    for eid in range(len(board.EDGE_VERTICES)):
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
    for hid in range(len(cfg.hexes)):
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
        for vid in range(len(board.VERTICES)):
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
    if cfg.me.dev_card_played_this_turn:
        # One development card per turn, which colonist reports directly.
        # Buying does NOT lock the rest of the hand: only the card just bought
        # is unplayable, and the engine has already subtracted it.
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
            # The card gives two cards, always. Taking only what the build is
            # short of throws the other half away when the build needs one --
            # so the spare goes to whatever we produce least of, provided the
            # bank has it.
            stock = ctx.cfg.bank
            missing = dict(missing)
            while sum(missing.values()) < 2:
                spare = next(
                    (r for r in sorted(RESOURCES, key=lambda x: ctx.my_pips.get(x, 0))
                     if stock is None
                     or stock.get(r, rules.BANK_PER_RESOURCE) > missing.get(r, 0)),
                    None,
                )
                if spare is None:
                    break
                missing[spare] = missing.get(spare, 0) + 1
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
            # You take these from the bank, so the bank has to have them. The
            # branch above checks that; this one did not, and would happily
            # name a resource the bank had run out of.
            stock = ctx.cfg.bank
            scarce = [
                r for r in sorted(RESOURCES, key=lambda r: ctx.my_pips.get(r, 0))
                if stock is None or stock.get(r, rules.BANK_PER_RESOURCE) > 0
            ][:2]
            if scarce:
                take = {r: 1 for r in scarce}
                out.append(
                    ScoredMove(
                        steps=[MoveStep(type="play_year_of_plenty", get=take)],
                        score=1.0,
                        reasoning=(
                            "Year of Plenty: take "
                            + " and ".join(scarce)
                            + ", your scarcest resources."
                        ),
                        location_hint="play Year of Plenty and take " + " + ".join(scarce),
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
            gained = int(round(best_ev))
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
                    steps=[MoveStep(
                        type="play_monopoly",
                        resource=best_r,
                        # the haul goes in `get` as well as `resource`: the
                        # name is what the player types into colonist, but
                        # without the cards attached the evaluation applies a
                        # move that changes nothing and prices it at zero
                        get={best_r: gained} if gained else None,
                    )],
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


def _starting_cards(ctx: Ctx, vid: int) -> dict[str, int]:
    """Cards the *second* settlement pays out immediately, per the setup rules.

    One card per adjacent resource hex (the desert pays nothing). The first
    settlement pays nothing at all.
    """
    cards: dict[str, int] = {}
    for h in board.VERTEX_HEXES[vid]:
        tile = ctx.cfg.hexes[h]
        if tile.resource != "desert" and tile.number is not None:
            cards[tile.resource] = cards.get(tile.resource, 0) + 1
    return cards


def _opening_build(cards: dict[str, int]) -> tuple[float, str]:
    """What those starting cards let you do on turn one."""
    if _afford(cards, COSTS["settlement"]):
        return 3.0, "and pays a full settlement on turn one"
    if _afford(cards, COSTS["city"]):
        return 2.5, "and pays a city on turn one"
    if _afford(cards, COSTS["road"]):
        return 1.5, "and pays a road immediately"
    if _afford(cards, COSTS["dev"]):
        return 1.2, "and pays a development card immediately"
    return 0.0, ""


def _draft_gap(cfg: BoardConfig) -> Optional[int]:
    """How many settlements go down between your two opening placements.

    Setup runs down the seating order and then back up it, so the player who
    picks first also picks last. Seat i of N places at position i and again at
    2N-1-i, which leaves 2N-2i-2 placements in between: six for the first seat
    of a four-player game, none at all for the last, who picks twice in a row.

    That difference is the whole strategy of the opening. Picking first means
    taking the best corner on the board and then choosing your second from what
    six placements have left; picking last means choosing a *pair*, with no risk
    that the other half is taken. A solver that ranks corners in isolation is
    answering a question nobody is being asked.

    Returns None when the seating is unknown, or when there is nothing left to
    look ahead to.
    """
    order = cfg.play_order
    if not order or cfg.me.color not in order or cfg.phase != "setup1":
        return None
    n = len(order)
    return max(0, 2 * n - 2 * order.index(cfg.me.color) - 2)


def _draft_forecast(ctx: Ctx, mine: int, gap: int) -> list[int]:
    """The corners still open when your second pick comes round.

    Rivals are assumed to take the best remaining corner each time, by the same
    production measure we use ourselves. They will not all agree with it, but
    "the good spots go first" is the part that matters, and being wrong about
    *which* good spot costs far less than pretending none of them go.
    """
    occupied = set(ctx.occupied) | {mine}
    open_now = [
        v for v in range(len(board.VERTICES))
        if board.is_vertex_placeable(v, occupied)
    ]
    open_now.sort(key=lambda v: -vertex_prod(ctx, v))
    for _ in range(gap):
        if not open_now:
            break
        taken = open_now.pop(0)
        occupied.add(taken)
        open_now = [v for v in open_now if board.is_vertex_placeable(v, occupied)]
    return open_now


def _setup_moves(ctx: Ctx) -> list[ScoredMove]:
    cfg = ctx.cfg
    my_p = cfg.players[cfg.me.color]
    second = cfg.phase == "setup2"
    first_numbers: set[int] = set()
    if second and my_p.settlements:
        for h in board.VERTEX_HEXES[my_p.settlements[0]]:
            if cfg.hexes[h].number is not None:
                first_numbers.add(cfg.hexes[h].number)
    moves = []
    overlap_penalties: list[float] = []
    for vid in range(len(board.VERTICES)):
        if not board.is_vertex_placeable(vid, ctx.occupied):
            continue
        prod = vertex_prod(ctx, vid)
        port, port_note = _port_bonus(ctx, vid)
        expand = _expansion(ctx, vid)
        # Doubling up on a number is worse the better the number is: two 8s
        # boom and bust together, two 12s barely matter.
        shared = {
            cfg.hexes[h].number for h in board.VERTEX_HEXES[vid] if cfg.hexes[h].number
        } & first_numbers
        overlap = len(shared)
        overlap_cost = sum(PIPS.get(n, 0) for n in shared) * 0.35

        # only the second settlement pays out, so only it gets the bonus
        opening, opening_note = (0.0, "")
        cards: dict[str, int] = {}
        if second:
            cards = _starting_cards(ctx, vid)
            opening, opening_note = _opening_build(cards)

        score = (
            W["prod"] * prod * 1.2 + W["port"] * port + W["expand"] * expand
            - overlap_cost + opening
        )
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
        if second and cards:
            starting = ", ".join(f"{n} {r}" for r, n in sorted(cards.items()))
            bits.append(f"starts you with {starting}" + (f" {opening_note}" if opening_note else ""))
        if overlap:
            bits.append(
                f"note: {overlap} number(s) overlap your first settlement "
                f"({', '.join(str(n) for n in sorted(shared))})"
            )
        moves.append(
            ScoredMove(
                steps=steps,
                score=score,
                reasoning="Setup placement: " + "; ".join(bits) + ".",
                location_hint=hint,
                # carried so the rescoring below can charge it; the turns model
                # works in expectations and cannot see correlated numbers
            )
        )
        overlap_penalties.append(OVERLAP_TURNS * sum(PIPS.get(n, 0) for n in shared))

    # Rank openings by the game they lead to, not by the corner in isolation.
    # An opening is only ever worth the race it sets up.
    #
    # Every candidate starts from the same position, so there is no "before" to
    # subtract and the raw figure is the projection itself. It is measured
    # against a hopeless start so the number stays positive and comparable, and
    # the projection a player actually cares about goes in the reasoning.
    gap = _draft_gap(cfg)
    scored = []
    for m, penalty in zip(moves, overlap_penalties):
        nxt = _after(cfg, m.steps)
        note = ""
        if gap is not None:
            # Judge the first placement by the pair it can still become. The
            # corner you take is only half the decision; the other half is what
            # survives until you choose again.
            left = _draft_forecast(ctx, m.steps[0].vertex, gap)
            if left:
                partner = max(left, key=lambda v: vertex_prod(build_ctx(nxt), v))
                nxt = _after(nxt, [MoveStep(type="setup_settlement", vertex=partner)])
                note = (
                    f" You pick again after {gap} more placements"
                    if gap
                    else " You pick again immediately, so choose both together"
                )
                if gap:
                    note += (
                        f", by when the best left looks like the "
                        f"{board.describe_vertex(cfg.hexes, partner)}"
                    )
                note += "."
        after = economy.turns_to_win(nxt, build_ctx(nxt))
        scored.append(
            m.model_copy(
                update={
                    "score": round(2 * economy.HORIZON - after - penalty, 2),
                    "reasoning": (
                        f"{m.reasoning[:-1]}; projects a win in ~{after:.0f} turns.{note}"
                    ),
                }
            )
        )
    scored.sort(key=lambda m: -m.score)
    return scored[:TOP_N]


def _steal_moves(ctx: Ctx) -> list[ScoredMove]:
    """Whom to rob, once the robber has landed.

    The card is already spent and the hex already chosen, so what is left is a
    single question: of the players touching this hex, who hurts most for
    losing a card. The front-runner, mostly -- a card taken from whoever is
    furthest behind changes nothing about who wins -- and among equals the one
    holding most, since that is the best chance of taking something they need.
    """
    cfg = ctx.cfg
    contest = race(cfg)
    hid = cfg.robber_hex
    out = []
    for color, p in cfg.players.items():
        if color == cfg.me.color or not p.resource_count:
            continue
        if not any(hid in board.VERTEX_HEXES[v] for v in p.settlements + p.cities):
            continue
        # closer to winning is worth more; theirs is the clock that binds
        threat = contest["turns"].get(color, economy.LOST)
        urgency = _clamp((economy.LOST - threat) / economy.LOST, 0.0, 1.0)
        out.append(
            ScoredMove(
                steps=[MoveStep(type="steal", steal_from=color, robber_hex=hid)],
                score=round(4.0 * urgency + 0.1 * p.resource_count, 2),
                reasoning=(
                    f"{color} holds {p.resource_count} cards and needs about "
                    f"{threat:.0f} more turns to win"
                    + (" -- the closest of anyone here." if color == contest["leader"] else ".")
                ),
                location_hint=f"steal from {color}",
            )
        )
    out.sort(key=lambda m: -m.score)
    return out or [
        ScoredMove(steps=[MoveStep(type="steal", robber_hex=hid)], score=0.0,
                   reasoning="Nobody on this hex is holding a card.",
                   location_hint="no one to steal from")
    ]


def _free_road_moves(ctx: Ctx) -> list[ScoredMove]:
    """Where to put a road you are not paying for.

    Road Building, mid-placement. The cards are already spent, so these are
    ranked on what the road opens rather than what it costs -- which is also
    why they cannot be priced by the usual difference: paying nothing, every
    one of them would score the same.
    """
    cfg = ctx.cfg
    base = economy.turns_to_win(cfg, ctx)
    out = []
    for eid in _legal_road_edges(ctx):
        nxt = cfg.model_copy(deep=True)
        me = nxt.players[nxt.me.color]
        me.roads.append(eid)
        me.longest_road_len = None
        gain = base - economy.turns_to_win(nxt, build_ctx(nxt))
        out.append(
            ScoredMove(
                steps=[MoveStep(type="build_road", edge=eid)],
                score=round(gain, 2),
                reasoning=_road_move(ctx, eid).reasoning,
                location_hint=f"free road on the {board.describe_edge(cfg.hexes, eid)}",
            )
        )
    out.sort(key=lambda m: -m.score)
    return out[:TOP_N]


def _pre_roll_moves(ctx: Ctx) -> list[ScoredMove]:
    """What you may do before the dice: any one development card, or roll.

    The rule is that a card may be played at any point in your turn, the roll
    included -- not that a knight is special. Offering only knights here left a
    player holding Road Building with nothing to do but roll, which reads as
    the app saying the card cannot be played at all.

    A knight is still the card with a reason to go first: played afterwards it
    cannot undo the 7 that has already happened, and if the robber is sitting
    on your own best hex, moving it before the roll is what makes the roll pay.
    """
    cfg = ctx.cfg
    plays = _dev_plays(ctx)
    knights = _score_robber(ctx, [m for m in plays if m.steps[0].type == "play_knight"])
    others = [m for m in plays if m.steps[0].type != "play_knight"]
    # every hex is a knight placement, so the list is long enough to push the
    # roll off the end -- and rolling is what happens on almost every turn
    out: list[ScoredMove] = (knights + others)[: TOP_N - 1]
    blocked = any(
        cfg.robber_hex in board.VERTEX_HEXES[v]
        for v in ctx.my_buildings
    )
    out.append(
        ScoredMove(
            steps=[MoveStep(type="roll_dice")],
            score=0.0,
            reasoning=(
                "Roll the dice -- nothing else is available until you do."
                + (
                    " The robber is on one of your hexes: a knight played now"
                    " frees it before the roll pays out."
                    if blocked and cfg.me.dev_cards.knight >= 1
                    else ""
                )
            ),
            location_hint="roll the dice",
        )
    )
    out.sort(key=lambda m: -m.score)
    return out[:TOP_N]


def _after(cfg: BoardConfig, steps: list[MoveStep]) -> BoardConfig:
    """The position a move leaves behind.

    Built by copy-and-mutate rather than by re-validating: a candidate is
    already known to be legal, and BoardConfig's validator is the expensive
    part of building one.
    """
    nxt = cfg.model_copy(deep=True)
    me = nxt.players[nxt.me.color]
    hand = {r: nxt.me.hand.get(r, 0) for r in RESOURCES}

    def pay(cost: dict[str, int]) -> None:
        for r, n in cost.items():
            hand[r] = hand.get(r, 0) - n

    for s in steps:
        if s.type in ("build_settlement", "setup_settlement"):
            if s.vertex is not None:
                me.settlements.append(s.vertex)
            if s.type == "build_settlement":
                pay(COSTS["settlement"])
        elif s.type == "build_city":
            if s.vertex is not None:
                if s.vertex in me.settlements:
                    me.settlements.remove(s.vertex)
                me.cities.append(s.vertex)
            pay(COSTS["city"])
        elif s.type in ("build_road", "setup_road"):
            if s.edge is not None:
                me.roads.append(s.edge)
            if s.type == "build_road":
                pay(COSTS["road"])
        elif s.type == "buy_dev":
            pay(COSTS["dev"])
            me.dev_card_count += 1
        elif s.type == "play_road_building":
            me.roads.extend(s.edges or [])
            nxt.me.dev_cards.road_building = max(0, nxt.me.dev_cards.road_building - 1)
        elif s.type == "play_knight":
            me.knights_played += 1
            nxt.me.dev_cards.knight = max(0, nxt.me.dev_cards.knight - 1)
            if s.robber_hex is not None:
                nxt.robber_hex = s.robber_hex
        elif s.type == "move_robber":
            if s.robber_hex is not None:
                nxt.robber_hex = s.robber_hex
        elif s.type in ("trade_bank", "play_year_of_plenty", "play_monopoly"):
            for r, n in (s.give or {}).items():
                hand[r] = hand.get(r, 0) - n
            for r, n in (s.get or {}).items():
                hand[r] = hand.get(r, 0) + n
            if s.type == "play_year_of_plenty":
                nxt.me.dev_cards.year_of_plenty = max(0, nxt.me.dev_cards.year_of_plenty - 1)
            elif s.type == "play_monopoly":
                nxt.me.dev_cards.monopoly = max(0, nxt.me.dev_cards.monopoly - 1)

    nxt.me.hand = {r: max(0, n) for r, n in hand.items()}
    if any(s.type in ("build_road", "setup_road", "play_road_building") for s in steps):
        # the reported longest road is now stale; force a recount
        me.longest_road_len = None
    return nxt


def _turns_saved(cfg: BoardConfig, base: float, move: ScoredMove) -> float:
    """What a move is worth: the turns it takes off the race to 10 points."""
    nxt = _after(cfg, move.steps)
    return base - economy.turns_to_win(nxt, build_ctx(nxt))


def _opponent_turns(cfg: BoardConfig, color: str) -> float:
    """Turns the given opponent needs, judged the same way we judge ourselves.

    Their hand is hidden, so it is taken as empty. That understates them by a
    turn or two, but the robber cares about the *difference* an obstruction
    makes, and a constant bias cancels out of a difference.
    """
    view = cfg.model_copy(deep=True)
    view.me = MyState(color=color, hand={})
    return economy.turns_to_win(view, build_ctx(view))


def race(cfg: BoardConfig) -> dict[str, Any]:
    """Everyone's clock and everyone's route, with the awards shared out once.

    turns_to_win answers "how fast can I reach ten points". That is only the
    right question if nobody else is trying: judged that way a player nine
    turns behind still gets told to build their fourth city, because the
    measure cannot see that the game ends first.

    Two passes, because Longest Road and Largest Army go to exactly one player.
    Asked in isolation every ladder takes Longest Road first -- six cards for
    two points is the best rate on the board, and each player computes the
    roads it needs against today's map, so all four conclude they are three
    roads away. Only one of them is. The first pass finds who genuinely gets
    there first; the second re-plans everybody else without it, which is what
    turns three identical road plans into the cities and knights the table is
    actually building.
    """
    views = {}
    for color in cfg.players:
        view = cfg.model_copy(deep=True)
        view.me = MyState(
            color=color,  # type: ignore[arg-type]
            hand=dict(cfg.me.hand) if color == cfg.me.color else {},
            bank_rates=cfg.me.bank_rates if color == cfg.me.color else None,
        )
        views[color] = (view, build_ctx(view))

    # pass 1: nobody contests anything, purely to see who arrives first
    claim: dict[str, tuple[str, float]] = {}
    for color, (view, ctx) in views.items():
        for step in economy.plan(view, ctx):
            kind = step["kind"]
            if kind in EXCLUSIVE and step["at"] < claim.get(kind, (None, economy.LOST))[1]:
                claim[kind] = (color, step["at"])

    # pass 2: everyone but the first claimant plans without it
    turns: dict[str, float] = {}
    plans: dict[str, list[dict]] = {}
    for color, (view, ctx) in views.items():
        due = {k: at for k, (who, at) in claim.items() if who != color}
        turns[color] = economy.turns_to_win(view, ctx, deadlines=due)
        route = economy.plan(view, ctx, deadlines=due)
        if color != cfg.me.color:
            plans[color] = [
                {**r, "where": board.describe_vertex(cfg.hexes, r["vertex"])
                 if r["vertex"] is not None else ""}
                for r in route[:2]
            ]

    mine = turns.get(cfg.me.color, economy.LOST)
    rivals = {c: t for c, t in turns.items() if c != cfg.me.color}
    leader = min(rivals, key=lambda c: rivals[c], default=None)
    return {
        "turns": turns,
        "plans": plans,
        # what we may not count on, because somebody else gets there first
        "deadlines": {k: at for k, (who, at) in claim.items() if who != cfg.me.color},
        "claims": {k: who for k, (who, _at) in claim.items()},
        "mine": mine,
        "leader": leader,
        "leader_turns": rivals.get(leader, economy.LOST) if leader else economy.LOST,
        "behind": mine - rivals[leader] if leader else 0.0,
    }


def _denial_weight(behind: float) -> float:
    """How much a turn taken off the leader is worth against one we save.

    Level with the field, our own progress is what wins; far enough behind, it
    is the only thing that can. Slowing the leader does not win the game, so
    this never exceeds parity -- it decides which of two moves to prefer, not
    whether to stop playing.
    """
    return _clamp(0.25 + 0.12 * behind, 0.25, 1.0)


def _dev_card_value(ctx: Ctx, base: float, due: dict) -> float:
    """What an unknown development card is worth, in turns.

    Buying one scored exactly nothing before -- the same as ending the turn.
    The position after the purchase is three cards lighter and holds a card
    whose face nobody can see, so turns_to_win has nothing to price, and every
    card the deck is mostly made of went uncredited.

    A deck is a distribution, though, and its two useful faces are both
    measurable. A hidden point is a real point. A knight is worth the turns it
    takes off whoever it blocks, plus the ground it makes up towards Largest
    Army -- which is the whole reason to keep buying while somebody else is
    ahead on the board.
    """
    cfg = ctx.cfg
    odds = rules.dev_card_odds()

    point = cfg.model_copy(deep=True)
    point.me.dev_cards.vp += 1
    vp_gain = base - economy.turns_to_win(point, build_ctx(point), deadlines=due)

    knight = cfg.model_copy(deep=True)
    knight.players[cfg.me.color].knights_played += 1
    army_gain = base - economy.turns_to_win(knight, build_ctx(knight), deadlines=due)

    # the best thing that knight could do to somebody, if we drew it
    denial = max(
        (m.score for m in _score_robber(ctx, _robber_moves(ctx, step_type="play_knight"))),
        default=0.0,
    )
    return odds["victory_point"] * vp_gain + odds["knight"] * (army_gain + max(0.0, denial))


def _score_robber(ctx: Ctx, moves: list[ScoredMove]) -> list[ScoredMove]:
    """Re-price robber placements as turns taken off opponents, not pips.

    Blocking a hex is only ever worth what it costs the people it blocks, so
    the measure is their own turns-to-win, recomputed with the robber sitting
    on it. Cheap because a placement can only affect the players who have
    built on that hex.
    """
    cfg = ctx.cfg
    me = cfg.me.color
    others = [c for c in cfg.players if c != me]
    if not others:
        return moves
    baseline = {c: _opponent_turns(cfg, c) for c in others}
    field = min(baseline.values())
    my_base = economy.turns_to_win(cfg, ctx)

    out = []
    for m in moves:
        hid = m.steps[0].robber_hex
        if hid is None:
            out.append(m)
            continue
        nxt = _after(cfg, m.steps)
        # Only the front-runner's clock binds. Slowing third place by four
        # turns does not bring the game any closer to us -- and summing the
        # setback across everyone the hex touches valued one placement at
        # eleven turns, more than winning outright. What a block is worth is
        # how much longer the *fastest* remaining opponent now needs, which
        # also prices the case where blocking the leader merely promotes
        # somebody else: then nothing has changed and it scores nothing.
        after = dict(baseline)
        for c in others:
            if any(
                hid in board.VERTEX_HEXES[v]
                for v in cfg.players[c].settlements + cfg.players[c].cities
            ):
                after[c] = _opponent_turns(nxt, c)
        cost_to_them = _clamp(min(after.values()) - field, 0.0, ROBBER_CAP)
        mine = _clamp(my_base - economy.turns_to_win(nxt, build_ctx(nxt)),
                      -ROBBER_CAP, ROBBER_CAP)
        steal = 0.35 if m.steps[0].steal_from else 0.0
        out.append(m.model_copy(update={"score": round(cost_to_them + mine + steal, 2)}))
    out.sort(key=lambda m: -m.score)
    return out


def solve(cfg: BoardConfig) -> list[ScoredMove]:
    ctx = build_ctx(cfg)
    if cfg.pending == "move_robber":
        return _score_robber(ctx, _robber_moves(ctx))[:TOP_N]
    if cfg.phase in ("setup1", "setup2"):
        return _setup_moves(ctx)
    if cfg.pending == "roll":
        return _pre_roll_moves(ctx)
    if cfg.pending == "place_road":
        return _free_road_moves(ctx)
    if cfg.pending == "steal":
        return _steal_moves(ctx)

    # Two stages, and the order between them matters. Generation scores in
    # weighted pips: good enough to decide what is worth considering, and to
    # break ties inside a family of candidates, but not comparable to a victory
    # point. Pricing then replaces every score with turns off the race.
    #
    # Chains are built from the *priced* atomic moves rather than the raw ones,
    # so the pair worth making is chosen by the measure that ranks it. Picking
    # them by pips first let the old objective veto, before the real one ever
    # saw them, exactly the moves the turns model exists to find.
    contest = race(cfg)
    weight, leader = _denial_weight(contest["behind"]), contest["leader"]
    # an award a rival takes first is not ours to plan around
    due = contest["deadlines"]
    base = economy.turns_to_win(cfg, ctx, deadlines=due)
    dev_worth = _dev_card_value(ctx, base, due)

    def priced(candidates: list[ScoredMove]) -> list[ScoredMove]:
        out = []
        for m in candidates:
            nxt = _after(cfg, m.steps)
            gain = base - economy.turns_to_win(nxt, build_ctx(nxt), deadlines=due)
            # Taking a corner can cost the leader more than it saves us, and
            # measuring only our own clock made those moves look like losses.
            # Only placements are checked: nothing else we build reaches them.
            if leader and any(s.vertex is not None for s in m.steps):
                gain += weight * (_opponent_turns(nxt, leader) - contest["leader_turns"])
            if m.steps[-1].type == "buy_dev":
                gain += dev_worth
            out.append(m.model_copy(update={"score": round(gain, 2)}))
        return out

    dev = _dev_plays(ctx)
    atomic = priced(_best_builds(ctx, ctx.hand))
    moves: list[ScoredMove] = list(atomic)
    moves += priced([m for m in dev if m.steps[0].type != "play_knight"])
    moves += priced(_trade_combos(ctx))
    moves += priced(_build_chains(ctx, atomic))
    # the robber is the one thing not measured against our own clock
    moves += _score_robber(ctx, [m for m in dev if m.steps[0].type == "play_knight"])

    moves.append(
        ScoredMove(
            steps=[MoveStep(type="end_turn")],
            score=0.0,
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
