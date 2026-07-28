"""Game-state engine: colonist snapshot + diffs -> current position -> BoardConfig.

The engine is a pure fold. `apply_snapshot` sets the base state, every
`apply_diff` deep-merges one colonist diff, and `board_config()` projects the
accumulated state onto our solver's `BoardConfig`. Because it is a fold over an
append-only frame log, replaying the log always reproduces the same position —
that is what makes recovery after a dropped connection trivial.
"""
from __future__ import annotations

import time
from collections import Counter
from typing import Any, Optional

from .. import board, rules
from ..models import BoardConfig, DevCards, HexTile, MyState, PlayerState, Port
from . import protocol as P
from .trades import TradeMemory


def _game_key(payload: dict) -> tuple:
    """Identity of a game: who is playing, and on which board.

    colonist never sends a game id, so a re-sent snapshot is otherwise
    indistinguishable from a new game. The roster plus the tile layout is
    stable across a reconnect and different for the next game.
    """
    roster = tuple(sorted(
        (str(u.get("userId") or u.get("username")), u.get("selectedColor"))
        for u in (payload.get("playerUserStates") or [])
        if isinstance(u, dict)
    ))
    hexes = (payload.get("gameState", {}).get("mapState", {}) or {}).get("tileHexStates") or {}
    tiles = tuple(sorted(
        (h.get("x"), h.get("y"), h.get("type"), h.get("diceNumber"))
        for h in hexes.values() if isinstance(h, dict)
    ))
    return (roster, tiles)


