"""Regenerate app/fixtures/default_board.json (the rulebook beginner layout).

Hexes and tokens are the fixed beginner setup; the 9 ports are placed on
evenly spaced coastal edges going clockwise (approximate -- edit in the UI's
JSON tab if you want exact beginner port positions).

Run: uv run python scripts/gen_default_board.py
"""
from __future__ import annotations

import json
import math
from pathlib import Path

from app import board

# rulebook beginner layout, row-major (3-4-5-4-3)
BEGINNER = [
    ("ore", 10), ("sheep", 2), ("wood", 9),
    ("wheat", 12), ("brick", 6), ("sheep", 4), ("brick", 10),
    ("wheat", 9), ("wood", 11), ("desert", None), ("wood", 3), ("ore", 8),
    ("wood", 8), ("ore", 3), ("wheat", 4), ("sheep", 5),
    ("brick", 5), ("wheat", 6), ("sheep", 11),
]

PORT_TYPES = ["3:1", "sheep", "3:1", "ore", "wheat", "3:1", "wood", "brick", "3:1"]


def main() -> None:
    coastal = sorted(
        board.COASTAL_EDGES,
        key=lambda e: math.atan2(board.EDGE_PIXEL[e][1], board.EDGE_PIXEL[e][0]),
    )
    ports = []
    for i, ptype in enumerate(PORT_TYPES):
        eid = coastal[round(i * len(coastal) / len(PORT_TYPES)) % len(coastal)]
        a, b = board.EDGE_VERTICES[eid]
        ports.append({"type": ptype, "vertices": [a, b]})

    desert = next(i for i, (r, _) in enumerate(BEGINNER) if r == "desert")
    cfg = {
        "hexes": [{"resource": r, "number": n} for r, n in BEGINNER],
        "ports": ports,
        "robber_hex": desert,
        "players": {
            c: {
                "settlements": [], "cities": [], "roads": [],
                "vp_visible": 0, "resource_count": 0, "dev_card_count": 0,
                "knights_played": 0, "longest_road": False, "largest_army": False,
            }
            for c in ("red", "blue", "orange", "green")
        },
        "me": {
            "color": "red",
            "hand": {"wood": 0, "brick": 0, "sheep": 0, "wheat": 0, "ore": 0},
            "dev_cards": {"knight": 0, "road_building": 0, "year_of_plenty": 0, "monopoly": 0, "vp": 0},
            "dev_card_bought_this_turn": False,
            "dev_card_played_this_turn": False,
        },
        "phase": "setup1",
        "turn": "red",
        "pending": None,
    }
    out = Path(__file__).resolve().parent.parent / "app" / "fixtures" / "default_board.json"
    out.write_text(json.dumps(cfg, indent=2) + "\n")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
