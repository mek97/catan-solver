"""CDP feed: colonist websocket -> store -> engine.

Attaches to a Chrome started with --remote-debugging-port, auto-attaches to
every target (colonist runs its socket inside a worker), and streams every
websocket frame. Raw frames are persisted *before* decoding, so a decoder bug
can never cost us data — the game can always be rebuilt with `rebuild()`.

Run standalone:  uv run python -m app.live.feed
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from typing import Any, Optional

import requests
import websockets

from . import protocol as P
from .engine import GameEngine
from .store import Store

CDP_HTTP = "http://localhost:9222"
COLONIST_WS = "socket.svr.colonist.io"


class LiveFeed:
    """Owns the store + engine and keeps them in sync with the live game."""

    def __init__(self, store: Optional[Store] = None) -> None:
        self.store = store or Store()
        self.engine = GameEngine()
        self.game_id: Optional[str] = None
        self.room_id: Optional[str] = None
        self.connected = False
        self.last_frame_at: float = 0.0
        self.error: Optional[str] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    # --- status -------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "game_id": self.game_id,
            "room_id": self.room_id,
            "has_state": bool(self.engine.state),
            "applied": self.engine.applied,
            "events": len(self.engine.events),
            "my_color": self.engine.my_color if self.engine.state else None,
            "turn": self.engine.current_turn() if self.engine.state else None,
            "last_frame_age": round(time.time() - self.last_frame_at, 1)
            if self.last_frame_at
            else None,
            "gaps": self.store.gaps(self.game_id) if self.game_id else [],
            "error": self.error,
            **self.store.stats(),
        }

    # --- ingest -------------------------------------------------------------

    def ingest(self, payload_b64: str, direction: str, opcode: int) -> None:
        self.last_frame_at = time.time()
        frame_id = self.store.add_frame(payload_b64, direction, opcode, self.game_id)
        if frame_id is None:  # already stored (idempotent replay)
            return
        if opcode != 2:
            return
        obj = P.decode_frame(payload_b64)
        if not obj:
            return
        data = P.envelope(obj)
        if not data:
            return

        if data.get("type") == P.MSG_ROOM_STATE:
            self.room_id = data.get("roomId") or self.room_id
            return

        if data.get("type") == P.MSG_GAME_SNAPSHOT:
            payload = data.get("payload") or {}
            self.engine.apply_snapshot(payload)
            self.game_id = f"{self.room_id or 'game'}-{int(time.time())}"
            self.store.start_game(
                self.game_id,
                self.room_id or "",
                self.engine.my_color,
                self.engine.play_order,
                payload.get("gameState", {}),
            )
            self.store.add_snapshot(self.game_id, self.engine.applied, self.engine.state)
            return

        if data.get("type") == P.MSG_GAME_DIFF and self.game_id:
            diff = (data.get("payload") or {}).get("diff") or {}
            for ev in self.engine.apply_diff(diff):
                self.store.add_event(
                    self.game_id,
                    ev.get("log_id"),
                    frame_id,
                    ev.get("kind", "?"),
                    ev.get("color"),
                    ev,
                )
            if self.engine.applied % 25 == 0:
                self.store.add_snapshot(self.game_id, self.engine.applied, self.engine.state)

    def rebuild(self, game_id: str) -> int:
        """Re-derive state by replaying stored raw frames. Returns frames applied."""
        engine = GameEngine()
        n = 0
        for _fid, payload in self.store.replay_frames(game_id):
            obj = P.decode_frame(payload)
            data = P.envelope(obj) if obj else None
            if not data:
                continue
            if data.get("type") == P.MSG_GAME_SNAPSHOT:
                engine.apply_snapshot(data.get("payload") or {})
                n += 1
            elif data.get("type") == P.MSG_GAME_DIFF:
                engine.apply_diff((data.get("payload") or {}).get("diff") or {})
                n += 1
        self.engine = engine
        self.game_id = game_id
        return n

    # --- CDP loop -----------------------------------------------------------

    async def _run(self) -> None:
        try:
            ver = requests.get(f"{CDP_HTTP}/json/version", timeout=5).json()
        except Exception as exc:
            self.error = f"no CDP on :9222 ({exc}); start Chrome with --remote-debugging-port=9222"
            return
        browser_ws = ver["webSocketDebuggerUrl"]
        msg_id = 0
        ws_urls: dict[str, str] = {}

        async with websockets.connect(browser_ws, max_size=64 * 1024 * 1024) as ws:
            self.connected = True
            self.error = None

            async def send(method: str, params: dict | None = None, session: str | None = None):
                nonlocal msg_id
                msg_id += 1
                m: dict[str, Any] = {"id": msg_id, "method": method, "params": params or {}}
                if session:
                    m["sessionId"] = session
                await ws.send(json.dumps(m))

            await send(
                "Target.setAutoAttach",
                {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True},
            )
            await send("Target.setDiscoverTargets", {"discover": True})

            while not self._stop.is_set():
                raw = await ws.recv()
                msg = json.loads(raw)
                method, params = msg.get("method", ""), msg.get("params", {})
                if method == "Target.attachedToTarget":
                    sid = params["sessionId"]
                    await send("Network.enable", {}, session=sid)
                    await send(
                        "Target.setAutoAttach",
                        {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True},
                        session=sid,
                    )
                elif method == "Network.webSocketCreated":
                    ws_urls[params["requestId"]] = params.get("url", "")
                elif method in (
                    "Network.webSocketFrameReceived",
                    "Network.webSocketFrameSent",
                ):
                    # A socket opened *before* we attached never emitted
                    # webSocketCreated, so its url is unknown. Don't drop those
                    # frames (that would silently break every reconnect) --
                    # let them through and rely on envelope decoding to ignore
                    # anything that isn't colonist traffic.
                    url = ws_urls.get(params["requestId"])
                    if url is not None and COLONIST_WS not in url:
                        continue
                    frame = params["response"]
                    self.ingest(
                        frame.get("payloadData", ""),
                        "recv" if method.endswith("Received") else "sent",
                        frame.get("opcode", 1),
                    )
        self.connected = False

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                asyncio.run(self._run())
            except Exception as exc:  # reconnect on any transport failure
                self.error = f"{type(exc).__name__}: {exc}"
                self.connected = False
            if self._stop.wait(3):
                break

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="catan-feed")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()


FEED = LiveFeed()


if __name__ == "__main__":
    FEED.start()
    print("feed running; ctrl-c to stop")
    try:
        while True:
            time.sleep(5)
            print(json.dumps(FEED.status(), default=str))
    except KeyboardInterrupt:
        FEED.stop()
