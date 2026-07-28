"""Playing positions out, and the one thing that stops it ranking moves.

Built to answer the research finding that a one-position estimate cannot see
far enough ahead. It plays: samples dice, pays everyone, lets each seat build
on a cheap policy, and reports how the game tends to end.

It works, it is fast, and it is not wired into the ranking -- because it cannot
see what a road is for. These tests hold both halves of that, so the day
somebody teaches it board topology there is a failing test waiting to say the
limitation is gone.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from app import rollout, rules, solver  # noqa: E402
from app.models import MoveStep  # noqa: E402
from test_economy import _settle, _spread  # noqa: E402
from test_solver import load  # noqa: E402


def _mid_game(**hand):
    """Two settlements a player on the best corners going.

    One settlement each -- what the spread helper gives by default -- produces
    so little that nobody passes two points in forty rounds, and every question
    asked of the simulation comes back the same. A position has to be able to
    reach ten before it can say anything about who gets there.
    """
    cfg = load(phase="main")
    spots = _spread(cfg, 2 * len(cfg.players))
    for i, color in enumerate(cfg.players):
        _settle(cfg, color, spots[i])
        _settle(cfg, color, spots[i + len(cfg.players)])
    cfg.me.hand = dict(hand)
    for color in cfg.players:
        if color != cfg.me.color:
            cfg.players[color].resource_count = 5
    return cfg


def test_a_game_played_out_ends_with_points_on_the_board():
    cfg = _mid_game()
    out = rollout.outlook(cfg, samples=80, seed=3)
    assert 0.0 <= out["win_rate"] <= 1.0
    assert out["points"] > 0, "everybody starts with settlements"
    assert set(out) == {"win_rate", "points", "margin"}


def test_the_awards_are_reachable():
    """Five settlements and four upgrades is nine points. Without the two
    trophies nobody can pass it, and every game ends with no winner."""
    cfg = _mid_game()
    reached = max(
        max(rollout.play_out(rollout.Table(cfg), __import__("random").Random(s)))
        for s in range(20)
    )
    assert reached >= rules.VICTORY_POINTS_TO_WIN, "ten has to be reachable"


def test_being_further_ahead_reads_as_better():
    cfg = _mid_game()
    behind = rollout.outlook(cfg, samples=200, seed=5)["margin"]
    me = cfg.players[cfg.me.color]
    me.cities = list(me.settlements)          # same board, twice the points
    me.settlements = []
    ahead = rollout.outlook(cfg, samples=200, seed=5)["margin"]
    assert ahead > behind


def test_a_point_now_shows_up_in_the_projection():
    cfg = _mid_game(ore=3, wheat=2)
    before = rollout.outlook(cfg, samples=200, seed=5)["points"]
    upgraded = solver._after(
        cfg, [MoveStep(type="build_city", vertex=cfg.players[cfg.me.color].settlements[0])]
    )
    after = rollout.outlook(upgraded, samples=200, seed=5)["points"]
    assert after > before, "a city is a point and the projection should see it"


def test_the_known_blind_spot_a_road_can_never_pay_off():
    """Why this does not rank moves.

    Settlements are gated on a supply counter, not on the board -- there is no
    topology in here, so a road buys nothing a later settlement needs. Every
    road is therefore pure cost, and ranking by this would advise never
    extending the network, which is the exact failure Szita et al. reported
    when they biased their rollouts towards building.

    If this test starts failing because roads now pay, the simulator has grown
    a board and can be promoted to ranking moves.
    """
    cfg = _mid_game(wood=1, brick=1)
    me = cfg.players[cfg.me.color]
    idle = rollout.outlook(cfg, samples=200, seed=11)["points"]

    from app import board

    edge = next(e for e in board.VERTEX_EDGES[me.settlements[0]]
                if all(e not in p.roads for p in cfg.players.values()))
    built = solver._after(cfg, [MoveStep(type="build_road", edge=edge)])
    with_road = rollout.outlook(built, samples=200, seed=11)["points"]
    assert with_road <= idle, (
        "the simulator has no board, so a road is only ever spent cards"
    )
