"""Screenshot -> BoardConfig via vision models.

Two interchangeable backends produce the same RawBoard shape:
  - Claude vision (Anthropic SDK, needs API credentials)
  - Codex CLI (`codex exec -i ...`, uses the locally-authed ChatGPT plan)

The model never emits canonical IDs. It references hexes by (row, pos) in the
standard 3-4-5-4-3 layout and pieces by hex + compass corner/edge; this module
resolves those references through app.board. Unresolvable references become
warnings, never hard failures -- the UI editor is the safety net.
"""
from __future__ import annotations

import base64
import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Literal, Optional

import anthropic
from pydantic import BaseModel, Field

from . import board
from .models import BoardConfig, DevCards, HexTile, MyState, PlayerState, Port

MODEL = "claude-opus-5"

Corner = Literal["N", "NE", "SE", "S", "SW", "NW"]
EdgeDir = Literal["NE", "E", "SE", "SW", "W", "NW"]
ColorName = Literal["red", "blue", "orange", "green"]


class RawHex(BaseModel):
    row: int = Field(description="0-4, top to bottom")
    pos: int = Field(description="0-based position within the row, left to right")
    resource: Literal["wood", "brick", "sheep", "wheat", "ore", "desert"]
    number: Optional[int] = Field(default=None, description="2-12 token, null for desert or unreadable")


class RawPiece(BaseModel):
    row: int
    pos: int
    corner: Corner = Field(description="corner of that hex the piece sits on")


class RawRoad(BaseModel):
    row: int
    pos: int
    edge: EdgeDir = Field(description="side of that hex the road lies along")


class RawPort(BaseModel):
    row: int
    pos: int
    side: EdgeDir = Field(description="side of the land hex the port attaches to")
    type: Literal["3:1", "wood", "brick", "sheep", "wheat", "ore"]


class RawPlayerPieces(BaseModel):
    settlements: list[RawPiece] = Field(default_factory=list)
    cities: list[RawPiece] = Field(default_factory=list)
    roads: list[RawRoad] = Field(default_factory=list)


class RawPanel(BaseModel):
    color: ColorName
    vp: int = 0
    resource_count: int = 0
    dev_card_count: int = 0
    knights_played: int = 0
    longest_road: bool = False
    largest_army: bool = False


class RawHand(BaseModel):
    wood: int = 0
    brick: int = 0
    sheep: int = 0
    wheat: int = 0
    ore: int = 0


class RawBoard(BaseModel):
    hexes: list[RawHex]
    robber: Optional[RawHex] = None
    ports: list[RawPort] = Field(default_factory=list)
    red: RawPlayerPieces = Field(default_factory=RawPlayerPieces)
    blue: RawPlayerPieces = Field(default_factory=RawPlayerPieces)
    orange: RawPlayerPieces = Field(default_factory=RawPlayerPieces)
    green: RawPlayerPieces = Field(default_factory=RawPlayerPieces)
    players: list[RawPanel] = Field(default_factory=list)
    my_hand: RawHand = Field(default_factory=RawHand)
    my_color: Optional[ColorName] = Field(
        default=None, description="color of the player whose hand is at the bottom of the screen"
    )
    uncertain: list[str] = Field(
        default_factory=list, description="anything you could not read confidently"
    )


PROMPT = """You are reading a screenshot of a Settlers of Catan board on colonist.io.

The board is 5 rows of hexes, top to bottom, with 3, 4, 5, 4, 3 hexes per row.
Before answering, count the hexes in each row and confirm the 3-4-5-4-3 shape.

Report, using 0-based row (top to bottom) and pos (left to right within the row):

1. hexes: every hex's resource and number token. Resources: wood (dark forest),
   sheep (light green pasture), wheat (yellow field), brick (red/orange clay),
   ore (grey mountain), desert (tan, no number). Numbers are the circular
   tokens, 2-12, never 7; the 6 and 8 tokens are printed in red.
   A standard board has 4 wood, 4 sheep, 4 wheat, 3 brick, 3 ore, 1 desert and
   tokens {2x1, 3x2, 4x2, 5x2, 6x2, 8x2, 9x2, 10x2, 11x2, 12x1}. If what you
   see disagrees, report what you SEE and note the mismatch in `uncertain` --
   never force-fit.
2. robber: the hex the grey robber piece stands on.
3. ports: for each port icon in the water, the land hex it attaches to, the
   side of that hex it touches (NE/E/SE/SW/W/NW), and its type.
4. For each player color (red, blue, orange, green): settlements (small house),
   cities (larger building), each as the hex it most clearly sits on plus the
   corner (N/NE/SE/S/SW/NW); roads as hex plus edge side (NE/E/SE/SW/W/NW).
5. players: each player panel's visible victory points, hand-card count,
   dev-card count, knights played, and Longest Road / Largest Army badges.
6. my_hand: the card row at the bottom of the screen belongs to the person who
   took the screenshot -- count each resource card type. Set my_color if the UI
   makes it clear which color that player is.

If you cannot read something confidently, leave it out or null and add a note
to `uncertain` -- do not guess."""


