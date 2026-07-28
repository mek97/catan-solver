"""Playing the game out, many times, to see who actually wins.

The ladder in `app.economy` answers "how fast could I reach ten points" by
walking a plan under expected production. It is a good estimate and it is
blind in the ways an estimate is: the dice never disappoint it, the robber
never lands on it, and the opponents it is racing never take a turn.

This plays instead. From a position it samples dice, pays everyone out, lets
every player act on a cheap policy, and asks who got to ten first -- then does
it again, a few dozen times, and reports how often that was us. Szita, Chaslot
and Spronck's agent reached roughly the strength of hand-written heuristics at
a thousand simulations a move and beat them convincingly at ten thousand; the
lesson taken here is not the number but that *playing it out* sees things a
one-position estimate cannot.

It is deliberately cheap per game. Positions are flat dicts of ints rather than
validated models, the policy is a handful of comparisons, and nothing allocates
per turn. That buys enough games per second to be worth asking, which a faithful
simulation would not.

What it does not model: trading between players, development cards beyond their
victory points, and ports. Each of those makes every player faster in roughly
the same way, and the question being asked is comparative.
"""
from __future__ import annotations

import random
from typing import Any, Optional

from . import board, rules

RESOURCES = rules.RESOURCES
R_IDX = {r: i for i, r in enumerate(RESOURCES)}
COSTS_V = {
    kind: [cost.get(r, 0) for r in RESOURCES] for kind, cost in rules.COSTS.items()
}
# 2d6, as the pairs that make each total -- sampled rather than summed so the
# distribution is exactly the game's
DICE = [a + b for a in range(1, 7) for b in range(1, 7)]


class Table:
    """Everything a rollout needs, packed once and reused across games."""

    def __init__(self, cfg) -> None:
        self.seats = list(cfg.players)
        self.me = cfg.me.color
        self.n = len(self.seats)
        self.robber = cfg.robber_hex

        # per seat: what each dice total pays them, as a resource vector
        self.pay: list[dict[int, list[int]]] = []
        self.vp0: list[int] = []
        self.hand0: list[list[int]] = []
        self.upgradeable: list[list[int]] = []
        self.spots: list[int] = []
        self.roads_left: list[int] = []
        for color in self.seats:
            p = cfg.players[color]
            table: dict[int, list[int]] = {}
            for weight, vids in ((1, p.settlements), (2, p.cities)):
                for v in vids:
                    for h in board.VERTEX_HEXES[v]:
                        tile = cfg.hexes[h]
                        if tile.resource == "desert" or tile.number is None:
                            continue
                        row = table.setdefault(tile.number, [0] * len(RESOURCES))
                        row[R_IDX[tile.resource]] += weight
                        table.setdefault(-tile.number, [0] * len(RESOURCES))
                        # remember which hex, so the robber can mute it
                        table[-tile.number][R_IDX[tile.resource]] = h
            self.pay.append({k: v for k, v in table.items() if k > 0})
            self.vp0.append(
                len(p.settlements) + 2 * len(p.cities)
                + (2 if p.longest_road else 0) + (2 if p.largest_army else 0)
            )
            hand = [0] * len(RESOURCES)
            if color == self.me:
                for r, n in cfg.me.hand.items():
                    if r in R_IDX:
                        hand[R_IDX[r]] = n
                self.vp0[-1] = max(self.vp0[-1], p.vp_visible) + cfg.me.dev_cards.vp
            else:
                # opponents' hands are hidden; spread what they hold evenly so
                # the count is right even though the composition is a guess
                each, extra = divmod(p.resource_count, len(RESOURCES))
                hand = [each + (1 if i < extra else 0) for i in range(len(RESOURCES))]
                self.vp0[-1] = max(self.vp0[-1], p.vp_visible)
            self.hand0.append(hand)
            self.upgradeable.append(list(p.settlements))
            self.spots.append(rules.PIECE_SUPPLY["settlement"] - len(p.settlements))
            self.roads_left.append(rules.PIECE_SUPPLY["road"] - len(p.roads))


def _afford(hand: list[int], cost: list[int]) -> bool:
    return all(hand[i] >= cost[i] for i in range(len(cost)))


def _pay(hand: list[int], cost: list[int]) -> None:
    for i, n in enumerate(cost):
        hand[i] -= n


class Seat:
    """One player's mutable state for the length of a game."""

    __slots__ = ("hand", "vp", "cities", "settles", "roads", "knights")

    def __init__(self, hand, vp, cities, settles, roads):
        self.hand = hand
        self.vp = vp
        self.cities = cities
        self.settles = settles
        self.roads = roads
        self.knights = 0


