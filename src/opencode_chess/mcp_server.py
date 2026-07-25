"""The Chess MCP server used by OpenCode.

It is intentionally a thin stdio MCP adapter over the runner's local HTTP API.
The runner is the only process that owns the board and Stockfish process, preventing
separate OpenCode tool processes from ever holding stale chess state.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP


BASE_URL = os.environ.get("OPENCODE_CHESS_URL", "http://127.0.0.1:8765").rstrip("/")
TIMEOUT = httpx.Timeout(15.0, connect=3.0)
mcp = FastMCP("OpenCode Chess")


def _request(method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
    """Call the local authoritative game API and give the model actionable errors."""
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            response = client.request(method, f"{BASE_URL}{path}", json=payload)
    except httpx.HTTPError as exc:
        return {
            "ok": False,
            "error": (
                f"The local chess runner at {BASE_URL} is unavailable: {exc}. "
                "Start `opencode-chess --config config/game.json` and try again."
            ),
        }
    try:
        body: Any = response.json()
    except ValueError:
        body = response.text
    if response.is_error:
        detail = body.get("detail", body) if isinstance(body, dict) else body
        return {"ok": False, "error": str(detail), "http_status": response.status_code}
    return {"ok": True, "data": body}


@mcp.tool()
def get_game_state() -> dict[str, Any]:
    """Read the authoritative game: FEN, graphical-square data, turn, history,
    current result, last Stockfish evaluation, and current OpenCode status."""
    return _request("GET", "/api/state")


@mcp.tool()
def get_board() -> dict[str, Any]:
    """Read a compact board view containing FEN, Unicode board, side to move,
    check flag, and last move. This does not ask Stockfish to analyze anything."""
    return _request("GET", "/api/board")


@mcp.tool()
def get_legal_moves() -> dict[str, Any]:
    """List every currently legal move as UCI and SAN. Read this immediately
    before choosing a move; do not construct or guess a move from an old position."""
    return _request("GET", "/api/legal-moves")


@mcp.tool()
def submit_move(
    uci: str,
    reasoning: str | None = None,
    candidate_moves: list[str] | None = None,
) -> dict[str, Any]:
    """Submit one OpenCode UCI move (for example e2e4 or e7e8q).

    The runner verifies that it is OpenCode's turn and that the move is legal before
    changing the board. On an error, the board is unchanged; inspect legal moves and
    submit a different move instead of assuming success.
    """
    return _request(
        "POST",
        "/api/opencode-move",
        {"uci": uci, "reasoning": reasoning, "candidate_moves": candidate_moves},
    )


@mcp.tool()
def game_status() -> dict[str, Any]:
    """Check whether the game is ongoing, paused, over by mate/stalemate/repetition/
    fifty-move rule, whose turn it is, and whether the side to move is in check."""
    return _request("GET", "/api/status")


@mcp.tool()
def get_evaluation() -> dict[str, Any]:
    """Return the evaluation and principal variation captured while Stockfish chose
    its own most recent reply. This never invokes Stockfish on OpenCode's turn."""
    return _request("GET", "/api/evaluation")


@mcp.tool()
def get_pgn() -> dict[str, Any]:
    """Return the complete current PGN, including available move comments."""
    return _request("GET", "/api/pgn")


@mcp.tool()
def new_game(
    ai_color: str | None = None,
    stockfish_skill_level: int | None = None,
) -> dict[str, Any]:
    """Start a fresh local game. ai_color must be 'white' or 'black' when supplied;
    Stockfish skill ranges from 0 to 20. The autonomous runner will then take turns."""
    return _request(
        "POST",
        "/api/new-game",
        {"ai_color": ai_color, "stockfish_skill_level": stockfish_skill_level},
    )


@mcp.tool()
def undo_last_move(plies: int = 1) -> dict[str, Any]:
    """Undo one or more recorded plies while paused or between turns. This is a
    recovery/control tool, not part of normal autonomous play."""
    return _request("POST", "/api/undo", {"plies": plies})


@mcp.tool()
def resign(reason: str | None = None) -> dict[str, Any]:
    """Resign OpenCode's side and end the game. Use only when resignation is truly
    intended; the winner and result are saved into the PGN."""
    return _request("POST", "/api/resign", {"reason": reason})


def main() -> None:
    # FastMCP handles the JSON-RPC stdio protocol. Never print diagnostic text to
    # stdout in this process because stdout is reserved for MCP messages.
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
