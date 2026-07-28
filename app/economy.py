"""How many turns a position needs to win.

Catan is a race to 10 victory points, so every judgement worth making reduces
to one question: does this move get me there sooner? The old scoring answered a
different question -- how much production does this add -- which is a decent
proxy early and actively wrong late. It would rank a road that opens a fat
corner above a city that wins the game, because 13 weighted pips beat a flat
+2, and at 8 points with nothing affordable it had nothing to say at all.

So the unit here is *turns*, and the model has three layers:

    production_rate   cards you collect per turn of your own
    turns_to_afford   turns until a given cost is payable, trades included
    turns_to_win      turns to climb the rest of the victory-point ladder

A move is then worth the turns it saves: evaluate the position, apply the move,
evaluate again. Roads earn credit exactly when they shorten a settlement's
arrival, ports when they improve a conversion that is actually on the path, and
the endgame weights itself, because the ladder is short and only points remain.

Everything here is an estimate and the estimates are deliberately simple: the
opponent's dice are not simulated, cards stolen are not modelled, and the ladder
is greedy rather than optimal. The point is a ranking that reflects the real
objective, not a forecast.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Optional

from . import board, rules
from .models import RESOURCES, BoardConfig

COSTS = rules.COSTS
PIPS = {k: v for k, v in rules.PIPS.items() if k != 7}

INF = float("inf")

# Ceiling on any single wait. Beyond this the estimate is meaningless anyway,
# and a finite number keeps a hopeless option comparable to a merely bad one.
HORIZON = 40.0

# What a position worth nothing evaluates to. Every estimate is capped here so
# that "no way through" is never scored better than "a long way through": when
# the dead end was priced as a fixed offset instead, a slow climb could total
# more than it, and the robber concluded that blocking its own best hex was the
# strongest move on the board.
LOST = 2 * HORIZON

# How far ahead of your road network a settlement spot is still worth counting.
MAX_ROAD_REACH = 3

# A victory point card is 5 of 25, so a point costs about five cards on average.
# Knights are 14 of 25, so three of them -- Largest Army -- take about five too.
DEV_CARDS_PER_VP = rules.DEV_DECK_SIZE / rules.DEV_DECK["victory_point"]
DEV_CARDS_PER_KNIGHT = rules.DEV_DECK_SIZE / rules.DEV_DECK["knight"]


def production_rate(cfg: BoardConfig, color: str) -> dict[str, float]:
    """Expected cards per resource, per turn *of your own*.

    A number's pips out of 36 is its chance on one roll, and with N players a
    round brings N rolls before your turn comes back around. Counting in your
    own turns is what makes the result comparable to a build you are deciding
    on now. The robber blocks its hex outright.
    """
    rolls_per_turn = max(1, len(cfg.players))
    rate = {r: 0.0 for r in RESOURCES}
    p = cfg.players.get(color)
    if p is None:
        return rate
    for yields_, vids in ((1, p.settlements), (2, p.cities)):
        for v in vids:
            for h in board.VERTEX_HEXES[v]:
                tile = cfg.hexes[h]
                if tile.resource == "desert" or tile.number is None:
                    continue
                if h == cfg.robber_hex:
                    continue
                rate[tile.resource] += yields_ * PIPS[tile.number] / 36.0 * rolls_per_turn
    return rate


def _vertex_rate(cfg: BoardConfig, vid: int, yields_: int = 1) -> dict[str, float]:
    """What one corner pays per turn, at settlement (1) or city (2) yield."""
    rolls_per_turn = max(1, len(cfg.players))
    out = {r: 0.0 for r in RESOURCES}
    for h in board.VERTEX_HEXES[vid]:
        tile = cfg.hexes[h]
        if tile.resource == "desert" or tile.number is None or h == cfg.robber_hex:
            continue
        out[tile.resource] += yields_ * PIPS[tile.number] / 36.0 * rolls_per_turn
    return out


def affordable(have: dict[str, float], cost: dict[str, int], rates: dict[str, int]) -> bool:
    """Can this hand pay this cost, converting surplus at the bank or a port?

    Surplus is whatever a resource holds beyond what the cost itself needs, so
    cards earmarked for the purchase are never traded away to fund it.
    """
    short = sum(max(0, n - int(have.get(r, 0))) for r, n in cost.items())
    if short == 0:
        return True
    spare = 0
    for r in RESOURCES:
        surplus = int(have.get(r, 0)) - cost.get(r, 0)
        if surplus > 0:
            spare += surplus // rates.get(r, rules.BANK_RATE)
    return spare >= short


def _covered(have: dict[str, float], cost: dict[str, int], rates: dict[str, int]) -> bool:
    """The same question asked of an *expected* hand, so it can answer in halves.

    Whole cards are what you build with, but "2.4 wheat by now" is the honest
    description of a position you are still waiting on, and rounding it to 2
    throws away exactly the resolution that separates two candidate moves.
    """
    short = sum(max(0.0, n - have.get(r, 0.0)) for r, n in cost.items())
    if short <= 0:
        return True
    spare = sum(
        max(0.0, have.get(r, 0.0) - cost.get(r, 0)) / rates.get(r, rules.BANK_RATE)
        for r in RESOURCES
    )
    return spare >= short


def turns_to_afford(
    have: dict[str, float],
    rate: dict[str, float],
    cost: dict[str, int],
    rates: dict[str, int],
    horizon: float = HORIZON,
) -> float:
    """Turns until `cost` is payable, collecting `rate` each turn.

    Found by bisection rather than by dividing each shortfall by its rate,
    because trading makes the answer non-linear: four spare sheep become the
    missing ore the moment the fourth sheep lands, and no per-resource division
    sees that. Waiting longer never makes a cost less payable, so the predicate
    is monotone and bisection is exact to the tolerance.
    """
    if affordable(have, cost, rates):
        return 0.0
    if not any(rate.get(r, 0.0) > 0 for r in RESOURCES):
        return INF

    def covered_at(t: float) -> bool:
        return _covered(
            {r: have.get(r, 0.0) + rate.get(r, 0.0) * t for r in RESOURCES}, cost, rates
        )

    if not covered_at(horizon):
        return INF
    lo, hi = 0.0, horizon
    for _ in range(24):
        mid = (lo + hi) / 2
        if covered_at(mid):
            hi = mid
        else:
            lo = mid
    return hi


def _pay(have: dict[str, float], cost: dict[str, int], rates: dict[str, int]) -> dict[str, float]:
    """Hand left after buying. Trades are charged, not tracked card by card."""
    out = {r: float(have.get(r, 0)) for r in RESOURCES}
    short = 0
    for r, n in cost.items():
        take = min(out[r], float(n))
        out[r] -= take
        short += n - take
    # fund the shortfall from whatever is most plentiful at its own rate
    while short > 0:
        best = max(RESOURCES, key=lambda r: out[r] / rates.get(r, rules.BANK_RATE))
        price = rates.get(best, rules.BANK_RATE)
        if out[best] < price:
            break
        out[best] -= price
        short -= 1
    return out


# --- the victory-point ladder ------------------------------------------------


@dataclass
class _Rung:
    """One step up the ladder: what it costs, what it pays, what it changes."""

    kind: str
    cost: dict[str, int]
    vp: int
    gain: dict[str, float] = field(default_factory=dict)
    vertex: Optional[int] = None


@dataclass
class _Position:
    """Just enough of a position to walk the ladder forward."""

    cfg: BoardConfig
    hand: dict[str, float]
    rate: dict[str, float]
    rates: dict[str, int]
    vp: int
    settlements_left: int
    cities_left: int
    roads_left: int
    upgradeable: list[int]       # my settlements, each a candidate city
    claimed: set[int]            # every corner I hold, real or simulated
    spots: dict[int, list[int]]  # vertex -> the road edges still needed for it
    laid: set[int]               # roads the ladder has already committed to
    knights: int
    knights_for_army: int  # 0 when Largest Army is out of reach
    roads_for_longest: int  # 0 when Longest Road is out of reach


def _road_paths(cfg: BoardConfig, color: str, extra: set[int]) -> dict[int, list[int]]:
    """The roads needed to reach each open, legal corner, edge by edge.

    Paths rather than distances, because roads are shared infrastructure. If
    every settlement were charged its own road count, then laying one road --
    which shortens the way to *several* corners at once -- would appear to
    discount all of them, and the solver would rank a road above the settlement
    it is trying to reach. Knowing which edges a corner needs lets the ladder
    add them to the network once and re-measure from there.

    An opponent's building blocks the way through: that is what severs a road
    network in this game.
    """
    occupied: set[int] = set()
    taken: set[int] = set()
    for p in cfg.players.values():
        occupied |= set(p.settlements) | set(p.cities)
        taken |= set(p.roads)
    mine = cfg.players[color]
    others = occupied - set(mine.settlements) - set(mine.cities)
    network = set(mine.roads) | extra
    taken -= network

    start: set[int] = set(mine.settlements) | set(mine.cities)
    for e in network:
        start.update(board.EDGE_VERTICES[e])

    # Cost is roads you still have to buy, so riding your own network is free.
    # Counting hops instead would strand the far end of a long road past the
    # reach limit, and the ladder would conclude it cannot settle anywhere.
    paths: dict[int, list[int]] = {v: [] for v in start}
    frontier = deque((v, 0) for v in start)
    while frontier:
        v, d = frontier.popleft()
        if len(paths[v]) < d or v in others:
            continue  # stale entry, or an opponent's building bars the way
        for e in board.VERTEX_EDGES[v]:
            if e in taken:
                continue
            a, b = board.EDGE_VERTICES[e]
            u = b if a == v else a
            free = e in network
            step = paths[v] if free else paths[v] + [e]
            if len(step) > MAX_ROAD_REACH:
                continue
            if u in paths and len(paths[u]) <= len(step):
                continue
            paths[u] = step
            # zero-cost hops go to the front so the search stays ordered by cost
            (frontier.appendleft if free else frontier.append)((u, len(step)))
    return {
        v: p for v, p in paths.items()
        if board.is_vertex_placeable(v, occupied)
    }


def _rungs(pos: _Position) -> list[_Rung]:
    """Every way to gain a victory point from here."""
    out: list[_Rung] = []
    cfg = pos.cfg

    if pos.cities_left > 0:
        for v in pos.upgradeable:
            # a city collects twice, so the *extra* is one more settlement's worth
            out.append(_Rung("city", COSTS["city"], 1, _vertex_rate(cfg, v), v))

    if pos.settlements_left > 0:
        for v, path in pos.spots.items():
            if len(path) > pos.roads_left:
                continue
            cost = dict(COSTS["settlement"])
            for r, n in COSTS["road"].items():
                cost[r] = cost.get(r, 0) + n * len(path)
            out.append(_Rung("settlement", cost, 1, _vertex_rate(cfg, v), v))

    # a point out of the deck: five cards on average, bought one at a time
    dev_cost = {r: int(round(n * DEV_CARDS_PER_VP)) for r, n in COSTS["dev"].items()}
    out.append(_Rung("dev", dev_cost, 1))

    if pos.knights_for_army > 0:
        n = int(round(pos.knights_for_army * DEV_CARDS_PER_KNIGHT))
        out.append(_Rung("army", {r: c * n for r, c in COSTS["dev"].items()},
                         rules.LARGEST_ARMY_VP))

    if pos.roads_for_longest > 0 and pos.roads_left >= pos.roads_for_longest:
        n = pos.roads_for_longest
        out.append(_Rung("longest_road", {r: c * n for r, c in COSTS["road"].items()},
                         rules.LONGEST_ROAD_VP))
    return out


def _apply_rung(pos: _Position, rung: _Rung, turns: float) -> None:
    """Advance the position as if the rung had been climbed."""
    for r in RESOURCES:
        pos.hand[r] = pos.hand.get(r, 0.0) + pos.rate.get(r, 0.0) * turns
    pos.hand = _pay(pos.hand, rung.cost, pos.rates)
    pos.vp += rung.vp
    for r, n in rung.gain.items():
        pos.rate[r] = pos.rate.get(r, 0.0) + n

    if rung.kind == "city":
        pos.cities_left -= 1
        pos.upgradeable = [v for v in pos.upgradeable if v != rung.vertex]
        # a city still stands on the corner: it stays claimed
    elif rung.kind == "settlement":
        pos.settlements_left -= 1
        if rung.vertex is not None:
            # the corner is mine now, and it becomes a future city
            pos.upgradeable = pos.upgradeable + [rung.vertex]
            pos.claimed.add(rung.vertex)
            path = pos.spots.get(rung.vertex, [])
            pos.roads_left -= len(path)
            # those roads stay on the board: every later corner measures from
            # the network that now includes them
            pos.laid |= set(path)
        pos.spots = _reachable(pos)
    elif rung.kind == "army":
        pos.knights_for_army = 0
    elif rung.kind == "longest_road":
        pos.roads_left -= pos.roads_for_longest
        pos.roads_for_longest = 0


def _reachable(pos: _Position) -> dict[int, list[int]]:
    """Open corners and their road paths, measured from the grown network.

    The distance rule has to be applied to the ladder's own settlements too,
    not just the ones already on the board. Without this a single road looks
    like it opens two corners -- both ends of the new edge -- and the ladder
    happily settles both, which the rules forbid and which made a road score
    higher than the settlement it was reaching for.
    """
    blocked = set(pos.claimed)
    for v in pos.claimed:
        blocked.update(board.VERTEX_ADJ[v])
    return {
        v: path
        for v, path in _road_paths(pos.cfg, pos.cfg.me.color, pos.laid).items()
        if v not in blocked
    }


def _build_position(cfg: BoardConfig, ctx) -> _Position:
    me = cfg.me.color
    p = cfg.players[me]
    left = p.pieces_left or {}

    def supply(kind: str, standing: int) -> int:
        if kind in left:
            return left[kind]
        return rules.PIECE_SUPPLY[kind] - standing

    knights_for_army = 0
    if not p.largest_army:
        need = max(rules.LARGEST_ARMY_MIN, ctx.max_opp_knights + 1) - p.knights_played
        knights_for_army = max(0, need)

    roads_for_longest = 0
    if not p.longest_road:
        target = max(rules.LONGEST_ROAD_MIN, max(ctx.opp_road_len.values(), default=0) + 1)
        roads_for_longest = max(0, target - ctx.my_road_len)

    return _Position(
        cfg=cfg,
        hand={r: float(cfg.me.hand.get(r, 0)) for r in RESOURCES},
        rate=production_rate(cfg, me),
        rates=dict(ctx.rates),
        vp=ctx.my_vp,
        settlements_left=supply("settlement", len(p.settlements)),
        cities_left=supply("city", len(p.cities)),
        roads_left=supply("road", len(p.roads)),
        upgradeable=list(p.settlements),
        claimed=set(p.settlements) | set(p.cities),
        spots={},
        laid=set(),
        knights=p.knights_played,
        knights_for_army=knights_for_army,
        roads_for_longest=roads_for_longest,
    )


def _seed_spots(pos: _Position) -> _Position:
    pos.spots = _reachable(pos)
    return pos


def _climb(cfg: BoardConfig, ctx, record: bool = False) -> tuple[float, list[dict]]:
    """Estimated turns to reach 10 victory points, and optionally the route.

    Greedy: repeatedly take whichever point is cheapest in turns-per-point, and
    let each one pay for the next -- a city bought for its ore also speeds up
    everything after it. Greedy is not optimal, but it is stable, which matters
    more here: the score of a move is a *difference* between two of these, so a
    consistent bias on both sides cancels out.
    """
    pos = _seed_spots(_build_position(cfg, ctx))
    total = 0.0
    route: list[dict] = []
    while pos.vp < rules.VICTORY_POINTS_TO_WIN:
        best: Optional[tuple[float, float, _Rung]] = None
        for rung in _rungs(pos):
            t = turns_to_afford(pos.hand, pos.rate, rung.cost, pos.rates)
            if t == INF:
                continue
            # Only points you still need count. Dividing by the full value
            # would rank a two-point award above a one-point city at nine
            # points, where the city wins outright and the eleventh point is
            # worth nothing.
            worth = min(rung.vp, rules.VICTORY_POINTS_TO_WIN - pos.vp)
            per_vp = (t + 1.0) / worth  # +1: the build itself takes a turn
            if best is None or per_vp < best[0]:
                best = (per_vp, t, rung)
        if best is None:
            return LOST, route  # no way through: the worst an estimate can be
        _, t, rung = best
        if record:
            route.append({
                "kind": rung.kind,
                "vertex": rung.vertex,
                "vp": rung.vp,
                "at": round(total + t + 1.0, 1),
                "cost": {r: n for r, n in rung.cost.items() if n},
            })
        total += t + 1.0
        _apply_rung(pos, rung, t)
        if total >= LOST:
            break
    return min(total, LOST), route


def turns_to_win(cfg: BoardConfig, ctx) -> float:
    """Estimated turns to reach 10 victory points from this position."""
    return _climb(cfg, ctx)[0]


def plan(cfg: BoardConfig, ctx) -> list[dict]:
    """The fastest route to 10 points we can see from here.

    Worth showing even when no move is affordable: "nothing to do this turn" is
    not the same as "nothing to aim for", and at nine points holding two cards
    the second is the only thing the player actually wants to know.
    """
    return _climb(cfg, ctx, record=True)[1]
