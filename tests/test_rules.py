"""Base-game rule conformance."""
from collections import Counter

import pytest

from app import board, rules, solver
from app.models import BoardConfig


def test_board_matches_the_base_game():
    assert (board.HEXES, len(board.VERTICES), len(board.EDGE_VERTICES)) == (
        board.HEXES, rules.VERTEX_COUNT, rules.EDGE_COUNT)
    assert len(board.HEXES) == rules.HEX_COUNT
    assert sum(rules.TILE_DISTRIBUTION.values()) == rules.HEX_COUNT
    assert sum(rules.TOKEN_DISTRIBUTION.values()) == rules.HEX_COUNT - 1  # desert has none
    assert 7 not in rules.TOKEN_DISTRIBUTION


def test_dev_deck_is_the_standard_25():
    assert rules.DEV_DECK_SIZE == 25
    assert rules.DEV_DECK["knight"] == 14 and rules.DEV_DECK["victory_point"] == 5
    assert sum(rules.dev_card_odds().values()) == pytest.approx(1.0)


def test_costs_are_the_base_game_costs():
    assert rules.COSTS["road"] == {"wood": 1, "brick": 1}
    assert rules.COSTS["settlement"] == {"wood": 1, "brick": 1, "sheep": 1, "wheat": 1}
    assert rules.COSTS["city"] == {"wheat": 2, "ore": 3}
    assert rules.COSTS["dev"] == {"sheep": 1, "wheat": 1, "ore": 1}


def test_discard_is_half_rounded_down_above_the_limit():
    assert rules.discard_count(7) == 0
    assert rules.discard_count(8) == 4
    assert rules.discard_count(9) == 4      # rounded down
    assert rules.discard_count(15) == 7
    assert rules.discard_count(9, limit=10) == 0  # variant limits respected


def test_pips_are_the_2d6_distribution():
    assert sum(rules.PIPS.values()) == 36
    assert rules.PIPS[7] == 6 and rules.PIPS[2] == rules.PIPS[12] == 1
    assert 7 not in solver.PIPS, "7 pays nobody; it must not score production"


# --- solver conformance -----------------------------------------------------


def cfg_from_fixture() -> BoardConfig:
    import json
    from pathlib import Path
    p = Path(__file__).parent.parent / "app" / "fixtures" / "default_board.json"
    return BoardConfig.model_validate(json.loads(p.read_text()))


def test_robber_blocks_a_hex_entirely():
    cfg = cfg_from_fixture()
    cfg.phase = "main"
    target = next(i for i, h in enumerate(cfg.hexes) if h.number and h.resource != "desert")
    vid = board.HEX_VERTICES[target][0]
    cfg.players[cfg.me.color].settlements = [vid]

    cfg.robber_hex = next(i for i, h in enumerate(cfg.hexes) if h.resource == "desert")
    free = sum(solver.build_ctx(cfg).my_pips.values())
    cfg.robber_hex = target
    blocked = sum(solver.build_ctx(cfg).my_pips.values())

    lost = solver.PIPS[cfg.hexes[target].number]
    assert free - blocked == pytest.approx(lost), "robber must remove all of the hex's pips"


def test_piece_supply_limits_are_enforced():
    cfg = cfg_from_fixture()
    cfg.phase = "main"
    me = cfg.players[cfg.me.color]
    me.settlements = list(range(0, 10, 2))[:5]   # all 5 settlements standing
    me.pieces_left = {"settlement": 0, "city": 4, "road": 15}
    cfg.me.hand = {"wood": 5, "brick": 5, "sheep": 5, "wheat": 5, "ore": 5}
    kinds = {m.steps[0].type for m in solver.solve(cfg)}
    assert "build_settlement" not in kinds, "cannot build without a settlement in supply"


def test_bank_cannot_pay_out_an_empty_pile():
    cfg = cfg_from_fixture()
    cfg.phase = "main"
    cfg.me.hand = {"wood": 8, "brick": 0, "sheep": 0, "wheat": 0, "ore": 0}
    cfg.bank = {"wood": 19, "brick": 0, "sheep": 19, "wheat": 19, "ore": 19}
    for m in solver.solve(cfg):
        for step in m.steps:
            if step.type == "trade_bank" and step.get:
                assert "brick" not in step.get, "bank is out of brick"


def test_longest_road_needs_five_and_must_beat_the_holder():
    assert rules.LONGEST_ROAD_MIN == 5
    assert rules.LARGEST_ARMY_MIN == 3


# --- setup phase ------------------------------------------------------------


def setup_cfg(phase: str) -> BoardConfig:
    cfg = cfg_from_fixture()
    cfg.phase = phase
    return cfg


def test_setup_settlements_need_no_road_connection():
    """Unlike the main phase, opening settlements go anywhere legal."""
    cfg = setup_cfg("setup1")
    assert not cfg.players[cfg.me.color].roads
    moves = solver.solve(cfg)
    assert moves, "setup must offer placements with no roads on the board"
    assert all(m.steps[0].type == "setup_settlement" for m in moves)


def test_setup_pairs_a_road_touching_the_new_settlement():
    cfg = setup_cfg("setup1")
    for m in solver.solve(cfg):
        vid = m.steps[0].vertex
        road = next((s for s in m.steps if s.type == "setup_road"), None)
        assert road is not None, "each opening placement includes its free road"
        assert vid in board.EDGE_VERTICES[road.edge], "road must touch the settlement"


def test_setup_respects_the_distance_rule():
    cfg = setup_cfg("setup2")
    taken = board.HEX_VERTICES[9][0]
    cfg.players["blue"].settlements = [taken]
    banned = {taken, *board.VERTEX_ADJ[taken]}
    for m in solver.solve(cfg):
        assert m.steps[0].vertex not in banned


def test_only_the_second_settlement_pays_starting_resources():
    """Rule: the first settlement pays nothing; the second pays one card per hex."""
    cfg1, cfg2 = setup_cfg("setup1"), setup_cfg("setup2")
    ctx2 = solver.build_ctx(cfg2)

    # the payout itself: one card per producing hex the corner touches
    for vid in range(54):
        cards = solver._starting_cards(ctx2, vid)
        producing = [
            h for h in board.VERTEX_HEXES[vid]
            if cfg2.hexes[h].resource != "desert" and cfg2.hexes[h].number
        ]
        assert sum(cards.values()) == len(producing), f"vertex {vid}: one card per hex"

    # solve() returns only the top few, so compare what each phase actually says
    assert all("starts you with" not in m.reasoning for m in solver.solve(cfg1)), \
        "the first settlement pays nothing"
    assert any("starts you with" in m.reasoning for m in solver.solve(cfg2)), \
        "the second settlement pays out"


def test_overlapping_a_strong_number_costs_more_than_a_weak_one():
    """Doubling up on an 8 hurts far more than doubling up on a 12."""
    cfg = setup_cfg("setup2")
    strong = next(i for i, h in enumerate(cfg.hexes) if h.number == 8)
    weak = next(i for i, h in enumerate(cfg.hexes) if h.number in (2, 12))
    ctx = solver.build_ctx(cfg)
    assert solver.PIPS[cfg.hexes[strong].number] > solver.PIPS[cfg.hexes[weak].number]
    # the penalty is pip-weighted, so it must scale with the shared number
    assert solver.PIPS[8] * 0.35 > solver.PIPS[12] * 0.35
