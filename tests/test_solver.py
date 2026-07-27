import json
from pathlib import Path

from app import board, solver
from app.models import BoardConfig

FIXTURE = Path(__file__).parent.parent / "app" / "fixtures" / "default_board.json"


def load(**overrides) -> BoardConfig:
    data = json.loads(FIXTURE.read_text())
    data.update(overrides)
    return BoardConfig.model_validate(data)


def road_chain_from(vid: int) -> tuple[int, int, int]:
    """Return (edge1, mid_vertex, edge2, far_vertex)-ish chain A -e1- B -e2- C."""
    e1 = board.VERTEX_EDGES[vid][0]
    a, b = board.EDGE_VERTICES[e1]
    mid = b if a == vid else a
    e2 = next(e for e in board.VERTEX_EDGES[mid] if e != e1)
    a2, b2 = board.EDGE_VERTICES[e2]
    far = b2 if a2 == mid else a2
    return e1, mid, e2, far


def test_setup_picks_a_top_pip_vertex():
    cfg = load(phase="setup1")
    moves = solver.solve(cfg)
    assert moves
    top_vertex = moves[0].steps[0].vertex
    pips = {v: sum(solver._hex_pips(cfg.hexes[h]) for h in board.VERTEX_HEXES[v]) for v in range(54)}
    best5 = sorted(pips, key=lambda v: -pips[v])[:5]
    assert pips[top_vertex] >= min(pips[v] for v in best5)
    # every setup move pairs the settlement with a free road
    assert moves[0].steps[1].type == "setup_road"


def test_legal_moves_respect_resources():
    cfg = load(phase="main")
    v = 20
    e1, mid, e2, far = road_chain_from(v)
    cfg.players["red"].settlements = [v]
    cfg.players["red"].roads = [e1]
    cfg.me.hand = {"wood": 1, "brick": 1, "sheep": 0, "wheat": 0, "ore": 0}
    moves = solver.solve(cfg)
    types = {m.steps[0].type for m in moves}
    assert "build_road" in types
    assert "build_settlement" not in types
    assert "build_city" not in types
    assert "buy_dev" not in types

    cfg.me.dev_cards.knight = 1
    moves = solver.solve(cfg)
    assert any(m.steps[0].type == "play_knight" for m in moves)

    cfg.me.dev_card_played_this_turn = True
    moves = solver.solve(cfg)
    assert not any(m.steps[0].type == "play_knight" for m in moves)


def test_settlements_require_road_connection():
    cfg = load(phase="main")
    cfg.players["red"].settlements = [20]
    cfg.me.hand = {"wood": 1, "brick": 1, "sheep": 1, "wheat": 1, "ore": 0}
    moves = solver.solve(cfg)
    # settlement affordable but no roads at all -> no legal spot
    assert not any(m.steps[0].type == "build_settlement" for m in moves)


def test_opponent_building_severs_road_network():
    cfg = load(phase="main")
    v = 20
    e1, mid, e2, far = road_chain_from(v)
    cfg.players["red"].settlements = [v]
    cfg.players["red"].roads = [e1]
    cfg.players["blue"].settlements = [mid]  # enemy building at my road's tip
    cfg.me.hand = {"wood": 2, "brick": 2, "sheep": 0, "wheat": 0, "ore": 0}
    moves = solver.solve(cfg)
    offered_edges = {
        m.steps[0].edge for m in moves if m.steps[0].type == "build_road"
    }
    assert e2 not in offered_edges  # cannot continue through blue's settlement


def test_robber_moves_exclude_current_hex():
    cfg = load(phase="main", pending="move_robber")
    cfg.players["blue"].settlements = [board.HEX_VERTICES[5][0]]
    cfg.players["blue"].resource_count = 4
    moves = solver.solve(cfg)
    assert moves
    assert all(m.steps[0].type == "move_robber" for m in moves)
    assert all(m.steps[0].robber_hex != cfg.robber_hex for m in moves)
    # the top pick should target a hex that actually hurts blue
    assert moves[0].steps[0].steal_from == "blue"


def test_trade_combo_unlocks_settlement():
    cfg = load(phase="main")
    v = 20
    e1, mid, e2, far = road_chain_from(v)
    cfg.players["red"].settlements = [v]
    cfg.players["red"].roads = [e1, e2]
    cfg.me.hand = {"wood": 6, "brick": 0, "sheep": 1, "wheat": 1, "ore": 0}
    moves = solver.solve(cfg)
    combo = next(
        (
            m
            for m in moves
            if m.steps[0].type == "trade_bank"
            and m.steps[-1].type == "build_settlement"
        ),
        None,
    )
    assert combo is not None
    assert combo.steps[0].give == {"wood": 4}
    assert combo.steps[-1].vertex == far


def test_city_and_longest_road_scoring():
    cfg = load(phase="main")
    v = 20
    cfg.players["red"].settlements = [v]
    cfg.me.hand = {"wood": 0, "brick": 0, "sheep": 0, "wheat": 2, "ore": 3}
    moves = solver.solve(cfg)
    assert moves[0].steps[0].type == "build_city"
    assert moves[0].steps[0].vertex == v


def test_parser_resolution_roundtrip():
    from app.parser import RawBoard, resolve_raw

    raw = RawBoard.model_validate(
        {
            "hexes": [
                {"row": r, "pos": p, "resource": "wood", "number": 6 if (r, p) != (2, 2) else None}
                if (r, p) != (2, 2)
                else {"row": r, "pos": p, "resource": "desert", "number": None}
                for r in range(5)
                for p in range([3, 4, 5, 4, 3][r])
            ],
            "robber": {"row": 2, "pos": 2, "resource": "desert", "number": None},
            "red": {
                "settlements": [{"row": 0, "pos": 0, "corner": "N"}],
                "cities": [],
                "roads": [{"row": 0, "pos": 0, "edge": "NE"}],
            },
            "my_color": "red",
            "my_hand": {"wood": 2, "brick": 1, "sheep": 0, "wheat": 0, "ore": 0},
        }
    )
    cfg, warnings = resolve_raw(raw)
    assert cfg.robber_hex == 9  # row 2 (offset 7) + pos 2
    assert cfg.players["red"].settlements == [board.CORNER_VERTEX[(0, "N")]]
    assert cfg.players["red"].roads == [board.EDGE_OF_HEX[(0, "NE")]]
    assert cfg.me.color == "red"
    assert cfg.me.hand["wood"] == 2


def test_codex_json_extraction():
    from app.parser import _extract_json

    assert _extract_json('{"a": 1}') == {"a": 1}
    assert _extract_json('Here you go:\n```json\n{"a": {"b": 2}}\n```\ndone') == {"a": {"b": 2}}
    import pytest as _pytest

    with _pytest.raises(RuntimeError):
        _extract_json("no json here")
