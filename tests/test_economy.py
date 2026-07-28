"""The economy model: turns, not pips.

These pin down the properties the ranking depends on. Magnitudes are left
alone deliberately -- they move whenever a weight is retuned, and asserting
them would only test that the constants are still the constants.
"""
import pytest

from app import board, economy, rules, solver
from app.models import BoardConfig

from test_solver import load, road_chain_from


def _cfg(**kw) -> BoardConfig:
    """A player with something on the board -- the fixture starts empty.

    Two settlements joined to a road, which is the least that makes reach,
    production and the ladder mean anything.
    """
    cfg = load(**kw)
    me = cfg.players[cfg.me.color]
    # the richest corner, plus a second one far enough away to be legal
    pips = {
        v: sum(
            solver.PIPS.get(cfg.hexes[h].number, 0)
            for h in board.VERTEX_HEXES[v]
            if cfg.hexes[h].number
        )
        for v in range(54)
    }
    first = max(pips, key=lambda v: pips[v])
    blocked = {first} | set(board.VERTEX_ADJ[first])
    second = max((v for v in pips if v not in blocked), key=lambda v: pips[v])
    me.settlements = [first, second]
    me.roads = [board.VERTEX_EDGES[first][0], board.VERTEX_EDGES[second][0]]
    return cfg


def test_production_counts_a_city_twice_and_the_robber_not_at_all():
    cfg = _cfg()
    v = cfg.players["red"].settlements[0]
    cfg.players["red"].settlements = [v]
    hexes = [h for h in board.VERTEX_HEXES[v] if cfg.hexes[h].number]

    base = economy.production_rate(cfg, "red")
    cfg.players["red"].cities = [v]
    cfg.players["red"].settlements = []
    doubled = economy.production_rate(cfg, "red")
    assert sum(doubled.values()) == pytest.approx(2 * sum(base.values()))

    cfg.robber_hex = hexes[0]
    blocked = economy.production_rate(cfg, "red")
    assert sum(blocked.values()) < sum(doubled.values())


def test_a_cost_you_can_already_pay_takes_no_turns():
    rates = {r: 4 for r in rules.RESOURCES}
    hand = {"wheat": 2, "ore": 3}
    assert economy.turns_to_afford(hand, {}, rules.COSTS["city"], rates) == 0.0


def test_waiting_is_shorter_when_production_is_faster():
    rates = {r: 4 for r in rules.RESOURCES}
    slow = {"wheat": 0.2, "ore": 0.2}
    fast = {"wheat": 1.0, "ore": 1.0}
    cost = rules.COSTS["city"]
    assert economy.turns_to_afford({}, fast, cost, rates) < economy.turns_to_afford({}, slow, cost, rates)


def test_a_port_shortens_the_wait_for_what_you_cannot_produce():
    """Surplus is only worth what the bank will give you for it."""
    rate = {"sheep": 2.0}
    cost = rules.COSTS["city"]  # wheat and ore, neither of which we make
    at_bank = economy.turns_to_afford({}, rate, cost, {r: 4 for r in rules.RESOURCES})
    at_port = economy.turns_to_afford({}, rate, cost, {r: 2 for r in rules.RESOURCES})
    assert at_port < at_bank


def test_producing_nothing_useful_never_arrives():
    assert economy.turns_to_afford({}, {}, rules.COSTS["road"], {r: 4 for r in rules.RESOURCES}) == economy.INF


def test_more_points_means_fewer_turns_left():
    cfg = _cfg()
    far = economy.turns_to_win(cfg, solver.build_ctx(cfg))
    # the same position, two victory points richer
    cfg.players["red"].cities = list(cfg.players["red"].settlements[:1])
    cfg.players["red"].settlements = cfg.players["red"].settlements[1:]
    near = economy.turns_to_win(cfg, solver.build_ctx(cfg))
    assert near < far


def test_the_plan_reaches_ten_points():
    cfg = _cfg()
    ctx = solver.build_ctx(cfg)
    steps = economy.plan(cfg, ctx)
    assert steps, "a plan should exist while the game is winnable"
    assert ctx.my_vp + sum(s["vp"] for s in steps) >= rules.VICTORY_POINTS_TO_WIN
    assert [s["at"] for s in steps] == sorted(s["at"] for s in steps)


def test_longest_road_and_largest_army_are_worth_two_each():
    """Both awards are 2 VP, so the ladder must not treat them as one."""
    cfg = _cfg()
    ctx = solver.build_ctx(cfg)
    pos = economy._seed_spots(economy._build_position(cfg, ctx))
    pos.knights_for_army = 3
    pos.roads_for_longest = 2
    kinds = {r.kind: r.vp for r in economy._rungs(pos)}
    assert kinds.get("army") == 2
    assert kinds.get("longest_road") == 2


