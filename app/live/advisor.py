"""Recommendations for a live game: moves (via the solver) + trade advice.

The solver already ranks builds/dev-plays/robber/bank-trades. This module adds
the things that only make sense with live opponent context: which *player*
trades to propose or accept, discard advice at a 7, and dice-tracker colour.
"""
from __future__ import annotations

from collections import Counter
from typing import Any

from .. import board, solver
from ..models import RESOURCES, BoardConfig, ScoredMove
from .engine import GameEngine

COSTS = solver.COSTS
PIPS = solver.PIPS


def _shortfall(hand: dict[str, int], cost: dict[str, int]) -> dict[str, int]:
    return {r: n - hand.get(r, 0) for r, n in cost.items() if hand.get(r, 0) < n}


def _surplus(hand: dict[str, int], goal: dict[str, int]) -> dict[str, int]:
    return {r: hand.get(r, 0) - goal.get(r, 0) for r in hand if hand.get(r, 0) > goal.get(r, 0)}


def trade_advice(eng: GameEngine, cfg: BoardConfig) -> list[dict[str, Any]]:
    """What to ask other players for, and what it's safe to give away."""
    out: list[dict[str, Any]] = []
    hand = cfg.me.hand
    ctx = solver.build_ctx(cfg)
    my_pips = ctx.my_pips

    # what am I closest to affording?
    targets = []
    for name in ("settlement", "city", "road", "dev"):
        need = _shortfall(hand, COSTS[name])
        targets.append((sum(need.values()), name, need))
    targets.sort()
    for missing, name, need in targets:
        if 0 < missing <= 2:
            give = sorted(
                (r for r in RESOURCES if hand.get(r, 0) > 0 and r not in need),
                key=lambda r: -(hand.get(r, 0) * (1 + my_pips.get(r, 0) / 10)),
            )
            if give:
                out.append(
                    {
                        "type": "want",
                        "need": need,
                        "for": name,
                        "offer": give[:2],
                        "text": (
                            f"Offer {' or '.join(give[:2])} for "
                            f"{', '.join(f'{n} {r}' for r, n in need.items())} — "
                            f"that completes a {name} this turn."
                        ),
                    }
                )

    # resources I over-produce are cheap for me to trade away
    rich = [r for r in RESOURCES if my_pips.get(r, 0) >= 8]
    for r in rich:
        out.append(
            {
                "type": "leverage",
                "resource": r,
                "text": (
                    f"You out-produce the table on {r} ({my_pips[r]:.0f} pips) — "
                    f"trade it freely, even at poor rates; it refills fastest."
                ),
            }
        )

    # never feed the leader
    leader = max(ctx.opp_vp, key=lambda c: ctx.opp_vp[c], default=None)
    if leader and ctx.opp_vp.get(leader, 0) >= 7:
        out.append(
            {
                "type": "caution",
                "player": leader,
                "text": f"Do not trade with {leader} — they are at {ctx.opp_vp[leader]} VP and closing.",
            }
        )

    # discard risk
    total = sum(hand.values())
    if total >= 8:
        keep: Counter[str] = Counter()
        for name in ("settlement", "city"):
            for r, n in COSTS[name].items():
                keep[r] = max(keep[r], n)
        dump = sorted(_surplus(hand, keep).items(), key=lambda kv: -kv[1])
        if dump:
            out.append(
                {
                    "type": "discard_risk",
                    "text": (
                        f"You hold {total} cards — a 7 costs you {total // 2}. "
                        f"Spend or trade surplus {dump[0][0]} first."
                    ),
                }
            )
    return out


def dice_stats(eng: GameEngine) -> dict[str, Any]:
    rolls = eng.dice_history()
    counts = Counter(rolls)
    n = len(rolls)
    expected = {k: n * (6 - abs(7 - k)) / 36 for k in range(2, 13)}
    cold = sorted(
        (k for k in range(2, 13) if k != 7),
        key=lambda k: counts.get(k, 0) - expected[k],
    )
    return {
        "rolls": n,
        "counts": {str(k): counts.get(k, 0) for k in range(2, 13)},
        "coldest": cold[:3],
        "hottest": cold[-3:][::-1],
    }


def recommend(eng: GameEngine) -> dict[str, Any]:
    cfg = eng.board_config()
    moves: list[ScoredMove] = solver.solve(cfg)
    return {
        "my_turn": eng.is_my_turn(),
        "turn": eng.current_turn(),
        "phase": cfg.phase,
        "pending": cfg.pending,
        "hand": cfg.me.hand,
        "moves": [m.model_dump() for m in moves],
        "trades": trade_advice(eng, cfg),
        "dice": dice_stats(eng),
        "players": eng.player_summary(),
    }
