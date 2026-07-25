"""FastAPI dashboard exposing the live graphical board and a small local API."""

from __future__ import annotations

import argparse
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from pydantic import BaseModel, Field
import uvicorn

from .config import GameConfig, load_config
from .game import GameError, GameManager


PROJECT_ROOT = Path(__file__).resolve().parents[2]
STATIC_DIRECTORY = PROJECT_ROOT / "static"


class NewGameRequest(BaseModel):
    ai_color: Literal["white", "black"] | None = None
    stockfish_skill_level: int | None = Field(default=None, ge=0, le=20)


class UndoRequest(BaseModel):
    plies: int = Field(default=1, ge=1, le=200)


class ResignRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


class OpenCodeMoveRequest(BaseModel):
    uci: str = Field(min_length=4, max_length=5)
    reasoning: str | None = Field(default=None, max_length=2000)
    candidate_moves: list[str] | None = Field(default=None, max_length=12)


def create_app(manager: GameManager) -> FastAPI:
    """Create an app around an injected manager so runner and web-only agree."""

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        manager.close()

    app = FastAPI(title="OpenCode vs Stockfish", version="0.1.0", lifespan=lifespan)

    def translate(error: GameError) -> HTTPException:
        return HTTPException(status_code=409, detail=str(error))

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def dashboard() -> str:
        return (STATIC_DIRECTORY / "index.html").read_text(encoding="utf-8")

    @app.get("/static/{asset_name}", include_in_schema=False)
    def static_asset(asset_name: str) -> FileResponse:
        path = (STATIC_DIRECTORY / asset_name).resolve()
        if path.parent != STATIC_DIRECTORY.resolve() or not path.is_file():
            raise HTTPException(status_code=404, detail="Static asset not found.")
        return FileResponse(path)

    @app.get("/api/state")
    def state() -> dict:
        return manager.state()

    @app.get("/api/status")
    def status() -> dict:
        return manager.game_status()

    @app.get("/api/board")
    def board() -> dict:
        return manager.board_view()

    @app.get("/api/legal-moves")
    def legal_moves() -> dict:
        return manager.legal_moves()

    @app.get("/api/evaluation")
    def evaluation() -> dict:
        return manager.evaluation()

    @app.get("/api/pgn", response_class=PlainTextResponse)
    def pgn() -> str:
        return manager.pgn()

    @app.post("/api/new-game")
    def new_game(request: NewGameRequest) -> dict:
        try:
            return manager.start_new_game(request.ai_color, request.stockfish_skill_level)
        except GameError as exc:
            raise translate(exc) from exc

    @app.post("/api/opencode-move")
    def opencode_move(request: OpenCodeMoveRequest) -> dict:
        try:
            return manager.submit_opencode_move(
                request.uci, request.reasoning, request.candidate_moves
            )
        except GameError as exc:
            raise translate(exc) from exc

    @app.post("/api/pause")
    def pause() -> dict:
        return manager.pause()

    @app.post("/api/resume")
    def resume() -> dict:
        return manager.resume()

    @app.post("/api/undo")
    def undo(request: UndoRequest) -> dict:
        try:
            return manager.undo(request.plies)
        except GameError as exc:
            raise translate(exc) from exc

    @app.post("/api/resign")
    def resign(request: ResignRequest) -> dict:
        try:
            return {"outcome": manager.resign_opencode(request.reason)}
        except GameError as exc:
            raise translate(exc) from exc

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Run only the local OpenCode chess dashboard API.")
    parser.add_argument("--config", default="config/game.json", help="Path to game JSON configuration.")
    args = parser.parse_args()
    config: GameConfig = load_config(args.config)
    manager = GameManager(config, PROJECT_ROOT)
    app = create_app(manager)
    uvicorn.run(app, host=config.web_host, port=config.web_port, log_level="info")


if __name__ == "__main__":
    main()
