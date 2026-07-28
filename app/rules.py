"""Base-game Catan rules, in one place.

Every constant here is from the standard 3-4 player base game. Anything the
solver needs to *know* (as opposed to judge) lives here so the rules are
auditable in isolation, and so a variant only has to change this file.

Where colonist reports a value authoritatively at runtime (piece supplies,
longest-road length, bank stock, discard limit) prefer that over recomputing:
these are the fallbacks for hand-entered boards.
"""
from __future__ import annotations

RESOURCES = ["wood", "brick", "sheep", "wheat", "ore"]

# --- board ------------------------------------------------------------------

# The 5-6 player extension is a different board, not a bigger one: 30 hexes,
# more of every resource, a third copy of most number tokens, two more ports,
# nine more development cards and a deeper bank. Everything that changes with
# it lives in a Variant so the difference is auditable in one place; everything
# below the variants is the same game either way.


class Variant:
    """Constants that depend on which box the board came out of."""

    def __init__(self, hexes, tiles, tokens, ports, dev_deck, bank):
        self.HEX_COUNT = hexes
        self.TILE_DISTRIBUTION = tiles
        self.TOKEN_DISTRIBUTION = tokens
        self.PORT_COUNT = ports
        self.DEV_DECK = dev_deck
        self.DEV_DECK_SIZE = sum(dev_deck.values())
        self.BANK_PER_RESOURCE = bank

    def __repr__(self):
        return f"<Variant {self.HEX_COUNT} hexes, {self.DEV_DECK_SIZE}-card deck>"


BASE = Variant(
    hexes=19,
    tiles={"wood": 4, "sheep": 4, "wheat": 4, "brick": 3, "ore": 3, "desert": 1},
    tokens={2: 1, 3: 2, 4: 2, 5: 2, 6: 2, 8: 2, 9: 2, 10: 2, 11: 2, 12: 1},
    ports=9,  # four 3:1 plus one 2:1 per resource
    dev_deck={"knight": 14, "victory_point": 5, "road_building": 2,
              "year_of_plenty": 2, "monopoly": 2},
    bank=19,
)

EXTENDED = Variant(
    hexes=30,
    tiles={"wood": 6, "sheep": 6, "wheat": 6, "brick": 5, "ore": 5, "desert": 2},
    tokens={2: 2, 3: 3, 4: 3, 5: 3, 6: 3, 8: 3, 9: 3, 10: 3, 11: 3, 12: 2},
    ports=11,
    dev_deck={"knight": 20, "victory_point": 5, "road_building": 3,
              "year_of_plenty": 3, "monopoly": 3},
    bank=24,
)

_BY_HEXES = {v.HEX_COUNT: v for v in (BASE, EXTENDED)}
ACTIVE = BASE


def use(variant: "Variant") -> "Variant":
    """Install a variant as the rules every module reads."""
    global ACTIVE
    ACTIVE = variant
    globals().update({k: v for k, v in vars(variant).items() if k.isupper()})
    return variant


def for_board(hex_count: int) -> "Variant":
    """The variant a board of this size is playing under.

    An unknown size keeps the base rules: the constants that matter most at
    runtime -- bank stock, piece supply, the discard limit -- are reported by
    colonist anyway, so a wrong guess here is a nudge, not a fault.
    """
    return _BY_HEXES.get(hex_count, BASE)

# ways each total can be rolled with 2d6, out of 36
PIPS = {2: 1, 3: 2, 4: 3, 5: 4, 6: 5, 7: 6, 8: 5, 9: 4, 10: 3, 11: 2, 12: 1}

# --- costs and supplies -----------------------------------------------------

COSTS = {
    "road": {"wood": 1, "brick": 1},
    "settlement": {"wood": 1, "brick": 1, "sheep": 1, "wheat": 1},
    "city": {"wheat": 2, "ore": 3},
    "dev": {"sheep": 1, "wheat": 1, "ore": 1},
}

# pieces each player starts with. Upgrading a settlement to a city returns the
# settlement to its owner's supply, so "available" is always supply minus
# what is currently standing on the board.
# unchanged by the extension: every player still gets the same pieces
PIECE_SUPPLY = {"settlement": 5, "city": 4, "road": 15}

# --- victory ----------------------------------------------------------------

VICTORY_POINTS_TO_WIN = 10
LONGEST_ROAD_MIN = 5   # segments; the holder keeps it on a tie
LARGEST_ARMY_MIN = 3   # knights played; the holder keeps it on a tie
LONGEST_ROAD_VP = 2
LARGEST_ARMY_VP = 2

DISCARD_LIMIT = 7      # a 7 makes anyone above this discard half, rounded down

# default bank trade rate when no port applies
BANK_RATE = 4
GENERIC_PORT_RATE = 3
RESOURCE_PORT_RATE = 2


def discard_count(hand_size: int, limit: int = DISCARD_LIMIT) -> int:
    """Cards lost to a 7: half of the hand, rounded down, only above the limit."""
    return hand_size // 2 if hand_size > limit else 0


def dev_card_odds() -> dict[str, float]:
    """Probability of each card type from a full deck."""
    return {k: v / DEV_DECK_SIZE for k, v in DEV_DECK.items()}


use(BASE)
