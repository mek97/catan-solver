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


def test_bank_lists_every_trade_the_rates_allow():
    """Bank showed 'nothing available' while sitting on four sheep, because the
    solver only emits bank trades inside a build combo."""
    from app.live.advisor import bank_options

    eng = replay()
    cfg = eng.board_config()
    cfg.me.hand = {"wood": 0, "brick": 0, "sheep": 4, "wheat": 0, "ore": 0}
    opts = bank_options(eng, cfg)
    assert opts, "four sheep at 4:1 is a legal trade and must be offered"
    assert all(list(o["give"])[0] == "sheep" for o in opts)
    assert all(list(o["get"])[0] != "sheep" for o in opts)
    assert opts == sorted(opts, key=lambda o: -o["score"])

    cfg.me.hand = {"wood": 0, "brick": 0, "sheep": 3, "wheat": 0, "ore": 0}
    assert bank_options(eng, cfg) == [], "three sheep cannot pay a 4:1"


def test_bank_options_respect_an_empty_bank():
    from app.live.advisor import bank_options

    eng = replay()
    cfg = eng.board_config()
    cfg.me.hand = {"wood": 0, "brick": 0, "sheep": 8, "wheat": 0, "ore": 0}
    cfg.bank = {"wood": 0, "brick": 0, "sheep": 19, "wheat": 0, "ore": 5}
    gets = {list(o["get"])[0] for o in bank_options(eng, cfg)}
    assert gets == {"ore"}, "only the pile with stock can be traded for"


# --- trade rejections -------------------------------------------------------


def _offer(oid, creator, offered, wanted, responses):
    return {"id": oid, "creator": creator, "offeredResources": offered,
            "wantedResources": wanted, "playerResponses": responses}


def test_memory_records_who_refused_our_offer():
    """A decline is only meaningful when we know which offer it answered."""
    from app.live.trades import TradeMemory

    mem = TradeMemory()
    colour = {1: "red", 2: "blue", 3: "orange"}.get
    # we are red, offering sheep(3) for ore(5); blue declines, orange accepts
    mem.observe_offer(_offer("a", 1, [3], [5], {"2": 2, "3": 1}), me=1, colour=colour)

    assert mem.was_refused("blue", ["sheep"], ["ore"]) == 1
    assert mem.refuses("blue", "ore") == 1
    assert mem.was_refused("orange", ["sheep"], ["ore"]) == 0
    assert mem.accepted[("orange", ("sheep",), ("ore",))] == 1


def test_repeated_frames_do_not_inflate_refusal_counts():
    """An open offer is re-sent on every diff; it is still one refusal."""
    from app.live.trades import TradeMemory

    mem = TradeMemory()
    colour = {1: "red", 2: "blue"}.get
    for _ in range(5):
        mem.observe_offer(_offer("a", 1, [3], [5], {"2": 2}), me=1, colour=colour)
    assert mem.refuses("blue", "ore") == 1


def test_opponent_offers_reveal_what_they_need():
    """What they ask for is what they're short of; what they give is spare."""
    from app.live.trades import TradeMemory

    mem = TradeMemory()
    colour = {1: "red", 2: "blue"}.get
    mem.observe_offer(_offer("a", 2, [4], [1], {}), me=1, colour=colour)
    assert mem.wants["blue"]["wood"] == 1
    assert mem.spare["blue"]["wheat"] == 1


def test_refused_trade_is_re_aimed_at_another_player():
    from app.live.advisor import _choose_partner
    from app.live.trades import TradeMemory

    mem = TradeMemory()
    cands = [{"color": "blue", "pips": 5, "cards": 6},
             {"color": "orange", "pips": 2, "cards": 4}]
    hand = {"sheep": 3}

    assert _choose_partner(mem, cands, "sheep", "ore", 1, hand)["color"] == "blue"

    mem.refused[("blue", ("sheep",), ("ore",))] = 1
    mem.wont_give["blue"]["ore"] = 1
    pick = _choose_partner(mem, cands, "sheep", "ore", 1, hand)
    assert pick["color"] == "orange", "must move on to a partner who hasn't refused"
    assert not pick["sweetened"]


def test_when_everyone_refuses_the_price_goes_up():
    from app.live.advisor import _choose_partner
    from app.live.trades import TradeMemory

    mem = TradeMemory()
    cands = [{"color": "blue", "pips": 5, "cards": 6},
             {"color": "orange", "pips": 2, "cards": 4}]
    for c in ("blue", "orange"):
        mem.refused[(c, ("sheep",), ("ore",))] = 1
        mem.wont_give[c]["ore"] = 1

    pick = _choose_partner(mem, cands, "sheep", "ore", 1, {"sheep": 3})
    assert pick["sweetened"] and pick["give_n"] == 2

    # ...but only if we can actually spare the extra card
    assert _choose_partner(mem, cands, "sheep", "ore", 1, {"sheep": 1}) is None


