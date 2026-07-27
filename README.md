# catan-solver

Personal Catan assistant for [colonist.io](https://colonist.io): drop a
screenshot of the board, get a parsed + editable board state, and a ranked
list of recommended moves with reasoning. Pair it with Claude Code + the
Claude-in-Chrome extension to have the moves clicked for you (see
`CLAUDE.md`).

## Quick start

```bash
uv sync
uv run uvicorn app.main:app --reload --port 8017
# open http://localhost:8017
```

Screenshot parsing has two interchangeable vision backends:

- **Claude** (preferred, fastest) — needs Anthropic API credentials:
  `ANTHROPIC_API_KEY` in the environment or in a gitignored `.env` at the
  repo root, or an `ant auth login` profile.
- **Codex CLI** (automatic fallback) — uses a locally authenticated `codex`
  CLI (ChatGPT plan, no API key). Slower (~30–90s) but zero setup if you
  already use Codex.

Auto mode tries Claude and falls back to Codex; force one with
`POST /api/parse?backend=claude|codex`. With neither available everything
else still works — enter the board by hand in the editor.

## Flow

1. **Screenshot** — drag a colonist.io screenshot into the panel (or paste it
   with ⌘V). One Claude vision call turns it into a board config. The model
   only ever references hexes by row/position and pieces by hex + compass
   direction; the backend resolves those to canonical IDs, so a misread is a
   two-click fix, never a corrupted board.
2. **Review** — the parsed board renders as an editable SVG. Modes: click a
   hex to change resource/number, click vertices/edges to place settlements,
   cities, and roads per color, move the robber. Fill in your hand + the
   player panels.
3. **Solve** — ranked moves with score, plain-English reasoning, and a
   `location_hint` phrased so a human (or Claude driving Chrome) can find the
   spot on screen ("settle the corner touching 8-wood, 6-brick, 11-sheep").
   Hovering a result pulses the referenced spots on the board.

## What the solver is

A single-turn heuristic evaluator (`app/solver.py`) — not MCTS. It enumerates
legal moves (builds, dev-card plays, bank/port trades, robber placement,
setup placements), scores them on production pips weighted by scarcity and
resource diversity, port synergy, expansion room, blocking, and the VP race,
and composes greedy combos (trade→build, build chains, Year-of-Plenty→build).
It's an advisor with a human in the loop: usually sensible, always
explainable. All weights live in `solver.W`.

## Live mode (colonist websocket via CDP)

Advisory only — the app never clicks in the game. It reads colonist's own
websocket, so there's no screenshotting, no vision error, and millisecond
latency.

```bash
# 1. Chrome with the debug port (separate profile)
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --remote-debugging-port=9222 --user-data-dir="$HOME/.chrome-catan-profile" \
  https://colonist.io

# 2. In the dashboard, hit Connect under "Live feed" (or POST /api/live/start)
```

The board, ranked moves, trade advice, dice tracker, and move log then refresh
every 2s as the real game progresses.

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
app/main.py       FastAPI: /api/geometry /api/config /api/parse /api/solve
app/static/       vanilla-JS SVG board editor
scripts/          fixture generator (beginner board layout)
app/live/         colonist websocket: protocol, store, state engine, advisor
tests/            geometry invariants, schema validation, solver, live replay
```

`uv run pytest` runs the suite.

## Ports

The default fixture places the 9 ports on evenly spaced coastal edges — close
to, but not exactly, the rulebook layout. Fix port positions for a real game
via the Raw JSON tab (each port is `{"type": ..., "vertices": [a, b]}`).

## Fair play

Use this in bot games or casual games where everyone's OK with it. Don't run
it in ranked colonist.io games against unsuspecting humans.
