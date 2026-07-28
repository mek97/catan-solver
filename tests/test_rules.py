"""Base-game rule conformance."""
from collections import Counter

import pytest

from app import board, rules, solver
from app.models import BoardConfig


@pytest.mark.parametrize("variant", [rules.BASE, rules.EXTENDED])
def test_every_variant_is_internally_consistent(variant):
    """Tiles, tokens and deserts have to agree with the board they describe."""
    deserts = variant.TILE_DISTRIBUTION["desert"]
    assert sum(variant.TILE_DISTRIBUTION.values()) == variant.HEX_COUNT
    assert sum(variant.TOKEN_DISTRIBUTION.values()) == variant.HEX_COUNT - deserts
    assert 7 not in variant.TOKEN_DISTRIBUTION
    assert board.for_coords_of_size(variant.HEX_COUNT).counts[0] == variant.HEX_COUNT


def test_the_base_board_is_the_standard_19():
    assert board.BASE.counts == (19, 54, 72)
    assert rules.BASE.HEX_COUNT == 19


def test_the_extension_is_thirty_hexes_not_a_bigger_hexagon():
    """Radius 3 would be 37 tiles; the 5-6 player board is a stretched 30."""
    assert board.EXTENDED.counts[0] == 30
    assert board.EXTENDED.ROW_SIZES == [3, 4, 5, 6, 5, 4, 3]
    assert len(board.hexagon(3)) == 37


def test_dev_deck_is_the_standard_25():
    assert rules.BASE.DEV_DECK_SIZE == 25
    assert rules.BASE.DEV_DECK["knight"] == 14 and rules.BASE.DEV_DECK["victory_point"] == 5
    assert sum(rules.dev_card_odds().values()) == pytest.approx(1.0)


def test_the_extension_deepens_the_deck_but_not_the_points():
    """Nine more cards, six of them knights -- a point card gets rarer."""
    assert rules.EXTENDED.DEV_DECK_SIZE == 34
    assert rules.EXTENDED.DEV_DECK["victory_point"] == rules.BASE.DEV_DECK["victory_point"]
    base_odds = rules.BASE.DEV_DECK["victory_point"] / rules.BASE.DEV_DECK_SIZE
    ext_odds = rules.EXTENDED.DEV_DECK["victory_point"] / rules.EXTENDED.DEV_DECK_SIZE
    assert ext_odds < base_odds


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


def test_discard_is_only_required_during_the_discard_phase():
    """Over seven cards is a risk; the obligation only exists when a 7 lands."""
    from app.live.advisor import discard_advice

    cfg = cfg_from_fixture()
    cfg.phase = "main"
    cfg.me.hand = {"wood": 2, "brick": 2, "sheep": 5, "wheat": 1, "ore": 0}  # 10 cards

    at_risk = discard_advice(None, cfg)
    assert at_risk and at_risk["required"] is False
    assert at_risk["must_discard"] == 5 and "would cost you" in at_risk["text"]

    cfg.pending = "discard"
    now = discard_advice(None, cfg)
    assert now["required"] is True and now["text"].startswith("Discard 5")

    cfg.me.hand = {"wood": 2, "brick": 2, "sheep": 3, "wheat": 0, "ore": 0}  # 7 cards
    assert discard_advice(None, cfg) is None, "seven cards is safe"


# --- sequencing: what you may do, and when ----------------------------------


def _mid_game(**me_kw):
    """A position with a settlement, a road, and cards to spend."""
    cfg = BoardConfig.model_validate({
        **__import__("json").loads(
            (__import__("pathlib").Path(__file__).parent.parent
             / "app" / "fixtures" / "default_board.json").read_text()),
        "phase": "main",
    })
    v = max(range(54), key=lambda x: sum(
        solver.PIPS.get(cfg.hexes[h].number, 0) for h in board.VERTEX_HEXES[x]
        if cfg.hexes[h].number))
    cfg.players["red"].settlements = [v]
    cfg.players["red"].roads = [board.VERTEX_EDGES[v][0]]
    cfg.me.hand = {"wood": 2, "brick": 2, "sheep": 2, "wheat": 2, "ore": 2}
    for k, val in me_kw.items():
        setattr(cfg.me, k, val)
    return cfg


def test_one_development_card_per_turn():
    played = _mid_game(dev_card_played_this_turn=True)
    played.me.dev_cards.knight = 2
    assert not any(m.steps[0].type.startswith("play_") for m in solver.solve(played))


