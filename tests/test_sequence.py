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