def test_an_award_already_held_is_not_chased_again():
    cfg = _cfg()
    cfg.players["red"].largest_army = True
    cfg.players["red"].longest_road = True
    ctx = solver.build_ctx(cfg)
    pos = economy._seed_spots(economy._build_position(cfg, ctx))
    assert pos.knights_for_army == 0 and pos.roads_for_longest == 0
    assert {r.kind for r in economy._rungs(pos)}.isdisjoint({"army", "longest_road"})


def test_the_ladder_obeys_the_distance_rule_on_its_own_settlements():
    """One road touches two corners, but they are adjacent: only one is legal.

    Without this the ladder settles both ends of every new road, and a road
    scores higher than the settlement it was reaching for.
    """
    cfg = _cfg()
    ctx = solver.build_ctx(cfg)
    pos = economy._seed_spots(economy._build_position(cfg, ctx))
    for v in list(pos.spots):
        pos.claimed.add(v)
        assert all(n not in economy._reachable(pos) for n in board.VERTEX_ADJ[v])
        pos.claimed.discard(v)


def test_riding_your_own_roads_is_free():
    """Reach is measured in roads still to buy, not corners crossed."""
    cfg = _cfg()
    me = cfg.players[cfg.me.color]
    paths = economy._road_paths(cfg, cfg.me.color, set())
    assert paths, "an established player can reach somewhere"
    assert all(not set(p) & set(me.roads) for p in paths.values()), (
        "a path must never charge for a road already built"
    )


# --- decisions the objective has to get right --------------------------------


def _spread(cfg, n):
    """n legal, mutually non-adjacent corners, richest first.

    Inland corners on purpose: a one-hex coastal corner leaves its owner unable
    to reach ten points at all, and a player with no way to win is one the
    robber cannot meaningfully slow down.
    """
    ranked = sorted(
        range(54),
        key=lambda v: -sum(
            solver.PIPS.get(cfg.hexes[h].number, 0)
            for h in board.VERTEX_HEXES[v]
            if cfg.hexes[h].number
        ),
    )
    out = []
    for v in ranked:
        if len(out) == n:
            break
        if all(v not in board.VERTEX_ADJ[o] for o in out):
            out.append(v)
    return out


def _settle(cfg, color, vertex):
    """Put a player on a corner with a road, so they have somewhere to go."""
    p = cfg.players[color]
    p.settlements.append(vertex)
    free = [e for e in board.VERTEX_EDGES[vertex]
            if all(e not in q.roads for q in cfg.players.values())]
    if free:
        p.roads.append(free[0])


def test_the_winning_move_is_taken():
    """At nine points a city ends the game. Nothing else can outrank it."""
    cfg = load(phase="main")
    me = cfg.players["red"]
    spots = _spread(cfg, 6)
    me.cities, me.settlements = spots[:3], spots[3:]  # 6 + 3 = 9 VP
    me.roads = [board.VERTEX_EDGES[spots[3]][0]]
    cfg.me.hand = {"wheat": 2, "ore": 3, "wood": 2, "brick": 2, "sheep": 1}

    assert solver.build_ctx(cfg).my_vp == 9
    top = solver.solve(cfg)[0]
    assert top.steps[-1].type == "build_city", f"took {top.location_hint} instead of winning"


def test_a_point_now_beats_production_later():
    """The old scoring lost this: 13 weighted pips outranked a flat +2 VP."""
    cfg = load(phase="main")
    me = cfg.players["red"]
    spots = _spread(cfg, 6)
    me.cities, me.settlements = spots[:3], spots[3:]
    me.roads = [board.VERTEX_EDGES[spots[3]][0]]
    cfg.me.hand = {"wheat": 2, "ore": 3, "wood": 1, "brick": 1}

    moves = {m.steps[-1].type: m.score for m in solver.solve(cfg)}
    assert moves["build_city"] > moves.get("build_road", -99)


def test_an_unreachable_position_is_not_scored_as_catastrophe():
    """A position with nothing reachable must stay comparable to a bad one.

    Charging a horizon per missing point put hundreds of turns into the total,
    and those swings landed straight in the move scores.
    """
    cfg = load(phase="main")  # nobody has built anything
    assert economy.turns_to_win(cfg, solver.build_ctx(cfg)) <= 2 * economy.HORIZON


