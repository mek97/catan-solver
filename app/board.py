"""Canonical geometry for the standard 19-hex Catan board.

Single source of truth for hex / vertex / edge IDs. The frontend fetches this
via /api/geometry and never derives geometry itself; the vision parser refers
to hexes by (row, pos) and pieces by hex+corner/edge, resolved here.

Coordinate system: axial (q, r), pointy-top hexes, screen y grows downward.
Every vertex is the North corner of exactly one hex coordinate or the South
corner of exactly one hex coordinate (the naming hex may lie outside the 19
land hexes) -- vertex key = (q, r, "N"|"S").

External IDs are dense ints assigned deterministically:
  hex_id    0..18  row-major (rows of 3-4-5-4-3, top to bottom)
  vertex_id 0..53  sorted by pixel (y, x)
  edge_id   0..71  sorted by midpoint (y, x)
"""
from __future__ import annotations

import math
from collections import Counter

SIZE = 50.0
_SQRT3 = math.sqrt(3.0)

HexCoord = tuple[int, int]
VertexKey = tuple[int, int, str]

CORNER_DIRS = ["N", "NE", "SE", "S", "SW", "NW"]  # clockwise
EDGE_DIRS = ["NE", "E", "SE", "SW", "W", "NW"]    # edge i joins corner i and i+1


def _hex_center(q: int, r: int) -> tuple[float, float]:
    return (_SQRT3 * SIZE * (q + r / 2.0), 1.5 * SIZE * r)


def _corner_key(q: int, r: int, corner: str) -> VertexKey:
    if corner == "N":
        return (q, r, "N")
    if corner == "NE":
        return (q + 1, r - 1, "S")
    if corner == "SE":
        return (q, r + 1, "N")
    if corner == "S":
        return (q, r, "S")
    if corner == "SW":
        return (q - 1, r + 1, "N")
    if corner == "NW":
        return (q, r - 1, "S")
    raise ValueError(f"unknown corner {corner!r}")


def _vertex_pixel(key: VertexKey) -> tuple[float, float]:
    q, r, d = key
    cx, cy = _hex_center(q, r)
    return (cx, cy - SIZE) if d == "N" else (cx, cy + SIZE)


# --- hexes ------------------------------------------------------------------

HEXES: list[HexCoord] = sorted(
    (
        (q, r)
        for r in range(-2, 3)
        for q in range(max(-2, -2 - r), min(2, 2 - r) + 1)
    ),
    key=lambda h: (h[1], h[0]),
)
HEX_ID: dict[HexCoord, int] = {h: i for i, h in enumerate(HEXES)}
ROW_SIZES = [3, 4, 5, 4, 3]
ROW_OFFSETS = [0, 3, 7, 12, 16]
HEX_PIXEL: list[tuple[float, float]] = [_hex_center(q, r) for q, r in HEXES]

# --- vertices ---------------------------------------------------------------

_vertex_keys = {_corner_key(q, r, c) for q, r in HEXES for c in CORNER_DIRS}
VERTICES: list[VertexKey] = sorted(
    _vertex_keys,
    key=lambda k: (round(_vertex_pixel(k)[1], 3), round(_vertex_pixel(k)[0], 3)),
)
VERTEX_ID: dict[VertexKey, int] = {k: i for i, k in enumerate(VERTICES)}
VERTEX_PIXEL: list[tuple[float, float]] = [_vertex_pixel(k) for k in VERTICES]

HEX_VERTICES: list[list[int]] = [
    [VERTEX_ID[_corner_key(q, r, c)] for c in CORNER_DIRS] for q, r in HEXES
]

# --- edges ------------------------------------------------------------------


def _edge_mid(e: tuple[int, int]) -> tuple[float, float]:
    (x1, y1), (x2, y2) = VERTEX_PIXEL[e[0]], VERTEX_PIXEL[e[1]]
    return ((x1 + x2) / 2.0, (y1 + y2) / 2.0)


_edge_keys: set[tuple[int, int]] = set()
for _vs in HEX_VERTICES:
    for _i in range(6):
        _a, _b = _vs[_i], _vs[(_i + 1) % 6]
        _edge_keys.add((min(_a, _b), max(_a, _b)))

