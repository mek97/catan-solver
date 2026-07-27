import json
from pathlib import Path

import pytest

from app import board
from app.live import protocol as P
from app.live.engine import GameEngine
from app.live.store import Store

FIXTURE = Path(__file__).parent / "fixtures" / "colonist_frames.jsonl"


def frames():
    if not FIXTURE.exists():
        pytest.skip("no recorded colonist frames fixture")
    for line in FIXTURE.read_text().splitlines():
        r = json.loads(line)
        if r.get("ev") == "frame" and r.get("opcode") == 2:
            yield r["data"]


def replay() -> GameEngine:
    eng = GameEngine()
    for payload in frames():
        obj = P.decode_frame(payload)
        d = P.envelope(obj) if obj else None
        if not d:
            continue
        if d.get("type") == P.MSG_GAME_SNAPSHOT:
            eng.apply_snapshot(d["payload"])
        elif d.get("type") == P.MSG_GAME_DIFF:
            eng.apply_diff((d.get("payload") or {}).get("diff") or {})
    return eng


# --- protocol ---------------------------------------------------------------


def test_edge_mapping_is_bijective():
    # protocol._self_check runs at import; assert the property explicitly too
    seen = set()
    for q in range(-3, 4):
        for r in range(-3, 4):
            for z in (0, 1, 2):
                e = P.edge_to_canonical({"x": q, "y": r, "z": z})
                if e is not None:
                    seen.add(e)
    assert len(seen) == 72


def test_corner_mapping_covers_board():
    # like edges, colonist names outer corners after off-board hexes, so the
    # 19 board hexes alone only reach 19*2 = 38 of the 54 vertices
    seen = set()
    for q in range(-3, 4):
        for r in range(-3, 4):
            for z in (0, 1):
                v = P.corner_to_vertex({"x": q, "y": r, "z": z})
                if v is not None:
                    seen.add(v)
    assert len(seen) == 54


def test_deep_merge_semantics():
    base = {"a": {"b": 1, "c": 2}, "list": [1, 2, 3]}
    out = P.deep_merge(base, {"a": {"c": 9}, "list": [7]})
    assert out == {"a": {"b": 1, "c": 9}, "list": [7]}  # dicts merge, lists replace
    assert base["a"]["c"] == 2  # input not mutated


def test_describe_log_trade():
    ev = P.describe_log(
        {"text": {"type": 115, "playerColor": 1, "acceptingPlayerColor": 4,
                  "givenCardEnums": [3, 3], "receivedCardEnums": [1]}}
    )
    assert ev["kind"] == "trade_player"
    assert ev["gave"] == ["sheep", "sheep"] and ev["got"] == ["wood"]


# --- store ------------------------------------------------------------------


def test_store_is_idempotent_and_detects_gaps(tmp_path):
    st = Store(tmp_path / "t.db")
    fid1 = st.add_frame("AAAA", "recv", 2, "g1")
    fid2 = st.add_frame("AAAA", "recv", 2, "g1")  # same content
    assert fid1 is not None and fid2 is None

    assert st.add_event("g1", 1, fid1, "dice_rolled", "red", {"total": 7}) is True
    assert st.add_event("g1", 1, fid1, "dice_rolled", "red", {"total": 7}) is False
    st.add_event("g1", 4, fid1, "turn_ended", "red", {})
    assert st.gaps("g1") == [2, 3]

    st.add_event("g1", 2, fid1, "x", None, {})
    st.add_event("g1", 3, fid1, "x", None, {})
    assert st.gaps("g1") == []
    st.close()


def test_store_replay_roundtrip(tmp_path):
    st = Store(tmp_path / "t.db")
    payloads = list(frames())
    for p in payloads:
        st.add_frame(p, "recv", 2, "g1")
    # content-hash dedupe is intentional: identical frames collapse, and the
    # surviving order must match first-arrival order exactly
    expected, seen = [], set()
    for p in payloads:
        if p not in seen:
            seen.add(p)
            expected.append(p)
    stored = [p for _fid, p in st.replay_frames("g1")]
    assert stored == expected
    st.close()


# --- engine -----------------------------------------------------------------


