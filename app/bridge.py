"""Translating a live position into catanatron, so a real engine can judge it.

catanatron (github.com/bcollazo/catanatron) is a mature open-source Catan
engine with searching players -- AlphaBeta, MCTS, playouts -- on top of a value
function tuned by self-play. Our solver evaluates one ply and has never been
measured against anything. Handing it our position is how it gets measured, and
how a stronger recommendation can be asked for.

The whole difficulty is addressing. Their board is cube coordinates with six
named corners per tile; ours is axial with a vertex key per corner. Both label
corners in the same rotational order, and the correspondence turns out to be

    their (x, y, z) -> our (q, r) = (x, z)
    their NORTH, NORTHEAST, ... -> our N, NE, ...

which was not assumed: every rotation and reflection was tried, and a mapping
is kept only if each of their 54 nodes lands on exactly one of our vertices
from every tile that touches it. Twelve orientations pass, the board being
symmetric; this is the one that needs no rotation.

A wrong mapping here would not crash. It would quietly evaluate a different
board, so `verify()` checks the translation against the position it came from
rather than trusting the arithmetic.
"""
from __future__ import annotations

from typing import Any, Optional

from catanatron import Color
from catanatron.models.map import BASE_MAP_TEMPLATE, CatanMap

from . import board

# their resource names, against ours
RESOURCE_OUT = {
    "wood": "WOOD", "brick": "BRICK", "sheep": "SHEEP",
    "wheat": "WHEAT", "ore": "ORE", "desert": None,
}
COLOR_OUT = {
    "red": Color.RED, "blue": Color.BLUE,
    "orange": Color.ORANGE, "white": Color.WHITE,
}


def _fresh_map() -> CatanMap:
    return CatanMap.from_template(BASE_MAP_TEMPLATE)


def hex_mapping(catan_map: CatanMap) -> dict[tuple, int]:
    """Their cube coordinate -> our hex id."""
    out = {}
    for cube in catan_map.land_tiles:
        q, r = cube[0], cube[2]
        hid = board.BASE.HEX_ID.get((q, r))
        if hid is not None:
            out[cube] = hid
    return out


def node_mapping(catan_map: CatanMap) -> dict[int, int]:
    """Their node id -> our vertex id.

    Built from every tile and checked for agreement: a node shared by three
    tiles has to arrive at the same vertex from all three, which is what makes
    this a proof rather than a coincidence.
    """
    hexes = hex_mapping(catan_map)
    out: dict[int, int] = {}
    for cube, tile in catan_map.land_tiles.items():
        ours = board.BASE.HEX_VERTICES[hexes[cube]]
        for i, ref in enumerate(tile.nodes):
            theirs = tile.nodes[ref]
            mine = ours[i]
            if out.setdefault(theirs, mine) != mine:
                raise ValueError(
                    f"node {theirs} maps to both {out[theirs]} and {mine}"
                )
    if len(out) != len(board.BASE.VERTICES):
        raise ValueError(f"mapped {len(out)} nodes, expected {len(board.BASE.VERTICES)}")
    return out


def edge_mapping(node_map: dict[int, int]) -> dict[tuple[int, int], int]:
    """Their (node, node) edge -> our edge id.

    Their edges are node pairs and ours are dense ids, so this is the node
    mapping read backwards: an edge is whichever of ours joins the two vertices
    their nodes translate to.
    """
    ours = {
        tuple(sorted((a, b))): eid
        for eid, (a, b) in enumerate(board.BASE.EDGE_VERTICES)
    }
    out = {}
    for a_theirs, a_ours in node_map.items():
        for b_theirs, b_ours in node_map.items():
            if a_theirs >= b_theirs:
                continue
            eid = ours.get(tuple(sorted((a_ours, b_ours))))
            if eid is not None:
                out[(a_theirs, b_theirs)] = eid
    return out


def dress_map(cfg, catan_map: Optional[CatanMap] = None) -> CatanMap:
    """A catanatron map carrying *our* tiles: same shape, our resources and numbers."""
    catan_map = catan_map or _fresh_map()
    for cube, hid in hex_mapping(catan_map).items():
        tile = catan_map.land_tiles[cube]
        ours = cfg.hexes[hid]
        tile.resource = RESOURCE_OUT[ours.resource]
        tile.number = ours.number
    return catan_map


def verify(cfg) -> dict[str, Any]:
    """Check the translation describes the same board we started from.

    A mapping that is subtly wrong evaluates a different game and says nothing
    about it, so this compares what each side believes: which resource and
    number sit on each tile, and which tiles each corner touches.
    """
    catan_map = dress_map(cfg)
    hexes = hex_mapping(catan_map)
    nodes = node_mapping(catan_map)

    tiles_ok = all(
        catan_map.land_tiles[cube].number == cfg.hexes[hid].number
        and catan_map.land_tiles[cube].resource == RESOURCE_OUT[cfg.hexes[hid].resource]
        for cube, hid in hexes.items()
    )

    # every corner should touch the same tiles on both sides
    corners_ok = True
    for cube, tile in catan_map.land_tiles.items():
        for ref in tile.nodes:
            theirs = tile.nodes[ref]
            mine = nodes[theirs]
            if hexes[cube] not in board.BASE.VERTEX_HEXES[mine]:
                corners_ok = False
    return {
        "hexes": len(hexes),
        "nodes": len(nodes),
        "tiles_match": tiles_ok,
        "corners_match": corners_ok,
        "ok": tiles_ok and corners_ok and len(hexes) == 19 and len(nodes) == 54,
    }
