"""colonist.io websocket protocol: decoding, vocabulary, coordinate mapping.

Everything here is derived from observed traffic on
`wss://socket.svr.colonist.io/?version=2` (msgpack, binary opcode 2) and
validated against app.board's canonical geometry. Colonist uses the *same*
axial convention we do, which makes the mapping exact rather than fuzzy:

    hex    (x, y)      -> our (q=x, r=y)
    corner (x, y, z)   -> our vertex key (x, y, "N" if z == 0 else "S")
    edge   (x, y, z)   -> z 0/1/2 = NW / W / SW side of hex (x, y)

The edge encoding was solved by geometric search and is asserted at import.
"""
from __future__ import annotations

import base64
import math
from typing import Any, Optional

import msgpack

from .. import board

# --- enums ------------------------------------------------------------------

RESOURCE = {0: "desert", 1: "wood", 2: "brick", 3: "sheep", 4: "wheat", 5: "ore"}
CARD = {1: "wood", 2: "brick", 3: "sheep", 4: "wheat", 5: "ore"}
# colonist player colors. Ids 1-4 are the standard palette, confirmed from the
# lobby's availableColors ["red","blue","orange","green"]. Other ids appear in
# tutorial//special modes; map_color() folds them into the four we model so an
# odd id can never break BoardConfig validation.
COLOR = {1: "red", 2: "blue", 3: "orange", 4: "green"}
PALETTE = ["red", "blue", "orange", "green"]


def map_color(color_id: Any) -> Optional[str]:
    """colonist color id -> one of our four colors (stable, never None for ints)."""
    if not isinstance(color_id, int):
        return None
    if color_id in COLOR:
        return COLOR[color_id]
    return PALETTE[(color_id - 1) % len(PALETTE)]

BUILDING = {1: "settlement", 2: "city"}
PIECE = {0: "road", 2: "settlement", 3: "city", 5: "robber"}

# portEdgeStates .type: 1 is the generic 3:1 (four of them on a standard board);
# 2..6 are the resource 2:1 ports, offset by one from the resource enum.
def port_type(t: Any) -> Optional[str]:
    if t == 1:
        return "3:1"
    return CARD.get((t or 0) - 1)

# message envelope types (data.type)
MSG_GAME_SNAPSHOT = 4      # full gameState
MSG_GAME_DIFF = 91         # partial state diff (the move stream)
MSG_ROOM_STATE = "stateUpdated"

# currentState.actionState — what the active player is being asked to do.
# Established by correlating the value with the log events that follow it:
# 24 always precedes a robber_moved, 28 precedes cards_discarded on a 7.
ACTION_IDLE = 0
ACTION_TURN = 1
ACTION_SETUP_PLACE = 3
ACTION_BUY_DEV = 4
ACTION_MOVE_ROBBER = 24
ACTION_DISCARD = 28

# gameLogState text types -> semantic event kind
LOG = {
    1: "turn_started",
    4: "piece_placed",
    5: "piece_bought",
    10: "dice_rolled",
    11: "robber_moved",
    14: "card_stolen",
    44: "turn_ended",
    47: "cards_received",
    55: "cards_discarded",
    58: "robber_placed",
    60: "info",
    64: "info",
    73: "info",
    74: "info",
    115: "trade_player",
    116: "trade_bank",
    118: "trade_offered",
}


def decode_frame(payload_b64: str) -> Optional[dict]:
    """Decode a base64 binary websocket frame into its msgpack object."""
    try:
        obj = msgpack.unpackb(base64.b64decode(payload_b64), raw=False)
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


def envelope(obj: dict) -> Optional[dict]:
    """Return the inner `data` dict of a colonist envelope, if present."""
    data = obj.get("data")
    return data if isinstance(data, dict) else None


# --- coordinate mapping -----------------------------------------------------

_S3 = math.sqrt(3)
_EDGE_OFFSET = {  # unit-size midpoint offsets from hex center, y down
    "NE": (_S3 / 4, -0.75),
    "E": (_S3 / 2, 0.0),
    "SE": (_S3 / 4, 0.75),
    "SW": (-_S3 / 4, 0.75),
    "W": (-_S3 / 2, 0.0),
    "NW": (-_S3 / 4, -0.75),
}
EDGE_Z_DIR = {0: "NW", 1: "W", 2: "SW"}


def hex_to_canonical(x: int, y: int) -> Optional[int]:
    return board.HEX_ID.get((x, y))


def corner_to_vertex(c: dict) -> Optional[int]:
    key = (c["x"], c["y"], "N" if c["z"] == 0 else "S")
    return board.VERTEX_ID.get(key)


