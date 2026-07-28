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

# Shortest gap between page reloads when recovering a mid-game attach. Low
# enough to get back in play quickly, high enough that a reload always has
# time to finish and deliver its snapshot before we consider another.
RESYNC_COOLDOWN = 10.0


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
        self._page_session: Optional[str] = None
        self._pending_nav: list[str] = []
        self._pending_reload = False
        self._cmd_lock = threading.Lock()
        # diffs arriving with no snapshot to apply them to => we attached
        # mid-game and need colonist to re-send one
        self._orphan_diffs = 0
        self._last_resync = 0.0
        self.resyncing = False

    # --- driving the attached browser ---------------------------------------

    def open_url(self, url: str) -> dict[str, Any]:
        """Point the attached Chrome at a URL (e.g. a colonist game link).

        Falls back to the CDP HTTP API (which opens a new tab) when no page
        session is attached yet, so this works even before the first frame.
        """
        if not url.startswith(("http://", "https://")):
            url = "https://" + url.lstrip("/")
        if self._page_session:
            with self._cmd_lock:
                self._pending_nav.append(url)
            return {"navigated": url, "via": "session"}
        try:
            requests.put(f"{CDP_HTTP}/json/new?{url}", timeout=5)
            return {"navigated": url, "via": "new-tab"}
        except Exception as exc:
            return {"error": f"could not open {url}: {exc}"}

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
            "resyncing": self.resyncing,
            # namespaced so the store's totals don't overwrite this game's
            "stored": self.store.stats(),
            "frames": self.store.stats()["frames"],
        }

    # --- ingest -------------------------------------------------------------

    def ingest(self, payload_b64: str, direction: str, opcode: int) -> None:
        self.last_frame_at = time.time()
        obj = P.decode_frame(payload_b64) if opcode == 2 else None
        data = P.envelope(obj) if obj else None

        # A snapshot must settle the game id *before* its frame is stored.
        # Storing first filed every snapshot under the previous game, so each
        # recorded game was diffs-only and no export could be replayed.
        is_snapshot = False
        if data and data.get("type") == P.MSG_GAME_SNAPSHOT:
            payload = data.get("payload") or {}
            was = self.engine.game_key
            # False here means a lobby message reusing type 4, not a snapshot
            if self.engine.apply_snapshot(payload):
                is_snapshot = True
                if self.game_id is None or self.engine.game_key != was:
                    self.game_id = f"{self.room_id or 'game'}-{int(time.time())}"
                    self.store.start_game(
                        self.game_id,
                        self.room_id or "",
                        self.engine.my_color,
                        self.engine.play_order,
                        payload.get("gameState", {}),
                    )
                # a resync re-sends the same game: keep one recording of it

        frame_id = self.store.add_frame(payload_b64, direction, opcode, self.game_id)
        if frame_id is None:  # already stored (idempotent replay)
            return
        if not data:
            return

        if is_snapshot:
            self.store.add_snapshot(self.game_id, self.engine.applied, self.engine.state)
            self._orphan_diffs = 0
            self.resyncing = False
            return

        if data.get("type") == P.MSG_ROOM_STATE:
            self.room_id = data.get("roomId") or self.room_id
            return

        if data.get("type") == P.MSG_GAME_DIFF and not self.engine.state:
            # a game is running but we have no snapshot for it
            self._orphan_diffs += 1
            if self._orphan_diffs >= 3:
                self._orphan_diffs = 0
                self.request_resync()
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

    def request_resync(self) -> bool:
        """Reload the colonist tab so it re-sends a full game snapshot.

        colonist only sends the full state (type 4) when the socket opens, so
        attaching to a game already in progress leaves us with diffs we cannot
        apply. Reloading reconnects the same seat and replays the snapshot; it
        does not affect the game itself.
        """
        now = time.time()
        if now - self._last_resync < RESYNC_COOLDOWN:  # don't thrash the page
            return False
        self._last_resync = now
        self.resyncing = True
        with self._cmd_lock:
            self._pending_reload = True
        return True

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
                # drain any queued navigation before blocking on the next event
                with self._cmd_lock:
                    navs, self._pending_nav = self._pending_nav, []
                    reload_now, self._pending_reload = self._pending_reload, False
                for url in navs:
                    await send("Page.navigate", {"url": url}, session=self._page_session)
                if reload_now and self._page_session:
                    await send("Page.reload", {}, session=self._page_session)

                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                msg = json.loads(raw)
                method, params = msg.get("method", ""), msg.get("params", {})
                if method == "Target.attachedToTarget":
                    sid = params["sessionId"]
                    info = params.get("targetInfo") or {}
                    await send("Network.enable", {}, session=sid)
                    await send(
                        "Target.setAutoAttach",
                        {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True},
                        session=sid,
                    )
                    if info.get("type") == "page":
                        await send("Page.enable", {}, session=sid)
                        # prefer a colonist tab; otherwise hold the first page
                        if COLONIST_WS.split(".")[-2] in (info.get("url") or "") or not self._page_session:
                            self._page_session = sid
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