def test_blocking_the_leader_beats_blocking_the_straggler():
    cfg = load(pending="move_robber")
    spots = _spread(cfg, 3)
    for color, v in zip(("red", "blue", "orange"), spots):
        _settle(cfg, color, v)
    cfg.players["blue"].vp_visible, cfg.players["blue"].resource_count = 7, 5
    cfg.players["orange"].vp_visible, cfg.players["orange"].resource_count = 2, 5

    best = solver.solve(cfg)[0]
    assert best.steps[0].robber_hex in board.VERTEX_HEXES[spots[1]], "should hit the leader"
    assert best.steps[0].robber_hex not in board.VERTEX_HEXES[spots[0]], "never block yourself"


def test_the_robber_cannot_outweigh_every_build():
    """One placement is worth a setback, not a game. It also only lasts until
    the next 7, which no single-position delta can express."""
    cfg = load(pending="move_robber")
    spots = _spread(cfg, 2)
    _settle(cfg, "red", spots[0])
    _settle(cfg, "blue", spots[1])
    cfg.players["blue"].resource_count = 8
    assert solver.solve(cfg)[0].score <= solver.ROBBER_CAP + 1.0


def test_a_port_you_would_take_is_worth_something():
    """Colonist reports the rates you have now, not the ones a move would give.

    Taking its number verbatim priced every port at zero in live games, because
    the settlement that earns the better rate has not been placed yet -- so the
    solver never saw a reason to settle one.
    """
    from app.models import Port

    cfg = load(phase="main")
    me = cfg.players["red"]
    spots = _spread(cfg, 4)
    me.settlements = spots[:2]
    port_at = spots[2]
    cfg.ports = [Port(type="ore", vertices=[port_at, board.VERTEX_ADJ[port_at][0]])]
    me.roads = [board.VERTEX_EDGES[port_at][0]]
    # a live game: colonist says every rate is currently 4:1
    cfg.me.bank_rates = {r: 4 for r in rules.RESOURCES}

    on_port = solver._after(cfg, [solver.MoveStep(type="build_settlement", vertex=port_at)])
    assert solver.build_ctx(on_port).rates["ore"] == 2, "the port must count once taken"

    elsewhere = solver._after(cfg, [solver.MoveStep(type="build_settlement", vertex=spots[3])])
    assert solver.build_ctx(elsewhere).rates["ore"] == 4


def test_a_reported_rate_still_wins_where_it_is_better():
    """Our port parsing cannot know about rule variants; colonist can."""
    cfg = load(phase="main")
    cfg.players["red"].settlements = _spread(cfg, 1)
    cfg.me.bank_rates = {**{r: 4 for r in rules.RESOURCES}, "sheep": 2}
    assert solver.build_ctx(cfg).rates["sheep"] == 2


def test_a_corner_a_rival_reaches_first_is_not_planned_on():
    """The ladder used to queue up spots someone else was closer to.

    Costing a settlement three roads away, while an opponent sits one road from
    it, is planning around a corner you will not get.
    """
    cfg = load(phase="main")
    spots = _spread(cfg, 6)
    _settle(cfg, "red", spots[0])
    ctx = solver.build_ctx(cfg)
    pos = economy._seed_spots(economy._build_position(cfg, ctx))
    open_to_us = economy._road_paths(cfg, "red", set())
    far = max(open_to_us, key=lambda v: len(open_to_us[v]))
    assert open_to_us[far], "need a corner we are not already on"

    assert far in pos.spots, "uncontested, it should be in the plan"
    # put blue's road network right next to it
    pos.rivals = {**pos.rivals, far: 0}
    assert far not in economy._reachable(pos), "a closer rival takes it off the plan"


def test_a_tie_is_still_worth_planning_for():
    """Arriving together is a race, not a loss."""
    cfg = load(phase="main")
    _settle(cfg, "red", _spread(cfg, 3)[0])
    ctx = solver.build_ctx(cfg)
    pos = economy._seed_spots(economy._build_position(cfg, ctx))
    paths = economy._road_paths(cfg, "red", set())
    v = next(iter(pos.spots))
    pos.rivals = {**pos.rivals, v: len(paths[v])}
    assert v in economy._reachable(pos)


# --- the race ---------------------------------------------------------------


def test_the_race_measures_everyone_not_just_us():
    cfg = load(phase="main")
    for color, v in zip(cfg.players, _spread(cfg, len(cfg.players))):
        _settle(cfg, color, v)
    r = solver.race(cfg)
    assert set(r["turns"]) == set(cfg.players), "every seat has a clock"
    assert r["leader"] != cfg.me.color, "the leader is somebody else"
    assert r["behind"] == pytest.approx(r["mine"] - r["leader_turns"])