def edge_to_canonical(e: dict) -> Optional[int]:
    """Map a colonist edge record to our canonical edge id (geometric match)."""
    d = EDGE_Z_DIR.get(e["z"])
    if d is None:
        return None
    cx = _S3 * board.SIZE * (e["x"] + e["y"] / 2)
    cy = 1.5 * board.SIZE * e["y"]
    ox, oy = _EDGE_OFFSET[d]
    mx, my = cx + ox * board.SIZE, cy + oy * board.SIZE
    best, best_d = None, 1e9
    for eid, (px, py) in enumerate(board.EDGE_PIXEL):
        dist = (px - mx) ** 2 + (py - my) ** 2
        if dist < best_d:
            best, best_d = eid, dist
    return best if best_d < (board.SIZE * 0.1) ** 2 else None


def build_maps(map_state: dict) -> dict[str, dict[str, int]]:
    """Build colonist-id -> canonical-id lookups from a full mapState snapshot."""
    hexes, corners, edges = {}, {}, {}
    for hid, h in (map_state.get("tileHexStates") or {}).items():
        cid = hex_to_canonical(h["x"], h["y"])
        if cid is not None:
            hexes[str(hid)] = cid
    for cid_, c in (map_state.get("tileCornerStates") or {}).items():
        v = corner_to_vertex(c)
        if v is not None:
            corners[str(cid_)] = v
    for eid, e in (map_state.get("tileEdgeStates") or {}).items():
        ce = edge_to_canonical(e)
        if ce is not None:
            edges[str(eid)] = ce
    return {"hexes": hexes, "corners": corners, "edges": edges}


def deep_merge(base: Any, diff: Any) -> Any:
    """Recursively merge a colonist diff into state.

    Dicts merge key-by-key; every other type replaces wholesale (colonist sends
    complete replacement arrays for things like `resourceCards.cards`).
    """
    if isinstance(base, dict) and isinstance(diff, dict):
        out = dict(base)
        for k, v in diff.items():
            out[k] = deep_merge(out.get(k), v) if k in out else v
        return out
    return diff


def describe_log(entry: dict) -> Optional[dict]:
    """Turn a gameLogState entry into a flat semantic event."""
    text = entry.get("text")
    if not isinstance(text, dict):
        return None
    kind = LOG.get(text.get("type"), f"log_{text.get('type')}")
    ev: dict[str, Any] = {"kind": kind}
    if "playerColor" in text:
        ev["color"] = map_color(text["playerColor"])
    if kind == "dice_rolled":
        ev["dice"] = [text.get("firstDice"), text.get("secondDice")]
        ev["total"] = (text.get("firstDice") or 0) + (text.get("secondDice") or 0)
    elif kind in ("piece_placed", "piece_bought"):
        ev["piece"] = PIECE.get(text.get("pieceEnum"), text.get("pieceEnum"))
    elif kind == "robber_moved":
        tile = text.get("tileInfo") or {}
        ev["tile"] = {
            "resource": RESOURCE.get(tile.get("resourceType")),
            "number": tile.get("diceNumber"),
        }
    elif kind == "cards_received":
        ev["cards"] = [CARD.get(c, c) for c in text.get("cardsToBroadcast", [])]
    elif kind in ("card_stolen", "cards_discarded"):
        ev["cards"] = [CARD.get(c, c) for c in text.get("cardEnums", [])]
    elif kind == "trade_player":
        ev["with"] = map_color(text.get("acceptingPlayerColor"))
        ev["gave"] = [CARD.get(c, c) for c in text.get("givenCardEnums", [])]
        ev["got"] = [CARD.get(c, c) for c in text.get("receivedCardEnums", [])]
    elif kind == "trade_bank":
        ev["gave"] = [CARD.get(c, c) for c in text.get("givenCardEnums", [])]
        ev["got"] = [CARD.get(c, c) for c in text.get("receivedCardEnums", [])]
    elif kind == "trade_offered":
        ev["wants"] = [CARD.get(c, c) for c in text.get("wantedCardEnums", [])]
        ev["offers"] = [CARD.get(c, c) for c in text.get("offeredCardEnums", [])]
    return ev


# Import-time sanity: every one of our 72 edges must be reachable, and the
# mapping must be injective. The scan runs over a radius-3 hex range because
# colonist names outer-ring edges after hexes that lie *outside* the 19-hex
# board (the 19 board hexes alone only own 19*3 = 57 edges).
def _self_check() -> None:
    seen: dict[int, tuple] = {}
    for q in range(-3, 4):
        for r in range(-3, 4):
            for z in (0, 1, 2):
                e = edge_to_canonical({"x": q, "y": r, "z": z})
                if e is None:
                    continue
                assert e not in seen or seen[e] == (q, r, z), (
                    f"edge {e} claimed by {seen[e]} and {(q, r, z)}"
                )
                seen[e] = (q, r, z)
    assert len(seen) == 72, f"edge mapping covers {len(seen)}/72 edges"


_self_check()