EDGE_VERTICES: list[tuple[int, int]] = sorted(
    _edge_keys,
    key=lambda e: (round(_edge_mid(e)[1], 3), round(_edge_mid(e)[0], 3)),
)
EDGE_ID: dict[tuple[int, int], int] = {e: i for i, e in enumerate(EDGE_VERTICES)}
EDGE_PIXEL: list[tuple[float, float]] = [_edge_mid(e) for e in EDGE_VERTICES]

# --- adjacency --------------------------------------------------------------

VERTEX_HEXES: list[list[int]] = [[] for _ in range(len(VERTICES))]
for _hid, _vs in enumerate(HEX_VERTICES):
    for _v in _vs:
        VERTEX_HEXES[_v].append(_hid)

VERTEX_ADJ: list[list[int]] = [[] for _ in range(len(VERTICES))]
VERTEX_EDGES: list[list[int]] = [[] for _ in range(len(VERTICES))]
for _eid, (_a, _b) in enumerate(EDGE_VERTICES):
    VERTEX_ADJ[_a].append(_b)
    VERTEX_ADJ[_b].append(_a)
    VERTEX_EDGES[_a].append(_eid)
    VERTEX_EDGES[_b].append(_eid)

CORNER_VERTEX: dict[tuple[int, str], int] = {
    (hid, c): HEX_VERTICES[hid][ci]
    for hid in range(len(HEXES))
    for ci, c in enumerate(CORNER_DIRS)
}

EDGE_OF_HEX: dict[tuple[int, str], int] = {}
HEX_EDGES: list[list[int]] = []
for _hid, _vs in enumerate(HEX_VERTICES):
    _row = []
    for _i, _d in enumerate(EDGE_DIRS):
        _a, _b = _vs[_i], _vs[(_i + 1) % 6]
        _eid = EDGE_ID[(min(_a, _b), max(_a, _b))]
        EDGE_OF_HEX[(_hid, _d)] = _eid
        _row.append(_eid)
    HEX_EDGES.append(_row)

_edge_use = Counter(e for row in HEX_EDGES for e in row)
COASTAL_EDGES: list[int] = sorted(e for e, n in _edge_use.items() if n == 1)

assert len(HEXES) == 19, len(HEXES)
assert len(VERTICES) == 54, len(VERTICES)
assert len(EDGE_VERTICES) == 72, len(EDGE_VERTICES)
assert len(COASTAL_EDGES) == 30, len(COASTAL_EDGES)

# --- rules helpers ----------------------------------------------------------


def is_vertex_placeable(vid: int, occupied: set[int]) -> bool:
    """Distance rule: vertex free and no adjacent vertex holds any building."""
    if vid in occupied:
        return False
    return not any(n in occupied for n in VERTEX_ADJ[vid])


# --- human-readable descriptions -------------------------------------------


def _tile(hexes, hid):
    t = hexes[hid]
    if isinstance(t, dict):
        return t.get("resource"), t.get("number")
    return t.resource, t.number


def hex_label(hexes, hid: int) -> str:
    res, num = _tile(hexes, hid)
    if res == "desert":
        return "the desert"
    return f"{num}-{res}" if num else res


def describe_hex(hexes, hid: int) -> str:
    row = next(i for i in range(5) if ROW_OFFSETS[i] <= hid < ROW_OFFSETS[i] + ROW_SIZES[i])
    pos = hid - ROW_OFFSETS[row]
    return f"the {hex_label(hexes, hid)} hex (row {row + 1}, position {pos + 1})"


def describe_vertex(hexes, vid: int) -> str:
    labels = [hex_label(hexes, h) for h in VERTEX_HEXES[vid]]
    where = "corner" if len(labels) == 3 else "coastal corner"
    return f"{where} touching " + ", ".join(labels)


def describe_edge(hexes, eid: int) -> str:
    a, b = EDGE_VERTICES[eid]
    shared = sorted(set(VERTEX_HEXES[a]) & set(VERTEX_HEXES[b]))
    if len(shared) == 2:
        # two adjacent hexes share exactly one edge, so this is unambiguous
        return (
            f"edge between {hex_label(hexes, shared[0])} and {hex_label(hexes, shared[1])}"
        )
    if len(shared) == 1:
        hid = shared[0]
        side = next(d for d in EDGE_DIRS if EDGE_OF_HEX[(hid, d)] == eid)
        return f"{side} coastal edge of the {hex_label(hexes, hid)} hex"
    return f"edge between the {describe_vertex(hexes, a)} and the {describe_vertex(hexes, b)}"