def test_being_behind_makes_denial_worth_more():
    """Level with the field our own progress wins; far behind it cannot."""
    assert solver._denial_weight(0) < solver._denial_weight(5)
    assert solver._denial_weight(5) < solver._denial_weight(20)
    # but slowing the leader never outweighs winning outright
    assert solver._denial_weight(100) <= 1.0


def test_a_move_that_costs_the_leader_more_than_it_costs_us_is_worth_making():
    """Scoring only our own clock made every such move look like a loss."""
    cfg = load(phase="main")
    spots = _spread(cfg, 4)
    _settle(cfg, "red", spots[0])
    _settle(cfg, "blue", spots[1])
    cfg.players["blue"].vp_visible = 8
    cfg.me.hand = {"wood": 1, "brick": 1, "sheep": 1, "wheat": 1}

    contested = solver.race(cfg)
    assert contested["leader"] == "blue"
    # the weight is what carries a denial into positive territory at all
    assert solver._denial_weight(contested["behind"]) > 0


def test_an_award_a_rival_takes_first_stops_counting():
    """Longest Road and Largest Army go to one player.

    Every ladder plans them anyway, ours and each opponent's alike, so a
    four-player table can have four players counting the same two points. On
    recorded games this alone made the estimate up to six turns optimistic.
    """
    cfg = load(phase="main")
    for color, v in zip(cfg.players, _spread(cfg, len(cfg.players))):
        _settle(cfg, color, v)
    ctx = solver.build_ctx(cfg)

    counting_on_it = [s["kind"] for s in economy.plan(cfg, ctx)]
    gone = [s["kind"] for s in economy.plan(
        cfg, ctx, deadlines={"longest_road": 1.0, "army": 1.0})]

    assert "longest_road" in counting_on_it, "the naive ladder plans the award"
    assert "longest_road" not in gone, "a rival takes it first, so it is not ours"
    assert "army" not in gone


def test_an_award_we_reach_first_is_still_ours():
    cfg = load(phase="main")
    for color, v in zip(cfg.players, _spread(cfg, len(cfg.players))):
        _settle(cfg, color, v)
    ctx = solver.build_ctx(cfg)
    far_off = {"longest_road": 90.0, "army": 90.0}
    assert [s["kind"] for s in economy.plan(cfg, ctx, deadlines=far_off)] == [
        s["kind"] for s in economy.plan(cfg, ctx)
    ]


def test_an_exclusive_award_is_claimed_by_exactly_one_player():
    """Six cards for two points is the best rate on the board, so asked in
    isolation every ladder takes Longest Road first -- each computing the roads
    it needs against today's map, so all of them conclude they are three roads
    away. One of them is right."""
    cfg = load(phase="main")
    for color, v in zip(cfg.players, _spread(cfg, len(cfg.players))):
        _settle(cfg, color, v)
    r = solver.race(cfg)

    assert set(r["claims"]) <= set(solver.EXCLUSIVE)
    for kind, who in r["claims"].items():
        assert who in cfg.players
        # everybody else has it taken off them, us included
        assert (kind in r["deadlines"]) == (who != cfg.me.color)
        others = [c for c in r["plans"] if c != who]
        assert not any(s["kind"] == kind for c in others for s in r["plans"][c]), (
            f"{kind} is {who}'s; nobody else should still be planning it"
        )


def test_a_players_reported_points_are_not_ignored():
    """Counting a rival's points off the board alone misses hidden cards and
    awards, so a player on seven looked like one on one -- and the race then
    treated the front-runner as the least urgent player at the table."""
    cfg = load(phase="main")
    for color, v in zip(cfg.players, _spread(cfg, len(cfg.players))):
        _settle(cfg, color, v)
    rival = next(c for c in cfg.players if c != cfg.me.color)
    cfg.players[rival].vp_visible = 8

    assert solver._opponent_turns(cfg, rival) < solver._opponent_turns(
        cfg, next(c for c in cfg.players if c not in (cfg.me.color, rival))
    ), "eight points has to read as closer to winning than two"


def test_a_development_card_is_worth_more_than_nothing():
    """Buying one scored exactly zero -- the same as ending the turn -- because
    the position cannot see the face of the card it just bought."""
    cfg = load(phase="main")
    for color, v in zip(cfg.players, _spread(cfg, len(cfg.players))):
        _settle(cfg, color, v)
    cfg.players[next(c for c in cfg.players if c != cfg.me.color)].vp_visible = 8
    ctx = solver.build_ctx(cfg)
    due = solver.race(cfg)["deadlines"]
    base = economy.turns_to_win(cfg, ctx, deadlines=due)
    assert solver._dev_card_value(ctx, base, due) > 0