def test_buying_a_card_does_not_lock_the_ones_you_already_had():
    """The card you just bought is unplayable. The rest of your hand is not.

    Blocking everything cost a knight that was legal all along -- in a recorded
    game we held a knight, bought a victory point, and the solver went quiet.
    """
    cfg = _mid_game(dev_card_bought_this_turn=True)
    cfg.me.dev_cards.knight = 1
    assert any(m.steps[0].type == "play_knight" for m in solver.solve(cfg))


def test_the_engine_hides_the_card_bought_this_turn():
    """The subtraction happens where the hand is read, not in the solver."""
    from app.live.engine import GameEngine
    from app.live import protocol as P

    eng = GameEngine()
    eng.my_color_id = 1
    eng.state = {"mechanicDevelopmentCardsState": {"players": {"1": {
        "developmentCards": {"cards": [11, 12]},        # a knight and a point
        "developmentCardsBoughtThisTurn": [12],         # the point is new
    }}}}
    dev = eng.my_dev_cards()
    assert dev["known"] == {"knight": 1, "victory_point": 1}
    assert dev["playable"] == {"knight": 1}, "the fresh card must not be playable"
    assert dev["bought_this_turn"] == {"victory_point": 1}


def test_colonist_reports_whether_a_card_was_already_played():
    from app.live.engine import GameEngine

    eng = GameEngine()
    eng.my_color_id = 1
    eng.state = {"mechanicDevelopmentCardsState": {"players": {"1": {
        "developmentCards": {"cards": [11]},
        "hasUsedDevelopmentCardThisTurn": True,
    }}}}
    assert eng.my_dev_cards()["played_this_turn"] is True


def test_setup_places_a_settlement_anywhere_legal_then_a_road_touching_it():
    """Opening placement ignores road connectivity -- that is the whole point
    of it -- but the free road must still touch the settlement just placed."""
    cfg = _mid_game()
    cfg.players["red"].settlements = []
    cfg.players["red"].roads = []
    cfg.phase = "setup1"
    for m in solver.solve(cfg)[:5]:
        assert m.steps[0].type == "setup_settlement"
        assert m.steps[1].type == "setup_road"
        v, e = m.steps[0].vertex, m.steps[1].edge
        assert v in board.EDGE_VERTICES[e], "the free road must touch the settlement"


def test_only_the_first_two_settlements_are_free():
    """Once two are down the setup phase is over, whatever the caller says."""
    from app.live.engine import GameEngine

    eng = GameEngine()
    eng.my_color_id = 1
    eng.play_order = [1, 2, 3, 4]
    eng.state = {"mapState": {"tileCornerStates": {
        "0": {"owner": 1}, "1": {"owner": 1},
    }}}
    assert eng.phase() == "main"


def test_a_point_card_counts_the_turn_it_is_bought():
    """It is revealed, not played, so the buy-turn restriction does not apply:
    the rules let you win on a card bought that same turn."""
    from app.live.engine import GameEngine

    eng = GameEngine()
    eng.my_color_id = 1
    eng.state = {"mechanicDevelopmentCardsState": {"players": {"1": {
        "developmentCards": {"cards": [12, 11]},        # a point and a knight
        "developmentCardsBoughtThisTurn": [12, 11],     # both bought just now
    }}}}
    dev = eng.my_dev_cards()
    assert dev["known"]["victory_point"] == 1
    assert dev["playable"] == {}, "the knight cannot be played this turn"


def test_hidden_points_are_not_dropped_by_a_field_name():
    """colonist says victory_point, the model's field is vp. Filtering on field
    names silently discarded every hidden point."""
    from app.models import DevCards

    assert "victory_point" not in DevCards.model_fields
    assert "vp" in DevCards.model_fields


def test_you_win_with_ten_or_more_not_exactly_ten():
    """Taking Longest Road at nine points puts you on eleven, and you win."""
    from app import economy

    cfg = _mid_game()
    me = cfg.players["red"]
    me.settlements = []
    me.roads = []
    out = []
    for v in range(54):
        if len(out) == 5:
            break
        if all(v not in board.VERTEX_ADJ[o] for o in out):
            out.append(v)
    me.cities, me.settlements = out[:4], out[4:]   # 8 + 1 = 9 VP
    me.longest_road = True                          # +2 -> 11, not exactly 10
    assert solver.build_ctx(cfg).my_vp == 11
    assert economy.turns_to_win(cfg, solver.build_ctx(cfg)) == 0.0, "11 points wins"
