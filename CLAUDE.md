# catan-solver — Claude Code playbook

Personal tool: screenshot of a colonist.io board → parsed board config →
ranked move recommendations → (optionally) Claude Code clicks the move in the
real Chrome tab.

## Run

```bash
uv sync
uv run uvicorn app.main:app --reload --port 8017   # http://localhost:8017
```

Tests: `uv run pytest`. Screenshot parsing backends (auto-selected, or force
via `POST /api/parse?backend=claude|codex`):

- **claude** — Anthropic credentials (`ANTHROPIC_API_KEY`, repo-root `.env`,
  or `ant auth login` profile). Fast.
- **codex** — shells out to a locally-authed `codex exec -i <img>` (ChatGPT
  plan, no API key). ~30–90s per parse; JSON shape enforced by prompt +
  pydantic validation, not `--output-schema` (OpenAI strict mode rejects
  pydantic's generated schema — don't re-add that flag).

Everything except parsing works with no credentials at all.

## Driving a game (Claude Code + claude-in-chrome)

The app is the brain; you are the hands and eyes. Loop per turn:

1. **Make sure the server is up** (port 8017, see above).
2. **Capture the board.** Take a screenshot of the colonist.io tab. Either
   POST it to the parser yourself, or let the user drop it into the UI:
   ```bash
   curl -s -X POST http://localhost:8017/api/parse -F "file=@board.png"
   ```
   The user usually reviews/corrects the parse in the UI at
   http://localhost:8017 — their edits are auto-pushed to the server.
3. **Get the recommendation.** The current board always lives at
   `GET /api/config`. Solve it:
   ```bash
   curl -s http://localhost:8017/api/config \
     | curl -s -X POST http://localhost:8017/api/solve \
         -H 'Content-Type: application/json' -d @- \
     | python3 -c "import json,sys; [print(m['score'], '-', m['location_hint']) for m in json.load(sys.stdin)['moves'][:3]]"
   ```
4. **Execute the top move in the browser.** Each move has a `location_hint`
   phrased in screen-findable terms ("settle the corner touching 8-wood,
   6-brick, 11-sheep", "move the robber to the 6-brick hex"). Find that spot
   in the live tab by the number tokens + terrain colors, click the matching
   build button / board position. Take a fresh screenshot after clicking to
   confirm the move registered before reporting it done.
5. **Confirm with the user before irreversible moves** (trades with players,
   playing dev cards) unless they've told you to play on autopilot.

Notes:
- Vertex/edge/hex IDs in the config are canonical (see `app/board.py`):
  hexes 0–18 row-major (rows of 3-4-5-4-3), vertices 0–53 and edges 0–71
  sorted top-to-bottom, left-to-right. `location_hint` exists so you never
  need pixel math — navigate by number tokens and terrain.
- `pending: "move_robber"` in the config means a 7 was rolled — solve returns
  robber placements only.
- During setup, set `phase` to `setup1`/`setup2`; solve returns settlement +
  free-road pairs.

## Fair play

Only assist in bot games or casual games with consent. Never in ranked games
against unsuspecting humans.

## Conventions

- Python 3.11+, uv-managed. FastAPI + vanilla JS (no frontend framework).
- `app/board.py` is the single source of geometry truth. The frontend gets
  geometry from `/api/geometry`; the vision parser emits row/pos + compass
  references, never IDs. Keep it that way.
- Solver weights live in `solver.W`; scores ≈ "weighted pips".
- Vision parsing: both backends produce the same `RawBoard` shape
  (`app/parser.py`) — Claude via one `messages.parse()` call (model
  `claude-opus-5`, structured output), Codex via `codex exec` subprocess with
  lenient JSON extraction. Backend fallback logic lives in `app/main.py`.
