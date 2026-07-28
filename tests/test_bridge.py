"""Translating our board into catanatron's, and proving it is the same board.

A wrong mapping does not crash. It evaluates a different game and reports
confidently on that one, so every check here compares the two descriptions of
the position rather than trusting the arithmetic that produced them.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from catanatron.models.map import BASE_MAP_TEMPLATE, CatanMap  # noqa: E402

from app import board, bridge  # noqa: E402
from test_solver import load  # noqa: E402


def test_every_tile_and_corner_lines_up():
    result = bridge.verify(load(phase="main"))
    assert result["hexes"] == 19
    assert result["nodes"] == 54
    assert result["tiles_match"], "resources and numbers must survive the crossing"
    assert result["corners_match"], "a corner must touch the same tiles on both sides"


def test_a_node_shared_by_three_tiles_maps_to_one_vertex():
    """What makes the mapping a proof rather than a coincidence: node_mapping
    raises if any node arrives at two different vertices."""
    nodes = bridge.node_mapping(CatanMap.from_template(BASE_MAP_TEMPLATE))
    assert len(nodes) == 54
    assert len(set(nodes.values())) == 54, "and it is a bijection, not a collapse"


def test_edges_translate_to_ours():
    catan_map = CatanMap.from_template(BASE_MAP_TEMPLATE)
    edges = bridge.edge_mapping(bridge.node_mapping(catan_map))
    assert len(edges) == len(board.BASE.EDGE_VERTICES) == 72
    assert len(set(edges.values())) == 72, "each of our edges is named once"


def test_the_board_it_builds_carries_our_tiles_not_theirs():
    cfg = load(phase="main")
    dressed = bridge.dress_map(cfg)
    for cube, hid in bridge.hex_mapping(dressed).items():
        tile, ours = dressed.land_tiles[cube], cfg.hexes[hid]
        assert tile.number == ours.number
        assert tile.resource == bridge.RESOURCE_OUT[ours.resource]


def test_a_desert_stays_a_desert():
    cfg = load(phase="main")
    dressed = bridge.dress_map(cfg)
    deserts = [t for t in dressed.land_tiles.values() if t.resource is None]
    assert len(deserts) == 1 and deserts[0].number is None


def _live_positions():
    """Every recorded position, replayed."""
    from app.live.export import replay_frames
    from app.live.feed import LiveFeed
    from app.live.store import Store

    src = Store()
    rows = src._conn.execute(
        "SELECT payload, direction, opcode FROM frames ORDER BY id"
    ).fetchall()
    feed = LiveFeed(store=Store(path=":memory:"))
    for r in rows:
        feed.ingest(r["payload"], r["direction"], r["opcode"])
    for g in feed.store._conn.execute("SELECT game_id FROM games"):
        eng = replay_frames(list(feed.store.replay_frames(g["game_id"])))
        if not eng.state:
            continue
        cfg = eng.board_config()
        if len(cfg.hexes) == 19 and len(cfg.players) <= bridge.MAX_SEATS:
            yield g["game_id"], cfg


def test_both_engines_agree_on_every_longest_road():
    """The end-to-end check on the translation.

    Longest road depends on every piece on the board: our roads, and the
    opponents' settlements that cut them. If a single road landed on the wrong
    edge, or a player went missing, the numbers part company. They do not, on
    any player of any recorded position.
    """
    from catanatron.state_functions import get_longest_road_length

    from app import solver

    checked = 0
    for _game, cfg in _live_positions():
        state = bridge.to_state(cfg)
        for color, p in cfg.players.items():
            enemy = {
                v for q in cfg.players.values() if q is not p
                for v in q.settlements + q.cities
            }
            assert get_longest_road_length(
                state, bridge.COLOR_OUT[color]
            ) == solver._longest_road_length(set(p.roads), enemy), (
                f"{color} disagrees in {_game}"
            )
            checked += 1
    assert checked > 20, "the recordings should cover more than a handful"


def test_every_piece_crosses_over():
    for _game, cfg in _live_positions():
        state = bridge.to_state(cfg)
        for color, p in cfg.players.items():
            theirs = bridge.COLOR_OUT[color]
            roads = sum(1 for c in state.board.roads.values() if c == theirs) // 2
            assert roads == len(p.roads), f"{color} lost roads in translation"


def test_a_missing_seat_is_not_a_smaller_game():
    """colonist deals green, their palette has white. Dropping the player
    silently was not a smaller game -- their settlements are what cut everyone
    else's roads, and every longest road came out wrong."""
    assert set(bridge.COLOR_OUT) == {"red", "blue", "orange", "green"}
    assert len(set(bridge.COLOR_OUT.values())) == 4, "four seats, four colours"


def test_a_five_player_board_is_refused_with_a_reason():
    """catanatron is a four-player, nineteen-hex engine -- four colours and no
    5-6 player extension. A position it cannot hold should say so, not fail
    obscurely halfway through building a state."""
    import pytest

    from app import rules
    from app.models import BoardConfig, MyState, PlayerState

    board.use(board.EXTENDED)
    rules.use(rules.EXTENDED)
    try:
        big = BoardConfig(
            hexes=[{"resource": "wood", "number": 6} for _ in range(30)],
            players={c: PlayerState() for c in
                     ("red", "blue", "orange", "green", "white")},
            me=MyState(color="red"),
            phase="main",
        )
        why = bridge.supported(big)
        assert why and "19-hex" in why
        with pytest.raises(ValueError):
            bridge.to_state(big)
    finally:
        board.use(board.BASE)
        rules.use(rules.BASE)

    assert bridge.supported(load(phase="main")) is None, "the base board is fine"
