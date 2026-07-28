"""Durable, crash-safe store for the colonist websocket feed.

Design goals:

* **Never lose a frame.** Every raw frame is appended verbatim before any
  decoding happens, so the entire game can always be rebuilt by replay even if
  the decoder or state engine has a bug.
* **Idempotent.** Frames are keyed by content hash and events by
  (game, log_id), both UNIQUE with INSERT OR IGNORE — replaying the same feed,
  or reconnecting mid-game, can never double-apply a move.
* **Gap-aware.** colonist's gameLogState ids are monotonic per game, so a
  missing id means we dropped something; `gaps()` surfaces that rather than
  silently advising on a stale board.
* **Durable.** WAL journal + synchronous=FULL: an OS crash loses nothing
  acknowledged, and readers (the API) never block the writer (the feed).
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Optional

DEFAULT_DB = Path(__file__).resolve().parent.parent.parent / "data" / "catan.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS games (
    game_id     TEXT PRIMARY KEY,
    room_id     TEXT,
    my_color    TEXT,
    play_order  TEXT,
    started_at  REAL NOT NULL,
    ended_at    REAL,
    snapshot    TEXT           -- first full gameState (type 4), JSON
);

CREATE TABLE IF NOT EXISTS frames (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    game_id     TEXT,
    direction   TEXT NOT NULL,
    opcode      INTEGER,
    sha256      TEXT NOT NULL UNIQUE,
    payload     TEXT NOT NULL   -- base64 of the raw frame, verbatim
);
CREATE INDEX IF NOT EXISTS frames_game ON frames(game_id, id);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id     TEXT NOT NULL,
    log_id      INTEGER,        -- colonist gameLogState key (monotonic per game)
    frame_id    INTEGER,
    ts          REAL NOT NULL,
    kind        TEXT NOT NULL,
    color       TEXT,
    body        TEXT NOT NULL,  -- JSON
    UNIQUE(game_id, log_id)
);
CREATE INDEX IF NOT EXISTS events_game ON events(game_id, log_id);

CREATE TABLE IF NOT EXISTS snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id     TEXT NOT NULL,
    ts          REAL NOT NULL,
    applied     INTEGER NOT NULL,  -- frames applied when taken
    state       TEXT NOT NULL      -- JSON of merged gameState
);
CREATE INDEX IF NOT EXISTS snapshots_game ON snapshots(game_id, id);
"""


# A steal is written into the log once per player who may read it, each
# rendering numbered, and only ours is sent. Every hole left in the sequence
# after reading the snapshot's history turned out to sit beside one of these:
#   robber_moved -> [?] -> log_15        8x
#   log_15       -> [?] -> turn_ended    7x
#   card_stolen  -> [?] -> piece_bought  4x
# log_15 carries a playerColor and a cardEnum -- the identity of the card, in
# the view belonging to whoever is entitled to see it. It is left unnamed
# because that reading is inference, not something colonist told us.
_PRIVATE_NEIGHBOURS = {
    "robber_moved", "robber_placed", "card_stolen", "card_stolen_blind", "log_15",
}


