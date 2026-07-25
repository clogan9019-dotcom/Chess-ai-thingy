# OpenCode vs Stockfish — autonomous local chess

A Windows-friendly system in which **your local OpenCode agent** plays a full chess game against **Stockfish** without anyone typing moves into a board.

```
OpenCode CLI (local Ollama model)
        │ discovers/calls only Chess MCP tools
        ▼
Chess MCP stdio server ──local HTTP──► authoritative game runner
                                           ├─ python-chess rule validation
                                           ├─ Stockfish (only its own turns)
                                           └─ live local web board / PGN saves
```

It does **not** use Playwright, Chess.com, Lichess, scraping, or browser chess automation. The included OpenCode agent has every tool denied except the `chess_*` MCP tools.

## What is included

- An MCP server with actual tools for state, board, legal moves, submit move, status, PGN, evaluation, new game, undo, and resignation.
- An authoritative `python-chess` board that validates every move before its state changes.
- One UCI Stockfish process; it is asked for a move **only when Stockfish is the side to move**. Its score/PV comes from that own-move search; there is no extra Stockfish analysis on OpenCode turns.
- An autonomous turn runner that starts `opencode run` on every OpenCode turn. OpenCode reads tools, reasons, submits a move, and the runner then asks Stockfish for exactly one reply.
- A dependency-free local live board at `http://127.0.0.1:8765`, with move history, last-move highlighting, captures, evaluation bar, engine/AI activity, pause/restart/undo/resign, and PGN download.
- Auto-saved `games/opencode-vs-stockfish-latest.pgn` and JSON state after every accepted move, plus a timestamped PGN after a finished match.

> **Important:** This is a game orchestration project, not a custom chess engine. `python-chess` owns chess legality and game-rule detection; Stockfish owns its own moves.

---

## Folder structure

```text
.
├── config/
│   ├── game.json                  # launch configuration — edit this
│   ├── game.example.json          # Windows path example
│   └── Modelfile.example          # optional persistent Ollama context size
├── src/opencode_chess/
│   ├── game.py                    # authoritative board, rules, Stockfish UCI
│   ├── mcp_server.py              # stdio Chess MCP adapter
│   ├── runner.py                  # autonomous OpenCode / Stockfish alternation
│   ├── web.py                     # local FastAPI UI/API
│   └── config.py                  # strict JSON config validation
├── static/
│   ├── index.html                 # live graphical board
│   ├── styles.css
│   └── app.js
├── templates/chess-player.md.template # restricted OpenCode player agent
├── opencode.json                  # OpenCode MCP configuration (v1 layout)
├── opencode.v2.example.json       # OpenCode v2 MCP configuration layout
├── opencode.ollama.example.jsonc  # Ollama provider merge example
├── pyproject.toml
└── requirements.txt
```

Runtime-only files are intentionally ignored by Git: `.venv/`, `games/`, `runtime/`, and the generated `.opencode/agents/chess-player.md`.

---

## Prerequisites (Windows 11)

Already-installed software from the question is sufficient, plus Python:

| Component | Required | Check |
|---|---:|---|
| Python 3.10+ (3.11 recommended) | Yes | `py --version` |
| OpenCode | Yes | `opencode --version` |
| Ollama and a **tool-capable** local model | Yes | `ollama list` |
| Stockfish UCI executable | Yes | `stockfish` or full `.exe` path |
| Playwright / Banksia / Chess.com account | **No** | Not used |

Use an Ollama model which supports tool/function calls through your OpenCode provider. Qwen-based reasoning/tool models are the intended choice. A model that only prints prose cannot submit an MCP move and will be paused with a useful dashboard error rather than causing a desynchronized game.

---

## Installation

