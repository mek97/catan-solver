"""Recommendations for a live game: moves (via the solver) + trade advice.

The solver already ranks builds/dev-plays/robber/bank-trades. This module adds
the things that only make sense with live opponent context: which *player*
trades to propose or accept, discard advice at a 7, and dice-tracker colour.
"""
from __future__ import annotations

from collections import Counter
from typing import Any, Optional

from .. import board, solver
from ..models import RESOURCES, BoardConfig, ScoredMove
from .engine import GameEngine

from .. import rules

COSTS = rules.COSTS


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
        "expected": {str(k): round(expected[k], 1) for k in range(2, 13)},
        "coldest": cold[:3],
        "hottest": cold[-3:][::-1],
    }


def _hand_after(cfg: BoardConfig, give: Counter, get: Counter) -> Optional[dict[str, int]]:
    """Resulting hand, or None if we can't cover the give side."""
    hand = dict(cfg.me.hand)
    for r, n in give.items():
        if hand.get(r, 0) < n:
            return None
        hand[r] -= n
    for r, n in get.items():
        hand[r] = hand.get(r, 0) + n
    return hand


def _best_score(cfg: BoardConfig, hand: dict[str, int]) -> float:
    probe = cfg.model_copy(deep=True)
    probe.me.hand = hand  # type: ignore[assignment]
    probe.pending = None  # score the build options, not a forced robber move
    moves = solver.solve(probe)
    return max((m.score for m in moves), default=0.0)


def evaluate_offer(eng: GameEngine, cfg: BoardConfig, offer: dict[str, Any]) -> dict[str, Any]:
    """Accept / reject / counter a trade offer, with the reason why.

    The test is simple and honest: does this trade raise the score of the best
    move available to me *this turn* by more than it plausibly helps them? A
    trade that unlocks a build for me is good even at a poor card ratio; a
    trade that mostly feeds the VP leader is bad even at a great one.
    """
    give = Counter(offer.get("wants") or [])   # what they want from me
    get = Counter(offer.get("offers") or [])   # what they hand over
    who = offer.get("from")
    result: dict[str, Any] = {"id": offer.get("id"), "from": who}

    hand_after = _hand_after(cfg, give, get)
    if hand_after is None:
        result.update(
            verdict="cannot",
            text=f"You don't hold {', '.join(f'{n} {r}' for r, n in give.items())}.",
        )
        return result

    ctx = solver.build_ctx(cfg)
    before = _best_score(cfg, dict(cfg.me.hand))
    after = _best_score(cfg, hand_after)
    gain = after - before

    # what it costs me in production terms: giving away what I rarely make hurts
    scarcity = sum(n * (10.0 / max(1.0, ctx.my_pips.get(r, 0.0) + 1)) for r, n in give.items())
    relief = sum(n * (10.0 / max(1.0, ctx.my_pips.get(r, 0.0) + 1)) for r, n in get.items())
    net = gain + (relief - scarcity) * 0.35

    leader_vp = max(ctx.opp_vp.values(), default=0)
    their_vp = ctx.opp_vp.get(who, 0) if who else 0
    feeds_leader = who and their_vp >= max(7, leader_vp)

    if feeds_leader:
        result.update(
            verdict="reject",
            score=round(net, 1),
            text=f"Reject — {who} is on {their_vp} VP. Don't hand the leader cards.",
        )
        return result

    if gain > 0.5:
        result.update(
            verdict="accept",
            score=round(net, 1),
            text=(
                f"Accept — giving {_fmt(give)} for {_fmt(get)} unlocks a better move "
                f"this turn (+{gain:.1f})."
            ),
        )
        return result

    if net > 0:
        result.update(
            verdict="accept",
            score=round(net, 1),
            text=f"Accept — {_fmt(get)} is scarcer for you than {_fmt(give)}.",
        )
        return result

    # would a different price make it worth it? offer what we over-produce
    spare = sorted(
        (r for r in RESOURCES if cfg.me.hand.get(r, 0) > 0 and r not in give),
        key=lambda r: -ctx.my_pips.get(r, 0.0),
    )
    if get and spare and ctx.my_pips.get(spare[0], 0) >= 6:
        result.update(
            verdict="counter",
            score=round(net, 1),
            text=(
                f"Counter — offer {spare[0]} instead of {_fmt(give)}; you make "
                f"{ctx.my_pips[spare[0]]:.0f} pips of it and little of what they asked."
            ),
            counter={"give": {spare[0]: sum(give.values())}, "get": dict(get)},
        )
        return result

    result.update(
        verdict="reject",
        score=round(net, 1),
        text=f"Reject — {_fmt(give)} costs you more than {_fmt(get)} is worth right now.",
    )
    return result


