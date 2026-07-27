from app import board


def test_geometry_counts():
    assert len(board.HEXES) == 19
    assert len(board.VERTICES) == 54
    assert len(board.EDGE_VERTICES) == 72
    assert all(len(vs) == 6 for vs in board.HEX_VERTICES)
    assert sum(len(vs) for vs in board.HEX_VERTICES) == 114
    assert sum(len(hs) for hs in board.VERTEX_HEXES) == 114
    assert len(board.COASTAL_EDGES) == 30


def test_adjacency_symmetry():
    for v, neighbors in enumerate(board.VERTEX_ADJ):
        assert len(neighbors) in (2, 3)
        for u in neighbors:
            assert v in board.VERTEX_ADJ[u]
    assert sum(len(n) for n in board.VERTEX_ADJ) == 144  # 2 * 72
    for v, edges in enumerate(board.VERTEX_EDGES):
        for e in edges:
            assert v in board.EDGE_VERTICES[e]
    for e, (a, b) in enumerate(board.EDGE_VERTICES):
        assert e in board.VERTEX_EDGES[a]
        assert e in board.VERTEX_EDGES[b]


def test_corner_and_edge_lookup():
    # every (hex, corner) and (hex, edge-dir) resolves
    for hid in range(19):
        for c in board.CORNER_DIRS:
            assert 0 <= board.CORNER_VERTEX[(hid, c)] < 54
        for d in board.EDGE_DIRS:
            assert 0 <= board.EDGE_OF_HEX[(hid, d)] < 72
    # shared-corner identities: a hex's SE corner is the N corner of the hex
    # below-right of it (axial (q, r+1)), when that hex is on the board
    for (q, r), hid in board.HEX_ID.items():
        below = (q, r + 1)
        if below in board.HEX_ID:
            hid2 = board.HEX_ID[below]
            assert board.CORNER_VERTEX[(hid, "SE")] == board.CORNER_VERTEX[(hid2, "N")]
            assert board.CORNER_VERTEX[(hid, "S")] == board.CORNER_VERTEX[(hid2, "NW")]
        right = (q + 1, r)
        if right in board.HEX_ID:
            hid2 = board.HEX_ID[right]
            assert board.EDGE_OF_HEX[(hid, "E")] == board.EDGE_OF_HEX[(hid2, "W")]


def test_distance_rule():
    v = board.HEX_VERTICES[9][0]  # a vertex of the center hex
    occupied = {v}
    assert not board.is_vertex_placeable(v, occupied)
    for n in board.VERTEX_ADJ[v]:
        assert not board.is_vertex_placeable(n, occupied)
    # a vertex two steps away is placeable
    two_away = board.VERTEX_ADJ[board.VERTEX_ADJ[v][0]]
    far = next(u for u in two_away if u != v and u not in board.VERTEX_ADJ[v])
    assert board.is_vertex_placeable(far, occupied)
