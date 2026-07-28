"""Canonical board geometry, derived rather than assumed.

Single source of truth for hex / vertex / edge IDs. The frontend fetches this
via /api/geometry and never derives geometry itself; the vision parser refers
to hexes by (row, pos) and pieces by hex+corner/edge, resolved here.

Coordinate system: axial (q, r), pointy-top hexes, screen y grows downward.
Every vertex is the North corner of exactly one hex coordinate or the South
corner of exactly one hex coordinate (the naming hex may lie off the board) --
vertex key = (q, r, "N"|"S").

External IDs are dense ints assigned deterministically:
  hex_id    row-major (top to bottom, left to right)
  vertex_id sorted by pixel (y, x)
  edge_id   sorted by midpoint (y, x)

The board *shape* is a parameter, not a constant. The 19-hex base game and the
30-hex 5-6 player extension are different boards, and colonist serves others
besides. Rather than hardcode which coordinates exist -- and get it wrong the
first time someone opens a board we have never seen -- a Layout is built from
whatever hex coordinates are in front of us, and `use()` installs it. Everything
downstream (vertices, edges, adjacency, the coastline) follows from the hexes,
so a new shape costs nothing.

`BASE` is the standard 19 and stays the default, for fixtures, hand-entered
boards, and anything that starts before a game does.
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


def hexagon(radius: int) -> list[HexCoord]:
    """The classic hexagonal arrangement: 19 hexes at radius 2, 37 at 3."""
    return [
        (q, r)
        for r in range(-radius, radius + 1)
        for q in range(max(-radius, -radius - r), min(radius, radius - r) + 1)
    ]


class Layout:
    """Every derived table for one board shape."""

    def __init__(self, coords) -> None:
        self.HEXES: list[HexCoord] = sorted(set(coords), key=lambda h: (h[1], h[0]))
        if not self.HEXES:
            raise ValueError("a board needs at least one hex")
        self.HEX_ID = {h: i for i, h in enumerate(self.HEXES)}
        self.HEX_PIXEL = [_hex_center(q, r) for q, r in self.HEXES]

        rows: dict[int, int] = Counter(r for _q, r in self.HEXES)
        self.ROW_SIZES = [rows[r] for r in sorted(rows)]
        self.ROW_OFFSETS = []
        running = 0
        for n in self.ROW_SIZES:
            self.ROW_OFFSETS.append(running)
            running += n

        keys = {_corner_key(q, r, c) for q, r in self.HEXES for c in CORNER_DIRS}
        self.VERTICES: list[VertexKey] = sorted(
            keys,
            key=lambda k: (round(_vertex_pixel(k)[1], 3), round(_vertex_pixel(k)[0], 3)),
        )
        self.VERTEX_ID = {k: i for i, k in enumerate(self.VERTICES)}
        self.VERTEX_PIXEL = [_vertex_pixel(k) for k in self.VERTICES]
        self.HEX_VERTICES = [
            [self.VERTEX_ID[_corner_key(q, r, c)] for c in CORNER_DIRS]
            for q, r in self.HEXES
        ]

        def mid(e: tuple[int, int]) -> tuple[float, float]:
            (x1, y1), (x2, y2) = self.VERTEX_PIXEL[e[0]], self.VERTEX_PIXEL[e[1]]
            return ((x1 + x2) / 2.0, (y1 + y2) / 2.0)

        edges: set[tuple[int, int]] = set()
        for vs in self.HEX_VERTICES:
            for i in range(6):
                a, b = vs[i], vs[(i + 1) % 6]
                edges.add((min(a, b), max(a, b)))
        self.EDGE_VERTICES = sorted(
            edges, key=lambda e: (round(mid(e)[1], 3), round(mid(e)[0], 3))
        )
        self.EDGE_ID = {e: i for i, e in enumerate(self.EDGE_VERTICES)}
        self.EDGE_PIXEL = [mid(e) for e in self.EDGE_VERTICES]

        self.VERTEX_HEXES = [[] for _ in self.VERTICES]
        for hid, vs in enumerate(self.HEX_VERTICES):
            for v in vs:
                self.VERTEX_HEXES[v].append(hid)

        self.VERTEX_ADJ = [[] for _ in self.VERTICES]
        self.VERTEX_EDGES = [[] for _ in self.VERTICES]
        for eid, (a, b) in enumerate(self.EDGE_VERTICES):
            self.VERTEX_ADJ[a].append(b)
            self.VERTEX_ADJ[b].append(a)
            self.VERTEX_EDGES[a].append(eid)
            self.VERTEX_EDGES[b].append(eid)

        self.CORNER_VERTEX = {
            (hid, c): self.HEX_VERTICES[hid][ci]
            for hid in range(len(self.HEXES))
            for ci, c in enumerate(CORNER_DIRS)
        }
        self.EDGE_OF_HEX = {}
        self.HEX_EDGES = []
        for hid, vs in enumerate(self.HEX_VERTICES):
            row = []
            for i, d in enumerate(EDGE_DIRS):
                a, b = vs[i], vs[(i + 1) % 6]
                eid = self.EDGE_ID[(min(a, b), max(a, b))]
                self.EDGE_OF_HEX[(hid, d)] = eid
                row.append(eid)
            self.HEX_EDGES.append(row)

        used = Counter(e for row in self.HEX_EDGES for e in row)
        self.COASTAL_EDGES = sorted(e for e, n in used.items() if n == 1)

    @property
    def counts(self) -> tuple[int, int, int]:
        return len(self.HEXES), len(self.VERTICES), len(self.EDGE_VERTICES)

    def __repr__(self) -> str:
        h, v, e = self.counts
        return f"<Layout {h} hexes, {v} vertices, {e} edges, rows {self.ROW_SIZES}>"


def rows(row_sizes: list[int], lefts: list[int]) -> list[HexCoord]:
    """Hexes from explicit row widths and their leftmost q."""
    top = -(len(row_sizes) // 2)
    return [
        (left + i, top + n)
        for n, (w, left) in enumerate(zip(row_sizes, lefts))
        for i in range(w)
    ]


# The standard board, and the shape the 5-6 player extension ships as. The
# extension is not a bigger hexagon -- radius 3 would be 37 hexes -- but a
# hexagon stretched by one row: 3-4-5-6-5-4-3. Reflecting about the middle row
# maps (q, r) to (q + r, -r), and both shapes are closed under it.
BASE = Layout(hexagon(2))
EXTENDED = Layout(rows([3, 4, 5, 6, 5, 4, 3], [0, -1, -2, -3, -3, -3, -3]))

# Layouts we can name without being told. Anything else gets built on sight.
_KNOWN = {len(lay.HEXES): lay for lay in (BASE, EXTENDED)}


def use(layout: "Layout") -> "Layout":
    """Install a layout as the board every module reads.

    Rebinding module globals rather than proxying attribute access: geometry is
    read in tight loops (road search, vertex scoring) and a per-access
    indirection is not free. A game's shape is fixed for its duration, so the
    swap happens once, when a snapshot arrives.
    """
    global ACTIVE
    ACTIVE = layout
    globals().update(
        {k: v for k, v in vars(layout).items() if k.isupper()}
    )
    return layout


def for_coords_of_size(n: int) -> "Layout":
    """The known layout with this many hexes, or the base board if unfamiliar.

    Used when all we have is a hex count -- a hand-entered board or a stored
    config. A live game gives us the coordinates themselves, which is better;
    see `for_coords`.
    """
    return _KNOWN.get(n, BASE)


def for_coords(coords) -> "Layout":
    """The layout these hex coordinates describe, reusing a known one if it fits."""
    wanted = sorted(set(map(tuple, coords)), key=lambda h: (h[1], h[0]))
    known = _KNOWN.get(len(wanted))
    if known is not None and known.HEXES == wanted:
        return known
    return Layout(wanted)


def _self_check() -> None:
    """Euler's formula on every shape we ship.

    V - E + F = 2 for a planar graph, counting each hex as a face plus one for
    the sea. It catches a mis-specified shape immediately -- a row placed one
    step off still looks like a board until you count its corners.
    """
    for lay in (BASE, EXTENDED):
        v, e, f = len(lay.VERTICES), len(lay.EDGE_VERTICES), len(lay.HEXES) + 1
        assert v - e + f == 2, f"{lay} is not a planar board: {v} - {e} + {f}"
    assert BASE.counts == (19, 54, 72), BASE.counts
    assert EXTENDED.counts == (30, 80, 109), EXTENDED.counts


_self_check()

ACTIVE = BASE
use(BASE)


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
    row = next(
        i for i in range(len(ROW_SIZES))
        if ROW_OFFSETS[i] <= hid < ROW_OFFSETS[i] + ROW_SIZES[i]
    )
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