def _fmt(c: Counter) -> str:
    return ", ".join(f"{n} {r}" for r, n in c.items()) or "nothing"


def offer_advice(eng: GameEngine, cfg: BoardConfig) -> list[dict[str, Any]]:
    """Verdicts for every open offer that isn't ours."""
    out = []
    for offer in eng.trade_offers():
        if offer.get("from_me"):
            continue
        ev = evaluate_offer(eng, cfg, offer)
        ev["offer"] = offer
        out.append(ev)
    return out


DEV_LABEL = {
    "knight": "Knight",
    "road_building": "Road Building",
    "year_of_plenty": "Year of Plenty",
    "monopoly": "Monopoly",
}


def dev_card_plays(eng: GameEngine, cfg: BoardConfig) -> list[dict[str, Any]]:
    """What each dev card would do, with the follow-up action spelled out.

    colonist hides the composition of our own hand behind a placeholder enum,
    so when we only know the count we evaluate every card type and mark them
    conditional rather than pretending to know what we hold.
    """
    mine = eng.my_dev_cards()
    if not mine["count"]:
        return []
    certain = bool(mine["known"]) and not mine["hidden"]

    out: list[dict[str, Any]] = []
    for kind in ("knight", "road_building", "year_of_plenty", "monopoly"):
        have = mine["known"].get(kind, 0)
        if certain and not have:
            continue
        probe = cfg.model_copy(deep=True)
        probe.me.dev_cards = probe.me.dev_cards.model_copy(update={kind: max(1, have)})
        probe.me.dev_card_played_this_turn = False
        probe.me.dev_card_bought_this_turn = False
        move = next(
            (m for m in solver.solve(probe) if m.steps[0].type == f"play_{kind}"), None
        )
        if not move:
            continue
        out.append(
            {
                "card": kind,
                "label": DEV_LABEL[kind],
                "held": have if certain else None,
                "certain": certain,
                "score": round(move.score, 1),
                "action": move.location_hint,
                "why": move.reasoning,
                "steps": [s.model_dump() for s in move.steps],
            }
        )
    out.sort(key=lambda d: -d["score"])
    if mine["bought_this_turn"]:
        for d in out:
            d["blocked"] = "bought this turn — cannot play until next turn"
    return out


def _partners_for(eng: GameEngine, ctx, res: str, leader_vp: int) -> list[dict[str, Any]]:
    """Opponents worth asking for a resource, best prospect first.

    Ranked by how much of it they produce and how many cards they hold, minus
    anyone at or past the leader's score -- helping them is how you lose.
    """
    production = eng.production_table()
    out = []
    for p in eng.player_summary():
        color = p["color"]
        if p["is_me"] or not p["cards"]:
            continue
        if ctx.opp_vp.get(color, 0) >= max(7, leader_vp):
            continue
        pips = sum(
            amt
            for by_number in production.get(color, {}).values()
            for rname, amt in by_number.items()
            if rname == res
        )
        out.append({"color": color, "pips": pips, "cards": p["cards"]})
    out.sort(key=lambda c: (-c["pips"], -c["cards"], c["color"]))
    return out