class GameEngine:
    def __init__(self) -> None:
        self.state: dict[str, Any] = {}
        self.maps: dict[str, dict[str, int]] = {"hexes": {}, "corners": {}, "edges": {}}
        self.my_color_id: Optional[int] = None
        self.play_order: list[int] = []
        self.applied = 0
        self.events: list[dict] = []
        self.trade_memory = TradeMemory()
        self.game_key: Optional[tuple] = None
        self._seen_log_ids: set[int] = set()

    # --- ingest -------------------------------------------------------------

    def apply_snapshot(self, payload: Any) -> bool:
        """Adopt a full game snapshot. Returns False if this isn't one.

        Message type 4 is reused in the lobby with a completely different
        (list) payload, so the shape has to be checked rather than assumed --
        blindly indexing it crashes the feed and loses the game.
        """
        if not isinstance(payload, dict) or not isinstance(payload.get("gameState"), dict):
            return False
        self.state = payload["gameState"]
        self.my_color_id = payload.get("playerColor")
        self.play_order = payload.get("playOrder", [])

        # The shape of the board comes from the board, not from an assumption:
        # a 5-6 player game is 30 hexes, and colonist ships others besides.
        # This has to happen before the id maps are built, since those are
        # keyed by the geometry.
        ms = self.state.get("mapState") or {}
        coords = [
            (h["x"], h["y"])
            for h in (ms.get("tileHexStates") or {}).values()
            if isinstance(h, dict) and "x" in h and "y" in h
        ]
        if coords:
            board.use(board.for_coords(coords))
            rules.use(rules.for_board(len(coords)))
        self.maps = P.build_maps(self.state.get("mapState", {}))

        # A resync mid-game re-sends the whole snapshot, and that is exactly
        # when the trade history matters most -- so only forget it when this is
        # demonstrably a different game.
        key = _game_key(payload)
        if self.game_key is not None and key != self.game_key:
            self.trade_memory = TradeMemory()
        self.game_key = key
        self.trade_memory.observe(
            self.state.get("tradeState") or {}, self.my_color_id, P.map_color
        )
        self.applied = 1
        return True

    def apply_diff(self, diff: dict) -> list[dict]:
        """Merge one diff; returns the semantic events it contained."""
        if not self.state:
            return []
        new_events = []
        for log_id, entry in (diff.get("gameLogState") or {}).items():
            try:
                lid = int(log_id)
            except (TypeError, ValueError):
                continue
            if lid in self._seen_log_ids:
                continue
            ev = P.describe_log(entry)
            if ev:
                ev["log_id"] = lid
                self._seen_log_ids.add(lid)
                new_events.append(ev)
        self.state = P.deep_merge(self.state, diff)
        # Read trades from merged state, never from the diff: a diff carries
        # only the fields that changed, so a response arrives detached from the
        # offer it answers. The memory dedupes the re-reads.
        if isinstance(diff.get("tradeState"), dict):
            self.trade_memory.observe(
                self.state.get("tradeState") or {}, self.my_color_id, P.map_color
            )
        self.applied += 1
        self.events.extend(new_events)
        return new_events

    # --- derived views ------------------------------------------------------

    @property
    def my_color(self) -> str:
        return P.map_color(self.my_color_id) or "red"

    def current_turn(self) -> Optional[str]:
        c = (self.state.get("currentState") or {}).get("currentTurnPlayerColor")
        return P.map_color(c) if c else None

    def is_my_turn(self) -> bool:
        cur = (self.state.get("currentState") or {}).get("currentTurnPlayerColor")
        return cur == self.my_color_id

    def dice_thrown(self) -> bool:
        """Has the active player rolled yet this turn?

        colonist states this outright in diceState.diceThrown, which is worth
        preferring over inferring it from actionState: the pre-roll window is
        short and changes the action state rarely, so a correlation over
        recorded games sees almost none of it.
        """
        return bool((self.state.get("diceState") or {}).get("diceThrown"))

    def action_state(self) -> Optional[int]:
        return (self.state.get("currentState") or {}).get("actionState")

    def phase(self) -> str:
        """setup1 / setup2 / main, inferred from settlements placed so far."""
        placed = Counter()
        for c in (self.state.get("mapState", {}).get("tileCornerStates") or {}).values():
            if isinstance(c, dict) and c.get("owner"):
                placed[c["owner"]] += 1
        n_players = max(len(self.play_order), len(self.state.get("playerStates") or {}), 1)
        mine = placed.get(self.my_color_id, 0)
        if sum(placed.values()) >= 2 * n_players or mine >= 2:
            return "main"
        return "setup1" if mine == 0 else "setup2"

    def dice_history(self) -> list[int]:
        return [e["total"] for e in self.events if e.get("kind") == "dice_rolled"]

    def robber_hex(self) -> int:
        rob = self.state.get("mechanicRobberState") or {}
        idx = rob.get("locationTileIndex")
        if idx is not None:
            mapped = self.maps["hexes"].get(str(idx))
            if mapped is not None:
                return mapped
        # fall back to the desert
        for hid, h in (self.state.get("mapState", {}).get("tileHexStates") or {}).items():
            if h.get("type") == 0:
                return self.maps["hexes"].get(str(hid), 0)
        return 0

    def turn_timer(self) -> Optional[dict[str, Any]]:
        """Seconds left in the current action, from startTime + allocatedTime."""
        cs = self.state.get("currentState") or {}
        start, allocated = cs.get("startTime"), cs.get("allocatedTime")
        if not start or not allocated:
            return None
        elapsed = time.time() - (start / 1000.0)
        return {
            "allocated": allocated,
            "remaining": max(0.0, round(allocated - elapsed, 1)),
        }

    def ports(self) -> list[Port]:
        """Real port positions from portEdgeStates (not inferred from ratios)."""
        out: list[Port] = []
        for _pid, p in (self.state.get("mapState", {}).get("portEdgeStates") or {}).items():
            ptype = P.port_type(p.get("type"))
            eid = P.edge_to_canonical(p)
            if ptype is None or eid is None:
                continue
            a, b = board.EDGE_VERTICES[eid]
            out.append(Port(type=ptype, vertices=[a, b]))  # type: ignore[arg-type]
        return out

    def pieces_left(self, color: str) -> dict[str, int]:
        """Pieces still in a player's supply, as colonist reports them."""
        keys = {
            "settlement": ("mechanicSettlementState", "bankSettlementAmount"),
            "city": ("mechanicCityState", "bankCityAmount"),
            "road": ("mechanicRoadState", "bankRoadAmount"),
        }
        out: dict[str, int] = {}
        for kind, (section, field) in keys.items():
            for cid, v in (self.state.get(section) or {}).items():
                if str(cid).isdigit() and P.map_color(int(cid)) == color:
                    if isinstance(v, dict) and isinstance(v.get(field), int):
                        out[kind] = v[field]
        return out

    def longest_roads(self) -> dict[str, int]:
        """Each player's longest road, per colonist's own calculation."""
        out: dict[str, int] = {}
        for cid, v in (self.state.get("mechanicLongestRoadState") or {}).items():
            if str(cid).isdigit() and isinstance(v, dict) and isinstance(v.get("longestRoad"), int):
                color = P.map_color(int(cid))
                if color:
                    out[color] = v["longestRoad"]
        return out

    def bank_stock(self) -> dict[str, int]:
        """Cards left in the bank; an empty pile cannot be traded for."""
        cards = (self.state.get("bankState") or {}).get("resourceCards") or {}
        return {P.CARD[int(k)]: v for k, v in cards.items() if int(k) in P.CARD}

    def discard_limit(self) -> int:
        ps = (self.state.get("playerStates") or {}).get(str(self.my_color_id)) or {}
        limit = ps.get("cardDiscardLimit")
        return limit if isinstance(limit, int) else 7

    def dev_cards_used(self) -> dict[str, int]:
        """How many dev cards each player has played — public information."""
        out: dict[str, int] = {}
        dev = (self.state.get("mechanicDevelopmentCardsState") or {}).get("players") or {}
        for cid, ps in dev.items():
            color = P.map_color(int(cid)) if str(cid).isdigit() else None
            if color:
                out[color] = len(ps.get("developmentCardsUsed") or [])
        return out

    def knights_played(self) -> dict[str, int]:
        """Knights each player has played — played cards are face up, so exact.

        This drives Largest Army, which is worth two points, so guessing it
        from the size of the used pile would over- or under-count anyone who
        played a monopoly or road building.
        """
        out: dict[str, int] = {}
        dev = (self.state.get("mechanicDevelopmentCardsState") or {}).get("players") or {}
        for cid, ps in dev.items():
            color = P.map_color(int(cid)) if str(cid).isdigit() else None
            if not color:
                continue
            used = ps.get("developmentCardsUsed") or []
            out[color] = sum(1 for c in used if P.DEV_CARD.get(c) == "knight")
        return out

    def my_dev_cards(self) -> dict[str, Any]:
        """Our own dev cards. colonist masks the enum as 10 when hidden, so we
        report a count always and a composition only when it's actually legible."""
        dev = (self.state.get("mechanicDevelopmentCardsState") or {}).get("players") or {}
        mine = dev.get(str(self.my_color_id)) or {}
        cards = (mine.get("developmentCards") or {}).get("cards") or []
        known = Counter(
            P.DEV_CARD[c] for c in cards if c in P.DEV_CARD
        )
        # colonist names the cards bought this turn rather than just counting
        # them, which is what makes the real rule expressible: a card cannot be
        # played the turn it is bought, but the others in your hand still can.
        fresh = Counter(
            P.DEV_CARD[c] for c in (mine.get("developmentCardsBoughtThisTurn") or [])
            if c in P.DEV_CARD
        )
        return {
            "count": len(cards),
            "known": dict(known),
            "hidden": sum(1 for c in cards if c not in P.DEV_CARD),
            "used": len(mine.get("developmentCardsUsed") or []),
            "bought_this_turn": dict(fresh),
            "playable": {k: n - fresh.get(k, 0) for k, n in known.items() if n - fresh.get(k, 0) > 0},
            # one development card per turn, and colonist tracks it for us
            "played_this_turn": bool(mine.get("hasUsedDevelopmentCardThisTurn")),
        }

    def dev_card_counts(self) -> dict[str, int]:
        """How many dev cards each player holds (composition is hidden)."""
        out: dict[str, int] = {}
        dev = (self.state.get("mechanicDevelopmentCardsState") or {}).get("players") or {}
        for cid, ps in dev.items():
            color = P.map_color(int(cid)) if str(cid).isdigit() else None
            if color:
                out[color] = len((ps.get("developmentCards") or {}).get("cards") or [])
        return out

    def production_table(self) -> dict[str, dict[str, dict[str, int]]]:
        """Per player: which dice number pays them what, and how much.

        {color: {"6": {"wood": 2}, "9": {"ore": 1}, ...}} — a city counts twice.
        The robber's hex is excluded, since it pays nobody.
        """
        ms = self.state.get("mapState") or {}
        corners = ms.get("tileCornerStates") or {}
        hexes = ms.get("tileHexStates") or {}
        robber = (self.state.get("mechanicRobberState") or {}).get("locationTileIndex")
        out: dict[str, dict[str, dict[str, int]]] = {}
        for cid_, c in corners.items():
            if not isinstance(c, dict) or not c.get("owner"):
                continue
            color = P.map_color(c["owner"])
            vid = self.maps["corners"].get(str(cid_))
            if not color or vid is None:
                continue
            weight = 2 if c.get("buildingType") == 2 else 1
            for hid, h in hexes.items():
                if self.maps["hexes"].get(str(hid)) not in board.VERTEX_HEXES[vid]:
                    continue
                if str(hid) == str(robber):
                    continue
                res, num = P.RESOURCE.get(h.get("type")), h.get("diceNumber")
                if not num or res in (None, "desert"):
                    continue
                slot = out.setdefault(color, {}).setdefault(str(num), {})
                slot[res] = slot.get(res, 0) + weight
        return out

    def trade_offers(self) -> list[dict[str, Any]]:
        """Open trade offers on the table right now.

        `offers` is what the creator gives away, `wants` is what they ask for —
        so from our side, accepting means giving `wants` and receiving `offers`.
        """
        offers = (self.state.get("tradeState") or {}).get("activeOffers") or {}
        out = []
        for oid, o in offers.items():
            if not isinstance(o, dict):
                continue
            creator = o.get("creator")
            responses = o.get("playerResponses") or {}
            out.append(
                {
                    "id": o.get("id", oid),
                    "from": P.map_color(creator),
                    "from_me": creator == self.my_color_id,
                    "offers": [P.CARD.get(c, c) for c in (o.get("offeredResources") or [])],
                    "wants": [P.CARD.get(c, c) for c in (o.get("wantedResources") or [])],
                    "my_response": responses.get(str(self.my_color_id)),
                    "responses": {
                        P.map_color(int(k)): v for k, v in responses.items() if str(k).isdigit()
                    },
                }
            )
        return out

    def bank_ratios(self) -> dict[str, int]:
        ps = (self.state.get("playerStates") or {}).get(str(self.my_color_id)) or {}
        ratios = ps.get("bankTradeRatiosState") or {}
        return {P.CARD[int(k)]: v for k, v in ratios.items() if int(k) in P.CARD}

    def my_hand(self) -> dict[str, int]:
        ps = (self.state.get("playerStates") or {}).get(str(self.my_color_id)) or {}
        cards = (ps.get("resourceCards") or {}).get("cards") or []
        hand = {r: 0 for r in P.CARD.values()}
        for c in cards:
            name = P.CARD.get(c)
            if name:
                hand[name] += 1
        return hand

    def player_summary(self) -> list[dict]:
        from .tracker import build_tracker

        tracker = build_tracker(self.events, P.PALETTE)
        dev_counts, dev_used = self.dev_card_counts(), self.dev_cards_used()
        production = self.production_table()
        out = []
        corners = self.state.get("mapState", {}).get("tileCornerStates") or {}
        edges = self.state.get("mapState", {}).get("tileEdgeStates") or {}
        for cid, ps in (self.state.get("playerStates") or {}).items():
            try:
                colid = int(cid)
            except ValueError:
                continue
            vps = ps.get("victoryPointsState") or {}
            settlements = sum(
                1 for c in corners.values()
                if isinstance(c, dict) and c.get("owner") == colid and c.get("buildingType") == 1
            )
            cities = sum(
                1 for c in corners.values()
                if isinstance(c, dict) and c.get("owner") == colid and c.get("buildingType") == 2
            )
            roads = sum(
                1 for e in edges.values()
                if isinstance(e, dict) and e.get("owner") == colid
            )
            cards = (ps.get("resourceCards") or {}).get("cards") or []
            color = P.map_color(colid) or str(colid)
            is_me = colid == self.my_color_id
            intel = (
                {"count": len(cards), "known": self.my_hand(), "unknown": 0}
                if is_me
                else tracker.intel(color, len(cards))
            )
            out.append(
                {
                    "color": color,
                    "is_me": is_me,
                    "vp_visible": sum(v for v in vps.values() if isinstance(v, int)),
                    "settlements": settlements,
                    "cities": cities,
                    "roads": roads,
                    "cards": len(cards),
                    "hand": intel,
                    "dev_cards": dev_counts.get(color, 0),
                    "dev_used": dev_used.get(color, 0),
                    "production": production.get(color, {}),
                }
            )
        return out

    # --- projection onto the solver's model ---------------------------------

    def board_config(self) -> BoardConfig:
        ms = self.state.get("mapState") or {}
        mine_dev = self.my_dev_cards()
        hexes = [HexTile(resource="desert", number=None) for _ in board.HEXES]
        for hid, h in (ms.get("tileHexStates") or {}).items():
            cid = self.maps["hexes"].get(str(hid))
            if cid is None:
                continue
            res = P.RESOURCE.get(h.get("type"), "desert")
            num = h.get("diceNumber") or None
            hexes[cid] = HexTile(
                resource=res, number=None if res == "desert" else num
            )

        # Seats, not colours. The palette is every colour the game *can* deal,
        # and filling one player per palette entry told the rest of the app
        # that a three-player game had four players -- a 33% error in the
        # production rate, which divides by how many rolls happen before your
        # turn comes round again. playOrder is the authority on who is here.
        seats = [c for c in (P.map_color(i) for i in self.play_order) if c]
        if not seats:
            seats = [
                c
                for c in (
                    P.map_color(int(i))
                    for i in (self.state.get("playerStates") or {})
                    if str(i).isdigit()
                )
                if c
            ]
        players: dict[str, PlayerState] = {
            c: PlayerState() for c in (seats or P.PALETTE[:4])
        }
        for cid_, c in (ms.get("tileCornerStates") or {}).items():
            if not isinstance(c, dict) or not c.get("owner"):
                continue
            vid = self.maps["corners"].get(str(cid_))
            if vid is None:
                continue
            color = P.map_color(c["owner"])
            if not color:
                continue
            seat = players.setdefault(color, PlayerState())
            if c.get("buildingType") == 2:
                seat.cities.append(vid)
            else:
                seat.settlements.append(vid)
        for eid, e in (ms.get("tileEdgeStates") or {}).items():
            if not isinstance(e, dict) or not e.get("owner"):
                continue
            ceid = self.maps["edges"].get(str(eid))
            color = P.map_color(e["owner"])
            if ceid is not None and color:
                players.setdefault(color, PlayerState()).roads.append(ceid)

        for cid, ps in (self.state.get("playerStates") or {}).items():
            color = P.map_color(int(cid)) if str(cid).isdigit() else None
            if not color:
                continue
            # playerStates is also authoritative on who is in the game
            players.setdefault(color, PlayerState())
            vps = ps.get("victoryPointsState") or {}
            players[color].vp_visible = sum(v for v in vps.values() if isinstance(v, int))
            players[color].resource_count = len(
                (ps.get("resourceCards") or {}).get("cards") or []
            )
            players[color].pieces_left = self.pieces_left(color)
            players[color].knights_played = self.knights_played().get(color, 0)
            players[color].longest_road_len = self.longest_roads().get(color)
        longest = (self.state.get("mechanicLongestRoadState") or {})
        for cid, v in longest.items():
            color = P.map_color(int(cid)) if str(cid).isdigit() else None
            if color and isinstance(v, dict) and v.get("hasLongestRoad"):
                players[color].longest_road = True
        army = (self.state.get("mechanicLargestArmyState") or {})
        for cid, v in army.items():
            color = P.map_color(int(cid)) if str(cid).isdigit() else None
            if color and isinstance(v, dict):
                if v.get("hasLargestArmy"):
                    players[color].largest_army = True
                if isinstance(v.get("knightsPlayed"), int):
                    players[color].knights_played = v["knightsPlayed"]

        ports = self.ports()
        # only advise on the robber when it is actually ours to move
        # A discard is only *required* while colonist is in the discard phase,
        # which it enters for everyone over the limit when a 7 is rolled --
        # note this is not gated on whose turn it is.
        action = self.action_state()
        phase = self.phase()
        pending = None
        if action == P.ACTION_DISCARD:
            pending = "discard"
        elif action == P.ACTION_MOVE_ROBBER and self.is_my_turn():
            pending = "move_robber"
        elif phase == "main" and self.is_my_turn() and not self.dice_thrown():
            # nothing is buildable until the dice are thrown; a knight is the
            # one thing you may play first, and often should
            pending = "roll"

        return BoardConfig(
            hexes=hexes,
            ports=ports,
            robber_hex=self.robber_hex(),
            players=players,  # type: ignore[arg-type]
            me=MyState(
                color=self.my_color,  # type: ignore[arg-type]
                hand=self.my_hand(),
                bank_rates=self.bank_ratios() or None,
                discard_limit=self.discard_limit(),
                # only the cards that are legal to play right now reach the
                # solver: one per turn, and never the one just bought
                dev_cards=DevCards(**{
                    k: v for k, v in mine_dev["playable"].items()
                    if k in DevCards.model_fields
                }),
                dev_card_bought_this_turn=bool(mine_dev["bought_this_turn"]),
                dev_card_played_this_turn=mine_dev["played_this_turn"],
            ),
            bank=self.bank_stock() or None,
            play_order=[c for c in (P.map_color(i) for i in self.play_order) if c],
            phase=phase,  # type: ignore[arg-type]
            turn=self.current_turn(),  # type: ignore[arg-type]
            pending=pending,  # type: ignore[arg-type]
        )