Run these commands in **PowerShell** from this repository's root.

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e .
```

`pip install -r requirements.txt` is also supported, but `-e .` additionally installs the convenient `opencode-chess` and `opencode-chess-mcp` commands into the virtual environment.

### 1. Configure Stockfish and match settings

Edit `config/game.json`.

```json
{
  "stockfish_path": "C:\\Program Files\\Stockfish\\stockfish-windows-x86-64-avx2.exe",
  "stockfish_skill_level": 8,
  "stockfish_move_time_seconds": 0.75,
  "ai_color": "white",
  "opencode_model": "ollama/qwen3:8b"
}
```

If `stockfish` works in a PowerShell window, leaving `"stockfish_path": "stockfish"` is fine. Otherwise use the full escaped Windows path as shown. `stockfish_skill_level` accepts **0–20**.

### 2. Configure Ollama / OpenCode

If OpenCode already talks to Ollama, keep that setup and set `opencode_model` in `config/game.json` to its exact OpenCode model ID (normally `ollama/<name from ollama list>`).

If it does not, merge the `provider` section from `opencode.ollama.example.jsonc` into your existing global OpenCode config, normally:

```text
%USERPROFILE%\.config\opencode\opencode.json
```

Replace `qwen3:8b` in that example with your local model. The model entry must have `"tools": true`. Confirm the ID OpenCode sees:

```powershell
opencode models ollama
```

#### Context length

`ollama_context_size` in `config/game.json` is displayed and tracked with the match. Set the model's actual Ollama context window persistently with the supplied Modelfile:

```powershell
Copy-Item config\Modelfile.example Modelfile
# Edit Modelfile: replace qwen3:8b and num_ctx if wanted
ollama create qwen3-chess -f .\Modelfile
```

Then use `"opencode_model": "ollama/qwen3-chess"` in `config/game.json`, and add the same model name to your OpenCode Ollama provider configuration. The runner materializes its restricted agent before each match using `opencode_temperature` from `game.json`; adjust that number (normally `0.1`–`0.35`) to change move-selection randomness.

### 3. Connect OpenCode to the Chess MCP server

This repository includes `opencode.json`, the OpenCode **v1** project configuration. It starts this exact command as a local stdio MCP server:

```text
.\.venv\Scripts\python.exe -m opencode_chess.mcp_server
```

The configured server name is `chess`, so OpenCode normally presents the following **actual bundled MCP tools** with a `chess_` prefix:

| Server implementation | Typical OpenCode-visible name | Purpose |
|---|---|---|
| `get_game_state` | `chess_get_game_state` | FEN, move history, status, eval, current board |
| `get_board` | `chess_get_board` | compact FEN / Unicode board |
| `get_legal_moves` | `chess_get_legal_moves` | all legal UCI and SAN moves |
| `submit_move` | `chess_submit_move` | validated OpenCode UCI move submission |
| `game_status` | `chess_game_status` | check, terminal status, side to move |
| `get_evaluation` | `chess_get_evaluation` | last Stockfish own-search score/PV |
| `get_pgn` | `chess_get_pgn` | current game state including PGN |
| `new_game`, `undo_last_move`, `resign` | `chess_new_game`, etc. | supported control/recovery actions |

The generated OpenCode player agent is explicitly told to **inspect the discovered tool list first**. The actual tool list OpenCode displays is authoritative; this table documents this repository's server, not guessed third-party tools.

If your OpenCode reports a v2 configuration schema, copy the MCP block from `opencode.v2.example.json` into the project `opencode.json` instead. The v2 schema places the server under `mcp.servers`. Do not keep both layouts at once.

Once a match is running, verify discovery in a second PowerShell window:

```powershell
opencode mcp list
```

The local web runner must be up before an MCP call can return board data. This is expected; it is how every fresh MCP process uses the same single authoritative board.

---

## Launch and watch a full autonomous match

1. Set the desired `ai_color` (`"white"` or `"black"`), Stockfish skill, model, and paths in `config/game.json`.
2. In a PowerShell window at the repository root, launch:

   ```powershell
   .\.venv\Scripts\opencode-chess.exe --config config\game.json
   ```

   Or, without the script shim:

   ```powershell
   .\.venv\Scripts\python.exe -m opencode_chess.runner --config config\game.json
   ```

3. Open **http://127.0.0.1:8765** in any browser to watch. The browser only renders this local board; it does not play moves or automate another site.

No manual chess move entry is available or required. The runner immediately starts a new game:

- If OpenCode is White, it launches OpenCode first.
- If OpenCode is Black, Stockfish makes its one White move first, then OpenCode is launched.
- Each OpenCode turn is a separate headless `opencode run --agent chess-player --model …` call, which discovers/uses MCP tools and independently reasons about that current position.
- After a successful OpenCode MCP submission, the runner invokes Stockfish only for its reply.

The terminal stays open while the game runs. `Ctrl+C` safely stops the runner; the current game has already been auto-saved.

### Optional: reuse a running OpenCode backend

For lower MCP cold-start latency, start a headless OpenCode backend from this project root in a separate terminal:

```powershell
opencode serve --port 4096
```

Then set this in `config/game.json` before launching the match:

```json
"opencode_server_url": "http://127.0.0.1:4096"
```

The runner adds `--attach` for each turn. Leave it `null` for the simplest setup.

---

## Configuration reference

| Setting | Meaning |
|---|---|
| `stockfish_path` | `stockfish` on PATH or a full `.exe` path |
| `stockfish_skill_level` | Integer 0–20; passed through Stockfish's `Skill Level` UCI option when supported |
| `stockfish_move_time_seconds` | Stockfish's time budget for **each of its own moves** |
| `ai_color` | `white` or `black`; OpenCode's side |
| `opencode_executable` | Usually `opencode`; set an absolute path if it is not on PATH |
| `opencode_model` | Exact `provider/model` passed to `opencode run --model` |
| `opencode_temperature` | Written to the restricted OpenCode player agent before launch |
| `ollama_context_size` | Recorded dashboard/match context setting; set actual Ollama `num_ctx` with the Modelfile instructions above |
| `max_opencode_thinking_time_seconds` | Hard timeout for one OpenCode CLI turn |
| `max_opencode_turn_retries` | Failed/no-submission attempts before automation pauses safely |
| `move_delay_seconds` | Delay between accepted plies for a watchable board |
| `web_host`, `web_port` | Local board binding; keep loopback unless you intentionally need LAN access |
| `auto_save_directory` | Directory for latest PGN/JSON and final timestamped PGN files |
| `initial_fen` | `null` for normal chess; optionally a legal FEN for a study/test start |
| `opencode_server_url` | `null` or optional `opencode serve` URL for `--attach` |

Changing the side or Stockfish skill in the live UI takes effect only when pressing **New / restart game**. This is a match control, not manual move entry.

---

## Game rules, errors, and recovery guarantees

### Rules detected after every accepted move

| Condition | Behaviour |
|---|---|
| Checkmate | Game finishes with the winning colour/result |
| Stalemate | Draw |
| Insufficient material | Draw |
| Threefold (or fivefold) repetition | Draw (the autonomous system ends when a claim is available) |
| Fifty-move rule (or automatic 75-move rule) | Draw |
| OpenCode resignation | Stockfish is awarded the result and PGN is saved |

### Illegal moves never desynchronize the game

`chess_submit_move` parses UCI, checks whose turn it is, and checks membership in the authoritative `python-chess` legal-move set **before** pushing the move. If OpenCode proposes `e2e5`, stale move data, bad promotion syntax, or a move on Stockfish's turn:

1. The tool returns a clear error and no board state changes.
2. The OpenCode agent is instructed to request legal moves and choose again in the same turn.
3. If the CLI exits without a confirmed submission, the runner starts a new OpenCode attempt.
4. After `max_opencode_turn_retries`, automation enters a visible safe error state. The board/PGN remain intact; fix the model/MCP error and click **Resume**. It never substitutes Stockfish for OpenCode.

Stockfish's return move is also checked against the same board's legal moves before it is applied. If Stockfish cannot start or returns an invalid move, the board is untouched and the dashboard shows the error.

---

## Live-board features

- Graphical local board with selectable orientation and last-move highlight
- Current FEN, turn/check/terminal result, elapsed game time
- Captured pieces, move history, PGN comments
- Last Stockfish evaluation/PV and an evaluation bar (White-positive)
- Last OpenCode reasoning, candidate moves, elapsed thought time, and token usage when the installed OpenCode CLI exposes it in JSON events
- Pause, resume, restart, undo, resignation, and PGN download
- Automatic latest PGN/JSON saves and final PGN archive

The UI intentionally has **no clickable move input**. Its controls are administrative only.

---

## Troubleshooting

### `Stockfish was not found …`

Set a full escaped executable path in `config/game.json`, for example:

```json
"stockfish_path": "C:\\Tools\\stockfish\\stockfish-windows-x86-64-avx2.exe"
```

or add the directory containing `stockfish.exe` to Windows `PATH`, open a new PowerShell, and run `stockfish` once to confirm it starts.

### The board opens, but OpenCode pauses after retries

Open the **Automation activity** panel. Common causes:

1. `opencode` is not on `PATH`: set `opencode_executable` to its full `.cmd`/`.exe` path.
2. The `opencode_model` ID is wrong: run `opencode models ollama` and copy its ID exactly.
3. The model does not support tool calls: use a Qwen/Ollama model configured with `"tools": true`.
4. OpenCode did not load the project MCP config: run from the repository root, check `opencode mcp list`, and use the appropriate v1/v2 `opencode.json` layout.
5. A changed dashboard port was not propagated: the supplied config uses `{env:OPENCODE_CHESS_URL}` and the runner sets it automatically. If you copied the MCP block manually, make it match `web_port`.

After correcting the issue, click **Resume**. No moves need to be re-entered.

### `The local chess runner … is unavailable` from an MCP tool

The MCP server is correct, but the live game runner is not listening yet. Start `opencode-chess --config config\game.json`, wait for the `Live board:` message, and retry. The MCP service intentionally does not maintain a second private board.

### OpenCode asks for tool permission or uses unrelated tools

The generated `.opencode/agents/chess-player.md` restricts permissions to `chess_*`, and the runner uses `--agent chess-player --auto`. Ensure you launch the included runner from this repository rather than a generic OpenCode Build agent. The prompt and permission policy deny shell, web, browser, file, and Playwright routes.

### Model is slow or Ollama runs out of memory

Use a smaller local model, lower `ollama_context_size`/Modelfile `num_ctx`, lower `max_opencode_thinking_time_seconds`, or increase `move_delay_seconds`. Stockfish time is controlled independently by `stockfish_move_time_seconds`.

### Port 8765 is occupied

Change `web_port` in `config/game.json`, then relaunch. The runner prints the new live-board URL. If you hand-edited `opencode.json`, ensure its MCP environment uses `{env:OPENCODE_CHESS_URL}` or the matching port.

### How can I inspect a saved game?

Open `games/opencode-vs-stockfish-latest.pgn` in Banksia, CuteChess, or any PGN viewer. The dashboard's **Download PGN** link produces the same current PGN.

---

## Development smoke test

After installation, this checks syntax and core legal move validation without requiring a real Stockfish move or OpenCode call:

```powershell
.\.venv\Scripts\python.exe -m compileall -q src
.\.venv\Scripts\python.exe -c "from opencode_chess.config import load_config; from opencode_chess.game import GameManager; g=GameManager(load_config('config/game.json'), '.'); g.start_new_game(); print(g.legal_moves()['count']); g.close()"
```

A normal initial chess position prints `20`.
