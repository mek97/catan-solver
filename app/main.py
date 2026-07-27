"""catan-solver web app: board editor UI + parse/solve API."""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import anthropic
from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import board, parser, solver
from .models import BoardConfig, ScoredMove, config_warnings

APP_DIR = Path(__file__).parent
FIXTURE = APP_DIR / "fixtures" / "default_board.json"

# load repo-root .env (gitignored) so ANTHROPIC_API_KEY can live there
_env_file = APP_DIR.parent / ".env"
if _env_file.exists():
    import os

    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Attach the live feed as soon as the server boots.

    The feed retries on its own if Chrome isn't up yet, so starting it here is
    safe in any order: launch the browser first or second, it connects either
    way, and the dashboard never has to ask.
    """
    from .live.feed import FEED

    FEED.start()
    try:
        yield
    finally:
        FEED.stop()


app = FastAPI(title="catan-solver", lifespan=lifespan)

LAST_CONFIG: Optional[BoardConfig] = None

ACCEPTED_IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}


def _geometry_payload() -> dict:
    hexes = []
    for hid, (cx, cy) in enumerate(board.HEX_PIXEL):
        points = " ".join(
            f"{board.VERTEX_PIXEL[v][0]:.2f},{board.VERTEX_PIXEL[v][1]:.2f}"
            for v in board.HEX_VERTICES[hid]
        )
        hexes.append({"id": hid, "cx": round(cx, 2), "cy": round(cy, 2), "points": points})
    vertices = [
        {"id": vid, "x": round(x, 2), "y": round(y, 2)}
        for vid, (x, y) in enumerate(board.VERTEX_PIXEL)
    ]
    edges = []
    for eid, (a, b) in enumerate(board.EDGE_VERTICES):
        (x1, y1), (x2, y2) = board.VERTEX_PIXEL[a], board.VERTEX_PIXEL[b]
        mx, my = board.EDGE_PIXEL[eid]
        edges.append(
            {
                "id": eid,
                "v1": a,
                "v2": b,
                "x1": round(x1, 2),
                "y1": round(y1, 2),
                "x2": round(x2, 2),
                "y2": round(y2, 2),
                "mx": round(mx, 2),
                "my": round(my, 2),
            }
        )
    return {
        "size": board.SIZE,
        "hexes": hexes,
        "vertices": vertices,
        "edges": edges,
        "coastal_edges": board.COASTAL_EDGES,
        "row_offsets": board.ROW_OFFSETS,
        "row_sizes": board.ROW_SIZES,
    }


GEOMETRY = _geometry_payload()


def _default_config() -> BoardConfig:
    return BoardConfig.model_validate(json.loads(FIXTURE.read_text()))


@app.get("/")
def index() -> FileResponse:
    return FileResponse(APP_DIR / "static" / "index.html")


@app.get("/api/geometry")
def geometry() -> dict:
    return GEOMETRY


@app.get("/api/config")
def get_config() -> BoardConfig:
    global LAST_CONFIG
    if LAST_CONFIG is None:
        LAST_CONFIG = _default_config()
    return LAST_CONFIG


@app.put("/api/config")
def put_config(cfg: BoardConfig) -> dict:
    global LAST_CONFIG
    LAST_CONFIG = cfg
    return {"ok": True, "warnings": config_warnings(cfg)}


@app.post("/api/solve")
def solve(cfg: BoardConfig) -> dict:
    global LAST_CONFIG
    LAST_CONFIG = cfg
    moves: list[ScoredMove] = solver.solve(cfg)
    return {
        "moves": [m.model_dump() for m in moves],
        "warnings": config_warnings(cfg),
    }


@app.post("/api/parse")
async def parse(file: UploadFile, backend: str = "auto") -> dict:
    """Parse a screenshot. backend: auto (Claude, falling back to Codex) | claude | codex."""
    global LAST_CONFIG
    media_type = file.content_type or "image/png"
    if media_type not in ACCEPTED_IMAGE_TYPES:
        raise HTTPException(415, f"unsupported image type {media_type}")
    if backend not in ("auto", "claude", "codex"):
        raise HTTPException(400, f"unknown backend {backend!r}")
    data = await file.read()
    try:
        if backend == "codex":
            cfg, raw, warnings = parser.parse_screenshot_codex(data, media_type)
            used = "codex"
        else:
            try:
                cfg, raw, warnings = parser.parse_screenshot(data, media_type)
                used = "claude"
            except (anthropic.AuthenticationError, parser.CredentialsMissing) as exc:
                if backend == "auto" and parser.codex_available():
                    cfg, raw, warnings = parser.parse_screenshot_codex(data, media_type)
                    used = "codex"
                else:
                    raise HTTPException(
                        501,
                        "screenshot parsing needs Anthropic API credentials "
                        "(ANTHROPIC_API_KEY or an `ant auth login` profile) or an "
                        "authenticated `codex` CLI; you can still edit the board by hand",
                    ) from exc
    except anthropic.APIStatusError as exc:
        raise HTTPException(502, f"vision call failed: {exc.message}") from exc
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from exc
    LAST_CONFIG = cfg
    return {"config": cfg.model_dump(), "raw": raw, "warnings": warnings, "backend": used}


# --- live feed (colonist websocket via CDP) ---------------------------------


@app.post("/api/live/start")
def live_start() -> dict:
    from .live.feed import FEED

    FEED.start()
    return FEED.status()


@app.post("/api/live/stop")
def live_stop() -> dict:
    from .live.feed import FEED

    FEED.stop()
    return {"stopped": True}


@app.get("/api/live/status")
def live_status() -> dict:
    from .live.feed import FEED

    return FEED.status()


@app.post("/api/live/open")
def live_open(url: str) -> dict:
    """Point the attached Chrome at a colonist URL (game link or lobby)."""
    from .live.feed import FEED

    FEED.start()
    return FEED.open_url(url)


@app.post("/api/live/resync")
def live_resync() -> dict:
    """Force colonist to re-send a full snapshot (used when we attach mid-game)."""
    from .live.feed import FEED

    return {"requested": FEED.request_resync(), **FEED.status()}


@app.get("/api/live/state")
def live_state() -> dict:
    """Current reconstructed position as a solver BoardConfig."""
    from .live.feed import FEED

    if not FEED.engine.state:
        raise HTTPException(409, "no live game state yet — start a game in the CDP browser")
    cfg = FEED.engine.board_config()
    return {"config": cfg.model_dump(), "warnings": config_warnings(cfg)}


@app.get("/api/live/moves")
def live_moves() -> dict:
    """Ranked moves + trade advice for the live position."""
    from .live.advisor import recommend
    from .live.feed import FEED

    if not FEED.engine.state:
        raise HTTPException(409, "no live game state yet")
    return recommend(FEED.engine)


@app.get("/api/live/log")
def live_log(limit: int = 200) -> dict:
    """Decoded move log for the current game, with gap detection."""
    from .live.feed import FEED

    if not FEED.game_id:
        raise HTTPException(409, "no live game yet")
    return {
        "game_id": FEED.game_id,
        "events": FEED.store.events(FEED.game_id, limit),
        "gaps": FEED.store.gaps(FEED.game_id),
    }


@app.post("/api/live/rebuild")
def live_rebuild(game_id: Optional[str] = None) -> dict:
    """Replay stored raw frames to re-derive state (crash/bug recovery)."""
    from .live.feed import FEED

    gid = game_id or FEED.game_id
    if not gid:
        raise HTTPException(400, "no game_id")
    applied = FEED.rebuild(gid)
    return {"game_id": gid, "frames_applied": applied, **FEED.status()}


app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")