def test_the_robber_is_priced_against_the_front_runner_not_the_whole_table():
    """Slowing third place by four turns does not bring the game closer to us.

    Summing the setback over everyone a hex touches valued one placement at
    eleven turns -- more than winning outright.
    """
    cfg = load(pending="move_robber")
    for color, v in zip(("red", "blue", "orange"), _spread(cfg, 3)):
        _settle(cfg, color, v)
    cfg.players["blue"].vp_visible = 8
    best = solver.solve(cfg)[0]
    assert best.score <= 2 * solver.ROBBER_CAP + 1
    assert best.steps[0].robber_hex in board.VERTEX_HEXES[
        cfg.players["blue"].settlements[0]
    ], "the front-runner is the one worth blocking"


def test_free_resources_rarely_make_the_estimate_worse():
    """Greedy planning has an artefact: extra cards can change which rung is
    taken first and leave the total worse. It cannot be eliminated without
    searching properly, so it is bounded and measured instead -- if this starts
    failing, the ladder has become less stable, not more."""
    cfg = load(phase="main")
    for color, v in zip(cfg.players, _spread(cfg, len(cfg.players))):
        _settle(cfg, color, v)
    cfg.me.hand = {r: 1 for r in rules.RESOURCES}
    base = economy.turns_to_win(cfg, solver.build_ctx(cfg))

    worse = []
    for r in rules.RESOURCES:
        for extra in (1, 2, 3, 4, 6):
            richer = cfg.model_copy(deep=True)
            richer.me.hand[r] = richer.me.hand.get(r, 0) + extra
            got = economy.turns_to_win(richer, solver.build_ctx(richer))
            if got > base:
                worse.append(got - base)

    assert len(worse) <= 6, f"{len(worse)}/25 probes went backwards"
    assert max(worse, default=0) <= 3.0, "and never by more than a few turns"


def test_a_plan_that_commits_beats_taking_whatever_is_cheapest():
    """Szita, Chaslot and Spronck found their agent zigzagged: it preferred
    cities and cards while building far fewer settlements than a strong human,
    and weighting its rollouts to build more eagerly made it play *worse*. The
    failure was incoherence. Choosing among coherent plans and keeping the best
    can only improve on the greedy one, since greedy is among them."""
    cfg = load(phase="main")
    for color, v in zip(cfg.players, _spread(cfg, len(cfg.players))):
        _settle(cfg, color, v)
    ctx = solver.build_ctx(cfg)

    greedy = economy.turns_to_win(cfg, ctx, prefer="mixed")
    name, best = economy.best_strategy(cfg, ctx)
    assert best <= greedy + 1e-9, "the minimum over plans includes the greedy one"
    assert name in economy.STRATEGIES


def test_the_plans_actually_disagree_somewhere():
    """If every plan gave the same answer on every board there would be nothing
    to choose between them. Not every position offers a real choice, so this
    asks that some do."""
    disagreed = 0
    for hand in ({"ore": 3, "wheat": 2}, {"wood": 4, "brick": 4}, {},
                 {"sheep": 3, "wheat": 3, "ore": 3}):
        cfg = load(phase="main")
        for color, v in zip(cfg.players, _spread(cfg, len(cfg.players))):
            _settle(cfg, color, v)
        cfg.me.hand = dict(hand)
        ctx = solver.build_ctx(cfg)
        answers = {round(economy.turns_to_win(cfg, ctx, prefer=n), 1)
                   for n in economy.STRATEGIES}
        if len(answers) > 1:
            disagreed += 1
    assert disagreed, "the plans are indistinguishable on every board tried"


def test_a_hand_over_the_discard_limit_is_discounted():
    """Roughly every other round somebody rolls a seven, and half of everything
    above the limit goes back. Counting the whole hand spends cards that in
    practice are lost -- catanatron's tuned evaluation carries the same term."""
    cfg = load(phase="main")
    for color, v in zip(cfg.players, _spread(cfg, len(cfg.players))):
        _settle(cfg, color, v)

    cfg.me.hand = {r: 2 for r in rules.RESOURCES}          # 10 cards, over the limit
    ctx = solver.build_ctx(cfg)
    discounted = economy._build_position(cfg, ctx).hand
    assert sum(discounted.values()) < 10, "cards at risk are not cards in hand"

    cfg.me.hand = {"wood": 2, "brick": 2, "sheep": 2, "wheat": 1}   # 7, at the limit
    safe = economy._build_position(cfg, solver.build_ctx(cfg)).hand
    assert sum(safe.values()) == 7, "at the limit nothing is discarded"
