"""Opponent card tracking from public events.

What is knowable, and what isn't:

* **Knowable exactly** — dice distributions, bank/port trades, player-to-player
  trades, discards on a 7, and build costs are all broadcast to everyone.
* **Not knowable** — which card a robber steal moved, and which development
  cards a player holds. colonist masks both.

So the honest model is: replay every public event to get a *known* composition,
then reconcile against the authoritative hand size colonist reports. The
difference is `unknown` — cards we know exist but not what they are. That's
strictly more information than a bare count and never claims false precision.
"""
from __future__ import annotations

from collections import Counter
from typing import Any, Iterable

from ..models import RESOURCES
from ..solver import COSTS

BUILD_COST = {
    "road": COSTS["road"],
    "settlement": COSTS["settlement"],
    "city": COSTS["city"],
    "dev": COSTS["dev"],
}


class CardTracker:
    """Folds the public event log into per-player known card composition."""

    def __init__(self, colors: Iterable[str]) -> None:
        self.known: dict[str, Counter] = {c: Counter() for c in colors}
        # steals move a card we can't identify; we count how many are in flight
        self.stolen_from: Counter = Counter()
        self.stolen_by: Counter = Counter()

    def _add(self, color: str | None, cards: Iterable[str], sign: int = 1) -> None:
        if not color or color not in self.known:
            return
        for c in cards:
            if c in RESOURCES:
                self.known[color][c] += sign
                if self.known[color][c] <= 0:
                    del self.known[color][c]

    def apply(self, ev: dict[str, Any]) -> None:
        kind, color = ev.get("kind"), ev.get("color")
        if kind == "cards_received":
            self._add(color, ev.get("cards") or [])
        elif kind == "cards_discarded":
            self._add(color, ev.get("cards") or [], -1)
        elif kind == "trade_bank":
            self._add(color, ev.get("gave") or [], -1)
            self._add(color, ev.get("got") or [])
        elif kind == "trade_player":
            other = ev.get("with")
            gave, got = ev.get("gave") or [], ev.get("got") or []
            self._add(color, gave, -1)
            self._add(color, got)
            self._add(other, got, -1)
            self._add(other, gave)
        elif kind == "card_stolen":
            # `color` is the thief; the card itself is hidden from the table
            victim = ev.get("from_color")
            self.stolen_by[color] += 1
            if victim:
                self.stolen_from[victim] += 1
        elif kind in ("piece_placed", "piece_bought"):
            piece = ev.get("piece")
            cost = {
                "road": BUILD_COST["road"],
                "settlement": BUILD_COST["settlement"],
                "city": BUILD_COST["city"],
            }.get(piece)
            # setup placements are free; colonist emits them in the setup phase,
            # which the engine filters before calling us
            if cost and not ev.get("free"):
                self._add(color, [r for r, n in cost.items() for _ in range(n)], -1)

    def intel(self, color: str, actual_count: int) -> dict[str, Any]:
        """Reconcile known composition against colonist's authoritative count."""
        known = Counter(self.known.get(color, Counter()))
        total_known = sum(known.values())
        if total_known > actual_count:
            # a steal (or an event we mis-costed) took cards we still count;
            # trim the most plentiful first rather than inventing precision
            over = total_known - actual_count
            for res, _ in known.most_common():
                if over <= 0:
                    break
                take = min(over, known[res])
                known[res] -= take
                over -= take
            known = +known  # drop zero/negative entries
        return {
            "count": actual_count,
            "known": {r: known[r] for r in RESOURCES if known[r]},
            "unknown": max(0, actual_count - sum(known.values())),
        }


def build_tracker(events: list[dict], colors: Iterable[str]) -> CardTracker:
    t = CardTracker(colors)
    for ev in events:
        t.apply(ev)
    return t