class CredentialsMissing(Exception):
    """No Anthropic credentials resolvable (no API key, auth token, or profile)."""


def _parse_call(client: anthropic.Anthropic, **kwargs):
    try:
        return client.messages.parse(**kwargs)
    except TypeError as exc:
        # the SDK raises TypeError at request time when no auth method resolves
        if "authentication" in str(exc).lower():
            raise CredentialsMissing(str(exc)) from exc
        raise


def parse_screenshot(image: bytes, media_type: str) -> tuple[BoardConfig, dict, list[str]]:
    """One vision call, then resolve row/pos references to canonical IDs."""
    client = anthropic.Anthropic()
    response = _parse_call(
        client,
        model=MODEL,
        max_tokens=16000,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": base64.standard_b64encode(image).decode(),
                        },
                    },
                    {"type": "text", "text": PROMPT},
                ],
            }
        ],
        output_format=RawBoard,
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("the model declined to process this image")
    raw = response.parsed_output
    if raw is None:
        raise RuntimeError("could not parse a board out of the model response")
    cfg, warnings = resolve_raw(raw)
    return cfg, raw.model_dump(), warnings


# --- Codex CLI backend ------------------------------------------------------

_IMG_SUFFIX = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}

CODEX_TIMEOUT = 300

# Codex has no schema-enforced output (OpenAI strict mode rejects pydantic's
# generated schema), so the exact shape is spelled out in the prompt and
# RawBoard.model_validate is the gate.
CODEX_SHAPE = """
Respond with ONLY a JSON object of exactly this shape (no prose, no fences):
{
  "hexes": [{"row": 0, "pos": 0, "resource": "wood|brick|sheep|wheat|ore|desert", "number": 2-12 or null}, ...all 19 hexes...],
  "robber": {"row": R, "pos": P, "resource": "<that hex's resource>", "number": null},
  "ports": [{"row": R, "pos": P, "side": "NE|E|SE|SW|W|NW", "type": "3:1|wood|brick|sheep|wheat|ore"}],
  "red":    {"settlements": [{"row": R, "pos": P, "corner": "N|NE|SE|S|SW|NW"}], "cities": [...same shape...], "roads": [{"row": R, "pos": P, "edge": "NE|E|SE|SW|W|NW"}]},
  "blue":   {...same shape as red...},
  "orange": {...},
  "green":  {...},
  "players": [{"color": "red", "vp": 0, "resource_count": 0, "dev_card_count": 0, "knights_played": 0, "longest_road": false, "largest_army": false}, ...one per visible player...],
  "my_hand": {"wood": 0, "brick": 0, "sheep": 0, "wheat": 0, "ore": 0},
  "my_color": "red|blue|orange|green" or null,
  "uncertain": ["notes about anything unreadable"]
}
Omit list entries you cannot read rather than guessing."""


def codex_available() -> bool:
    return shutil.which("codex") is not None and (Path.home() / ".codex" / "auth.json").exists()


def _extract_json(text: str) -> dict:
    """Lenient JSON extraction: tolerate prose or code fences around the object."""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise RuntimeError(f"codex returned no JSON object: {text[:200]!r}")
    return json.loads(text[start : end + 1])


