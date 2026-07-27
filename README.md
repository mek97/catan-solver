# catan-solver

Live advisor for [colonist.io](https://colonist.io). It reads the game's own
websocket over the Chrome DevTools Protocol, reconstructs the position, and
ranks what to do next — placements, bank and player trades, dev cards, the
robber — with the reasoning shown.

Advisory only: it never clicks anything in the game. You play; it advises.

## Quick start

```bash
./start.sh
```

That's the whole thing. It launches Chrome on a dedicated profile with the
remote-debugging port open, starts the server, attaches the live feed, and
opens the dashboard.

**Play in the Chrome window it opens** — that's the one the feed can read.
Paste a game link into the bar under the board, or just start a game from
colonist's lobby; the dashboard follows either way.

```bash
./start.sh --no-browser     # Chrome already running on :9222
PORT=9000 ./start.sh        # different port
```

Nothing needs starting in a particular order: the feed retries until Chrome
appears, so launching them in either order works.

## Live mode (colonist websocket via CDP)

Advisory only — the app never clicks in the game. It reads colonist's own
websocket, so there's no screenshotting, no vision error, and millisecond
latency.

`./start.sh` sets this up for you. To do it by hand:

```bash
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --remote-debugging-port=9222 --user-data-dir="$HOME/.chrome-catan-profile" \
  https://colonist.io
uv run uvicorn app.main:app --port 8017
```

The feed attaches when the server boots, so there is no connect step. The
board, ranked moves, trade advice, dice tracker, and move log refresh every 2s
as the real game progresses.

**How it works.** colonist speaks msgpack over
`wss://socket.svr.colonist.io/?version=2`. A `type: 4` frame carries the full
game state; every later `type: 91` frame is a partial diff. `app/live/`
persists each raw frame, folds the diffs into a live position, projects that
onto the solver's `BoardConfig`, and ranks moves.

Colonist uses the same axial coordinates we do, so the mapping is exact rather
than approximate: `hex (x,y) → (q,r)`, `corner (x,y,z) → vertex (x,y,N|S)`,
`edge z 0/1/2 → NW/W/SW side of that hex`. Both mappings are asserted at import
and covered by tests.

**Durability.** SQLite (WAL, `synchronous=FULL`) at `data/catan.db`. Raw frames
are stored verbatim *before* decoding, so `POST /api/live/rebuild` can
re-derive an entire game by replay if the decoder ever misreads something.
Frames dedupe on content hash and events on colonist's monotonic log id, so
reconnecting mid-game can't double-apply a move; `gaps` in `/api/live/status`
reports dropped event ids rather than silently advising on a stale board.

### Live endpoints

| Endpoint | Purpose |
|---|---|
| `POST /api/live/start` / `stop` | attach/detach the CDP feed |
| `GET /api/live/status` | connection, frame/event counts, gap list |
| `GET /api/live/state` | current position as a `BoardConfig` |
| `GET /api/live/moves` | ranked moves + trade advice + dice stats |
| `GET /api/live/log` | decoded move log |
| `POST /api/live/rebuild` | replay stored frames to re-derive state |

### What the feed captures

Every game action, decoded: dice rolls, settlement/city/road placements, dev
card purchases, robber moves and steals, discards on a 7, resource
distributions, bank/port trades, player-to-player trades, and open trade
offers.

## Layout

```
app/board.py      canonical geometry: 19 hexes / 54 vertices / 72 edges + adjacency
app/models.py     BoardConfig schema (the parser ⇄ editor ⇄ solver contract)
app/solver.py     move enumeration + scoring + reasoning
app/parser.py     screenshot → config (Claude vision, structured output)
app/rules.py      base-game rules in one place (costs, supplies, deck, limits)
app/main.py       FastAPI: geometry, config, parse, solve, live/*
app/static/       vanilla-JS board + action console
app/live/         colonist websocket: protocol, store, state engine, advisor
scripts/          fixture generator (beginner board layout)
start.sh          launcher: Chrome + server + dashboard
tests/            geometry, schema, solver, live replay, base-game rules
```

`uv run pytest` runs the suite.

## Rules

`app/rules.py` holds the base-game constants — tile and token distributions,
build costs, piece supplies, the 25-card development deck, victory thresholds,
the 2d6 table. `tests/test_rules.py` asserts conformance rather than trusting
the code. Where colonist reports a value authoritatively (piece supplies,
longest road, bank stock, discard limit) the live feed uses that, and the
derivations here are the fallback for hand-entered boards.

## Fair play

Use this in bot games or casual games where everyone's OK with it. Don't run
it in ranked colonist.io games against unsuspecting humans.
