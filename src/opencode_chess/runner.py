"""Autonomous OpenCode-versus-Stockfish match runner.

OpenCode is invoked once per its turn. Its restricted chess-player agent discovers
and calls the local Chess MCP server; this runner never chooses OpenCode's move.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time
from typing import Any

import uvicorn

from .config import GameConfig, load_config
from .game import GameError, GameManager
from .web import PROJECT_ROOT, create_app


PROMPT = """You have been assigned exactly one turn as the OpenCode player in a live local chess game against Stockfish.

Do not use browser automation, Playwright, a chess website, shell commands, files, web tools, or any tool outside the Chess MCP server. Begin by inspecting the Chess MCP tools that are actually available. Then read the current position and legal moves, independently analyse the position, and submit exactly one legal UCI move through the discovered move-submission tool. Include a short reasoning and candidate moves if that tool supports them. If a submission is rejected, reread legal moves and try another legal move in this same turn. Never merely print a move: the MCP submission must confirm it. Stop after one successfully accepted move."""


def _write_chess_agent(config: GameConfig, project_root: Path) -> None:
    """Materialize a small generated agent so JSON config controls temperature."""
    template = project_root / "templates" / "chess-player.md.template"
    destination = project_root / ".opencode" / "agents" / "chess-player.md"
    destination.parent.mkdir(parents=True, exist_ok=True)
    source = template.read_text(encoding="utf-8")
    destination.write_text(
        source.replace("{{TEMPERATURE}}", str(config.opencode_temperature)), encoding="utf-8"
    )


def _stockfish_available(path: str) -> bool:
    candidate = Path(path).expanduser()
    return candidate.is_file() or shutil.which(path) is not None


def _json_token_usage(text: str) -> dict[str, int] | None:
    """Best-effort usage extraction from `opencode run --format json` event lines.

    OpenCode versions/providers expose slightly different event schemas, so this is
    intentionally optional rather than inventing a token count.
    """
    best: dict[str, int] | None = None
    for line in text.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        stack = [value]
        while stack:
            item = stack.pop()
            if isinstance(item, dict):
                normalized = {str(k).lower().replace("-", "_"): v for k, v in item.items()}
                input_tokens = normalized.get("input_tokens", normalized.get("prompt_tokens"))
                output_tokens = normalized.get("output_tokens", normalized.get("completion_tokens"))
                total_tokens = normalized.get("total_tokens")
                if any(isinstance(v, int) for v in (input_tokens, output_tokens, total_tokens)):
                    usage: dict[str, int] = {}
                    if isinstance(input_tokens, int):
                        usage["input"] = input_tokens
                    if isinstance(output_tokens, int):
                        usage["output"] = output_tokens
                    if isinstance(total_tokens, int):
                        usage["total"] = total_tokens
                    elif usage:
                        usage["total"] = sum(usage.values())
                    best = usage
                stack.extend(item.values())
            elif isinstance(item, list):
                stack.extend(item)
    return best


def _clean_tail(text: str) -> str:
    # Keep dashboard logs readable if a CLI writes terminal colour escapes.
    return re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)[-4000:]


def run_opencode_turn(config: GameConfig, project_root: Path) -> tuple[bool, float, str, dict[str, int] | None, str | None]:
    """Run the actual installed OpenCode CLI and return process-level information."""
    command = [
        config.opencode_executable,
        "run",
        "--auto",
        "--format",
        "json",
        "--agent",
        "chess-player",
        "--model",
        config.opencode_model,
        "--dir",
        str(project_root),
    ]
    if config.opencode_server_url:
        command.extend(["--attach", config.opencode_server_url])
    command.append(PROMPT)
    environment = os.environ.copy()
    environment["OPENCODE_CHESS_URL"] = f"http://127.0.0.1:{config.web_port}"
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            cwd=project_root,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=config.max_opencode_thinking_time_seconds,
            check=False,
        )
        elapsed = time.monotonic() - started
        output = (completed.stdout or "") + ("\n" + completed.stderr if completed.stderr else "")
        if completed.returncode != 0:
            return False, elapsed, _clean_tail(output), _json_token_usage(output), f"OpenCode exited with code {completed.returncode}."
        return True, elapsed, _clean_tail(output), _json_token_usage(output), None
    except subprocess.TimeoutExpired as exc:
        elapsed = time.monotonic() - started
        stdout = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        return False, elapsed, _clean_tail(stdout + "\n" + stderr), None, (
            f"OpenCode exceeded the {config.max_opencode_thinking_time_seconds:g}s thinking limit."
        )
    except FileNotFoundError:
        elapsed = time.monotonic() - started
        return False, elapsed, "", None, (
            f"OpenCode executable '{config.opencode_executable}' was not found on PATH."
        )
    except OSError as exc:
        elapsed = time.monotonic() - started
        return False, elapsed, "", None, f"Could not launch OpenCode: {exc}"


def _start_dashboard(manager: GameManager, config: GameConfig) -> tuple[uvicorn.Server, threading.Thread]:
    app = create_app(manager)
    server = uvicorn.Server(
        uvicorn.Config(app, host=config.web_host, port=config.web_port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, name="chess-dashboard", daemon=True)
    thread.start()
    deadline = time.monotonic() + 8
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    if not server.started:
        raise RuntimeError(f"Dashboard did not start on http://{config.web_host}:{config.web_port}")
    return server, thread


def run_match(manager: GameManager, config: GameConfig, project_root: Path) -> None:
    """Alternate turns. Only the OpenCode CLI decides OpenCode moves."""
    manager.start_new_game()
    retries = 0
    while True:
        snapshot = manager.state()
        if snapshot["outcome"]["terminal"] or snapshot["state"] == "finished":
            return
        if snapshot["state"] in ("paused", "idle"):
            time.sleep(0.2)
            continue
        if snapshot["state"] == "error":
            # A human may click Resume after fixing an executable/path issue.
            time.sleep(0.5)
            continue

        live_config = manager.config
        if snapshot["turn"] == live_config.stockfish_color:
            try:
                manager.play_stockfish_move()
            except GameError:
                time.sleep(0.5)
                continue
            retries = 0
            time.sleep(live_config.move_delay_seconds)
            continue

        if snapshot["turn"] != manager.config.ai_color:
            manager.set_runner_error("Turn ownership was inconsistent; board was not changed.")
            continue

        try:
            manager.begin_opencode_turn()
        except GameError as exc:
            manager.set_runner_error(str(exc))
            continue
        before_ply = len(snapshot["history"])
        process_ok, elapsed, tail, usage, process_error = run_opencode_turn(live_config, project_root)
        after = manager.state()
        moved = len(after["history"]) == before_ply + 1 and after["history"][-1]["actor"] == "opencode"
        if moved:
            manager.record_opencode_attempt(elapsed, tail, True, usage)
            retries = 0
            time.sleep(live_config.move_delay_seconds)
            continue

        retries += 1
        error = process_error or "OpenCode ended without a confirmed move submission."
        manager.record_opencode_attempt(elapsed, tail, False, usage, error)
        if retries >= live_config.max_opencode_turn_retries:
            manager.set_runner_error(
                f"OpenCode failed to submit a move after {retries} attempts. "
                "The board is intact. Check the dashboard log/model MCP support, then click Resume to retry."
            )
            retries = 0
        else:
            # Short backoff avoids hammering a local Ollama model after a malformed call.
            time.sleep(min(3.0, 0.5 * retries))


def main() -> None:
    parser = argparse.ArgumentParser(description="Run an autonomous OpenCode vs Stockfish match.")
    parser.add_argument("--config", default="config/game.json", help="Path to game JSON configuration.")
    parser.add_argument(
        "--project-root",
        default=str(PROJECT_ROOT),
        help="Project root containing opencode.json and .opencode (normally leave unchanged).",
    )
    args = parser.parse_args()
    project_root = Path(args.project_root).resolve()
    config = load_config(args.config)

    _write_chess_agent(config, project_root)
    if not _stockfish_available(config.stockfish_path):
        print(
            f"Stockfish was not found at '{config.stockfish_path}'. Edit {args.config} or put stockfish on PATH.",
            file=sys.stderr,
        )
        raise SystemExit(2)

    manager = GameManager(config, project_root)
    server: uvicorn.Server | None = None
    try:
        server, _thread = _start_dashboard(manager, config)
        print(f"Live board: http://{config.web_host}:{config.web_port}")
        print(f"OpenCode plays {config.ai_color}; Stockfish plays {config.stockfish_color} at skill {config.stockfish_skill_level}.")
        run_match(manager, config, project_root)
        result = manager.state()["outcome"]
        print(f"Match ended: {result['label']} ({result['result']})")
    except KeyboardInterrupt:
        print("\nStopping match. Current PGN was auto-saved.")
    finally:
        if server is not None:
            server.should_exit = True
        manager.close()


if __name__ == "__main__":
    main()
