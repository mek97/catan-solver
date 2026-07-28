"""Turn sequence: the opening draft, and what may happen before the dice.

Both are ordering rules rather than scoring ones -- they decide which moves
exist, not which is best -- so these tests are about availability and shape.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from app import board, solver  # noqa: E402
from app.live.engine import GameEngine  # noqa: E402
from test_solver import load  # noqa: E402

ORDER = ["red", "blue", "orange", "green"]


def _seated(seat: int, phase: str = "setup1", order=None):
    cfg = load(phase=phase)
    order = list(order or ORDER)
    rest = [c for c in order if c != "red"]
    rest.insert(seat, "red")
    cfg.play_order = rest
    return cfg


# --- the opening draft -------------------------------------------------------


@pytest.mark.parametrize("seat,expected", [(0, 6), (1, 4), (2, 2), (3, 0)])
def test_the_draft_snakes_back(seat, expected):
    """Seat i of N places at i and again at 2N-1-i: 2N-2i-2 picks in between."""
    assert solver._draft_gap(_seated(seat)) == expected


def test_the_last_seat_picks_twice_in_a_row():
    assert solver._draft_gap(_seated(3)) == 0


@pytest.mark.parametrize("n", [3, 5, 6])
def test_the_snake_holds_at_any_table_size(n):
    order = ["red", "blue", "orange", "green", "white", "brown"][:n]
    assert solver._draft_gap(_seated(0, order=order)) == 2 * n - 2
    assert solver._draft_gap(_seated(n - 1, order=order)) == 0


def test_no_lookahead_once_the_second_settlement_is_the_one_being_placed():
    """In setup2 there is nothing left to look ahead to."""
    assert solver._draft_gap(_seated(0, phase="setup2")) is None
    assert solver._draft_gap(_seated(0, phase="main")) is None


def test_unknown_seating_is_not_guessed():
    cfg = load(phase="setup1")
    cfg.play_order = []
    assert solver._draft_gap(cfg) is None


def test_the_forecast_takes_the_good_corners_away():
    """Rivals pick between your turns, and they pick the best that is left."""
    cfg = _seated(0)
    ctx = solver.build_ctx(cfg)
    mine = max(range(len(board.VERTICES)), key=lambda v: solver.vertex_prod(ctx, v))

    none_taken = solver._draft_forecast(ctx, mine, 0)
    after_six = solver._draft_forecast(ctx, mine, 6)
    assert len(after_six) < len(none_taken), "six placements must cost you options"
    assert mine not in none_taken, "your own corner is not open to you twice"
    best_before = max(solver.vertex_prod(ctx, v) for v in none_taken)
    best_after = max(solver.vertex_prod(ctx, v) for v in after_six)
    assert best_after <= best_before, "what is left cannot be better than what was"


def test_where_you_sit_changes_what_you_are_told():
    first = solver.solve(_seated(0))[0]
    last = solver.solve(_seated(3))[0]
    assert "6 more placements" in first.reasoning
    assert "immediately" in last.reasoning


# --- before the dice ---------------------------------------------------------


def _ready_to_roll():
    cfg = load(phase="main")
    v = max(
        range(54),
        key=lambda x: sum(
            solver.PIPS.get(cfg.hexes[h].number, 0)
            for h in board.VERTEX_HEXES[x]
            if cfg.hexes[h].number
        ),
    )
    cfg.players["red"].settlements = [v]
    cfg.players["red"].roads = [board.VERTEX_EDGES[v][0]]
    cfg.me.hand = {"wood": 2, "brick": 2, "sheep": 2, "wheat": 2, "ore": 2}
    cfg.pending = "roll"
    return cfg, v


def test_nothing_is_buildable_before_the_dice():
    cfg, _ = _ready_to_roll()
    kinds = {m.steps[0].type for m in solver.solve(cfg)}
    assert kinds == {"roll_dice"}


def test_a_knight_is_the_one_card_you_may_play_first():
    cfg, _ = _ready_to_roll()
    cfg.me.dev_cards.knight = 1
    kinds = {m.steps[0].type for m in solver.solve(cfg)}
    assert kinds == {"roll_dice", "play_knight"}


def test_a_knight_already_played_is_not_offered_again():
    cfg, _ = _ready_to_roll()
    cfg.me.dev_cards.knight = 1
    cfg.me.dev_card_played_this_turn = True
    assert {m.steps[0].type for m in solver.solve(cfg)} == {"roll_dice"}


def test_freeing_your_own_hex_before_the_roll_is_the_advice():
    """A knight played after the roll cannot undo a roll that already paid
    nothing. On your own blocked hex, playing it first is the whole point."""
    cfg, v = _ready_to_roll()
    cfg.me.dev_cards.knight = 1
    cfg.robber_hex = board.VERTEX_HEXES[v][0]
    best = solver.solve(cfg)[0]
    assert best.steps[0].type == "play_knight"


def test_the_engine_reads_the_roll_from_colonist_not_from_a_guess():
    """diceState.diceThrown states it outright; the action state does not."""
    eng = GameEngine()
    eng.my_color_id = 1
    eng.state = {"diceState": {"diceThrown": False}}
    assert eng.dice_thrown() is False
    eng.state = {"diceState": {"dice1": 3, "dice2": 4, "diceThrown": True}}
    assert eng.dice_thrown() is True
    eng.state = {}
    assert eng.dice_thrown() is False


# --- road building owes you two ---------------------------------------------


def test_the_second_free_road_is_still_owed_after_the_first():
    """Reported live: placing one road and the app treated the pair as done.

    colonist has no field for how many free roads remain, and its action state
    clears once the first is down -- observed as 30 -> 31 -> 0 with the second
    road still to come. Counting them from the card is what survives that.
    """
    eng = GameEngine()
    eng.my_color_id = 1
    me = eng.my_color

    eng._track_free_roads({"kind": "dev_card_played", "color": me, "card": "road_building"})
    assert eng.free_roads == 2
    eng._track_free_roads({"kind": "piece_placed", "color": me, "piece": "road"})
    assert eng.free_roads == 1, "one road placed, one still owed"
    eng._track_free_roads({"kind": "piece_placed", "color": me, "piece": "road"})
    assert eng.free_roads == 0


def test_somebody_else_playing_road_building_owes_us_nothing():
    eng = GameEngine()
    eng.my_color_id = 1
    eng._track_free_roads({"kind": "dev_card_played", "color": "blue",
                           "card": "road_building"})
    assert eng.free_roads == 0


def test_an_unplaced_free_road_does_not_survive_the_turn():
    eng = GameEngine()
    eng.my_color_id = 1
    me = eng.my_color
    eng._track_free_roads({"kind": "dev_card_played", "color": me, "card": "road_building"})
    eng._track_free_roads({"kind": "turn_ended", "color": me})
    assert eng.free_roads == 0


def test_only_one_development_card_a_turn():
    """colonist reports it directly; both paths that offer cards must respect it."""
    from app.live.advisor import dev_card_plays

    played = _seated(0, phase="main")
    played.me.dev_cards.knight = 2
    played.me.dev_card_played_this_turn = True
    assert not [m for m in solver.solve(played) if m.steps[0].type.startswith("play_")]

    class Eng(GameEngine):
        def my_dev_cards(self):
            return {"count": 2, "known": {"knight": 2}, "hidden": 0, "used": 1,
                    "bought_this_turn": {}, "playable": {"knight": 2},
                    "played_this_turn": True}

    assert dev_card_plays(Eng(), played) == []


# --- a played card runs to the end of its action -----------------------------


def test_a_knight_keeps_advising_through_the_steal():
    """Playing a knight is three steps, and the card leaves your hand on the
    first. Traced live: state 24 (move it) -> 27 (pick a victim) -> 0. Without
    the third, the advice stopped halfway through the action it started."""
    from test_economy import _settle, _spread
    from test_solver import load as _load

    cfg = _load(phase="main", pending="steal")
    for color, v in zip(("red", "blue", "orange"), _spread(cfg, 3)):
        _settle(cfg, color, v)
    cfg.robber_hex = board.VERTEX_HEXES[cfg.players["blue"].settlements[0]][0]
    cfg.players["blue"].resource_count = 7
    cfg.players["blue"].vp_visible = 8
    cfg.players["orange"].resource_count = 2

    moves = solver.solve(cfg)
    assert moves and all(m.steps[0].type == "steal" for m in moves)
    assert moves[0].steps[0].steal_from == "blue", "adjacent, holding cards, closest to winning"


def test_nobody_worth_robbing_still_says_so():
    from test_economy import _settle, _spread
    from test_solver import load as _load

    cfg = _load(phase="main", pending="steal")
    for color, v in zip(("red", "blue"), _spread(cfg, 2)):
        _settle(cfg, color, v)
    cfg.robber_hex = board.VERTEX_HEXES[cfg.players["red"].settlements[0]][0]
    moves = solver.solve(cfg)
    assert moves and moves[0].steps[0].steal_from is None


def test_an_offer_you_answered_is_not_asked_again():
    """It stays on the table waiting on the player who made it. Re-advising it
    asks you to decide the same thing twice and hides that it is in progress."""
    from app.live.advisor import offer_advice

    class Eng:
        def trade_offers(self):
            return [
                {"id": "a", "from": "blue", "from_me": False, "my_response": 1,
                 "offers": ["ore"], "wants": ["sheep"]},
                {"id": "b", "from": "green", "from_me": False, "my_response": 0,
                 "offers": ["wood"], "wants": ["brick"]},
            ]

    cfg = _seated(0, phase="main")
    cfg.me.hand = {"sheep": 2, "brick": 2}
    verdicts = {a["id"]: a["verdict"] for a in offer_advice(Eng(), cfg)}
    assert verdicts["a"] == "waiting", "already accepted -- in progress"
    assert verdicts["b"] != "waiting", "unanswered -- still a decision"


def test_the_opening_road_is_shown_once_the_settlement_is_down():
    """Reported live: place the opening settlement and the road hint vanishes.

    The phase is worked out from how many settlements are down, so the moment
    one lands the phase reads as the *next* placement and the advice jumps to a
    different corner -- taking with it the road you still owe on this one.
    """
    cfg = _seated(0, phase="setup1")
    first = solver.solve(cfg)[0]
    v = first.steps[0].vertex

    cfg.players["red"].settlements = [v]
    cfg.phase = "setup2"          # what the engine reports at that moment
    cfg.pending = "setup_road"

    moves = solver.solve(cfg)
    assert moves, "the road still has to be placed"
    for m in moves:
        assert m.steps[0].type == "setup_road"
        assert v in board.EDGE_VERTICES[m.steps[0].edge], "it must touch that settlement"


def test_the_engine_spots_a_settlement_with_no_road():
    eng = GameEngine()
    eng.my_color_id = 1
    eng.maps = {"corners": {"7": 7}, "edges": {"3": 3}, "hexes": {}}
    eng.state = {"mapState": {"tileCornerStates": {"7": {"owner": 1}},
                              "tileEdgeStates": {}}}
    assert eng.setup_road_owed() == 7

    attached = next(e for e in board.VERTEX_EDGES[7])
    eng.maps["edges"] = {"3": attached}
    eng.state["mapState"]["tileEdgeStates"] = {"3": {"owner": 1}}
    assert eng.setup_road_owed() is None, "settled and connected -- nothing owed"


def test_the_second_opening_road_is_shown_too():
    """Reported live: it worked for the first settlement and not the second.

    Placing the second takes the count to two, so the phase reads "main"
    immediately -- while colonist is still asking for the road that comes with
    it. Gating the check on the phase covered one placement and missed the one
    after it.
    """
    eng = GameEngine()
    eng.my_color_id = 1
    v1, v2 = 7, 20
    attached = board.VERTEX_EDGES[v1][0]
    eng.maps = {"corners": {"a": v1, "b": v2}, "edges": {"x": attached}, "hexes": {}}
    eng.state = {
        "mapState": {
            "tileCornerStates": {"a": {"owner": 1}, "b": {"owner": 1}},
            "tileEdgeStates": {"x": {"owner": 1}},
        },
        "playerStates": {str(i): {} for i in (1, 2, 3, 4)},
    }
    assert eng.phase() == "main", "two settlements down: the phase has moved on"
    assert eng.setup_road_owed() == v2, "the road for the second one is still owed"


def test_a_settled_and_connected_board_owes_nothing():
    eng = GameEngine()
    eng.my_color_id = 1
    v = 7
    eng.maps = {"corners": {"a": v}, "edges": {"x": board.VERTEX_EDGES[v][0]}, "hexes": {}}
    eng.state = {"mapState": {"tileCornerStates": {"a": {"owner": 1}},
                              "tileEdgeStates": {"x": {"owner": 1}}}}
    assert eng.setup_road_owed() is None