def _act(s: Seat, rng: random.Random) -> None:
    """One player's build step: take the cheapest point available.

    A city first, then a settlement, then roads while the network is short of
    the five that take Longest Road, then a development card. Roads and knights
    are here because without them nobody can pass nine points -- five
    settlements and four upgrades is nine, and the two awards are four of the
    ten. Leaving them out did not make games shorter, it made them unwinnable.

    Crude on purpose: it runs hundreds of thousands of times.
    """
    for _ in range(4):                       # a turn buys a few things at most
        if s.cities > 0 and _afford(s.hand, COSTS_V["city"]):
            _pay(s.hand, COSTS_V["city"]); s.vp += 1; s.cities -= 1
        elif s.settles > 0 and _afford(s.hand, COSTS_V["settlement"]):
            _pay(s.hand, COSTS_V["settlement"]); s.vp += 1; s.settles -= 1
        elif s.roads > 0 and s.roads > rules.PIECE_SUPPLY["road"] - 6 and _afford(s.hand, COSTS_V["road"]):
            _pay(s.hand, COSTS_V["road"]); s.roads -= 1
        elif _afford(s.hand, COSTS_V["dev"]):
            _pay(s.hand, COSTS_V["dev"])
            draw = rng.random()
            vp_odds = rules.DEV_DECK["victory_point"] / rules.DEV_DECK_SIZE
            knight_odds = rules.DEV_DECK["knight"] / rules.DEV_DECK_SIZE
            if draw < vp_odds:
                s.vp += 1
            elif draw < vp_odds + knight_odds:
                s.knights += 1
        else:
            return


def _awards(seats: list[Seat]) -> list[int]:
    """The two trophies, worth two points each and held by one player at a time."""
    bonus = [0] * len(seats)
    built = [rules.PIECE_SUPPLY["road"] - s.roads for s in seats]
    best = max(built)
    if best >= rules.LONGEST_ROAD_MIN and built.count(best) == 1:
        bonus[built.index(best)] += rules.LONGEST_ROAD_VP
    knights = [s.knights for s in seats]
    best_k = max(knights)
    if best_k >= rules.LARGEST_ARMY_MIN and knights.count(best_k) == 1:
        bonus[knights.index(best_k)] += rules.LARGEST_ARMY_VP
    return bonus


def play_out(table: Table, rng: random.Random, rounds: int = 40) -> list[int]:
    """One game from here. Returns each seat's final points.

    Points rather than a winner: from a position somebody else has nearly won,
    every move we could make loses, and a win-or-not answer says only that --
    the same 0% for the best move available and the worst. How close it came
    still separates them.
    """
    n = table.n
    seats = [
        Seat(list(table.hand0[i]), table.vp0[i], rules.PIECE_SUPPLY["city"],
             table.spots[i], table.roads_left[i])
        for i in range(n)
    ]
    limit = rules.DISCARD_LIMIT

    for _ in range(rounds):
        for turn in range(n):
            roll = rng.choice(DICE)
            if roll == 7:
                for s in seats:
                    total = sum(s.hand)
                    if total > limit:
                        for _drop in range(total // 2):
                            i = max(range(len(RESOURCES)), key=lambda k: s.hand[k])
                            s.hand[i] -= 1
            else:
                for i, s in enumerate(seats):
                    row = table.pay[i].get(roll)
                    if row:
                        for j, amount in enumerate(row):
                            s.hand[j] += amount
            _act(seats[turn], rng)
            bonus = _awards(seats)
            if seats[turn].vp + bonus[turn] >= rules.VICTORY_POINTS_TO_WIN:
                return [s.vp + b for s, b in zip(seats, bonus)]
    bonus = _awards(seats)
    return [s.vp + b for s, b in zip(seats, bonus)]


def outlook(cfg, samples: int = 200, rounds: int = 40, seed: int = 0) -> dict[str, float]:
    """How this position tends to end, over `samples` played-out games."""
    table = Table(cfg)
    rng = random.Random(seed)
    me = table.seats.index(table.me)
    wins = 0
    mine = 0.0
    margin = 0.0
    for _ in range(samples):
        vps = play_out(table, rng, rounds)
        best_other = max(v for i, v in enumerate(vps) if i != me) if table.n > 1 else 0
        if vps[me] >= rules.VICTORY_POINTS_TO_WIN and vps[me] >= best_other:
            wins += 1
        mine += vps[me]
        margin += vps[me] - best_other
    return {
        "win_rate": wins / samples,
        "points": mine / samples,
        "margin": margin / samples,
    }
