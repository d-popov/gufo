#!/usr/bin/env python3
"""What tools/bench/concurrent-mixed.py trusts: tokens are counted only when the streamed
pieces match the server's count, and a stall is a stall wherever it falls in a window."""

import importlib.util
import sys
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "concurrent_mixed", Path(__file__).parents[2] / "tools/bench/concurrent-mixed.py")
cm = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = cm  # dataclasses look their module up here
SPEC.loader.exec_module(cm)


def run(pieces, tokens, error=None):
    r = cm.Run("r", "m", "p", 1)
    r.pieces = list(pieces)
    r.completion_tokens = tokens
    r.error = error
    return r


class WindowReadingTest(unittest.TestCase):
    def test_pieces_count_as_tokens_only_when_the_server_agrees(self):
        self.assertTrue(run(range(100), 100).counted)
        self.assertTrue(run(range(100), 100 + 1).counted)          # within 1%
        self.assertFalse(run(range(90), 100).counted)               # pieces missing: a window would undercount
        self.assertFalse(run(range(100), 0).counted)
        self.assertFalse(run(range(100), 100, error="timeout").counted)

    def test_tokens_between_counts_pieces_in_a_half_open_window(self):
        r = run([1.0, 2.0, 3.0, 4.0], 4)
        self.assertEqual(r.tokens_between(2.0, 4.0), 2)
        self.assertEqual(r.tokens_between(0.0, 1.0), 0)

    def test_a_stall_is_found_wherever_it_falls(self):
        r = run([0.0, 1.0, 2.0, 12.0, 13.0], 5)
        self.assertEqual(r.longest_gap(0.0, 13.0), 10.0)
        # The stall crosses both edges of the window: the whole window is the gap.
        self.assertEqual(r.longest_gap(4.0, 10.0), 6.0)
        # The window ends in a stall that has not yet produced its next piece.
        self.assertEqual(r.longest_gap(0.0, 11.0), 9.0)
        # The window starts in a stall.
        self.assertEqual(r.longest_gap(3.0, 12.5), 9.0)
        # Steady pieces leave small gaps, including at the edges.
        steady = run([i * 0.5 for i in range(20)], 20)
        self.assertAlmostEqual(steady.longest_gap(1.25, 8.0), 0.5, places=6)

    def test_an_invalid_run_says_why(self):
        bad = run(range(90), 100)
        self.assertIn("90 pieces vs 100 tokens", cm.invalid_reason([bad], 0.0, 1.0))
        self.assertEqual(cm.invalid_reason([run(range(10), 10, error="x")], 0.0, 1.0), "a request failed")


if __name__ == "__main__":
    unittest.main()