def parse_screenshot_codex(image: bytes, media_type: str) -> tuple[BoardConfig, dict, list[str]]:
    """Same contract as parse_screenshot, via `codex exec` (no API key needed)."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        img_path = tmp / f"board{_IMG_SUFFIX.get(media_type, '.png')}"
        img_path.write_bytes(image)
        out_path = tmp / "answer.json"
        cmd = [
            "codex", "exec",
            "--ephemeral",
            "--skip-git-repo-check",
            "--sandbox", "read-only",
            "--color", "never",
            "-C", td,
            "-i", str(img_path),
            "-o", str(out_path),
            PROMPT + "\n" + CODEX_SHAPE,
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=CODEX_TIMEOUT)
        except FileNotFoundError as exc:
            raise RuntimeError("codex CLI not found on PATH") from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"codex timed out after {CODEX_TIMEOUT}s") from exc
        if proc.returncode != 0 or not out_path.exists():
            tail = (proc.stderr or proc.stdout or "").strip()[-400:]
            raise RuntimeError(f"codex exec failed (exit {proc.returncode}): {tail}")
        raw = RawBoard.model_validate(_extract_json(out_path.read_text()))
    cfg, warnings = resolve_raw(raw)
    return cfg, raw.model_dump(), warnings


def _hex_id(row: int, pos: int) -> Optional[int]:
    if 0 <= row < 5 and 0 <= pos < board.ROW_SIZES[row]:
        return board.ROW_OFFSETS[row] + pos
    return None


def resolve_raw(raw: RawBoard) -> tuple[BoardConfig, list[str]]:
    warnings = list(raw.uncertain)

    tiles: list[Optional[HexTile]] = [None] * 19
    for h in raw.hexes:
        hid = _hex_id(h.row, h.pos)
        if hid is None:
            warnings.append(f"hex at row {h.row} pos {h.pos} is out of range -- dropped")
            continue
        number = h.number if h.resource != "desert" else None
        if number is not None and (number < 2 or number > 12 or number == 7):
            warnings.append(f"hex row {h.row} pos {h.pos}: invalid token {number} -- cleared")
            number = None
        tiles[hid] = HexTile(resource=h.resource, number=number)
    for hid, t in enumerate(tiles):
        if t is None:
            warnings.append(f"hex {hid} missing from the parse -- filled with desert, fix by hand")
            tiles[hid] = HexTile(resource="desert", number=None)

    robber_hex = 0
    if raw.robber is not None:
        hid = _hex_id(raw.robber.row, raw.robber.pos)
        if hid is not None:
            robber_hex = hid
        else:
            warnings.append("robber location unreadable -- defaulted to hex 0")
    else:
        deserts = [i for i, t in enumerate(tiles) if t.resource == "desert"]
        robber_hex = deserts[0] if deserts else 0

    ports: list[Port] = []
    for p in raw.ports:
        hid = _hex_id(p.row, p.pos)
        if hid is None:
            warnings.append(f"port at row {p.row} pos {p.pos} unresolvable -- dropped")
            continue
        eid = board.EDGE_OF_HEX[(hid, p.side)]
        if eid not in board.COASTAL_EDGES:
            # snap to the nearest coastal edge of the same hex
            mx, my = board.EDGE_PIXEL[eid]
            coastal = [e for e in board.HEX_EDGES[hid] if e in board.COASTAL_EDGES]
            pool = coastal or board.COASTAL_EDGES
            eid = min(
                pool,
                key=lambda e: (board.EDGE_PIXEL[e][0] - mx) ** 2 + (board.EDGE_PIXEL[e][1] - my) ** 2,
            )
            warnings.append(f"port on a non-coastal side of hex {hid} -- snapped to the coast")
        a, b = board.EDGE_VERTICES[eid]
        ports.append(Port(type=p.type, vertices=[a, b]))

    players: dict[str, PlayerState] = {}
    occupied_v: set[int] = set()
    occupied_e: set[int] = set()
    for color in ("red", "blue", "orange", "green"):
        pieces: RawPlayerPieces = getattr(raw, color)
        state = PlayerState()
        for kind, out in (("settlements", state.settlements), ("cities", state.cities)):
            for piece in getattr(pieces, kind):
                hid = _hex_id(piece.row, piece.pos)
                if hid is None:
                    warnings.append(
                        f"couldn't place {color} {kind[:-1]} at row {piece.row} pos {piece.pos} -- add it by hand"
                    )
                    continue
                vid = board.CORNER_VERTEX[(hid, piece.corner)]
                if vid in occupied_v:
                    warnings.append(
                        f"{color} {kind[:-1]} at vertex {vid} clashes with an earlier piece -- dropped"
                    )
                    continue
                occupied_v.add(vid)
                out.append(vid)
        for road in pieces.roads:
            hid = _hex_id(road.row, road.pos)
            if hid is None:
                warnings.append(
                    f"couldn't place {color} road at row {road.row} pos {road.pos} -- add it by hand"
                )
                continue
            eid = board.EDGE_OF_HEX[(hid, road.edge)]
            if eid in occupied_e:
                warnings.append(f"{color} road at edge {eid} clashes with an earlier road -- dropped")
                continue
            occupied_e.add(eid)
            state.roads.append(eid)
        players[color] = state

    for panel in raw.players:
        if panel.color in players:
            p = players[panel.color]
            p.vp_visible = panel.vp
            p.resource_count = panel.resource_count
            p.dev_card_count = panel.dev_card_count
            p.knights_played = panel.knights_played
            p.longest_road = panel.longest_road
            p.largest_army = panel.largest_army

    my_color = raw.my_color or "red"
    if raw.my_color is None:
        warnings.append("couldn't tell which color you are -- defaulted to red, fix in the panel")

    cfg = BoardConfig(
        hexes=tiles,  # type: ignore[arg-type]
        ports=ports,
        robber_hex=robber_hex,
        players=players,  # type: ignore[arg-type]
        me=MyState(
            color=my_color,  # type: ignore[arg-type]
            hand={
                "wood": raw.my_hand.wood,
                "brick": raw.my_hand.brick,
                "sheep": raw.my_hand.sheep,
                "wheat": raw.my_hand.wheat,
                "ore": raw.my_hand.ore,
            },
            dev_cards=DevCards(),
        ),
        phase="main",
        turn=my_color,  # type: ignore[arg-type]
    )
    return cfg, warnings
