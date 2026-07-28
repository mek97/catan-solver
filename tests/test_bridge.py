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