def _choose_partner(
    mem, candidates: list[dict[str, Any]], give: str, res: str, count: int, hand
) -> Optional[dict[str, Any]]:
    """Pick who to ask and at what price, given what has already been refused.

    Refusals are the whole point of this function: asking green a third time
    for the ore they have twice declined is worse than useless, so we walk on
    to the next partner, and only when everyone has said no do we raise the
    price.
    """
    give_n = max(1, count)
    # Someone who has already handed this resource over is a better bet than
    # whoever merely produces the most of it; someone who has refused it is a
    # worse one. Production order survives as the tiebreak.
    ranked = sorted(
        candidates,
        key=lambda c: (-mem.gives(c["color"], res), mem.refuses(c["color"], res)),
    )

    for cand in ranked:
        if not mem.was_refused(cand["color"], [give] * give_n, [res] * count):
            return {**cand, "give_n": give_n, "sweetened": False}

    # everyone has turned this down -- the trade isn't dead, the price is.
    # Offer one more card if we can spare it.
    best = ranked[0]
    if hand.get(give, 0) > give_n:
        return {**best, "give_n": give_n + 1, "sweetened": True}
    return None


def trade_proposals(eng: GameEngine, cfg: BoardConfig, limit: int = 3) -> list[dict[str, Any]]:
    """Concrete offers to make: who to ask, what to give, and why they'd agree.

    Partner choice uses their production (who actually makes the resource),
    their hand size, and what they have already refused.
    """
    ctx = solver.build_ctx(cfg)
    hand = cfg.me.hand
    mem = eng.trade_memory
    leader_vp = max(ctx.opp_vp.values(), default=0)

    # what we're short of, for the most valuable build we can nearly afford
    targets: list[tuple[int, int, str, dict[str, int]]] = []
    for rank, name in enumerate(("city", "settlement", "dev", "road")):
        need = {r: n - hand.get(r, 0) for r, n in COSTS[name].items() if hand.get(r, 0) < n}
        if need:
            targets.append((sum(need.values()), rank, name, need))
    targets.sort()

    spare = sorted(
        (r for r in RESOURCES if hand.get(r, 0) > 0),
        key=lambda r: -(ctx.my_pips.get(r, 0.0) + hand.get(r, 0)),
    )
    out: list[dict[str, Any]] = []
    seen: set[tuple] = set()
    for missing, _rank, build, need in targets:
        if missing > 2:
            continue
        for res, count in need.items():
            give = next((r for r in spare if r != res), None)
            if not give:
                continue
            candidates = _partners_for(eng, ctx, res, leader_vp)
            if not candidates:
                continue
            pick = _choose_partner(mem, candidates, give, res, count, hand)
            if pick is None:
                continue

            color, give_n = pick["color"], pick["give_n"]
            if (color, give, res, count) in seen:
                continue  # the same ask, already listed against a better build
            seen.add((color, give, res, count))
            offer, want = {give: give_n}, {res: count}
            # score it the same way we score an incoming offer, so proposing a
            # trade competes with building instead of always sorting last
            after = _hand_after(cfg, Counter(offer), Counter(want))
            gain = (_best_score(cfg, after) - _best_score(cfg, dict(hand))) if after else 0.0

            why = (
                f"they produce {pick['pips']} of it and hold {pick['cards']} cards"
                if pick["pips"]
                else f"they hold {pick['cards']} cards"
            )
            note = _history_note(mem, color, res)
            if pick["sweetened"]:
                why = f"everyone has refused 1-for-1, so sweeten it to {give_n} {give}"
            elif note:
                why = f"{why}; {note}"

            out.append(
                {
                    "to": color,
                    "give": offer,
                    "get": want,
                    "for": build,
                    "score": round(gain, 1),
                    "sweetened": pick["sweetened"],
                    "text": (
                        f"Ask {color} for {count} {res} — offer {give_n} {give}. "
                        f"It completes your {build}; {why}."
                    ),
                }
            )
            if len(out) >= limit * 2:
                break
    out.sort(key=lambda p: -p["score"])
    return out[:limit]


