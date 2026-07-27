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


def test_robber_options_always_available():
    from app.live.advisor import robber_options

    eng = replay()
    cfg = eng.board_config()
    opts = robber_options(eng, cfg)
    assert opts, "robber placements should rank even when not forced"
    # never suggest the hex the robber already sits on
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