class Store:
    """Thread-safe SQLite store. One instance per process is fine."""

    def __init__(self, path: Path | str = DEFAULT_DB):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=FULL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    # --- writes -------------------------------------------------------------

    def add_frame(
        self, payload_b64: str, direction: str, opcode: int, game_id: Optional[str]
    ) -> Optional[int]:
        """Append a raw frame. Returns row id, or None if already stored."""
        sha = hashlib.sha256(f"{direction}:{opcode}:{payload_b64}".encode()).hexdigest()
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO frames (ts, game_id, direction, opcode, sha256, payload)"
                " VALUES (?,?,?,?,?,?)",
                (time.time(), game_id, direction, opcode, sha, payload_b64),
            )
            self._conn.commit()
            return cur.lastrowid if cur.rowcount else None

    def start_game(
        self, game_id: str, room_id: str, my_color: str, play_order: list, snapshot: dict
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO games (game_id, room_id, my_color, play_order,"
                " started_at, snapshot) VALUES (?,?,?,?,?,?)",
                (
                    game_id,
                    room_id,
                    my_color,
                    json.dumps(play_order),
                    time.time(),
                    json.dumps(snapshot),
                ),
            )
            self._conn.commit()

    def add_event(
        self,
        game_id: str,
        log_id: Optional[int],
        frame_id: Optional[int],
        kind: str,
        color: Optional[str],
        body: dict,
    ) -> bool:
        """Record a semantic event. False if it was already recorded."""
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO events (game_id, log_id, frame_id, ts, kind, color, body)"
                " VALUES (?,?,?,?,?,?,?)",
                (game_id, log_id, frame_id, time.time(), kind, color, json.dumps(body)),
            )
            self._conn.commit()
            return bool(cur.rowcount)

    def add_snapshot(self, game_id: str, applied: int, state: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO snapshots (game_id, ts, applied, state) VALUES (?,?,?,?)",
                (game_id, time.time(), applied, json.dumps(state)),
            )
            self._conn.commit()

    # --- reads --------------------------------------------------------------

    def latest_game(self) -> Optional[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM games ORDER BY started_at DESC LIMIT 1"
            ).fetchone()

    def events(self, game_id: str, limit: int = 500) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT log_id, ts, kind, color, body FROM events WHERE game_id=?"
                " ORDER BY COALESCE(log_id, id) DESC LIMIT ?",
                (game_id, limit),
            ).fetchall()
        out = []
        for r in reversed(rows):
            body = json.loads(r["body"])
            body.update(log_id=r["log_id"], ts=r["ts"], kind=r["kind"], color=r["color"])
            out.append(body)
        return out

    def gaps(self, game_id: str) -> list[int]:
        """Log ids we never accounted for -- a sign we actually lost events.

        Not every missing id is a loss, and on this recording none of them were.
        colonist numbers entries it never sends us -- a steal is written once
        per player entitled to read it, and only our copy arrives -- so holes
        appear beside every steal. Reported literally, the warning fired
        constantly while nothing was wrong, which is worse than not warning at
        all.

        A reconnect does not cause loss either: the snapshot that follows one
        carries the log from id 0, so anything missed during the outage is
        recovered when the socket comes back.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT log_id, kind FROM events WHERE game_id=? AND log_id IS NOT NULL"
                " ORDER BY log_id",
                (game_id,),
            ).fetchall()
        seen = [(r["log_id"], r["kind"]) for r in rows]
        if len(seen) < 2:
            return []
        ids = {i for i, _k in seen}
        missing = sorted(set(range(seen[0][0], seen[-1][0] + 1)) - ids)
        if not missing:
            return []
        # a hole bracketed by a robber move and a steal is the withheld card
        private: set[int] = set()
        for (a, ka), (b, kb) in zip(seen, seen[1:]):
            if b - a > 1 and (ka in _PRIVATE_NEIGHBOURS or kb in _PRIVATE_NEIGHBOURS):
                private.update(range(a + 1, b))
        return [i for i in missing if i not in private]

    def latest_snapshot(self, game_id: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT state FROM snapshots WHERE game_id=? ORDER BY id DESC LIMIT 1",
                (game_id,),
            ).fetchone()
        return json.loads(row["state"]) if row else None

    def replay_frames(self, game_id: str) -> Iterable[tuple[int, str]]:
        """All raw frames for a game, in arrival order — the rebuild path."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, payload FROM frames WHERE game_id=? AND opcode=2 ORDER BY id",
                (game_id,),
            ).fetchall()
        return [(r["id"], r["payload"]) for r in rows]

    def stats(self) -> dict[str, Any]:
        with self._lock:
            f = self._conn.execute("SELECT COUNT(*) n FROM frames").fetchone()["n"]
            e = self._conn.execute("SELECT COUNT(*) n FROM events").fetchone()["n"]
            g = self._conn.execute("SELECT COUNT(*) n FROM games").fetchone()["n"]
        return {"frames": f, "events": e, "games": g, "db": str(self.path)}

    def close(self) -> None:
        with self._lock:
            self._conn.close()