def bank_options(eng: GameEngine, cfg: BoardConfig, limit: int = 4) -> list[dict[str, Any]]:
    """Every bank/port trade you can actually make right now, ranked.

    The solver only emits bank trades as part of a combo that completes a
    build, so a trade that merely improves your hand never surfaced -- the
    category read "nothing available" while you were sitting on four sheep.
    This lists what the rates and the bank's stock genuinely allow.
    """
    ctx = solver.build_ctx(cfg)
    hand = cfg.me.hand
    stock = cfg.bank or {}
    before = _best_score(cfg, dict(hand))
    out: list[dict[str, Any]] = []

    for give in RESOURCES:
        rate = ctx.rates.get(give, rules.BANK_RATE)
        if hand.get(give, 0) < rate:
            continue
        for get in RESOURCES:
            if get == give or stock.get(get, rules.BANK_PER_RESOURCE) < 1:
                continue
            after = _hand_after(cfg, Counter({give: rate}), Counter({get: 1}))
            if after is None:
                continue
            gain = _best_score(cfg, after) - before
            # even a trade that builds nothing this turn is worth something if
            # it converts a surplus into what we never produce
            scarcity = 1.0 / max(1.0, ctx.my_pips.get(get, 0.0) + 1)
            out.append(
                {
                    "give": {give: rate},
                    "get": {get: 1},
                    "rate": rate,
                    "score": round(gain + scarcity, 1),
                    "text": f"trade {rate} {give} for 1 {get}"
                    + (f" ({rate}:1)" if rate != rules.BANK_RATE else ""),
                    "why": (
                        f"unlocks a better move this turn (+{gain:.1f})" if gain > 0.1
                        else f"you produce little {get}; {give} refills"
                    ),
                }
            )
    out.sort(key=lambda o: -o["score"])
    return out[:limit]


def _history_note(mem, partner: str, resource: str) -> str:
    """One clause on how this player has handled this resource before."""
    yes, no = mem.gives(partner, resource), mem.refuses(partner, resource)
    if yes:
        return f"they have given up {resource} {yes}x before"
    return f"they have refused {resource} {no}x" if no else ""


def trade_history(eng: GameEngine) -> dict[str, Any]:
    """What has been refused and accepted so far, and what each player needs."""
    return eng.trade_memory.summary() if eng is not None else {}


def recent_trades(eng: GameEngine, limit: int = 8) -> list[dict[str, Any]]:
    """Completed trades and offers, newest last."""
    kinds = ("trade_player", "trade_bank", "trade_offered")
    return [e for e in eng.events if e.get("kind") in kinds][-limit:]


def discard_advice(eng: GameEngine, cfg: BoardConfig) -> Optional[dict[str, Any]]:
    """On a 7, which cards to throw away: keep what completes the best build."""
    hand = cfg.me.hand
    total = sum(hand.values())
    to_drop = rules.discard_count(total, cfg.me.discard_limit)
    if not to_drop:
        return None
    ctx = solver.build_ctx(cfg)
    # value each card: what it costs to replace (rarity for me) + build usefulness
    need: Counter[str] = Counter()
    for name in ("city", "settlement", "road", "dev"):
        for r, n in COSTS[name].items():
            need[r] = max(need[r], n)
    keep_rank = sorted(
        RESOURCES,
        key=lambda r: (need[r] > 0, -ctx.my_pips.get(r, 0.0)),
        reverse=True,
    )
    drop: Counter[str] = Counter()
    left = to_drop
    for r in reversed(keep_rank):  # discard the least useful first
        if left <= 0:
            break
        spare = max(0, hand.get(r, 0) - (need[r] if hand.get(r, 0) >= need[r] else 0))
        take = min(left, spare if spare else hand.get(r, 0))
        if take:
            drop[r] += take
            left -= take
    for r in reversed(keep_rank):  # still short: take from anything left
        if left <= 0:
            break
        take = min(left, hand.get(r, 0) - drop[r])
        if take > 0:
            drop[r] += take
            left -= take
    required = cfg.pending == "discard"
    listed = ", ".join(f"{n} {r}" for r, n in drop.items())
    return {
        "required": required,
        "must_discard": to_drop,
        "drop": dict(drop),
        "text": (
            f"Discard {to_drop}: {listed}"
            if required
            else f"Holding {total} cards — a 7 would cost you {to_drop}: {listed}"
        ),
    }


