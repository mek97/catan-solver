"""Export a game as a self-contained, replayable bundle.

The point is diagnosis: hand someone this file and they can reproduce the exact
position and advice you were looking at, without your machine, your browser, or
the live game.

The raw frames are what make that possible -- everything else in the bundle is
derived from them and is included only so a reader can see what we *thought*
was true without running anything.

    uv run python -m app.live.export --list
    uv run python -m app.live.export --game <id> -o bundle.json
    uv run python -m app.live.export --replay bundle.json
"""
from __future__ import annotations

import argparse
import gzip
import json
import sys
from pathlib import Path
from typing import Any, Optional

from . import protocol as P
from .engine import GameEngine
from .store import Store

FORMAT_VERSION = 1


def build_export(store: Store, game_id: str, note: str = "") -> dict[str, Any]:
    """Everything needed to reproduce a game, plus what we made of it."""
    frames = list(store.replay_frames(game_id))
    engine = replay_frames(frames)

    bundle: dict[str, Any] = {
        "format": FORMAT_VERSION,
        "game_id": game_id,
        "note": note,
        "frames": [{"id": fid, "payload": payload} for fid, payload in frames],
        "events": store.events(game_id, limit=100000),
        "gaps": store.gaps(game_id),
        "derived": None,
        "advice": None,
        "error": None,
    }

    if engine.state:
        try:
            cfg = engine.board_config()
            bundle["derived"] = {
                "my_color": engine.my_color,
                "turn": engine.current_turn(),
                "phase": engine.phase(),
                "applied": engine.applied,
                "config": cfg.model_dump(),
                "players": engine.player_summary(),
            }
            from .advisor import recommend

            bundle["advice"] = recommend(engine)
        except Exception as exc:  # a broken derivation is exactly what we want to see
            bundle["error"] = f"{type(exc).__name__}: {exc}"
    return bundle


def replay_frames(frames) -> GameEngine:
    """Fold raw frames into an engine -- the same path the live feed uses."""
    engine = GameEngine()
    for _fid, payload in frames:
        obj = P.decode_frame(payload)
        data = P.envelope(obj) if obj else None
        if not data:
            continue
        if data.get("type") == P.MSG_GAME_SNAPSHOT:
            engine.apply_snapshot(data.get("payload"))
        elif data.get("type") == P.MSG_GAME_DIFF:
            engine.apply_diff((data.get("payload") or {}).get("diff") or {})
    return engine


def load_export(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    if raw[:2] == b"\x1f\x8b":  # gzip magic
        raw = gzip.decompress(raw)
    return json.loads(raw.decode())


def replay_export(bundle: dict[str, Any]) -> GameEngine:
    """Rebuild the engine from a bundle's raw frames."""
    return replay_frames([(f.get("id"), f["payload"]) for f in bundle.get("frames", [])])


def _summarise(bundle: dict[str, Any]) -> str:
    d, a = bundle.get("derived"), bundle.get("advice")
    lines = [
        f"game    {bundle['game_id']}",
        f"frames  {len(bundle.get('frames', []))}",
        f"events  {len(bundle.get('events', []))}",
        f"gaps    {bundle.get('gaps') or 'none'}",
    ]
    if bundle.get("note"):
        lines.append(f"note    {bundle['note']}")
    if bundle.get("error"):
        lines.append(f"ERROR   {bundle['error']}")
    if d:
        lines.append(f"state   {d['phase']}, {d['turn']}'s turn, you are {d['my_color']}")
    if a and a.get("moves"):
        lines.append(f"top     {a['moves'][0]['location_hint']}")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--list", action="store_true", help="list recorded games")
    ap.add_argument("--game", help="game id to export (default: most recent)")
    ap.add_argument("-o", "--out", help="write here (.gz compresses)")
    ap.add_argument("--note", default="", help="what looked wrong")
    ap.add_argument("--replay", help="load a bundle and re-derive it")
    args = ap.parse_args(argv)

    if args.replay:
        bundle = load_export(Path(args.replay))
        print(_summarise(bundle))
        engine = replay_export(bundle)
        print("\nre-derived from raw frames:")
        if not engine.state:
            print("  no game state (no snapshot in the bundle)")
            return 1
        cfg = engine.board_config()
        print(f"  {engine.phase()}, {engine.current_turn()}'s turn, you are {engine.my_color}")
        print(f"  hand {({k: v for k, v in cfg.me.hand.items() if v})}")
        from .advisor import recommend

        for m in recommend(engine)["moves"][:3]:
            print(f"  {m['score']:5.1f}  {m['location_hint'][:78]}")
        return 0

    store = Store()
    if args.list:
        with store._lock:  # noqa: SLF001 - CLI convenience
            rows = store._conn.execute(
                "SELECT g.game_id, g.room_id, g.started_at,"
                " (SELECT COUNT(*) FROM frames f WHERE f.game_id = g.game_id) n"
                " FROM games g ORDER BY g.started_at DESC LIMIT 25"
            ).fetchall()
        for r in rows:
            print(f"{r['game_id']:28s} room={r['room_id'] or '-':10s} frames={r['n']}")
        return 0

    game_id = args.game
    if not game_id:
        row = store.latest_game()
        if not row:
            print("no recorded games", file=sys.stderr)
            return 1
        game_id = row["game_id"]

    bundle = build_export(store, game_id, note=args.note)
    out = Path(args.out or f"catan-{game_id}.json.gz")
    data = json.dumps(bundle).encode()
    out.write_bytes(gzip.compress(data) if out.suffix == ".gz" else data)
    print(_summarise(bundle))
    print(f"\nwrote {out}  ({out.stat().st_size / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
