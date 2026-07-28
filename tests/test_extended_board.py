"""The 5-6 player extension: 30 hexes, six colours, a deeper deck.

Built around the board from a real 5-player game, so the fixture is a board
that was actually dealt rather than one that merely satisfies the counts.
"""
import pytest

from app import board, economy, rules, solver
from app.models import BoardConfig, MyState, PlayerState

# rows of 3-4-5-6-5-4-3, read off a live 5-player game
ROWS = [
    [("wood", 10), ("brick", 11), ("wheat", 11)],
    [("sheep", 6), ("wood", 9), ("wood", 4), ("brick", 8)],
    [("wheat", 3), ("ore", 5), ("ore", 2), ("brick", 5), ("wheat", 9)],
    [("ore", 8), ("sheep", 9), ("sheep", 6), ("ore", 3), ("wood", 10), ("desert", None)],
    [("sheep", 4), ("desert", None), ("brick", 12), ("sheep", 12), ("wheat", 3)],
    [("sheep", 8), ("wheat", 10), ("wood", 11), ("wood", 6)],
    [("ore", 2), ("brick", 5), ("wheat", 4)],
]


@pytest.fixture
def extended():
    board.use(board.EXTENDED)
    rules.use(rules.EXTENDED)
    yield BoardConfig(
        hexes=[{"resource": r, "number": n} for row in ROWS for r, n in row],
        players={c: PlayerState() for c in ("red", "blue", "orange", "green", "white")},
        me=MyState(color="white", hand={}),
        phase="main",
    )
    board.use(board.BASE)
    rules.use(rules.BASE)


def test_the_dealt_board_matches_the_extension(extended):
    assert len(extended.hexes) == 30
    counts = {}
    for t in extended.hexes:
        counts[t.resource] = counts.get(t.resource, 0) + 1
    assert counts == rules.EXTENDED.TILE_DISTRIBUTION
    tokens = {}
    for t in extended.hexes:
        if t.number:
            tokens[t.number] = tokens.get(t.number, 0) + 1
    assert tokens == rules.EXTENDED.TOKEN_DISTRIBUTION


def test_the_geometry_covers_every_corner_of_it(extended):
    assert board.ACTIVE.counts == (30, 80, 109)
    assert all(len(board.VERTEX_HEXES[v]) in (1, 2, 3) for v in range(80))
    # the middle row is the widest, so the board is a stretched hexagon
    assert board.ROW_SIZES == [3, 4, 5, 6, 5, 4, 3]


def test_a_five_player_game_is_solved_end_to_end(extended):
    """Geometry, rules and the turns model all have to agree on the new size."""
    spots = []
    for v in range(len(board.VERTICES)):
        if len(spots) == 5:
            break
        if all(v not in board.VERTEX_ADJ[o] for o in spots):
            spots.append(v)
    for color, v in zip(extended.players, spots):
        extended.players[color].settlements = [v]
        extended.players[color].roads = [board.VERTEX_EDGES[v][0]]
    extended.me.hand = {"wood": 1, "brick": 1, "sheep": 1, "wheat": 1}

    moves = solver.solve(extended)
    assert moves, "a 30-hex board must produce moves"
    assert all(0 <= (m.steps[-1].vertex or 0) < 80 for m in moves)
    assert economy.turns_to_win(extended, solver.build_ctx(extended)) < economy.LOST


def test_five_seats_slow_your_own_production(extended):
    """Cards arrive per round, and a round is longer with more players.

    More seats means more rolls before your turn returns, so the same corner
    pays more per turn of your own -- the model has to count seats, not assume
    four.
    """
    extended.players["white"].settlements = [10]
    five = sum(economy.production_rate(extended, "white").values())
    del extended.players["green"]
    four = sum(economy.production_rate(extended, "white").values())
    assert five > four


def test_the_extension_palette_has_six_colours():
    from app.live import protocol as P

    assert len(P.PALETTE) == 6
    assert P.map_color(5) and P.map_color(6)
    assert len({P.map_color(i) for i in range(1, 7)}) == 6, "six seats need six colours"
