"""Configuration loading and validation for the local match runner."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import json
from pathlib import Path
from typing import Any, Literal


ColorName = Literal["white", "black"]


@dataclass(frozen=True)
class GameConfig:
    """All runtime settings. Paths are resolved relative to the launch directory."""

    stockfish_path: str = "stockfish"
    stockfish_skill_level: int = 8
    stockfish_move_time_seconds: float = 0.75
    ai_color: ColorName = "white"
    opencode_executable: str = "opencode"
    opencode_model: str = "ollama/qwen3:8b"
    opencode_temperature: float = 0.2
    ollama_context_size: int = 16384
    max_opencode_thinking_time_seconds: float = 180.0
    max_opencode_turn_retries: int = 3
    move_delay_seconds: float = 0.5
    web_host: str = "127.0.0.1"
    web_port: int = 8765
    auto_save_directory: str = "games"
    initial_fen: str | None = None
    opencode_server_url: str | None = None

    @property
    def stockfish_color(self) -> ColorName:
        return "black" if self.ai_color == "white" else "white"

    def with_start_options(
        self, ai_color: ColorName | None = None, stockfish_skill_level: int | None = None
    ) -> "GameConfig":
        return replace(
            self,
            ai_color=ai_color or self.ai_color,
            stockfish_skill_level=(
                self.stockfish_skill_level
                if stockfish_skill_level is None
                else stockfish_skill_level
            ),
        )

    def public_dict(self) -> dict[str, Any]:
        """Safe, UI-facing configuration fields (not process executable details)."""
        return {
            "ai_color": self.ai_color,
            "stockfish_color": self.stockfish_color,
            "stockfish_skill_level": self.stockfish_skill_level,
            "stockfish_move_time_seconds": self.stockfish_move_time_seconds,
            "opencode_model": self.opencode_model,
            "opencode_temperature": self.opencode_temperature,
            "ollama_context_size": self.ollama_context_size,
            "max_opencode_thinking_time_seconds": self.max_opencode_thinking_time_seconds,
            "move_delay_seconds": self.move_delay_seconds,
            "web_host": self.web_host,
            "web_port": self.web_port,
        }


def _validate(raw: dict[str, Any]) -> GameConfig:
    known = set(GameConfig.__dataclass_fields__)
    unknown = set(raw) - known
    if unknown:
        raise ValueError(f"Unknown game configuration key(s): {', '.join(sorted(unknown))}")

    config = GameConfig(**raw)
    if config.ai_color not in ("white", "black"):
        raise ValueError("ai_color must be 'white' or 'black'.")
    if not 0 <= config.stockfish_skill_level <= 20:
        raise ValueError("stockfish_skill_level must be an integer from 0 through 20.")
    if config.stockfish_move_time_seconds <= 0:
        raise ValueError("stockfish_move_time_seconds must be greater than zero.")
    if not 0 <= config.opencode_temperature <= 2:
        raise ValueError("opencode_temperature must be between 0 and 2.")
    if config.ollama_context_size < 2048:
        raise ValueError("ollama_context_size must be at least 2048.")
    if config.max_opencode_thinking_time_seconds <= 0:
        raise ValueError("max_opencode_thinking_time_seconds must be greater than zero.")
    if config.max_opencode_turn_retries < 1:
        raise ValueError("max_opencode_turn_retries must be at least 1.")
    if config.move_delay_seconds < 0:
        raise ValueError("move_delay_seconds cannot be negative.")
    if not 1 <= config.web_port <= 65535:
        raise ValueError("web_port must be a valid TCP port.")
    return config


def load_config(path: str | Path) -> GameConfig:
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"Game configuration was not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("Game configuration must be a JSON object.")
    return _validate(raw)


def config_as_json(config: GameConfig) -> str:
    """Useful in logs and tests without exposing implementation details."""
    return json.dumps(asdict(config), indent=2, sort_keys=True)