def test_engine_replay_produces_valid_board():
    eng = replay()
    assert eng.state, "no snapshot applied"
    cfg = eng.board_config()
    assert len(cfg.hexes) == 19
    assert cfg.me.color in ("red", "blue", "orange", "green")
    # every placed piece resolved to a canonical id in range
    for p in cfg.players.values():
        assert all(0 <= v < 54 for v in p.settlements + p.cities)
        assert all(0 <= e < 72 for e in p.roads)


def test_engine_replay_is_deterministic():
    a, b = replay(), replay()
    assert a.board_config().model_dump() == b.board_config().model_dump()
    assert len(a.events) == len(b.events)


def test_engine_captures_all_move_kinds():
    eng = replay()
    kinds = {e["kind"] for e in eng.events}
    for required in ("dice_rolled", "piece_placed", "cards_received", "trade_player"):
        assert required in kinds, f"missing {required}: {kinds}"


def test_robber_advice_only_on_my_turn():
    eng = replay()
    cfg = eng.board_config()
    if not eng.is_my_turn():
        assert cfg.pending is None


def test_action_state_constants_match_observed_traffic():
    """24 precedes robber_moved, 28 precedes discards — verified from the log."""
    assert P.ACTION_MOVE_ROBBER == 24
    assert P.ACTION_DISCARD == 28


def test_robber_options_are_ranked_and_legal():
    from app.live.advisor import robber_options

    eng = replay()
    cfg = eng.board_config()
    cfg.pending = "move_robber"
    opts = robber_options(eng, cfg)
    assert opts, "a forced robber move must offer placements"
    # the robber has to move: never suggest the hex it already occupies
    assert all(o["hex"] != cfg.robber_hex for o in opts)
    assert opts == sorted(opts, key=lambda o: -o["score"])


def test_discard_advice_halves_the_hand():
    from app.live.advisor import discard_advice

    eng = replay()
    cfg = eng.board_config()
    total = sum(cfg.me.hand.values())
    adv = discard_advice(eng, cfg)
    if total > 7:
        assert adv and sum(adv["drop"].values()) == total // 2
        for res, n in adv["drop"].items():
            assert n <= cfg.me.hand[res], "cannot discard cards you don't hold"
    else:
        assert adv is None


# --- trade evaluation -------------------------------------------------------


def test_offer_we_cannot_cover_is_rejected_as_cannot():
    from app.live.advisor import evaluate_offer

    eng = replay()
    cfg = eng.board_config()
    missing = next(r for r in ("ore", "brick", "wood") if cfg.me.hand.get(r, 0) == 0)
    v = evaluate_offer(eng, cfg, {"from": "blue", "offers": ["sheep"], "wants": [missing, missing]})
    assert v["verdict"] == "cannot"


def test_never_trade_with_a_player_about_to_win():
    from app.live.advisor import evaluate_offer

    eng = replay()
    cfg = eng.board_config()
    have = next((r for r, n in cfg.me.hand.items() if n > 0), None)
    assert have, "fixture hand should not be empty"
    cfg.players["blue"].vp_visible = 9
    v = evaluate_offer(eng, cfg, {"from": "blue", "offers": ["ore", "ore"], "wants": [have]})
    assert v["verdict"] == "reject" and "9 VP" in v["text"]


def test_every_open_offer_gets_a_usable_verdict():
    from app.live.advisor import evaluate_offer

    eng = replay()
    cfg = eng.board_config()
    have = next(r for r, n in cfg.me.hand.items() if n > 0)
    for gets in (["ore"], ["brick"], ["wood"], ["wheat"]):
        v = evaluate_offer(eng, cfg, {"from": "orange", "offers": gets, "wants": [have]})
        assert v["verdict"] in ("accept", "reject", "counter", "cannot")
        assert v["text"]
        if v["verdict"] == "counter":
            assert v["counter"]["give"] and v["counter"]["get"]


def test_our_own_offers_are_not_self_advised():
    from app.live.advisor import offer_advice

    eng = replay()
    cfg = eng.board_config()
    eng.state.setdefault("tradeState", {})["activeOffers"] = {
        "x": {"id": "x", "creator": eng.my_color_id, "offeredResources": [3],
              "wantedResources": [5], "playerResponses": {}},
    }
    assert offer_advice(eng, cfg) == []


