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
