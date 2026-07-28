"""Memory of how trades actually went, so advice stops repeating itself.

colonist reports each player's response to an open offer: 0 none, 1 accept,
2 decline. Those codes were identified by correlating them against who
actually completed the trade (92% of accepting players had responded 1).

Two things fall out of recording them:

* stop re-proposing a trade that has already been refused by the same player
* learn what each opponent will and won't part with, which is the difference
  between "ask green for ore" and "green has refused ore three times; orange
  hasn't been asked".
"""
from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any, Iterable, Optional

from . import protocol as P

RESPONSE_NONE = 0
RESPONSE_ACCEPT = 1
RESPONSE_DECLINE = 2


def _key(cards: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted(cards))


def _cards(enums: Optional[Iterable[Any]]) -> tuple[str, ...]:
    """colonist card enums -> resource names, sorted so a price is one key.

    str() on anything unmapped keeps the tuple sortable; an unknown card is
    better recorded under an odd name than allowed to raise mid-fold.
    """
    return _key(str(P.CARD.get(c, c)) for c in (enums or []))


class TradeMemory:
    """Per-game record of who refused what, and what they were willing to give."""

    def __init__(self) -> None:
        # (partner, what is offered, what is asked of them) -> times.
        # Recorded for anyone's offer -- a refusal prices the trade whoever
        # made it -- with `mine` marking the ones we asked for ourselves.
        self.refused: Counter = Counter()
        self.accepted: Counter = Counter()
        self.mine: Counter = Counter()
        # partner -> resources they have declined to hand over
        self.wont_give: defaultdict[str, Counter] = defaultdict(Counter)
        # partner -> resources they have agreed to hand over (the best signal
        # there is: they did it once, they will probably do it again)
        self.will_give: defaultdict[str, Counter] = defaultdict(Counter)
        # partner -> resources they have asked others for (they need these)
        self.wants: defaultdict[str, Counter] = defaultdict(Counter)
        # partner -> resources they have offered up (they have a surplus)
        self.spare: defaultdict[str, Counter] = defaultdict(Counter)
        self._seen: set[tuple] = set()

    # --- ingest -------------------------------------------------------------

    def observe_offer(self, offer: dict[str, Any], me: Optional[int], colour) -> None:
        """Record one open offer and every response registered against it.

        Must be given a *whole* offer -- colonist's diffs carry only the fields
        that changed, so a response usually arrives with no creator and no
        resources attached. Feed this the merged state, not the raw diff.
        """
        creator = offer.get("creator")
        if creator is None:
            return
        give = _cards(offer.get("offeredResources"))   # creator hands over
        want = _cards(offer.get("wantedResources"))    # creator asks for
        if not give or not want:
            return
        creator_colour = colour(creator)
        if not creator_colour:
            return
        mine = me is not None and creator == me

        if not mine:
            # what an opponent asks for is what they are short of
            for r in want:
                self.wants[creator_colour][r] += 1
            for r in give:
                self.spare[creator_colour][r] += 1

        for pid, response in (offer.get("playerResponses") or {}).items():
            if response == RESPONSE_NONE or not str(pid).isdigit():
                continue
            pid_int = int(pid)
            if pid_int == creator or pid_int == me:
                continue
            partner = colour(pid_int)
            if not partner:
                continue
            # dedupe: an open offer is re-sent on every diff that touches it
            fingerprint = (offer.get("id"), pid_int, response)
            if fingerprint in self._seen:
                continue
            self._seen.add(fingerprint)

            # Every response teaches the same thing regardless of who asked:
            # this player would not hand over `want` for `give`. Refusals of
            # our own offers are rare; refusals of everyone's are plentiful,
            # and they price the same trade we are about to propose.
            bucket = self.refused if response == RESPONSE_DECLINE else self.accepted
            bucket[(partner, give, want)] += 1
            if mine:
                self.mine[(partner, give, want)] += 1
            side = self.wont_give if response == RESPONSE_DECLINE else self.will_give
            for r in want:
                side[partner][r] += 1

    def observe(self, trade_state: dict[str, Any], me: Optional[int], colour) -> None:
        for offer in (trade_state.get("activeOffers") or {}).values():
            if isinstance(offer, dict):
                self.observe_offer(offer, me, colour)

    # --- queries ------------------------------------------------------------

    def was_refused(self, partner: str, give: Iterable[str], want: Iterable[str]) -> int:
        return self.refused[(partner, _key(give), _key(want))]

    def refused_us(self, partner: str, give: Iterable[str], want: Iterable[str]) -> int:
        """Refusals of an offer we made ourselves, for wording advice."""
        return self.mine[(partner, _key(give), _key(want))]

    def refuses(self, partner: str, resource: str) -> int:
        """How many times this player has declined to hand over a resource."""
        return self.wont_give[partner][resource]

    def gives(self, partner: str, resource: str) -> int:
        """How many times this player has agreed to hand a resource over."""
        return self.will_give[partner][resource]

    def summary(self) -> dict[str, Any]:
        return {
            "refused": [
                {"partner": p, "gave": list(g), "wanted": list(w), "times": n}
                for (p, g, w), n in self.refused.most_common()
            ],
            "accepted": [
                {"partner": p, "gave": list(g), "wanted": list(w), "times": n}
                for (p, g, w), n in self.accepted.most_common()
            ],
            "refused_us": [
                {"partner": p, "gave": list(g), "wanted": list(w), "times": n}
                for (p, g, w), n in self.mine.most_common()
            ],
            "wont_give": {p: dict(c) for p, c in self.wont_give.items() if c},
            "will_give": {p: dict(c) for p, c in self.will_give.items() if c},
            "wants": {p: dict(c) for p, c in self.wants.items() if c},
            "spare": {p: dict(c) for p, c in self.spare.items() if c},
        }
