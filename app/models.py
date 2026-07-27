"""Board-config schema: the contract between parser, UI editor, and solver."""
from __future__ import annotations

from collections import Counter
from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator

from . import board, rules

RESOURCES = rules.RESOURCES

Resource = Literal["wood", "brick", "sheep", "wheat", "ore"]
HexResource = Literal["wood", "brick", "sheep", "wheat", "ore", "desert"]
Color = Literal["red", "blue", "orange", "green"]
PortType = Literal["3:1", "wood", "brick", "sheep", "wheat", "ore"]

# base-game distributions, used for soft warnings only
STANDARD_TOKENS = Counter(rules.TOKEN_DISTRIBUTION)
STANDARD_RESOURCES = Counter(rules.TILE_DISTRIBUTION)


class HexTile(BaseModel):
    resource: HexResource
    number: Optional[int] = None

    @model_validator(mode="after")
    def _check(self) -> "HexTile":
        if self.resource == "desert":
            if self.number is not None:
                raise ValueError("desert has no number token")
        elif self.number is not None and (
            self.number < 2 or self.number > 12 or self.number == 7
        ):
            raise ValueError(f"invalid number token {self.number}")
        return self


class Port(BaseModel):
    type: PortType
    vertices: list[int] = Field(min_length=2, max_length=2)


class PlayerState(BaseModel):
    settlements: list[int] = Field(default_factory=list)
    cities: list[int] = Field(default_factory=list)
    roads: list[int] = Field(default_factory=list)
    # authoritative values when the live feed supplies them; None => derive
    pieces_left: Optional[dict[str, int]] = None
    longest_road_len: Optional[int] = None
    vp_visible: int = 0
    resource_count: int = 0
    dev_card_count: int = 0
    knights_played: int = 0
    longest_road: bool = False
    largest_army: bool = False


class DevCards(BaseModel):
    knight: int = 0
    road_building: int = 0
    year_of_plenty: int = 0
    monopoly: int = 0
    vp: int = 0


class MyState(BaseModel):
    color: Color
    hand: dict[Resource, int] = Field(default_factory=dict)
    # colonist reports each player's real bank/port ratios; when present these
    # win over anything we'd derive from port geometry, which can't know about
    # rule variants and is only as good as our port parsing
    bank_rates: Optional[dict[Resource, int]] = None
    discard_limit: int = 7
    dev_cards: DevCards = Field(default_factory=DevCards)
    dev_card_bought_this_turn: bool = False
    dev_card_played_this_turn: bool = False


class BoardConfig(BaseModel):
    hexes: list[HexTile]
    ports: list[Port] = Field(default_factory=list)
    robber_hex: int = 0
    players: dict[Color, PlayerState] = Field(default_factory=dict)
    me: MyState
    bank: Optional[dict[Resource, int]] = None  # cards left in the bank
    phase: Literal["setup1", "setup2", "main"] = "main"
    turn: Optional[Color] = None
    pending: Optional[Literal["move_robber"]] = None

    @model_validator(mode="after")
    def _check(self) -> "BoardConfig":
        if len(self.hexes) != 19:
            raise ValueError(f"expected 19 hexes, got {len(self.hexes)}")
        if not 0 <= self.robber_hex < 19:
            raise ValueError(f"robber_hex {self.robber_hex} out of range")
        for tile in self.hexes:
            if tile.resource != "desert" and tile.number is not None:
                if tile.number < 2 or tile.number > 12 or tile.number == 7:
                    raise ValueError(f"invalid number token {tile.number}")
        if self.me.color not in self.players:
            self.players[self.me.color] = PlayerState()
        seen_v: dict[int, str] = {}
        seen_e: dict[int, str] = {}
        for color, p in self.players.items():
            limits = rules.PIECE_SUPPLY
            if (len(p.settlements) > limits["settlement"]
                    or len(p.cities) > limits["city"]
                    or len(p.roads) > limits["road"]):
                raise ValueError(f"{color} exceeds piece limits")
            for v in p.settlements + p.cities:
                if not 0 <= v < 54:
                    raise ValueError(f"vertex id {v} out of range")
                if v in seen_v:
                    raise ValueError(f"vertex {v} occupied twice ({seen_v[v]} and {color})")
                seen_v[v] = color
            for e in p.roads:
                if not 0 <= e < 72:
                    raise ValueError(f"edge id {e} out of range")
                if e in seen_e:
                    raise ValueError(f"edge {e} occupied twice ({seen_e[e]} and {color})")
                seen_e[e] = color
        for port in self.ports:
            for v in port.vertices:
                if not 0 <= v < 54:
                    raise ValueError(f"port vertex {v} out of range")
        return self


class MoveStep(BaseModel):
    type: Literal[
        "build_settlement",
        "build_city",
        "build_road",
        "buy_dev",
        "play_knight",
        "play_road_building",
        "play_year_of_plenty",
        "play_monopoly",
        "trade_bank",
        "move_robber",
        "setup_settlement",
        "setup_road",
        "end_turn",
    ]
    vertex: Optional[int] = None
    edge: Optional[int] = None
    edges: Optional[list[int]] = None
    give: Optional[dict[str, int]] = None
    get: Optional[dict[str, int]] = None
    resource: Optional[str] = None
    robber_hex: Optional[int] = None
    steal_from: Optional[str] = None


class ScoredMove(BaseModel):
    steps: list[MoveStep]
    score: float
    reasoning: str
    location_hint: str


def config_warnings(cfg: BoardConfig) -> list[str]:
    """Soft sanity checks -- never block solving (a half-corrected parse must stay editable)."""
    warnings: list[str] = []
    resources = Counter(t.resource for t in cfg.hexes)
    if resources != STANDARD_RESOURCES:
        diff = {r: resources.get(r, 0) for r in STANDARD_RESOURCES if resources.get(r, 0) != STANDARD_RESOURCES[r]}
        warnings.append(f"resource mix differs from a standard board: {diff}")
    tokens = Counter(t.number for t in cfg.hexes if t.number is not None)
    if tokens != STANDARD_TOKENS:
        warnings.append("number tokens differ from the standard distribution")
    if cfg.ports and len(cfg.ports) != 9:
        warnings.append(f"{len(cfg.ports)} ports configured (a standard board has 9)")
    occupied = {
        v for p in cfg.players.values() for v in p.settlements + p.cities
    }
    for color, p in cfg.players.items():
        for v in p.settlements + p.cities:
            if any(n in occupied for n in board.VERTEX_ADJ[v]):
                warnings.append(f"{color} building at vertex {v} violates the distance rule")
        road_anchors = {v for v in p.settlements + p.cities}
        for e in p.roads:
            road_anchors.update(board.EDGE_VERTICES[e])
        for e in p.roads:
            a, b = board.EDGE_VERTICES[e]
            others = {
                v2
                for e2 in p.roads
                if e2 != e
                for v2 in board.EDGE_VERTICES[e2]
            } | set(p.settlements) | set(p.cities)
            if a not in others and b not in others and len(p.roads) > 1:
                warnings.append(f"{color} road at edge {e} looks disconnected from its network")
    return warnings
