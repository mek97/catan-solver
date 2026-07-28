"""Where our advice and catanatron's differ, across every recorded game.

Replays the stored frames, stops at each position where it was our turn, and
asks both. The interesting number is not the disagreement rate -- two decent
players disagree constantly -- it is whether the disagreements have a shape:
one of us systematically refusing a whole class of move.

  uv run python .claude/scripts/compare_engines.py
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app import bridge, solver  # noqa: E402
from app.live import protocol as P  # noqa: E402
from app.live.engine import GameEngine  # noqa: E402
from app.live.store import Store  # noqa: E402


def kind_of(text: str) -> str:
    t = (text or "").lower()
    for key in ("settlement", "city", "road", "dev", "knight", "monopoly",
                "year of plenty", "robber", "trade", "roll", "end"):
        if key in t:
            return key
    return "other"


def main() -> None:
    store = Store()
    games = [g for g, _ in store.games()] if hasattr(store, "games") else []
    if not games:
        import sqlite3
        con = sqlite3.connect(store.path if hasattr(store, "path") else "data/catan.db")
        games = [r[0] for r in con.execute(
            "select game_id, count(*) c from frames group by game_id "
            "having c > 300 order by c desc limit 6")]

    ours_kind: Counter[str] = Counter()
    theirs_kind: Counter[str] = Counter()
    agree = 0
    seen = 0
    ours_holds_they_build = 0
    positions: set = set()
    examples: list[str] = []

    for gid in games:
        eng = GameEngine()
        step = 0
        for _fid, payload in store.replay_frames(gid):
            obj = P.decode_frame(payload)
            data = P.envelope(obj) if obj else None
            if not data:
                continue
            if data.get("type") == P.MSG_GAME_SNAPSHOT:
                eng.apply_snapshot(data.get("payload") or {})
            elif data.get("type") == P.MSG_GAME_DIFF:
                eng.apply_diff((data.get("payload") or {}).get("diff") or {})
            else:
                continue

            step += 1
            if not eng.state or not eng.is_my_turn():
                continue
            try:
                cfg = eng.board_config()
            except Exception:
                continue
            if bridge.supported(cfg):        # 5-6 players etc
                continue
            # one row per distinct position, not per frame
            key = (gid, cfg.pending, tuple(sorted(cfg.me.hand.items())),
                   tuple(sorted(cfg.players[cfg.me.color].settlements)),
                   tuple(sorted(cfg.players[cfg.me.color].roads)))
            if key in positions:
                continue
            positions.add(key)

            try:
                theirs = bridge.second_opinion(cfg)
            except Exception:
                continue
            # only positions with a real choice to make: if the engine has one
            # legal move, agreeing with it proves nothing
            if not theirs or (theirs.get("legal_moves") or 0) < 3:
                continue
            try:
                moves = solver.solve(cfg)
            except Exception:
                continue

            best = max(moves, key=lambda m: m.score, default=None)
            positive = best if best and best.score > 0 else None
            our_text = positive.location_hint if positive else "hold / end turn"
            seen += 1
            ok = kind_of(our_text)
            tk = kind_of(theirs.get("text", ""))
            ours_kind[ok] += 1
            theirs_kind[tk] += 1
            if ok == tk:
                agree += 1
            elif positive is None and tk in ("road", "settlement", "city", "dev"):
                ours_holds_they_build += 1
                if len(examples) < 5:
                    examples.append(
                        f"  {gid[:18]:20s} we: hold (best {best.score:+.1f} "
                        f"{kind_of(best.location_hint) if best else '-'})  "
                        f"engine: {theirs.get('text','')[:52]}"
                    )

    print(f"positions compared: {seen}")
    if not seen:
        return
    print(f"same kind of move:  {agree} ({agree / seen:.0%})")
    print(f"we hold, engine builds: {ours_holds_they_build} "
          f"({ours_holds_they_build / seen:.0%})")
    print("\nwhat each of us picks:")
    kinds = sorted(set(ours_kind) | set(theirs_kind))
    print(f"  {'move':16s} {'ours':>6s} {'engine':>7s}")
    for k in kinds:
        print(f"  {k:16s} {ours_kind[k]:6d} {theirs_kind[k]:7d}")
    if examples:
        print("\nexamples where we sit still and it does not:")
        print("\n".join(examples))


if __name__ == "__main__":
    main()