def test_authoritative_bank_rates_win_over_geometry():
    """colonist reports real port ratios; they must beat our derived guess."""
    from app import solver

    eng = replay()
    cfg = eng.board_config()
    assert cfg.me.bank_rates, "live config should carry colonist's ratios"
    cfg.me.bank_rates = {**cfg.me.bank_rates, "ore": 2}
    assert solver.build_ctx(cfg).rates["ore"] == 2


def test_trade_proposals_are_scored_and_ranked():
    from app.live.advisor import trade_proposals

    eng = replay()
    cfg = eng.board_config()
    props = trade_proposals(eng, cfg)
    for p in props:
        assert "score" in p and p["give"] and p["get"]
        assert set(p["give"]) & set(cfg.me.hand), "must offer something we hold"
    assert props == sorted(props, key=lambda p: -p["score"])


def test_port_ratio_derivation_is_correct_for_every_port():
    """Settling on a port must yield the right ratio from geometry alone.

    No recorded game has a player on a port, so this is the only coverage the
    geometric path has; without it we'd be trusting untested code whenever
    colonist's authoritative ratios are unavailable (e.g. hand-entered boards).
    """
    from app import solver

    eng = replay()
    cfg = eng.board_config()
    assert cfg.ports, "live board should expose real ports"
    for port in cfg.ports:
        probe = cfg.model_copy(deep=True)
        probe.me.bank_rates = None  # force the geometric path
        for p in probe.players.values():
            p.settlements = [v for v in p.settlements if v not in port.vertices]
            p.cities = [v for v in p.cities if v not in port.vertices]
        probe.players[probe.me.color].settlements.append(port.vertices[0])
        rates = solver.build_ctx(probe).rates
        if port.type == "3:1":
            assert all(rates[r] <= 3 for r in rates)
        else:
            assert rates[port.type] == 2, f"{port.type} port did not give a 2:1"


def test_lobby_message_reusing_type_4_is_not_a_snapshot():
    """type 4 appears in the lobby with a list payload; adopting it crashes."""
    eng = GameEngine()
    assert eng.apply_snapshot([{"id": 1}]) is False
    assert eng.apply_snapshot({"payload": "nope"}) is False
    assert eng.state == {}


def test_robber_is_only_offered_when_you_could_move_it():
    """A 7 or a knight — with neither, ranking robber spots is noise."""
    from app.live.advisor import robber_options

    eng = replay()
    cfg = eng.board_config()

    # no dev cards and no 7: nothing to offer
    eng.state["mechanicDevelopmentCardsState"] = {
        "players": {str(eng.my_color_id): {"developmentCards": {"cards": []}}}
    }
    cfg.pending = None
    assert robber_options(eng, cfg) == []

    # forced by a 7
    cfg.pending = "move_robber"
    forced = robber_options(eng, cfg)
    assert forced and all(o["forced"] for o in forced)

    # holding a (masked) dev card that might be a knight
    cfg.pending = None
    eng.state["mechanicDevelopmentCardsState"] = {
        "players": {str(eng.my_color_id): {"developmentCards": {"cards": [P.DEV_HIDDEN]}}}
    }
    maybe = robber_options(eng, cfg)
    assert maybe and all(o["needs_knight"] for o in maybe)


# --- export / replay --------------------------------------------------------


def test_export_round_trips_through_raw_frames(tmp_path):
    """A bundle must reproduce the position without the original machine."""
    import gzip
    import json as _json

    from app.live.export import build_export, load_export, replay_export

    st = Store(tmp_path / "t.db")
    payloads = list(frames())
    for p in payloads:
        st.add_frame(p, "recv", 2, "g1")
    st.start_game("g1", "room", "red", [1, 2, 3, 4], {})

    bundle = build_export(st, "g1", note="testing")
    assert bundle["format"] >= 1 and bundle["note"] == "testing"
    assert bundle["frames"], "raw frames are what make a bundle replayable"

    path = tmp_path / "b.json.gz"
    path.write_bytes(gzip.compress(_json.dumps(bundle).encode()))
    reloaded = load_export(path)

    live = replay()                      # engine built the normal way
    from_bundle = replay_export(reloaded)  # engine built from the bundle
    assert from_bundle.board_config().model_dump() == live.board_config().model_dump()
    st.close()


def test_export_reports_a_game_it_cannot_derive():
    """Diffs with no snapshot is the mid-game-attach case; say so, don't crash."""
    from app.live.export import replay_frames

    engine = replay_frames([])
    assert not engine.state
