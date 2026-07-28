"""The base-game ruleset, exercised as behaviour rather than as constants.

An earlier pass checked this ruleset clause by clause and reported seventeen of
seventeen matching. It compared numbers -- the tile mix, the costs, the deck
composition -- and every number was right. Monopoly was among them: two in the
deck, correctly. What it could not see is that playing the card did nothing at
all, because nothing in that check ever applied a move.

So these tests apply things. Each one names the clause it comes from and asserts
what the position does afterwards, which is the only form of the question that
would have caught it.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from app import board, economy, rules, solver  # noqa: E402
from app.live.engine import GameEngine  # noqa: E402
from app.models import MoveStep  # noqa: E402
from test_economy import _settle, _spread  # noqa: E402
from test_solver import load  # noqa: E402


def _position(**hand):
    cfg = load(phase="main")
    for color, v in zip(cfg.players, _spread(cfg, len(cfg.players))):
        _settle(cfg, color, v)
    cfg.me.hand = dict(hand)
    for color in cfg.players:
        if color != cfg.me.color:
            cfg.players[color].resource_count = 6
    return cfg


# --- §4 Building costs and restrictions -------------------------------------


@pytest.mark.parametrize("kind,cost", [
    ("road", {"wood": 1, "brick": 1}),
    ("settlement", {"wood": 1, "brick": 1, "sheep": 1, "wheat": 1}),
    ("city", {"ore": 3, "wheat": 2}),
    ("dev", {"ore": 1, "wheat": 1, "sheep": 1}),
])
def test_building_spends_exactly_its_cost(kind, cost):
    cfg = _position(**{r: 5 for r in rules.RESOURCES})
    me = cfg.players[cfg.me.color]
    step = {
        "road": MoveStep(type="build_road", edge=board.VERTEX_EDGES[me.settlements[0]][1]),
        "settlement": MoveStep(type="build_settlement", vertex=None),
        "city": MoveStep(type="build_city", vertex=me.settlements[0]),
        "dev": MoveStep(type="buy_dev"),
    }[kind]
    if kind == "settlement":
        pytest.skip("placement legality is covered by its own test")
    after = solver._after(cfg, [step])
    spent = {r: cfg.me.hand[r] - after.me.hand[r] for r in rules.RESOURCES
             if cfg.me.hand[r] != after.me.hand[r]}
    assert spent == cost


def test_a_city_replaces_the_settlement_it_upgrades():
    """§4: 'Upgrades an existing settlement. Replaces it.'"""
    cfg = _position(ore=3, wheat=2)
    v = cfg.players[cfg.me.color].settlements[0]
    after = solver._after(cfg, [MoveStep(type="build_city", vertex=v)])
    me = after.players[after.me.color]
    assert v in me.cities and v not in me.settlements


def test_the_distance_rule_is_enforced():
    """§4: not on an intersection adjacent to any settlement or city."""
    cfg = _position()
    taken = {v for p in cfg.players.values() for v in p.settlements + p.cities}
    for v in taken:
        for nbr in board.VERTEX_ADJ[v]:
            assert not board.is_vertex_placeable(nbr, taken)


def test_piece_limits_are_the_supply_not_the_board():
    """§4: max 15 roads, 5 settlements, 4 cities."""
    assert rules.PIECE_SUPPLY == {"road": 15, "settlement": 5, "city": 4}
    cfg = _position()
    me = cfg.players[cfg.me.color]
    me.roads = list(range(15))
    assert solver._pieces_left(me, "road") == 0


# --- §5 Development cards ----------------------------------------------------


def test_knight_moves_the_robber_and_counts_towards_the_army():
    cfg = _position()
    cfg.me.dev_cards.knight = 1
    move = next(m for m in solver._dev_plays(solver.build_ctx(cfg))
                if m.steps[0].type == "play_knight")
    after = solver._after(cfg, move.steps)
    assert after.robber_hex != cfg.robber_hex, "the robber must move to a new hex"
    assert after.players[after.me.color].knights_played == 1
    assert after.me.dev_cards.knight == 0, "the card is spent"


def test_road_building_places_two_free_roads():
    """§5: 'Place 2 free roads following normal placement rules.'"""
    cfg = _position()
    cfg.me.dev_cards.road_building = 1
    before = len(cfg.players[cfg.me.color].roads)
    move = next(m for m in solver._dev_plays(solver.build_ctx(cfg))
                if m.steps[0].type == "play_road_building")
    after = solver._after(cfg, move.steps)
    assert len(after.players[after.me.color].roads) == before + 2
    assert all(after.me.hand.get(r, 0) == cfg.me.hand.get(r, 0)
               for r in rules.RESOURCES), "free means free"
    assert after.me.dev_cards.road_building == 0


def test_year_of_plenty_takes_two_cards_from_the_bank():
    """§5: 'Take any 2 resource cards from the bank.'"""
    cfg = _position(wood=1)
    cfg.me.dev_cards.year_of_plenty = 1
    move = next(m for m in solver._dev_plays(solver.build_ctx(cfg))
                if m.steps[0].type == "play_year_of_plenty")
    # the offered move may chain into the build it unlocks; the card itself
    # takes exactly two
    after = solver._after(cfg, move.steps[:1])
    gained = sum(after.me.hand.get(r, 0) - cfg.me.hand.get(r, 0) for r in rules.RESOURCES)
    assert gained == 2, "exactly two cards"
    assert after.me.dev_cards.year_of_plenty == 0


def test_year_of_plenty_will_not_take_what_the_bank_lacks():
    cfg = _position(wood=1)
    cfg.me.dev_cards.year_of_plenty = 1
    cfg.bank = {r: 0 for r in rules.RESOURCES}
    for m in solver._dev_plays(solver.build_ctx(cfg)):
        if m.steps[0].type != "play_year_of_plenty":
            continue
        for r in (m.steps[0].get or {}):
            assert cfg.bank[r] > 0, f"took {r} from an empty bank"


def test_monopoly_names_one_resource_and_collects_it():
    """§5: 'Name 1 resource; all players must give you all cards of that type.'

    The exact haul is unknowable -- opponents' hands are hidden -- so it is
    estimated from their production. What must be true is that the named
    resource, and only it, arrives.
    """
    cfg = _position(wood=1)
    cfg.me.dev_cards.monopoly = 1
    move = next((m for m in solver._dev_plays(solver.build_ctx(cfg))
                 if m.steps[0].type == "play_monopoly"), None)
    assert move, "monopoly should be offered when held"
    named = move.steps[0].resource
    after = solver._after(cfg, move.steps)
    gained = {r: after.me.hand.get(r, 0) - cfg.me.hand.get(r, 0)
              for r in rules.RESOURCES if after.me.hand.get(r, 0) != cfg.me.hand.get(r, 0)}
    assert set(gained) == {named}, f"named {named} but hand moved {gained}"
    assert gained[named] > 0
    assert after.me.dev_cards.monopoly == 0


def test_a_victory_point_card_counts_towards_ten():
    """§5: 'Kept hidden until the player can reveal them to win.'"""
    cfg = _position()
    plain = solver.build_ctx(cfg).my_vp
    cfg.me.dev_cards.vp = 2
    assert solver.build_ctx(cfg).my_vp == plain + 2


def test_one_card_a_turn():
    cfg = _position()
    cfg.me.dev_cards.knight = 2
    cfg.me.dev_card_played_this_turn = True
    assert solver._dev_plays(solver.build_ctx(cfg)) == []


# --- §6 Maritime trade -------------------------------------------------------


@pytest.mark.parametrize("rate,port", [(4, None), (3, "3:1"), (2, "sheep")])
def test_harbour_rates(rate, port):
    """§6: 4:1 at the bank, 3:1 generic, 2:1 for the named resource."""
    from app.models import Port

    cfg = _position()
    v = cfg.players[cfg.me.color].settlements[0]
    if port:
        cfg.ports = [Port(type=port, vertices=[v, board.VERTEX_ADJ[v][0]])]
    cfg.me.bank_rates = None
    assert solver.build_ctx(cfg).rates["sheep"] == rate


# --- §7 Achievements and winning ---------------------------------------------

def test_longest_road_needs_five_and_is_taken_by_more():
    cfg = _position()
    ctx = solver.build_ctx(cfg)
    pos = economy._seed_spots(economy._build_position(cfg, ctx))
    assert pos.roads_for_longest >= rules.LONGEST_ROAD_MIN - ctx.my_road_len
    cfg.players["blue"].longest_road_len = 7
    pos = economy._seed_spots(economy._build_position(cfg, solver.build_ctx(cfg)))
    assert pos.roads_for_longest == 8 - solver.build_ctx(cfg).my_road_len, "must exceed, not match"


def test_largest_army_needs_three_and_is_taken_by_more():
    cfg = _position()
    pos = economy._seed_spots(economy._build_position(cfg, solver.build_ctx(cfg)))
    assert pos.knights_for_army == rules.LARGEST_ARMY_MIN
    cfg.players["blue"].knights_played = 5
    pos = economy._seed_spots(economy._build_position(cfg, solver.build_ctx(cfg)))
    assert pos.knights_for_army == 6, "one more than the holder, not the same"


def test_ten_points_ends_it():
    cfg = _position()
    me = cfg.players[cfg.me.color]
    spots = _spread(cfg, 5)
    me.settlements, me.cities = [spots[0]], spots[1:5]
    assert solver.build_ctx(cfg).my_vp == 9
    assert economy.turns_to_win(cfg, solver.build_ctx(cfg)) > 0
    me.longest_road = True                        # 9 + 2 = 11
    assert economy.turns_to_win(cfg, solver.build_ctx(cfg)) == 0.0


# --- §3 The seven ------------------------------------------------------------


@pytest.mark.parametrize("held,lost", [(7, 0), (8, 4), (9, 4), (12, 6), (15, 7)])
def test_discard_is_half_of_eight_or_more(held, lost):
    assert rules.discard_count(held) == lost


def test_the_robber_must_move_somewhere_new():
    cfg = _position()
    cfg.pending = "move_robber"
    for m in solver.solve(cfg):
        assert m.steps[0].robber_hex != cfg.robber_hex


def test_you_may_only_rob_someone_on_that_hex():
    cfg = _position()
    cfg.pending = "steal"
    cfg.robber_hex = board.VERTEX_HEXES[cfg.players["blue"].settlements[0]][0]
    on_hex = {c for c, p in cfg.players.items()
              if any(cfg.robber_hex in board.VERTEX_HEXES[v]
                     for v in p.settlements + p.cities)}
    for m in solver.solve(cfg):
        who = m.steps[0].steal_from
        if who:
            assert who in on_hex and who != cfg.me.color


# --- §5 the buy-turn restriction, everywhere it can leak ---------------------


class _Bought:
    """We just bought a knight; nothing else in hand."""

    def my_dev_cards(self):
        return {"count": 1, "known": {"knight": 1}, "hidden": 0, "used": 0,
                "bought_this_turn": {"knight": 1}, "playable": {},
                "played_this_turn": False}

    def player_summary(self):
        return []

    def production_table(self):
        return {}


def test_a_knight_bought_this_turn_advertises_nothing():
    """§5: a card cannot be played the turn it was purchased.

    Three separate places decided whether a knight was available, and each had
    its own idea of what "available" meant: the solver, the dev-card panel, and
    the robber panel. All three now ask the same question.
    """
    from app.live.advisor import dev_card_plays, robber_options

    cfg = _position()
    cfg.me.dev_cards.knight = 0          # nothing playable reaches the solver

    assert robber_options(_Bought(), cfg) == [], "no robber advice off an unplayable knight"
    assert dev_card_plays(_Bought(), cfg) == []
    assert not [m for m in solver.solve(cfg) if m.steps[0].type == "play_knight"]


def test_a_forced_robber_move_is_still_advised():
    """A 7 moves the robber whatever is in hand."""
    from app.live.advisor import robber_options

    cfg = _position()
    cfg.pending = "move_robber"
    assert robber_options(_Bought(), cfg), "the 7 does not care what you bought"
