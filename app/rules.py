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

HEX_COUNT = 19
VERTEX_COUNT = 54
EDGE_COUNT = 72

TILE_DISTRIBUTION = {"wood": 4, "sheep": 4, "wheat": 4, "brick": 3, "ore": 3, "desert": 1}
TOKEN_DISTRIBUTION = {2: 1, 3: 2, 4: 2, 5: 2, 6: 2, 8: 2, 9: 2, 10: 2, 11: 2, 12: 1}
PORT_COUNT = 9  # four 3:1 plus one 2:1 per resource

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
PIECE_SUPPLY = {"settlement": 5, "city": 4, "road": 15}

BANK_PER_RESOURCE = 19

# 25-card development deck
DEV_DECK = {"knight": 14, "victory_point": 5, "road_building": 2, "year_of_plenty": 2, "monopoly": 2}
DEV_DECK_SIZE = sum(DEV_DECK.values())

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
