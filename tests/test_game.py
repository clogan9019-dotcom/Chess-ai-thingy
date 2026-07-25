from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from opencode_chess.config import GameConfig
from opencode_chess.game import GameError, GameManager


class GameManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = GameConfig(auto_save_directory="saved", ai_color="white")
        self.game = GameManager(self.config, self.root)

    def tearDown(self):
        self.game.close()
        self.temp.cleanup()

    def test_legal_opencode_move_is_recorded_and_saved(self):
        self.game.start_new_game()
        response = self.game.submit_opencode_move("e2e4", "Controls the centre", ["e2e4"])
        self.assertTrue(response["accepted"])
        state = self.game.state()
        self.assertEqual(state["history"][0]["san"], "e4")
        self.assertEqual(state["turn"], "black")
        self.assertEqual(state["state"], "waiting_for_stockfish")
        self.assertTrue((self.root / "saved" / "opencode-vs-stockfish-latest.pgn").is_file())

    def test_illegal_move_keeps_authoritative_board_unchanged(self):
        self.game.start_new_game()
        original_fen = self.game.state()["fen"]
        with self.assertRaises(GameError):
            self.game.submit_opencode_move("e2e5")
        state = self.game.state()
        self.assertEqual(state["fen"], original_fen)
        self.assertEqual(state["history"], [])
        self.assertEqual(state["illegal_move_attempts"], 1)

    def test_checkmate_initial_fen_is_detected(self):
        self.game.close()
        checkmated = replace(
            self.config,
            initial_fen="7k/6Q1/6K1/8/8/8/8/8 b - - 0 1",
        )
        self.game = GameManager(checkmated, self.root)
        state = self.game.start_new_game()
        self.assertTrue(state["outcome"]["terminal"])
        self.assertEqual(state["outcome"]["kind"], "checkmate")
        self.assertEqual(state["outcome"]["winner"], "white")

    def test_resignation_persists_a_terminal_result(self):
        self.game.start_new_game()
        outcome = self.game.resign_opencode("Test resignation")
        self.assertEqual(outcome["kind"], "resignation")
        state = self.game.state()
        self.assertTrue(state["outcome"]["terminal"])
        self.assertEqual(state["outcome"]["result"], "0-1")
        self.assertIn("0-1", state["pgn"])


if __name__ == "__main__":
    unittest.main()
