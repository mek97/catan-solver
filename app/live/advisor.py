"""Recommendations for a live game: moves (via the solver) + trade advice.

The solver already ranks builds/dev-plays/robber/bank-trades. This module adds
the things that only make sense with live opponent context: which *player*
trades to propose or accept, discard advice at a 7, and dice-tracker colour.
"""
from __future__ import annotations

from collections import Counter
from typing import Any, Optional

from .. import board, economy, solver
from ..models import RESOURCES, BoardConfig, ScoredMove
from .engine import GameEngine
from .trades import RESPONSE_ACCEPT as TRADE_ACCEPTED, RESPONSE_DECLINE as TRADE_DECLINED

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

    # "what should I ask for" is answered by trade_proposals, which names the
    # partner, prices the offer, and remembers what each player has refused.
    # This function used to answer it too, and worse: it offered anything not
    # in the shortfall, so holding one brick it would offer the brick to get
    # the wood for a road -- leaving you holding a wood and no road. The same
    # bug was fixed in trade_proposals and survived here, which is the argument
    # against having answered it twice.

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


def _position_turns(cfg: BoardConfig, hand: dict[str, int]) -> float:
    """Turns to win holding this hand. Lower is better.

    The whole position, not the best move available this turn. Asking only what
    is buildable right now scores a card you cannot yet use at exactly zero,
    which is why every caller used to add a hand-tuned scarcity term on top --
    and those terms then dwarfed the real numbers they were correcting. The
    ladder already prices a card you never produce: it shortens the wait on
    every rung that needs one.

    It is also some three hundred times cheaper than solving the position,
    which the callers were doing in a loop.
    """
    probe = cfg.model_copy(deep=True)
    probe.me.hand = dict(hand)  # type: ignore[assignment]
    probe.pending = None  # value the position, not a forced robber move
    return economy.turns_to_win(probe, solver.build_ctx(probe))


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
    # turns this trade takes off the race; what you give and what you get are
    # both already priced by the ladder, so nothing is added on top
    gain = _position_turns(cfg, dict(cfg.me.hand)) - _position_turns(cfg, hand_after)
    net = gain

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
    """Verdicts for every open offer that is ours to answer.

    An offer stays on the table after you answer it, waiting on the player who
    made it, and colonist records your reply on it. Re-advising one you have
    already accepted or declined asks you to decide something twice and hides
    the fact that the trade is simply in progress -- so an answered offer is
    reported as waiting rather than as a choice.
    """
    out = []
    for offer in eng.trade_offers():
        if offer.get("from_me"):
            continue
        answered = offer.get("my_response") in (TRADE_ACCEPTED, TRADE_DECLINED)
        if answered:
            out.append({
                "id": offer.get("id"),
                "from": offer.get("from"),
                "verdict": "waiting",
                "score": 0.0,
                "text": (
                    f"You accepted — waiting on {offer.get('from')}."
                    if offer.get("my_response") == TRADE_ACCEPTED
                    else f"You declined {offer.get('from')}'s offer."
                ),
                "offer": offer,
            })
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
    if not mine["count"] or mine["played_this_turn"]:
        return []   # one card per turn, and colonist says whether we used it

    # What we may play now, which is not the same as what we hold: a card
    # cannot be played the turn it is bought. Reading the whole hand here and
    # then clearing the restrictions produced a confident "play your knight"
    # for a knight bought moments earlier, which colonist then refused.
    playable = mine["playable"]
    certain = bool(mine["known"]) and not mine["hidden"]

    out: list[dict[str, Any]] = []
    for kind in ("knight", "road_building", "year_of_plenty", "monopoly"):
        have = playable.get(kind, 0)
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
    mem, candidates: list[dict[str, Any]], give: str, res: str, count: int, surplus
) -> Optional[dict[str, Any]]:
    """Pick who to ask and at what price, given what has already been refused.

    Refusals are the whole point of this function: asking green a third time
    for the ore they have twice declined is worse than useless, so we walk on
    to the next partner, and only when everyone has said no do we raise the
    price.
    """
    base = max(1, count)
    # Someone who has already handed this resource over is a better bet than
    # whoever merely produces the most of it; someone who has refused it is a
    # worse one. Production order survives as the tiebreak.
    ranked = sorted(
        candidates,
        key=lambda c: (-mem.gives(c["color"], res), mem.refuses(c["color"], res)),
    )

    # Every price we can afford, cheapest first, and for each the partner most
    # likely to agree. Raising the price used to skip this check, so an offer
    # that had already been refused at the higher price was made again, and
    # again -- the same trade going round in a loop while the memory that was
    # supposed to prevent it recorded another refusal each time.
    for price in range(base, int(surplus.get(give, 0)) + 1):
        for cand in ranked:
            if not mem.was_refused(cand["color"], [give] * price, [res] * count):
                return {**cand, "give_n": price, "sweetened": price > base}
    return None  # every partner has refused every price we can afford


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

    out: list[dict[str, Any]] = []
    seen: set[tuple] = set()
    for missing, _rank, build, need in targets:
        if missing > 2:
            continue
        # Spare means spare *after* the build we are funding. Anything the
        # build itself needs is not ours to trade away: offering your only
        # brick to get the wood for a road leaves you holding the wood and
        # unable to build the road, which is worse than doing nothing.
        cost = COSTS[build]
        surplus = {r: hand.get(r, 0) - cost.get(r, 0) for r in RESOURCES}
        spare = sorted(
            (r for r in RESOURCES if surplus[r] > 0),
            key=lambda r: -(ctx.my_pips.get(r, 0.0) + hand.get(r, 0)),
        )
        for res, count in need.items():
            give = next((r for r in spare if r != res), None)
            if not give:
                continue
            candidates = _partners_for(eng, ctx, res, leader_vp)
            if not candidates:
                continue
            pick = _choose_partner(mem, candidates, give, res, count, surplus)
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
            gain = (_position_turns(cfg, dict(hand)) - _position_turns(cfg, after)) if after else 0.0

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
                        # only claim completion when this really is the last
                        # card missing; two short is two trades away
                        + (
                            f"It completes your {build}"
                            if missing <= count
                            else f"It is one of the {missing} cards your {build} still needs"
                        )
                        + f"; {why}."
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
    before = _position_turns(cfg, dict(hand))
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
            # a trade that builds nothing this turn still counts: the ladder
            # sees the surplus become something we never produce
            gain = before - _position_turns(cfg, after)
            out.append(
                {
                    "give": {give: rate},
                    "get": {get: 1},
                    "rate": rate,
                    "score": round(gain, 2),
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
    dev = (
        eng.my_dev_cards() if eng is not None
        else {"count": 0, "known": {}, "hidden": 0, "playable": {},
              "bought_this_turn": {}, "played_this_turn": False}
    )
    # What we could play, not what we hold. A knight bought this turn cannot be
    # played until the next one, and reading the whole hand here advertised the
    # robber on the strength of a card the rules will not let you use -- the
    # third place that made the same mistake, after the solver and the dev-card
    # panel. A masked card might still be a knight, unless it is one of the
    # ones just bought.
    fresh = sum((dev.get("bought_this_turn") or {}).values())
    could_knight = (
        not dev.get("played_this_turn")
        and (
            (dev.get("playable") or {}).get("knight", 0) > 0
            or max(0, dev.get("hidden", 0) - fresh) > 0
        )
    )
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
    ctx = solver.build_ctx(cfg)
    due = solver.race(cfg)["deadlines"]
    plan_name, turns = economy.best_strategy(cfg, ctx, deadlines=due)
    steps = []
    for s in economy.plan(cfg, ctx, deadlines=due, prefer=plan_name):
        where = (
            board.describe_vertex(cfg.hexes, s["vertex"]) if s["vertex"] is not None else ""
        )
        steps.append({**s, "where": where})
    return {
        "turns": round(turns, 1),
        "vp": ctx.my_vp,
        "strategy": plan_name,
        "steps": steps,
    }


def _second_opinion(cfg: BoardConfig) -> Optional[dict[str, Any]]:
    """What catanatron's alpha-beta search would play here.

    Shown next to our own recommendation, not in place of it. They disagree on
    most positions, and a disagreement is either a judgement call or a move we
    failed to generate -- which look identical until both answers are on screen
    together. It is also the only opinion here that came from searching rather
    than from evaluating one ply.

    Absent on a 30-hex board or a fifth seat: catanatron plays neither.
    """
    try:
        from .. import bridge

        return bridge.second_opinion(cfg)
    except Exception:
        return None      # never let a second opinion break the first one


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
        "race": solver.race(cfg),
        # a searching engine's answer, beside ours rather than instead of it
        "engine": _second_opinion(cfg),
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
