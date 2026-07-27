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

from .. import board
from ..models import BoardConfig, DevCards, HexTile, MyState, PlayerState, Port
from . import protocol as P


class GameEngine:
    def __init__(self) -> None:
        self.state: dict[str, Any] = {}
        self.maps: dict[str, dict[str, int]] = {"hexes": {}, "corners": {}, "edges": {}}
        self.my_color_id: Optional[int] = None
        self.play_order: list[int] = []
        self.applied = 0
        self.events: list[dict] = []
        self._seen_log_ids: set[int] = set()

    # --- ingest -------------------------------------------------------------

    def apply_snapshot(self, payload: dict) -> None:
        self.state = payload["gameState"]
        self.my_color_id = payload.get("playerColor")
        self.play_order = payload.get("playOrder", [])
        self.maps = P.build_maps(self.state.get("mapState", {}))
        self.applied = 1

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

    def dev_cards_used(self) -> dict[str, int]:
        """Knights/dev cards each player has *played* — public information."""
        out: dict[str, int] = {}
        dev = (self.state.get("mechanicDevelopmentCardsState") or {}).get("players") or {}
        for cid, ps in dev.items():
            color = P.map_color(int(cid)) if str(cid).isdigit() else None
            if color:
                out[color] = len(ps.get("developmentCardsUsed") or [])
        return out

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
        hexes = [HexTile(resource="desert", number=None) for _ in range(19)]
        for hid, h in (ms.get("tileHexStates") or {}).items():
            cid = self.maps["hexes"].get(str(hid))
            if cid is None:
                continue
            res = P.RESOURCE.get(h.get("type"), "desert")
            num = h.get("diceNumber") or None
            hexes[cid] = HexTile(
                resource=res, number=None if res == "desert" else num
            )

        players: dict[str, PlayerState] = {c: PlayerState() for c in P.PALETTE}
        for cid_, c in (ms.get("tileCornerStates") or {}).items():
            if not isinstance(c, dict) or not c.get("owner"):
                continue
            vid = self.maps["corners"].get(str(cid_))
            if vid is None:
                continue
            color = P.map_color(c["owner"])
            if not color:
                continue
            if c.get("buildingType") == 2:
                players[color].cities.append(vid)
            else:
                players[color].settlements.append(vid)
        for eid, e in (ms.get("tileEdgeStates") or {}).items():
            if not isinstance(e, dict) or not e.get("owner"):
                continue
            ceid = self.maps["edges"].get(str(eid))
            color = P.map_color(e["owner"])
            if ceid is not None and color:
                players[color].roads.append(ceid)

        for cid, ps in (self.state.get("playerStates") or {}).items():
            color = P.map_color(int(cid)) if str(cid).isdigit() else None
            if not color:
                continue
            vps = ps.get("victoryPointsState") or {}
            players[color].vp_visible = sum(v for v in vps.values() if isinstance(v, int))
            players[color].resource_count = len(
                (ps.get("resourceCards") or {}).get("cards") or []
            )
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
        pending = (
            "move_robber"
            if (self.action_state() == P.ACTION_MOVE_ROBBER and self.is_my_turn())
            else None
        )

        return BoardConfig(
            hexes=hexes,
            ports=ports,
            robber_hex=self.robber_hex(),
            players=players,  # type: ignore[arg-type]
            me=MyState(
                color=self.my_color,  # type: ignore[arg-type]
                hand=self.my_hand(),
                dev_cards=DevCards(),
            ),
            phase=self.phase(),  # type: ignore[arg-type]
            turn=self.current_turn(),  # type: ignore[arg-type]
            pending=pending,  # type: ignore[arg-type]
        )