def test_engine_folds_a_response_onto_the_offer_it_answers():
    """The offer and the response to it arrive in separate diffs.

    colonist sends only changed fields, so the diff carrying a decline has no
    creator and no resources on it -- the memory has to read the merged state
    or it records a refusal of nothing.
    """
    eng = replay()
    me = eng.my_color_id
    other = next(c for c in (1, 2, 3, 4) if c != me)
    mem = eng.trade_memory
    before = mem.refuses(P.map_color(other), "ore")

    eng.apply_diff({"tradeState": {"activeOffers": {
        "z": _offer("z", me, [3], [5], {str(other): 0})}}})
    assert mem.refuses(P.map_color(other), "ore") == before, "no response yet"

    # second diff: just the response, detached from the offer
    eng.apply_diff({"tradeState": {"activeOffers": {
        "z": {"playerResponses": {str(other): 2}}}}})
    assert mem.refuses(P.map_color(other), "ore") == before + 1
    assert mem.refused_us(P.map_color(other), ["sheep"], ["ore"]) == 1


def test_a_decline_of_anyones_offer_is_recorded():
    """Whoever asked, a refusal prices the trade we are about to propose."""
    eng = replay()
    me = eng.my_color_id
    a, b = [c for c in (1, 2, 3, 4) if c != me][:2]
    mem = eng.trade_memory
    # the recorded game already has trades in it, so measure the change
    before = mem.refuses(P.map_color(b), "ore")
    wanted_before = mem.wants[P.map_color(a)]["ore"]

    eng.apply_diff({"tradeState": {"activeOffers": {
        "q": _offer("q", a, [3], [5], {str(b): 2})}}})
    assert mem.refuses(P.map_color(b), "ore") == before + 1
    # ...but it was not us who was turned down
    assert mem.refused_us(P.map_color(b), ["sheep"], ["ore"]) == 0
    # and the asker revealed what they are short of
    assert mem.wants[P.map_color(a)]["ore"] == wanted_before + 1


def test_a_snapshot_is_recorded_inside_the_game_it_opens():
    """The snapshot frame must land in its own game, or nothing replays.

    Storing the frame before minting the game id filed every snapshot under
    the *previous* game, which is why exported bundles rebuilt to nothing.
    """
    import base64
    import msgpack

    from app.live.export import replay_frames
    from app.live.feed import LiveFeed
    from app.live.store import Store

    src = replay()
    snapshot = {"data": {"type": P.MSG_GAME_SNAPSHOT, "payload": {
        "gameState": src.state, "playerColor": src.my_color_id,
        "playOrder": src.play_order, "playerUserStates": []}}}
    diff = {"data": {"type": P.MSG_GAME_DIFF, "payload": {"diff": {"someState": {"a": 1}}}}}

    def frame(obj):
        return base64.b64encode(msgpack.packb(obj, use_bin_type=True)).decode()

    feed = LiveFeed(store=Store(path=":memory:"))
    feed.ingest(frame(snapshot), "in", 2)
    game_id = feed.game_id
    feed.ingest(frame(diff), "in", 2)

    assert game_id, "a snapshot must open a game"
    rebuilt = replay_frames(list(feed.store.replay_frames(game_id)))
    assert rebuilt.state, "the snapshot must be replayable from its own game"
    assert rebuilt.my_color_id == src.my_color_id


def test_a_resync_continues_the_same_recording():
    """Re-sending a snapshot mid-game must not split the game in two."""
    import base64
    import msgpack

    from app.live.feed import LiveFeed
    from app.live.store import Store

    src = replay()
    payload = {"gameState": src.state, "playerColor": src.my_color_id,
               "playOrder": src.play_order, "playerUserStates": []}
    blob = base64.b64encode(msgpack.packb(
        {"data": {"type": P.MSG_GAME_SNAPSHOT, "payload": payload}}, use_bin_type=True)).decode()

    feed = LiveFeed(store=Store(path=":memory:"))
    feed.ingest(blob, "in", 2)
    first = feed.game_id

    # the resync snapshot differs only in the clock, so it is a distinct frame
    # carrying the same game
    again = base64.b64encode(msgpack.packb(
        {"data": {"type": P.MSG_GAME_SNAPSHOT, "payload": {**payload, "timeLeftInState": 42}}},
        use_bin_type=True)).decode()
    feed.ingest(again, "in", 2)
    assert feed.game_id == first, "a resync is the same game, so one recording"
