"""What is left in the bank, counted from public play.

colonist reports the true counts even when a game hides them -- the setting
stops its UI displaying them, not the state carrying them. Reading those
numbers would be exact and would also hand the player something the table
agreed nobody should have.

So this counts instead, the way a person with a good memory could: the bank
starts full, pays out on every roll, and takes cards back whenever somebody
builds, discards or trades with it. Everything it watches is announced in the
game log to all players.

It is an estimate. A dropped log entry leaves it permanently off by that much,
and it cannot see cards moved by effects it never learns the size of -- so it
reports how confident it is, and stays inside the bounds a real deck has.
"""
from __future__ import annotations

from typing import Any, Iterable, Optional

from .. import rules

RESOURCES = rules.RESOURCES


class BankCount:
    """A running estimate of the bank, from events every player can see."""

    def __init__(self, per_resource: Optional[int] = None, seats: int = 4) -> None:
        self.full = per_resource or rules.BANK_PER_RESOURCE
        self.left: dict[str, int] = {r: self.full for r in RESOURCES}
        self.watched = 0        # events that moved cards either way
        # Opening placements are free, and the log does not say so. Counting
        # them as purchases hands the bank cards nobody ever paid, which was
        # the largest remaining drift. Two settlements and two roads a seat.
        self.free_left = {"settlement": 2 * seats, "road": 2 * seats}

    # --- ingest -------------------------------------------------------------

    def _move(self, cards: Iterable[str], sign: int) -> None:
        for r in cards:
            if r in self.left:
                # a real deck cannot go negative, nor hold more than it started
                self.left[r] = max(0, min(self.full, self.left[r] + sign))
                self.watched += 1

    def apply(self, ev: dict[str, Any]) -> None:
        kind = ev.get("kind")
        if kind in ("cards_received", "year_of_plenty_taken"):
            self._move(ev.get("cards") or [], -1)          # bank pays out
        elif kind == "cards_discarded":
            self._move(ev.get("cards") or [], +1)          # a 7 sends them back
        elif kind == "trade_bank":
            self._move(ev.get("gave") or [], +1)
            self._move(ev.get("got") or [], -1)
        elif kind in ("piece_placed", "piece_bought"):
            # what you build with goes back to the bank; setup placements are
            # free and the engine has already filtered those out
            key = _piece_cost_key(ev.get("piece"))
            if key and self.free_left.get(key, 0) > 0:
                self.free_left[key] -= 1        # an opening placement, paid for by nobody
                return
            cost = rules.COSTS.get(key)
            if cost and not ev.get("free"):
                self._move([r for r, n in cost.items() for _ in range(n)], +1)
        elif kind == "monopoly_stole":
            # moves between players only; the bank is untouched
            pass
        elif kind == "card_stolen_blind" or kind == "card_stolen":
            pass

    # --- reading ------------------------------------------------------------

    def paid_for_dev_cards(self, bought: int) -> None:
        """Charge the bank for development cards bought.

        Buying one returns an ore, a wheat and a sheep to the bank, and it is
        the one purchase the game log does not announce -- "bought" entries
        cover roads, settlements and cities only. How many each player holds
        and has played is public, though, so the total bought is countable
        without reading anything hidden. Left out, the estimate drifted low on
        exactly those three resources.
        """
        self._move([r for r, n in rules.COSTS["dev"].items() for _ in range(n)] * bought, +1)

    def snapshot(self) -> dict[str, int]:
        return dict(self.left)

    def scarce(self, threshold: int = 3) -> dict[str, int]:
        """Resources close enough to running out that a trade may be refused."""
        return {r: n for r, n in self.left.items() if n <= threshold}

    def empty(self) -> list[str]:
        return sorted(r for r, n in self.left.items() if n <= 0)


def _piece_cost_key(piece: Optional[str]) -> Optional[str]:
    return {"road": "road", "settlement": "settlement", "city": "city"}.get(piece or "")


def count_bank(
    events: list[dict],
    per_resource: Optional[int] = None,
    seats: int = 4,
    dev_bought: int = 0,
) -> BankCount:
    bank = BankCount(per_resource, seats)
    for ev in events:
        bank.apply(ev)
    bank.paid_for_dev_cards(dev_bought)
    return bank