def robber_options(eng: GameEngine, cfg: BoardConfig, limit: int = 3) -> list[dict[str, Any]]:
    """Best robber placements, ranked — but only when you could actually move it.

    There are exactly two ways to move the robber: a 7 puts you in the robber
    phase, or you play a knight. With neither available the ranking is noise,
    so we return nothing rather than advertise a move you cannot make.
    """
    forced = cfg.pending == "move_robber"
    dev = eng.my_dev_cards() if eng is not None else {"count": 0, "known": {}, "hidden": 0}
    # a masked dev card might be a knight; a known hand tells us outright
    could_knight = dev["known"].get("knight", 0) > 0 or dev["hidden"] > 0
    if not forced and not could_knight:
        return []

    # priced the same way solve() prices a knight -- in turns taken off the
    # players it blocks. Reading the raw generation score here put pips and
    # turns side by side in one panel, which makes them look comparable.
    ctx = solver.build_ctx(cfg)
    ranked = solver._score_robber(ctx, solver._robber_moves(ctx))
    out = []
    for m in ranked[:limit]:
        step = m.steps[0]
        out.append(
            {
                "hex": step.robber_hex,
                "steal_from": step.steal_from,
                "score": round(m.score, 2),
                "text": m.location_hint,
                "why": m.reasoning,
                "forced": forced,
                "needs_knight": not forced,
            }
        )
    return out


def victory_plan(eng: GameEngine, cfg: BoardConfig) -> dict[str, Any]:
    """The route to ten points, so a turn with no affordable move still says
    something. "Nothing to do" and "nothing to aim for" are different answers."""
    from .. import economy

    ctx = solver.build_ctx(cfg)
    steps = []
    for s in economy.plan(cfg, ctx):
        where = (
            board.describe_vertex(cfg.hexes, s["vertex"]) if s["vertex"] is not None else ""
        )
        steps.append({**s, "where": where})
    return {
        "turns": round(economy.turns_to_win(cfg, ctx), 1),
        "vp": ctx.my_vp,
        "steps": steps,
    }


def recommend(eng: GameEngine) -> dict[str, Any]:
    cfg = eng.board_config()
    moves: list[ScoredMove] = solver.solve(cfg)
    return {
        "discard": discard_advice(eng, cfg),
        "robber": robber_options(eng, cfg),
        "dev_plays": dev_card_plays(eng, cfg),
        "bank_options": bank_options(eng, cfg),
        "proposals": trade_proposals(eng, cfg),
        "trade_history": trade_history(eng),
        "plan": victory_plan(eng, cfg),
        "my_dev": eng.my_dev_cards(),
        "my_turn": eng.is_my_turn(),
        "turn": eng.current_turn(),
        "phase": cfg.phase,
        "pending": cfg.pending,
        "hand": cfg.me.hand,
        "moves": [m.model_dump() for m in moves],
        "trades": trade_advice(eng, cfg),
        "dice": dice_stats(eng),
        "players": eng.player_summary(),
        "timer": eng.turn_timer(),
        "offers": eng.trade_offers(),
        "offer_advice": offer_advice(eng, cfg),
        "trade_log": recent_trades(eng),
        "rolls": eng.dice_history()[-24:],
    }
