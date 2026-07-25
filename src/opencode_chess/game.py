"""Authoritative game state, rule validation, persistence, and Stockfish integration.

This module deliberately delegates chess rules to python-chess and moves to Stockfish.
It is an orchestrator, not a chess engine.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
import json
from pathlib import Path
import threading
from typing import Any
from uuid import uuid4

import chess
import chess.engine
import chess.pgn

from .config import ColorName, GameConfig


PIECE_UNICODE = {
    "P": "♙", "N": "♘", "B": "♗", "R": "♖", "Q": "♕", "K": "♔",
    "p": "♟", "n": "♞", "b": "♝", "r": "♜", "q": "♛", "k": "♚",
}


class GameError(RuntimeError):
    """A recoverable game operation error that must not change the board."""


class GameManager:
    """One locked, authoritative game shared by HTTP, MCP, and the runner.

    Board mutation happens only in this class. That keeps an invalid MCP request,
    a browser refresh, or an OpenCode retry from desynchronizing Stockfish.
    """

    def __init__(self, config: GameConfig, root_directory: str | Path = ".") -> None:
        self._lock = threading.RLock()
        self.config = config
        self.root_directory = Path(root_directory).resolve()
        self._engine: chess.engine.SimpleEngine | None = None
        self._engine_error: str | None = None
        self._version = 0
        self._state = "idle"
        self._resume_state = "idle"
        self._message = "Ready. Start a new game from the runner or the dashboard."
        self._last_error: str | None = None
        self._last_engine_evaluation: dict[str, Any] | None = None
        self._last_opencode: dict[str, Any] | None = None
        self._started_at: datetime | None = None
        self._finished_at: datetime | None = None
        self._game_id = ""
        self._initial_fen: str | None = None
        self._forced_outcome: dict[str, Any] | None = None
        self._board = chess.Board()
        self._pgn = chess.pgn.Game()
        self._history: list[dict[str, Any]] = []
        self._illegal_move_attempts = 0
        self._new_game_locked()
        self._state = "idle"

    # ---------- general state ----------

    @staticmethod
    def _color_name(color: chess.Color) -> ColorName:
        return "white" if color == chess.WHITE else "black"

    @staticmethod
    def _other(color: ColorName) -> ColorName:
        return "black" if color == "white" else "white"

    def _touch(self, message: str | None = None) -> None:
        self._version += 1
        if message is not None:
            self._message = message

    def _new_game_locked(
        self,
        ai_color: ColorName | None = None,
        stockfish_skill_level: int | None = None,
    ) -> None:
        self.config = self.config.with_start_options(ai_color, stockfish_skill_level)
        # A dashboard restart may change strength while keeping the same healthy
        # UCI process alive. Apply it immediately rather than silently retaining
        # the previous game's level.
        if self._engine is not None and "Skill Level" in self._engine.options:
            try:
                self._engine.configure({"Skill Level": self.config.stockfish_skill_level})
            except Exception as exc:
                self._engine_error = str(exc)
        try:
            self._board = chess.Board(self.config.initial_fen) if self.config.initial_fen else chess.Board()
        except ValueError as exc:
            raise GameError(f"initial_fen is not a valid FEN: {exc}") from exc
        self._initial_fen = self._board.fen()
        self._game_id = uuid4().hex[:12]
        self._started_at = datetime.now(UTC)
        self._finished_at = None
        self._history = []
        self._illegal_move_attempts = 0
        self._forced_outcome = None
        self._last_error = None
        self._last_engine_evaluation = None
        self._last_opencode = None
        self._pgn = chess.pgn.Game()
        self._pgn.headers["Event"] = "OpenCode vs Stockfish"
        self._pgn.headers["Site"] = "Local"
        self._pgn.headers["Date"] = self._started_at.strftime("%Y.%m.%d")
        self._pgn.headers["Round"] = self._game_id
        self._pgn.headers["White"] = "OpenCode" if self.config.ai_color == "white" else "Stockfish"
        self._pgn.headers["Black"] = "OpenCode" if self.config.ai_color == "black" else "Stockfish"
        if self._initial_fen != chess.STARTING_FEN:
            self._pgn.headers["SetUp"] = "1"
            self._pgn.headers["FEN"] = self._initial_fen
        self._pgn.headers["Result"] = "*"

    def start_new_game(
        self,
        ai_color: ColorName | None = None,
        stockfish_skill_level: int | None = None,
    ) -> dict[str, Any]:
        """Reset the board and make it ready for the autonomous turn loop."""
        with self._lock:
            if ai_color is not None and ai_color not in ("white", "black"):
                raise GameError("ai_color must be 'white' or 'black'.")
            if stockfish_skill_level is not None and not 0 <= stockfish_skill_level <= 20:
                raise GameError("stockfish_skill_level must be from 0 through 20.")
            self._new_game_locked(ai_color, stockfish_skill_level)
            self._state = self._turn_state_locked()
            self._touch("New game started.")
            self._save_locked()
            return self.state()

    def _turn_state_locked(self) -> str:
        outcome = self._outcome_locked()
        if outcome["terminal"]:
            return "finished"
        return "waiting_for_opencode" if self._side_to_move_locked() == self.config.ai_color else "waiting_for_stockfish"

    def _side_to_move_locked(self) -> ColorName:
        return self._color_name(self._board.turn)

    def _outcome_locked(self) -> dict[str, Any]:
        """Return our explicit rule result, including claimable automatic draws.

        In tournament chess a player normally claims 3-fold/50-move draws. There is
        no human claimant in this autonomous system, so a claimable draw ends the
        match to avoid continuing past a detected draw condition.
        """
        if self._forced_outcome is not None:
            return dict(self._forced_outcome)
        if self._board.is_checkmate():
            winner = self._other(self._side_to_move_locked())
            return {
                "terminal": True,
                "kind": "checkmate",
                "result": "1-0" if winner == "white" else "0-1",
                "winner": winner,
                "label": f"Checkmate — {winner.title()} wins",
            }
        if self._board.is_stalemate():
            return {"terminal": True, "kind": "stalemate", "result": "1/2-1/2", "winner": None, "label": "Draw by stalemate"}
        if self._board.is_insufficient_material():
            return {"terminal": True, "kind": "insufficient_material", "result": "1/2-1/2", "winner": None, "label": "Draw by insufficient material"}
        if self._board.is_seventyfive_moves() or self._board.can_claim_fifty_moves():
            return {"terminal": True, "kind": "fifty_move_rule", "result": "1/2-1/2", "winner": None, "label": "Draw by fifty-move rule"}
        if self._board.is_fivefold_repetition() or self._board.can_claim_threefold_repetition():
            return {"terminal": True, "kind": "repetition", "result": "1/2-1/2", "winner": None, "label": "Draw by repetition"}
        return {"terminal": False, "kind": "ongoing", "result": "*", "winner": None, "label": "Game in progress"}

    def _check_for_finish_locked(self) -> dict[str, Any]:
        outcome = self._outcome_locked()
        self._pgn.headers["Result"] = outcome["result"]
        if outcome["terminal"]:
            self._state = "finished"
            self._finished_at = datetime.now(UTC)
            self._touch(outcome["label"])
        return outcome

    # ---------- serialization / board presentation ----------

    def _board_squares_locked(self) -> list[dict[str, Any]]:
        squares: list[dict[str, Any]] = []
        for rank in range(7, -1, -1):
            for file_index in range(8):
                square = chess.square(file_index, rank)
                piece = self._board.piece_at(square)
                squares.append(
                    {
                        "square": chess.square_name(square),
                        "piece": piece.symbol() if piece else None,
                        "glyph": PIECE_UNICODE[piece.symbol()] if piece else "",
                        "color": self._color_name(piece.color) if piece else None,
                    }
                )
        return squares

    def _last_move_locked(self) -> dict[str, Any] | None:
        return self._history[-1] if self._history else None

    def _captured_locked(self) -> dict[str, list[str]]:
        # Works for normal start positions and is still useful for a custom FEN.
        initial_counts = {symbol: 0 for symbol in PIECE_UNICODE}
        current_counts = {symbol: 0 for symbol in PIECE_UNICODE}
        initial_board = chess.Board(self._initial_fen)
        for piece in initial_board.piece_map().values():
            initial_counts[piece.symbol()] += 1
        for piece in self._board.piece_map().values():
            current_counts[piece.symbol()] += 1
        captured: dict[str, list[str]] = {"white": [], "black": []}
        for symbol, initial in initial_counts.items():
            missing = max(0, initial - current_counts[symbol])
            # A black piece was captured by White and vice versa.
            capturer = "black" if symbol.isupper() else "white"
            captured[capturer].extend([PIECE_UNICODE[symbol]] * missing)
        return captured

    def _clock_seconds_locked(self) -> float:
        if not self._started_at:
            return 0.0
        end = self._finished_at or datetime.now(UTC)
        return max(0.0, (end - self._started_at).total_seconds())

    def state(self) -> dict[str, Any]:
        with self._lock:
            outcome = self._outcome_locked()
            last_move = self._last_move_locked()
            return {
                "version": self._version,
                "game_id": self._game_id,
                "state": self._state,
                "message": self._message,
                "last_error": self._last_error,
                "fen": self._board.fen(),
                "turn": self._side_to_move_locked(),
                "in_check": self._board.is_check(),
                "board": self._board_squares_locked(),
                "board_unicode": str(self._board),
                "legal_move_count": self._board.legal_moves.count(),
                "history": list(self._history),
                "last_move": last_move,
                "captured": self._captured_locked(),
                "outcome": outcome,
                "evaluation": self._last_engine_evaluation,
                "opencode": self._last_opencode,
                "illegal_move_attempts": self._illegal_move_attempts,
                "elapsed_seconds": round(self._clock_seconds_locked(), 1),
                "config": self.config.public_dict(),
                "pgn": self._pgn_text_locked(),
            }

    def game_status(self) -> dict[str, Any]:
        with self._lock:
            result = self._outcome_locked()
            return {
                "state": self._state,
                "turn": self._side_to_move_locked(),
                "in_check": self._board.is_check(),
                "legal_move_count": self._board.legal_moves.count(),
                "outcome": result,
                "message": self._message,
                "last_error": self._last_error,
            }

    def board_view(self) -> dict[str, Any]:
        with self._lock:
            return {
                "fen": self._board.fen(),
                "unicode": str(self._board),
                "turn": self._side_to_move_locked(),
                "in_check": self._board.is_check(),
                "last_move": self._last_move_locked(),
            }

    def legal_moves(self) -> dict[str, Any]:
        with self._lock:
            moves = []
            for move in self._board.legal_moves:
                moves.append({"uci": move.uci(), "san": self._board.san(move)})
            return {
                "turn": self._side_to_move_locked(),
                "fen": self._board.fen(),
                "count": len(moves),
                "moves": moves,
            }

    def _pgn_text_locked(self) -> str:
        exporter = chess.pgn.StringExporter(headers=True, variations=False, comments=True)
        return self._pgn.accept(exporter)

    def pgn(self) -> str:
        with self._lock:
            return self._pgn_text_locked()

    def evaluation(self) -> dict[str, Any]:
        with self._lock:
            return {
                "evaluation": self._last_engine_evaluation,
                "note": "Evaluation is returned from Stockfish's own most recent move search; no extra engine analysis is run on OpenCode turns.",
            }

    # ---------- move validation / PGN ----------

    def _append_move_locked(
        self,
        move: chess.Move,
        actor: str,
        reasoning: str | None = None,
        candidates: list[str] | None = None,
        evaluation: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        san = self._board.san(move)
        color = self._side_to_move_locked()
        from_square, to_square = chess.square_name(move.from_square), chess.square_name(move.to_square)
        self._board.push(move)
        node = self._pgn.end().add_variation(move)
        comment_parts: list[str] = []
        if reasoning:
            comment_parts.append(f"OpenCode: {reasoning}" if actor == "opencode" else reasoning)
        if candidates:
            comment_parts.append("Candidates: " + ", ".join(candidates[:6]))
        if evaluation and evaluation.get("display"):
            comment_parts.append("Stockfish eval: " + str(evaluation["display"]))
        if comment_parts:
            node.comment = " | ".join(comment_parts)
        record = {
            "ply": len(self._history) + 1,
            "move_number": (len(self._history) // 2) + 1,
            "color": color,
            "actor": actor,
            "uci": move.uci(),
            "san": san,
            "from": from_square,
            "to": to_square,
            "promotion": chess.piece_name(move.promotion) if move.promotion else None,
            "reasoning": reasoning or None,
            "candidates": candidates or [],
            "evaluation": evaluation,
            "at": datetime.now(UTC).isoformat(),
        }
        self._history.append(record)
        self._last_error = None
        self._save_locked()
        return record

    def submit_opencode_move(
        self,
        uci: str,
        reasoning: str | None = None,
        candidate_moves: list[str] | None = None,
    ) -> dict[str, Any]:
        """Apply exactly one legal OpenCode move or return a recoverable error.

        Crucially, no mutation occurs until parsing, side ownership, and legal-move
        membership all pass. Stockfish therefore cannot receive a bad board.
        """
        with self._lock:
            outcome = self._outcome_locked()
            if outcome["terminal"]:
                raise GameError(f"The game is already over: {outcome['label']}.")
            if self._state == "paused":
                raise GameError("The game is paused. Resume it before submitting a move.")
            if self._side_to_move_locked() != self.config.ai_color:
                raise GameError("It is not OpenCode's turn. Do not submit a move yet.")
            try:
                move = chess.Move.from_uci(uci.strip().lower())
            except ValueError as exc:
                self._illegal_move_attempts += 1
                self._last_error = f"Invalid UCI move '{uci}'. Use coordinate UCI such as e2e4."
                self._touch(self._last_error)
                raise GameError(self._last_error) from exc
            if move not in self._board.legal_moves:
                self._illegal_move_attempts += 1
                legal = ", ".join(m.uci() for m in list(self._board.legal_moves)[:24])
                self._last_error = f"Illegal move '{uci}' in the current position. First legal moves: {legal}"
                self._touch(self._last_error)
                raise GameError(self._last_error)
            record = self._append_move_locked(move, "opencode", reasoning, candidate_moves)
            outcome = self._check_for_finish_locked()
            if not outcome["terminal"]:
                self._state = "waiting_for_stockfish"
                self._touch(f"OpenCode played {record['san']}. Stockfish is next.")
            self._save_locked()
            return {"accepted": True, "move": record, "outcome": outcome, "next_state": self._state}

    def resign_opencode(self, reason: str | None = None) -> dict[str, Any]:
        with self._lock:
            if self._outcome_locked()["terminal"]:
                raise GameError("The game is already over.")
            winner = self.config.stockfish_color
            result = "1-0" if winner == "white" else "0-1"
            outcome = {
                "terminal": True,
                "kind": "resignation",
                "result": result,
                "winner": winner,
                "label": "OpenCode resigned" + (f": {reason}" if reason else ""),
            }
            self._forced_outcome = outcome
            self._pgn.headers["Result"] = result
            self._state = "finished"
            self._finished_at = datetime.now(UTC)
            self._touch(outcome["label"])
            self._save_locked(final=True)
            return outcome

    # ---------- Stockfish ----------

    def _engine_locked(self) -> chess.engine.SimpleEngine:
        if self._engine:
            return self._engine
        try:
            self._engine = chess.engine.SimpleEngine.popen_uci(self.config.stockfish_path)
            options = self._engine.options
            if "Skill Level" in options:
                self._engine.configure({"Skill Level": self.config.stockfish_skill_level})
            # Some builds expose UCI_LimitStrength/UCI_Elo. Skill Level is the
            # portable setting requested by this project, so do not force Elo.
            self._engine_error = None
            return self._engine
        except Exception as exc:  # engine error messages are surfaced in dashboard
            self._engine = None
            self._engine_error = str(exc)
            raise GameError(
                f"Could not start Stockfish from '{self.config.stockfish_path}': {exc}"
            ) from exc

    @staticmethod
    def _format_score(score: chess.engine.PovScore | None) -> dict[str, Any] | None:
        if score is None:
            return None
        white_score = score.white()
        if white_score.is_mate():
            plies = white_score.mate()
            display = f"#{plies:+d}" if plies is not None else "mate"
            return {"type": "mate", "value": plies, "display": display, "perspective": "white"}
        centipawns = white_score.score()
        if centipawns is None:
            return {"type": "unknown", "value": None, "display": "?", "perspective": "white"}
        return {
            "type": "centipawn",
            "value": centipawns,
            "pawns": round(centipawns / 100.0, 2),
            "display": f"{centipawns / 100.0:+.2f}",
            "perspective": "white",
        }

    def play_stockfish_move(self) -> dict[str, Any]:
        """Ask Stockfish for one reply only, then validate and apply it."""
        with self._lock:
            outcome = self._outcome_locked()
            if outcome["terminal"]:
                return {"accepted": False, "outcome": outcome}
            if self._state == "paused":
                return {"accepted": False, "paused": True}
            if self._side_to_move_locked() != self.config.stockfish_color:
                raise GameError("It is not Stockfish's turn.")
            self._state = "stockfish_thinking"
            self._touch("Stockfish is calculating its reply.")
            board_for_engine = self._board.copy(stack=True)
            try:
                engine = self._engine_locked()
                result = engine.play(
                    board_for_engine,
                    chess.engine.Limit(time=self.config.stockfish_move_time_seconds),
                    info=chess.engine.INFO_SCORE | chess.engine.INFO_PV,
                )
            except GameError:
                self._state = "error"
                self._touch(self._engine_error or "Stockfish could not calculate a move.")
                raise
            except Exception as exc:
                self._engine_error = str(exc)
                self._state = "error"
                self._touch(f"Stockfish failed while calculating: {exc}")
                raise GameError(self._message) from exc

            if result.move is None or result.move not in self._board.legal_moves:
                self._state = "error"
                self._touch("Stockfish returned no legal move. Board was left unchanged.")
                raise GameError(self._message)
            evaluation = self._format_score(result.info.get("score"))
            if evaluation:
                evaluation["pv"] = [move.uci() for move in result.info.get("pv", [])[:8]]
                evaluation["at_ply"] = len(self._history) + 1
                self._last_engine_evaluation = evaluation
            rationale = "Stockfish reply"
            if evaluation:
                rationale += f" ({evaluation['display']} from White's perspective)"
            record = self._append_move_locked(result.move, "stockfish", rationale, evaluation=evaluation)
            outcome = self._check_for_finish_locked()
            if not outcome["terminal"]:
                self._state = "waiting_for_opencode"
                self._touch(f"Stockfish played {record['san']}. OpenCode is next.")
            self._save_locked(final=outcome["terminal"])
            return {"accepted": True, "move": record, "evaluation": evaluation, "outcome": outcome}

    # ---------- runner / dashboard controls ----------

    def begin_opencode_turn(self) -> None:
        with self._lock:
            if self._outcome_locked()["terminal"] or self._state == "paused":
                return
            if self._side_to_move_locked() != self.config.ai_color:
                raise GameError("Cannot start OpenCode: it is not OpenCode's turn.")
            self._state = "opencode_thinking"
            self._touch("OpenCode is reading the position and thinking.")

    def record_opencode_attempt(
        self,
        elapsed_seconds: float,
        output_tail: str,
        succeeded: bool,
        token_usage: dict[str, int] | None = None,
        error: str | None = None,
    ) -> None:
        with self._lock:
            self._last_opencode = {
                "elapsed_seconds": round(elapsed_seconds, 2),
                "succeeded": succeeded,
                "token_usage": token_usage,
                "output_tail": output_tail[-4000:],
                "at": datetime.now(UTC).isoformat(),
                "error": error,
            }
            if error:
                self._last_error = error
            if not succeeded and self._state == "opencode_thinking":
                self._state = "waiting_for_opencode"
            self._touch("OpenCode move submitted." if succeeded else (error or "OpenCode did not submit a move; retrying."))
            self._save_locked()

    def set_runner_error(self, message: str) -> None:
        """Expose a fatal automation/process problem without touching the board."""
        with self._lock:
            self._last_error = message
            self._state = "error"
            self._touch(message)
            self._save_locked()

    def pause(self) -> dict[str, Any]:
        with self._lock:
            if self._state not in ("paused", "finished", "idle"):
                self._resume_state = self._state
                self._state = "paused"
                self._touch("Game paused.")
            return self.state()

    def resume(self) -> dict[str, Any]:
        with self._lock:
            if self._state == "paused":
                self._state = self._turn_state_locked()
                self._touch("Game resumed.")
            elif self._state == "error":
                self._state = self._turn_state_locked()
                self._touch("Retrying from the unchanged board.")
            return self.state()

    def undo(self, plies: int = 1) -> dict[str, Any]:
        with self._lock:
            if plies < 1:
                raise GameError("plies must be at least 1.")
            if plies > len(self._history):
                raise GameError(f"Cannot undo {plies} plies; only {len(self._history)} are recorded.")
            if self._state == "stockfish_thinking" or self._state == "opencode_thinking":
                raise GameError("Pause the game before undoing.")
            for _ in range(plies):
                self._board.pop()
                self._history.pop()
            self._forced_outcome = None
            self._rebuild_pgn_locked()
            self._finished_at = None
            self._last_error = None
            self._state = self._turn_state_locked()
            self._touch(f"Undid {plies} {'ply' if plies == 1 else 'plies'}.")
            self._save_locked()
            return self.state()

    def _rebuild_pgn_locked(self) -> None:
        headers = dict(self._pgn.headers)
        self._pgn = chess.pgn.Game()
        self._pgn.headers.update(headers)
        board = chess.Board(self._initial_fen)
        node = self._pgn
        for record in self._history:
            move = chess.Move.from_uci(record["uci"])
            node = node.add_variation(move)
            pieces: list[str] = []
            if record.get("reasoning"):
                pieces.append(str(record["reasoning"]))
            if record.get("candidates"):
                pieces.append("Candidates: " + ", ".join(record["candidates"][:6]))
            evaluation = record.get("evaluation")
            if evaluation and evaluation.get("display"):
                pieces.append("Stockfish eval: " + str(evaluation["display"]))
            if pieces:
                node.comment = " | ".join(pieces)
            board.push(move)
        self._pgn.headers["Result"] = self._outcome_locked()["result"]

    # ---------- save / shutdown ----------

    def _save_locked(self, final: bool = False) -> None:
        directory = (self.root_directory / self.config.auto_save_directory).resolve()
        directory.mkdir(parents=True, exist_ok=True)
        latest = directory / "opencode-vs-stockfish-latest.pgn"
        latest.write_text(self._pgn_text_locked(), encoding="utf-8")
        snapshot = {
            "saved_at": datetime.now(UTC).isoformat(),
            "game_id": self._game_id,
            "fen": self._board.fen(),
            "state": self._state,
            "outcome": self._outcome_locked(),
            "history": self._history,
            "config": self.config.public_dict(),
        }
        (directory / "opencode-vs-stockfish-latest.json").write_text(
            json.dumps(snapshot, indent=2), encoding="utf-8"
        )
        if final:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            (directory / f"opencode-vs-stockfish-{stamp}.pgn").write_text(
                self._pgn_text_locked(), encoding="utf-8"
            )

    def close(self) -> None:
        with self._lock:
            if self._engine is not None:
                try:
                    self._engine.quit()
                except Exception:
                    pass
                finally:
                    self._engine = None
